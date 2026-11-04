"""Client for the Meta Model API's Anthropic-compatible Messages endpoint.

``complete`` is the simple one-shot helper. Pipeline stages use ``call`` and
``structured``, which add per-call metadata logging (request, job revision,
model, prompt version, tokens, latency, cost), a per-request spending cap and
a guard that customer data never goes to a Contributor-tier model.

The endpoint accepts only ``tool_choice: auto``, so structured output asks the
model to call one tool and falls back to JSON found in the text reply.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel, ValidationError

from . import redact
from .config import ModelSettings, Tier, load_settings
from .settings import FORBIDDEN_MODEL_MARKERS, PRICES, app_settings


@lru_cache(maxsize=1)
def settings() -> ModelSettings:
    return load_settings()


@lru_cache(maxsize=1)
def client() -> Anthropic:
    s = settings()
    # auth_token is sent as "Authorization: Bearer ...", matching ANTHROPIC_AUTH_TOKEN.
    return Anthropic(base_url=s.base_url, auth_token=s.auth_token, api_key=None, max_retries=2, timeout=180)


def complete(
    prompt: str,
    *,
    system: str | None = None,
    tier: Tier | None = None,
    max_tokens: int = 1024,
) -> str:
    """Send one user message and return the concatenated text reply."""
    kwargs = {"system": system} if system else {}
    response = client().messages.create(
        model=settings().model_for(tier),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    return "".join(block.text for block in response.content if block.type == "text")


# --- Gateway (swappable for tests) -------------------------------------------


@dataclass
class ModelReply:
    model: str
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


class Gateway(Protocol):
    def create(
        self, *, model: str, system: str, messages: list[dict], tools: list[dict] | None, max_tokens: int
    ) -> ModelReply: ...


class MetaGateway:
    def create(self, *, model, system, messages, tools, max_tokens) -> ModelReply:
        kwargs: dict[str, Any] = {"system": system} if system else {}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = {"type": "auto"}
        r = client().messages.create(model=model, max_tokens=max_tokens, messages=messages, **kwargs)
        return ModelReply(
            model=model,
            text="".join(b.text for b in r.content if b.type == "text"),
            tool_calls=[{"name": b.name, "input": b.input} for b in r.content if b.type == "tool_use"],
            input_tokens=getattr(r.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(r.usage, "output_tokens", 0) or 0,
            stop_reason=r.stop_reason,
        )


_gateway: Gateway | None = None


def gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        _gateway = MetaGateway()
    return _gateway


def set_gateway(g: Gateway | None) -> None:
    global _gateway
    _gateway = g


# --- Call context, cost and budget -------------------------------------------


class BudgetExceeded(RuntimeError):
    pass


class ModelOutputError(RuntimeError):
    """The model did not return output that validates against the schema."""


@dataclass
class CallContext:
    purpose: str
    prompt_version: str
    request_id: str | None = None
    job_id: str | None = None
    job_revision: int | None = None
    attempt_id: str | None = None


def estimate_cost(model: str, input_tokens: int, output_tokens: int, images: int = 0) -> float:
    price = PRICES.get(model, {})
    if "image" in price:
        return images * price["image"]
    return (input_tokens * price.get("in", 0) + output_tokens * price.get("out", 0)) / 1_000_000


def request_spend(request_id: str) -> float:
    from sqlalchemy import func, select

    from .db import session
    from .records import ModelCall

    with session() as s:
        return float(s.scalar(select(func.coalesce(func.sum(ModelCall.cost_usd), 0.0)).where(ModelCall.request_id == request_id)) or 0.0)


def check_budget(ctx: CallContext) -> None:
    if ctx.request_id is None:
        return
    spent = request_spend(ctx.request_id)
    cap = app_settings().request_cost_cap_usd
    if spent >= cap:
        raise BudgetExceeded(f"model spend ${spent:.3f} reached the ${cap:.2f} cap for request {ctx.request_id}")


def log_call(ctx: CallContext, model: str, *, input_tokens=0, output_tokens=0, images=0, latency_ms=0, ok=True, error="") -> None:
    from .db import session
    from .records import ModelCall

    with session() as s:
        s.add(
            ModelCall(
                request_id=ctx.request_id,
                job_id=ctx.job_id,
                job_revision=ctx.job_revision,
                attempt_id=ctx.attempt_id,
                purpose=ctx.purpose,
                model=model,
                prompt_version=ctx.prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                images=images,
                latency_ms=latency_ms,
                cost_usd=estimate_cost(model, input_tokens, output_tokens, images),
                ok=ok,
                error=redact.for_logs(error)[:2000],
            )
        )


def assert_standard_tier(model: str) -> None:
    if any(marker in model.lower() for marker in FORBIDDEN_MODEL_MARKERS):
        raise ValueError(f"model {model!r} is a Contributor tier; customer data must not be sent to it")


# --- Content helpers ---------------------------------------------------------


def image_block(source: str | Path | bytes, media_type: str = "image/png") -> dict:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _count_images(content: list[dict] | str) -> int:
    return 0 if isinstance(content, str) else sum(1 for c in content if c.get("type") == "image")


def call(
    ctx: CallContext,
    model: str,
    system: str,
    content: list[dict] | str,
    *,
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    history: list[dict] | None = None,
) -> ModelReply:
    assert_standard_tier(model)
    check_budget(ctx)
    messages = list(history or []) + [{"role": "user", "content": content}]
    started = time.monotonic()
    try:
        reply = gateway().create(model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens)
    except Exception as e:
        log_call(ctx, model, latency_ms=int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise
    log_call(
        ctx,
        model,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        images=_count_images(content),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return reply


# --- Structured output --------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


def inline_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema with $refs inlined (some endpoints reject $ref)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _json_from_text(text: str) -> Any:
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in reply")
        candidate = text[start : end + 1]
    return json.loads(candidate)


def structured(
    ctx: CallContext,
    model: str,
    system: str,
    content: list[dict] | str,
    schema: type[T],
    *,
    tool_name: str,
    tool_description: str,
    max_tokens: int = 8192,
    repair_attempts: int = 1,
) -> tuple[T, str]:
    """Ask for one ``tool_name`` call matching ``schema``; return (parsed, model)."""
    tool = {"name": tool_name, "description": tool_description, "input_schema": inline_schema(schema)}
    instruction = f"\n\nReturn your answer by calling the `{tool_name}` tool exactly once. Do not answer in prose."
    if isinstance(content, str):
        content = [text_block(content + instruction)]
    else:
        content = list(content) + [text_block(instruction.strip())]
    history: list[dict] = []
    last_error = ""
    for _ in range(repair_attempts + 1):
        reply = call(ctx, model, system, content, tools=[tool], max_tokens=max_tokens, history=history)
        raw: Any = None
        for tc in reply.tool_calls:
            if tc["name"] == tool_name:
                raw = tc["input"]
                break
        try:
            if raw is None:
                raw = _json_from_text(reply.text)
            return schema.model_validate(raw), model
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = str(e)[:3000]
            # Ask once more with the validation error; tool_use blocks can't be
            # replayed without ids, so restate the request as plain text history.
            history = [
                {"role": "user", "content": content},
                {"role": "assistant", "content": json.dumps(raw)[:6000] if raw is not None else (reply.text or "(no output)")[:6000]},
            ]
            content = [text_block(f"That output failed validation: {last_error}\nCall `{tool_name}` again with corrected input.")]
    raise ModelOutputError(f"{ctx.purpose}: invalid structured output from {model}: {last_error}")
