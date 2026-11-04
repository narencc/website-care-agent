import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn

from feluda import db, llm, profiles, settings


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Fresh SQLite database and storage per test; SAM off; no real model calls."""
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    s = settings.load_app_settings(
        {
            "FELUDA_DATA_DIR": str(tmp_path / "var"),
            "FELUDA_USE_SAM": "0",
            "FELUDA_INLINE_WORKER": "0",
            "FELUDA_FRESHNESS_RETRY_DELAY_S": "0.2",
        }
    )
    settings.use_settings(s)
    db.reset_engine()
    db.init_db()
    from feluda import secrets_store, users

    users.ensure_dev_users()
    secrets_store.write_secrets({"test_accounts": {"demo_dental_local": {"name": "Website Care Test", "email": "care-test@feluda.example"}}})
    yield s
    llm.set_gateway(None)
    db.reset_engine()
    settings.use_settings(None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def demo_site(tmp_path, monkeypatch, app_env):
    """Run the demo WordPress site on a free port and point the demo profile at it."""
    from demo_site import app as demo

    monkeypatch.setattr(demo, "STATE_FILE", tmp_path / "demo_state.json")
    monkeypatch.setattr(demo, "INBOX_FILE", tmp_path / "demo_inbox.json")
    demo._cache.clear()
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(demo.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(origin + "/__demo/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.05)

    real = profiles.load_profiles()
    patched = {k: dict(v) for k, v in real.items()}
    patched["demo_dental_local"]["origins"] = [origin]
    patched["demo_dental_local"]["admin_url"] = origin + "/wp-admin/"
    patched["demo_dental_local"]["form_tests"] = {**patched["demo_dental_local"]["form_tests"], "receipt_timeout_s": 4}
    monkeypatch.setattr(profiles, "load_profiles", lambda: patched)
    settings.use_settings(replace(app_env, approved_test_origins=(origin,)))

    class Site:
        url = origin

        def set(self, **changes):
            r = httpx.post(origin + "/__demo/set", json=changes)
            r.raise_for_status()

        def reset(self):
            httpx.post(origin + "/__demo/reset").raise_for_status()

    site = Site()
    site.reset()
    yield site
    server.should_exit = True
    thread.join(timeout=5)


class ScriptedGateway:
    """Fake Meta model gateway: routes by tool name to a scripted responder."""

    def __init__(self, responders):
        self.responders = responders
        self.calls = []

    def create(self, *, model, system, messages, tools, max_tokens):
        name = tools[0]["name"] if tools else "_text"
        self.calls.append({"model": model, "tool": name, "messages": messages})
        out = self.responders[name](model=model, messages=messages)
        if isinstance(out, llm.ModelReply):
            return out
        return llm.ModelReply(model=model, text="", tool_calls=[{"name": name, "input": out}], input_tokens=1000, output_tokens=200)


@pytest.fixture
def scripted():
    def install(responders):
        g = ScriptedGateway(responders)
        llm.set_gateway(g)
        return g

    return install
