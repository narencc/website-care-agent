"""Intake integrations and media models.

- Email and ticket webhooks (``POST /intake/email``, ``POST /intake/ticket``),
  authenticated by FELUDA_INTAKE_TOKEN, plus an optional IMAP poller for a
  support mailbox (``python -m feluda.intake imap``).
- Voice notes: Muse Voice Transcribe (``/v1/audio/transcriptions``). When the
  endpoint is unavailable the request waits for a typed transcript instead of
  guessing.
- Replacement-image drafts: Muse Image 1.0 (``/v1/images/generations``). A
  draft is only a proposal until a Care lead approves it for a job.

The website URL is a required intake field: it is taken from the payload, or
from a URL in the message, or from the sender's customer account; otherwise
the message is rejected rather than guessed.
"""

from __future__ import annotations

import argparse
import base64
import email
import email.policy
import imaplib
import os
import re
import time
from typing import Any

import httpx

from . import llm, netguard

TRANSCRIBE_MODEL = os.environ.get("FELUDA_TRANSCRIBE_MODEL") or "muse-voice-transcribe-1.0"
IMAGE_MODEL = os.environ.get("FELUDA_IMAGE_MODEL") or "muse-image-1.0"
PRICE_TRANSCRIBE_PER_HOUR = 0.18
PRICE_IMAGE = 0.01
URL_RE = re.compile(r"\bhttps?://[^\s<>\"')]+", re.I)
DOMAIN_RE = re.compile(r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|co|io|in|uk|au|ca|de|example|test|dental|clinic)\b", re.I)


class MediaModelUnavailable(RuntimeError):
    pass


def _endpoint(path: str) -> tuple[str, dict[str, str]]:
    s = llm.settings()
    return s.base_url.rstrip("/") + path, {"Authorization": f"Bearer {s.auth_token}"}


def _wav_seconds(data: bytes) -> float | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE" and len(data) > 44:
        byte_rate = int.from_bytes(data[28:32], "little")
        return (len(data) - 44) / byte_rate if byte_rate else None
    return None


def transcribe(ctx: llm.CallContext, data: bytes, filename: str, media_type: str) -> str:
    """Speech to text with Muse Voice Transcribe (speaker labels when the model returns them)."""
    llm.assert_standard_tier(TRANSCRIBE_MODEL)
    llm.check_budget(ctx)
    url, headers = _endpoint("/v1/audio/transcriptions")
    started = time.monotonic()
    try:
        resp = httpx.post(url, headers=headers, data={"model": TRANSCRIBE_MODEL, "response_format": "verbose_json"}, files={"file": (filename, data, media_type)}, timeout=300)
    except httpx.HTTPError as e:
        llm.log_call(ctx, TRANSCRIBE_MODEL, latency_ms=int((time.monotonic() - started) * 1000), ok=False, error=str(e))
        raise MediaModelUnavailable(f"transcription request failed: {type(e).__name__}") from e
    latency = int((time.monotonic() - started) * 1000)
    if resp.status_code in (404, 405, 501):
        llm.log_call(ctx, TRANSCRIBE_MODEL, latency_ms=latency, ok=False, error=f"HTTP {resp.status_code}: endpoint unavailable")
        raise MediaModelUnavailable(f"{TRANSCRIBE_MODEL} is not available on this API endpoint (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        llm.log_call(ctx, TRANSCRIBE_MODEL, latency_ms=latency, ok=False, error=f"HTTP {resp.status_code}")
        raise RuntimeError(f"transcription failed with HTTP {resp.status_code}")
    body = resp.json()
    seconds = body.get("duration") or _wav_seconds(data) or 0
    from .db import session
    from .records import ModelCall

    llm.log_call(ctx, TRANSCRIBE_MODEL, latency_ms=latency)
    with session() as s:  # audio is billed per hour, not per token
        last = s.query(ModelCall).filter(ModelCall.request_id == ctx.request_id, ModelCall.model == TRANSCRIBE_MODEL).order_by(ModelCall.id.desc()).first()
        if last:
            last.cost_usd = PRICE_TRANSCRIBE_PER_HOUR * seconds / 3600
    segments = body.get("segments") or []
    if segments and any(seg.get("speaker") for seg in segments):
        return "\n".join(f"{seg.get('speaker', 'Speaker')}: {seg.get('text', '').strip()}" for seg in segments)
    return (body.get("text") or "").strip()


def generate_image(ctx: llm.CallContext, prompt: str) -> tuple[bytes, str]:
    """A Muse Image 1.0 draft. Returns (bytes, media type)."""
    llm.assert_standard_tier(IMAGE_MODEL)
    llm.check_budget(ctx)
    url, headers = _endpoint("/v1/images/generations")
    started = time.monotonic()
    try:
        resp = httpx.post(url, headers=headers, json={"model": IMAGE_MODEL, "prompt": prompt, "size": "1024x1024"}, timeout=300)
        resp.raise_for_status()
        b64 = resp.json()["data"][0]["b64_json"]
    except Exception as e:
        llm.log_call(ctx, IMAGE_MODEL, latency_ms=int((time.monotonic() - started) * 1000), ok=False, error=f"{type(e).__name__}: {e}")
        raise MediaModelUnavailable(f"image generation failed: {type(e).__name__}") from e
    data = base64.b64decode(b64)
    llm.log_call(ctx, IMAGE_MODEL, latency_ms=int((time.monotonic() - started) * 1000))
    from .db import session
    from .records import ModelCall

    with session() as s:
        last = s.query(ModelCall).filter(ModelCall.model == IMAGE_MODEL, ModelCall.request_id == ctx.request_id).order_by(ModelCall.id.desc()).first()
        if last:
            last.cost_usd = PRICE_IMAGE
    media_type = "image/webp" if data[8:12] == b"WEBP" else ("image/jpeg" if data[:2] == bytes([0xFF, 0xD8]) else "image/png")
    return data, media_type


# --- Email / ticket parsing -------------------------------------------------------------


def site_urls_in(text: str) -> list[str]:
    """Website URLs mentioned in a message, in order (email addresses are not websites)."""
    text = text or ""
    found = [m.group(0).rstrip(".,;") for m in URL_RE.finditer(text) if "@" not in m.group(0)]
    for m in DOMAIN_RE.finditer(text):
        if text[max(0, m.start() - 1) : m.start()] != "@" and "://" not in text[max(0, m.start() - 3) : m.start()]:
            found.append("https://" + m.group(0))
    return found


def find_site_url(text: str) -> str | None:
    urls = site_urls_in(text)
    return urls[0] if urls else None


def resolve_site_url(explicit: str | None, text: str, sender: str | None) -> str | None:
    """The explicit field wins. Otherwise a known sender's account decides: a URL in the
    message is used only if it is one of that account's sites (a link to Facebook or
    Google Maps in the text is not the site to change). Unknown senders: first URL."""
    if explicit:
        return explicit
    origins: list[str] = []
    if sender:
        from .db import session
        from .users import account_for_email

        with session() as s:
            acct = account_for_email(s, sender)
            origins = list(acct.site_origins or []) if acct else []
    urls = site_urls_in(text)
    if origins:
        for url in urls:
            try:
                if netguard.origin_of(netguard.normalize_site_url(url)) in origins:
                    return url
            except Exception:
                continue
        return origins[0] + "/"
    return urls[0] if urls else None


def from_email_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a webhook email ({from, subject, text, site_url?, attachments:[{filename, content_type, data_b64}]})."""
    text = f"Subject: {payload.get('subject', '').strip()}\n\n{payload.get('text', '').strip()}".strip()
    attachments = [(a["filename"], base64.b64decode(a["data_b64"]), a.get("content_type")) for a in payload.get("attachments") or []]
    return {
        "site_url": resolve_site_url(payload.get("site_url"), text, payload.get("from")),
        "raw_text": text,
        "customer": payload.get("from", ""),
        "channel": "email",
        "attachments": attachments,
    }


def from_mime(raw: bytes) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    attachments = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        attachments.append({"filename": part.get_filename() or "attachment", "content_type": part.get_content_type(), "data_b64": base64.b64encode(data).decode()})
    return from_email_payload({"from": str(msg.get("From", "")), "subject": str(msg.get("Subject", "")), "text": text, "attachments": attachments})


def intake(payload: dict[str, Any]) -> str:
    """Create a request from a normalised email/ticket payload; raises ActionError when invalid."""
    from .service import ActionError, create_request

    if not payload.get("site_url"):
        raise ActionError("No website URL: include it in the message, or register the sender's domain on their customer account.")
    netguard.normalize_site_url(payload["site_url"])
    return create_request(
        site_url=payload["site_url"],
        raw_text=payload.get("raw_text", ""),
        actor="intake",
        customer=payload.get("customer", ""),
        channel=payload.get("channel", "ticket"),
        attachments=payload.get("attachments") or [],
        system_intake=True,
    )


def poll_imap_once() -> list[str]:
    """Read unseen messages from the support mailbox (FELUDA_INTAKE_IMAP_HOST/USER/PASSWORD)."""
    host, user, password = (os.environ.get(k) for k in ("FELUDA_INTAKE_IMAP_HOST", "FELUDA_INTAKE_IMAP_USER", "FELUDA_INTAKE_IMAP_PASSWORD"))
    if not (host and user and password):
        raise SystemExit("Set FELUDA_INTAKE_IMAP_HOST, FELUDA_INTAKE_IMAP_USER and FELUDA_INTAKE_IMAP_PASSWORD.")
    created = []
    with imaplib.IMAP4_SSL(host) as box:
        box.login(user, password)
        box.select("INBOX")
        _t, data = box.search(None, "UNSEEN")
        for num in (data[0] or b"").split():
            _t, msg_data = box.fetch(num, "(RFC822)")
            try:
                created.append(intake(from_mime(msg_data[0][1])))
            except Exception as e:
                print(f"message {num.decode()}: not accepted: {e}")
    return created


def main() -> None:
    from .db import init_db

    ap = argparse.ArgumentParser(prog="python -m feluda.intake")
    ap.add_argument("command", choices=["imap"])
    ap.add_argument("--every", type=int, default=0, help="poll every N seconds (0 = once)")
    args = ap.parse_args()
    init_db()
    while True:
        print("created:", poll_imap_once())
        if not args.every:
            break
        time.sleep(args.every)


if __name__ == "__main__":
    main()
