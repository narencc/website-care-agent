"""Immutable evidence storage with customer-scoped paths and signed, short-lived links.

Local filesystem for the prototype; the key layout (``<request>/<job>/<evidence>/<file>``)
maps directly onto an object-store bucket.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path

from .settings import app_settings

_SAFE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class StorageError(ValueError):
    pass


def _resolve(key: str) -> Path:
    if not _SAFE.match(key) or ".." in key or key.startswith("/"):
        raise StorageError(f"unsafe storage key {key!r}")
    root = app_settings().evidence_dir.resolve()
    path = (root / key).resolve()
    if root not in path.parents:
        raise StorageError("key escapes storage root")
    return path


def put(key: str, data: bytes) -> str:
    path = _resolve(key)
    if path.exists():
        raise StorageError(f"evidence {key} already exists; evidence is immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


def get(key: str) -> bytes:
    return _resolve(key).read_bytes()


def path_of(key: str) -> Path:
    return _resolve(key)


def _sig(key: str, exp: int) -> str:
    return hmac.new(app_settings().secret.encode(), f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_url(key: str | None, ttl_s: int | None = None) -> str | None:
    if not key:
        return None
    exp = int(time.time()) + (ttl_s or app_settings().signed_link_ttl_s)
    return f"/evidence/{key}?exp={exp}&sig={_sig(key, exp)}"


def verify(key: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(_sig(key, exp), sig)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
