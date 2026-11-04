"""Secrets for flow checks: approved test accounts and test-inbox credentials.

Read from a JSON file outside the repository (FELUDA_SECRETS_FILE, default
``<data dir>/secrets.json``), never from site profiles or the database, and
injected only into the flow-check browser session or inbox connector. Values
never reach model prompts or logs.

    {
      "test_accounts": {"demo_dental_local": {"name": "...", "email": "..."}},
      "inboxes": {"demo_dental_local": {"password": "..."}}
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .settings import app_settings


def secrets_path() -> Path:
    return Path(os.environ.get("FELUDA_SECRETS_FILE") or app_settings().data_dir / "secrets.json")


def _load() -> dict[str, Any]:
    path = secrets_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_account(ref: str | None) -> dict[str, str] | None:
    if not ref:
        return None
    return (_load().get("test_accounts") or {}).get(ref)


def inbox_secret(ref: str | None) -> dict[str, str] | None:
    if not ref:
        return None
    return (_load().get("inboxes") or {}).get(ref)


def write_secrets(data: dict[str, Any]) -> Path:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load()
    for section, values in data.items():
        existing.setdefault(section, {}).update(values)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return path
