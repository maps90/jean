from __future__ import annotations

import os
from pathlib import Path

import pytest

from jean.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JEAN_") or key in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp-test")
    yield monkeypatch


def test_defaults_resolve(clean_env):
    settings = Settings.load()
    assert settings.slack_bot_token == "xoxb-test"
    assert settings.slack_app_token == "xapp-test"
    assert settings.idle_minutes == 15
    assert settings.approval_ttl == 1800
    assert settings.permission_mode == "bypassPermissions"
    assert settings.health_port == 8080
    assert settings.soul_parse_model == "claude-haiku-4-5-20251001"
    assert settings.database_url == "postgresql://jean:jean@localhost:5432/jean"
    assert settings.anthropic_api_key is None
    assert settings.claude_code_oauth_token is None


def test_home_expands_under_home_dir(clean_env):
    clean_env.setenv("JEAN_HOME", "~/.jean")
    settings = Settings.load()
    assert settings.home == Path.home() / ".jean"
    assert settings.identity_path == Path.home() / ".jean" / "IDENTITY.md"
    assert settings.cache_dir == Path.home() / ".jean" / "cache"


def test_oauth_token_read_when_api_key_unset(clean_env):
    clean_env.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abc")
    settings = Settings.load()
    assert settings.claude_code_oauth_token == "sk-ant-oat01-abc"
    assert settings.anthropic_api_key is None


def test_database_url_override(clean_env):
    clean_env.setenv("JEAN_DATABASE_URL", "postgresql://x:y@host:5432/db")
    settings = Settings.load()
    assert settings.database_url == "postgresql://x:y@host:5432/db"
