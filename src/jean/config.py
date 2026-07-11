from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """jean runtime configuration.

    All fields come from `JEAN_*` env vars except the two auth tokens, which
    are unprefixed by convention. Use `Settings.load()`
    rather than `Settings()` directly so the auth vars get wired in.
    """

    model_config = SettingsConfigDict(env_prefix="JEAN_", extra="ignore")

    slack_bot_token: str
    slack_app_token: str

    anthropic_api_key: str | None = None
    claude_code_oauth_token: str | None = None

    database_url: str = "postgresql://jean:jean@localhost:5432/jean"
    home: Path = Path.home() / ".jean"
    idle_minutes: int = 15
    approval_ttl: int = 1800
    permission_mode: str = "bypassPermissions"
    health_port: int = 8080
    model: str | None = None
    soul_parse_model: str = "claude-haiku-4-5-20251001"

    # Weekly Postgres retention cleanup: prune resolved approvals and sessions
    # idle longer than the retention window. Set cleanup_enabled=false to skip.
    cleanup_enabled: bool = True
    cleanup_retention_days: int = 30

    @classmethod
    def load(cls) -> Settings:
        """Build Settings, wiring the two unprefixed auth env vars in."""
        kwargs: dict[str, str] = {}
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if api_key:
            kwargs["anthropic_api_key"] = api_key
        if oauth_token:
            kwargs["claude_code_oauth_token"] = oauth_token
        return cls(**kwargs)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self.home = self.home.expanduser()

    @property
    def identity_path(self) -> Path:
        return self.home / "IDENTITY.md"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"
