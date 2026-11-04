"""Redaction of credentials and unnecessary personal data.

Two levels, because the same phone number can be the subject of a change
request (needed by the planner) and personal data in a log line (not needed):

- ``for_model``: credentials, card numbers and government IDs. Phone numbers
  and emails stay, since requested changes are usually about them.
- ``for_logs``: everything above plus email addresses and phone numbers.

The original request text is stored unchanged for the Care lead's review;
only copies sent to models or written to logs/errors are redacted.
"""

from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("credential", re.compile(r"(?i)\b(pass(?:word|wd|code)?|pwd|secret|api[_ -]?key|token|login code|otp)\b(\s*(?:is|=|:)\s*)(\S+)")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
]
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}(?!\w)")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _cards(text: str) -> str:
    def sub(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[REDACTED:card]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

    return _CARD.sub(sub, text)


def for_model(text: str | None) -> str:
    if not text:
        return text or ""
    for kind, pattern in _SECRET_PATTERNS:
        if kind == "credential":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:credential]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return _cards(text)


def for_logs(text: str | None) -> str:
    text = for_model(text)
    text = _EMAIL.sub("[REDACTED:email]", text)
    return _PHONE.sub("[REDACTED:phone]", text)


class RedactingFilter(logging.Filter):
    """Redacts every log record handled by the handlers it is attached to."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the format string and each argument in place: some formatters (uvicorn's
        # access log) unpack record.args, so the args must keep their shape.
        if isinstance(record.msg, str):
            record.msg = for_logs(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(for_logs(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: for_logs(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:
            # Formatters render tracebacks after filters run; pre-render it so it is redacted too.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = for_logs(record.exc_text)
        return True


def install_log_redaction() -> None:
    """Attach to the root handlers and to uvicorn's own (they do not propagate)."""
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
