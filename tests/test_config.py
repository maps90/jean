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
    # Not "bypassPermissions": that mode skips the CLI's permission system, so
    # the can_use_tool hook -- jean's Slack Approve/Deny buttons -- never fires.
    assert settings.permission_mode == "default"
    assert settings.health_port == 8080
    assert settings.soul_parse_model == "claude-haiku-4-5-20251001"
    assert settings.database_url == "postgresql://jean:jean@localhost:5432/jean"
    assert settings.anthropic_api_key is None
    assert settings.claude_code_oauth_token is None
    assert settings.cleanup_enabled is True
    assert settings.cleanup_retention_days == 30


def test_cleanup_settings_override(clean_env):
    clean_env.setenv("JEAN_CLEANUP_ENABLED", "false")
    clean_env.setenv("JEAN_CLEANUP_RETENTION_DAYS", "90")
    settings = Settings.load()
    assert settings.cleanup_enabled is False
    assert settings.cleanup_retention_days == 90


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


def test_external_paths_default_under_home(clean_env):
    clean_env.setenv("JEAN_HOME", "~/.jean")
    settings = Settings.load()
    assert settings.identity_path == Path.home() / ".jean" / "IDENTITY.md"
    assert settings.mcp_config_path == Path.home() / ".jean" / "mcp.json"
    assert settings.plugins_path == Path.home() / ".jean" / "jean.json"
    assert settings.marketplace_cache_dir == Path.home() / ".jean" / "marketplaces"
    assert settings.marketplace_token is None


def test_external_paths_override(clean_env):
    clean_env.setenv("JEAN_IDENTITY_PATH", "/etc/jean/soul.md")
    clean_env.setenv("JEAN_PLUGINS_PATH", "/etc/jean/jean.json")
    clean_env.setenv("JEAN_MCP_CONFIG_PATH", "/etc/jean/mcp.json")
    clean_env.setenv("JEAN_MARKETPLACE_TOKEN", "ghp_abc")
    settings = Settings.load()
    assert settings.identity_path == Path("/etc/jean/soul.md")
    assert settings.plugins_path == Path("/etc/jean/jean.json")
    assert settings.mcp_config_path == Path("/etc/jean/mcp.json")
    assert settings.marketplace_token == "ghp_abc"


def test_approvers_default_empty(clean_env):
    assert Settings.load().approvers == ()


def test_approvers_parsed_from_comma_separated_env(clean_env):
    """The ops-level backstop: an approver set that does not depend on the LLM
    extracting IDENTITY.md correctly."""
    clean_env.setenv("JEAN_APPROVERS", "U11111, U22222")
    assert Settings.load().approvers == ("U11111", "U22222")


def test_approvers_rejects_a_non_slack_id(clean_env):
    clean_env.setenv("JEAN_APPROVERS", "not-a-slack-id")
    with pytest.raises(ValueError):
        Settings.load()


def test_db_pool_defaults_are_modest(clean_env):
    # jean shares a small managed Postgres with other apps; a worker must not
    # hog connection slots. Keep the default pool small enough that N workers
    # fit inside a low `max_connections` budget.
    settings = Settings.load()
    assert settings.db_pool_min == 1
    assert settings.db_pool_max == 5


def test_db_pool_size_overridable(clean_env):
    clean_env.setenv("JEAN_DB_POOL_MIN", "2")
    clean_env.setenv("JEAN_DB_POOL_MAX", "3")
    settings = Settings.load()
    assert settings.db_pool_min == 2
    assert settings.db_pool_max == 3
