from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from jean.persona.model import USER_ID_RE


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
    # Ops-level approver backstop: JEAN_APPROVERS="U11111,U22222". Used only when
    # IDENTITY.md yields no approver for an action (see approval/authz.py). It
    # exists so a soul that fails to parse cannot leave jean with an approval
    # nobody is authorized to click.
    # NoDecode: pydantic-settings would otherwise JSON-decode a tuple field, and
    # this one is written as a plain comma-separated list.
    approvers: Annotated[tuple[str, ...], NoDecode] = ()
    # "default" is what makes approvals real: the CLI asks before every tool it
    # does not auto-allow, and jean answers by posting Approve/Deny buttons and
    # waiting on the click (agent_options.build_can_use_tool). Under
    # "bypassPermissions" the CLI skips its permission system entirely, so the
    # hook is never called and the only gate left is the agent *choosing* to
    # call request_approval -- i.e. the persona could decide not to. A thread
    # can still opt out for itself with `/mode bypassPermissions`.
    permission_mode: str = "default"
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

    @field_validator("approvers", mode="before")
    @classmethod
    def _parse_approvers(cls, value: object) -> tuple[str, ...]:
        """`JEAN_APPROVERS="U11111, U22222"` -> ("U11111", "U22222").

        Validated here rather than trusted: these ids go straight into the set the
        approval gate authorizes clicks against, so a typo must fail at boot, not
        silently authorize nobody.
        """
        if value is None or value == "":
            return ()
        parts = value.split(",") if isinstance(value, str) else list(value)
        ids = tuple(str(p).strip() for p in parts if str(p).strip())
        for uid in ids:
            if not USER_ID_RE.match(uid):
                raise ValueError(f"invalid Slack user id in JEAN_APPROVERS: {uid!r}")
        return ids

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
