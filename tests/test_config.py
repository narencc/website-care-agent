import pytest

from feluda.config import DEFAULT_BASE_URL, DEFAULT_MODEL, ConfigError, load_settings


def test_defaults_with_only_model_api_key():
    s = load_settings({"MODEL_API_KEY": "k"})
    assert s.base_url == DEFAULT_BASE_URL
    assert s.auth_token == "k"
    assert s.model == DEFAULT_MODEL
    assert s.model_for("haiku") == DEFAULT_MODEL
    assert s.model_for("subagent") == DEFAULT_MODEL


def test_auth_token_wins_over_model_api_key():
    s = load_settings({"ANTHROPIC_AUTH_TOKEN": "a", "MODEL_API_KEY": "m"})
    assert s.auth_token == "a"


def test_unexpanded_reference_falls_back():
    s = load_settings({"ANTHROPIC_AUTH_TOKEN": "$MODEL_API_KEY", "MODEL_API_KEY": "m"})
    assert s.auth_token == "m"


def test_tier_overrides():
    s = load_settings(
        {
            "MODEL_API_KEY": "k",
            "ANTHROPIC_BASE_URL": "https://example.test",
            "ANTHROPIC_MODEL": "base",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "big",
            "CLAUDE_CODE_SUBAGENT_MODEL": "sub",
        }
    )
    assert s.base_url == "https://example.test"
    assert s.model_for() == "base"
    assert s.model_for("opus") == "big"
    assert s.model_for("sonnet") == "base"
    assert s.model_for("subagent") == "sub"


def test_feluda_base_url_wins_over_inherited_anthropic_url():
    s = load_settings({"MODEL_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://api.anthropic.com", "FELUDA_MODEL_BASE_URL": "https://api.meta.ai"})
    assert s.base_url == "https://api.meta.ai"


def test_missing_token_raises():
    with pytest.raises(ConfigError):
        load_settings({})


def test_repr_hides_token():
    assert "secret" not in repr(load_settings({"MODEL_API_KEY": "secret"}))
