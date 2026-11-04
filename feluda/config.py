"""Model endpoint settings read from the environment.

Uses the same variables as Claude Code pointed at the Meta Model API:

    ANTHROPIC_BASE_URL              https://api.meta.ai
    ANTHROPIC_AUTH_TOKEN            falls back to MODEL_API_KEY
    ANTHROPIC_MODEL                 default model for all calls
    ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL   per-tier overrides
    CLAUDE_CODE_SUBAGENT_MODEL      model for sub-agent calls

A local .env file is loaded if python-dotenv is installed; real environment
variables take precedence over it.

FELUDA_MODEL_BASE_URL, when set, wins over ANTHROPIC_BASE_URL. Processes
started from inside Claude Code inherit its own ANTHROPIC_BASE_URL, which
would otherwise silently override the Meta endpoint in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Mapping

DEFAULT_BASE_URL = "https://api.meta.ai"
DEFAULT_MODEL = "muse-wonder-1.0"

Tier = Literal["opus", "sonnet", "haiku", "subagent"]


class ConfigError(RuntimeError):
    """Raised when required model settings are missing."""


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    auth_token: str
    model: str
    opus_model: str
    sonnet_model: str
    haiku_model: str
    subagent_model: str

    def model_for(self, tier: Tier | None = None) -> str:
        if tier is None:
            return self.model
        return {
            "opus": self.opus_model,
            "sonnet": self.sonnet_model,
            "haiku": self.haiku_model,
            "subagent": self.subagent_model,
        }[tier]

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return (
            f"ModelSettings(base_url={self.base_url!r}, model={self.model!r}, "
            f"opus_model={self.opus_model!r}, sonnet_model={self.sonnet_model!r}, "
            f"haiku_model={self.haiku_model!r}, subagent_model={self.subagent_model!r})"
        )


def load_settings(env: Mapping[str, str] | None = None) -> ModelSettings:
    """Build settings from ``env`` (defaults to ``os.environ`` plus .env)."""
    if env is None:
        _load_dotenv()
        env = os.environ

    def get(name: str) -> str | None:
        value = env.get(name, "").strip()
        # An unexpanded "$MODEL_API_KEY" copied into a .env file is not a token.
        return value if value and not value.startswith("$") else None

    token = get("ANTHROPIC_AUTH_TOKEN") or get("MODEL_API_KEY")
    if not token:
        raise ConfigError(
            "No API key: set MODEL_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the environment or .env"
        )

    model = get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    return ModelSettings(
        base_url=get("FELUDA_MODEL_BASE_URL") or get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL,
        auth_token=token,
        model=model,
        opus_model=get("ANTHROPIC_DEFAULT_OPUS_MODEL") or model,
        sonnet_model=get("ANTHROPIC_DEFAULT_SONNET_MODEL") or model,
        haiku_model=get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or model,
        subagent_model=get("CLAUDE_CODE_SUBAGENT_MODEL") or model,
    )
