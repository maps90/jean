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
    # Per-worker asyncpg pool bounds. jean typically shares a managed Postgres
    # with other apps, and each worker also opens a separate LISTEN connection
    # on top of the pool -- so N workers cost N*(db_pool_max + 1) slots against
    # the server's `max_connections`. Keep the default modest; raise it only on
    # a server with headroom to spare.
    db_pool_min: int = 1
    db_pool_max: int = 5
    home: Path = Path.home() / ".jean"
    idle_minutes: int = 15
    approval_ttl: int = 1800
    permission_mode: str = "bypassPermissions"
    health_port: int = 8080
    model: str | None = None
    soul_parse_model: str = "claude-haiku-4-5-20251001"

    # External file paths (mountable from a Secret); default under home.
    identity_path: Path | None = None
    mcp_config_path: Path | None = None
    plugins_path: Path | None = None
    marketplace_cache_dir: Path | None = None
    marketplace_token: str | None = None

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
        self.identity_path = (self.identity_path or self.home / "IDENTITY.md").expanduser()
        self.mcp_config_path = (self.mcp_config_path or self.home / "mcp.json").expanduser()
        self.plugins_path = (self.plugins_path or self.home / "jean.json").expanduser()
        self.marketplace_cache_dir = (
            self.marketplace_cache_dir or self.home / "marketplaces"
        ).expanduser()

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"
