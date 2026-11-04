"""SAM 3.1 text-prompted detection via the Meta Model API ``/v1/responses`` endpoint.

SAM is a visual supplement: the DOM locates targets first. A missing or
failed SAM result never blocks a DOM-resolved target.

Response text carries detections as ``<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|>``
followed by an RLE mask, which we do not decode.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass

import httpx

from .llm import CallContext, assert_standard_tier, check_budget, log_call, settings
from .settings import app_settings

BOX = re.compile(r"<\|box;x1=(-?[\d.]+);y1=(-?[\d.]+);x2=(-?[\d.]+);y2=(-?[\d.]+);w=(\d+);h=(\d+)\|>")


@dataclass
class SamBox:
    x1: float
    y1: float
    x2: float
    y2: float
    image_w: int
    image_h: int

    def as_dict(self) -> dict:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2, "w": self.image_w, "h": self.image_h}


def parse_boxes(text: str) -> list[SamBox]:
    return [SamBox(float(a), float(b), float(c), float(d), int(w), int(h)) for a, b, c, d, w, h in BOX.findall(text)]


def iou(a: dict, b: dict) -> float:
    ix = max(0.0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
    iy = max(0.0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
    inter = ix * iy
    area = lambda r: max(0.0, r["x2"] - r["x1"]) * max(0.0, r["y2"] - r["y1"])  # noqa: E731
    union = area(a) + area(b) - inter
    return inter / union if union else 0.0


def detect(ctx: CallContext, png: bytes, prompt: str, *, timeout: float = 60.0) -> list[SamBox]:
    model = app_settings().sam_model
    assert_standard_tier(model)
    check_budget(ctx)
    s = settings()
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png).decode()},
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "store": False,
    }
    started = time.monotonic()
    try:
        resp = httpx.post(
            s.base_url.rstrip("/") + "/v1/responses",
            headers={"Authorization": f"Bearer {s.auth_token}"},
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log_call(ctx, model, images=1, latency_ms=int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise
    log_call(ctx, model, images=1, latency_ms=int((time.monotonic() - started) * 1000))
    text = "".join(
        part.get("text", "")
        for item in data.get("output", [])
        for part in item.get("content", []) or []
        if isinstance(part, dict)
    )
    return parse_boxes(text)
