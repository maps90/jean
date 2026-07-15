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
    # "plan" makes the approval a single, informed click: the CLI keeps the agent
    # read-only until it presents a plan (the ExitPlanMode tool), jean posts THAT
    # plan for one Approve/Deny, and on approval flips the turn to run its steps
    # unattended (agent_options.build_can_use_tool switches to bypassPermissions;
    # session.py re-arms plan for the next turn, so the approval binds to one plan).
    # Alternatives, both reachable per-thread via `/mode`: "default" gates every
    # mutating tool one click at a time; "bypassPermissions" skips the CLI's
    # permission system entirely, so the hook never fires and the only gate left is
    # the agent *choosing* to call request_approval -- i.e. the persona could
    # decide not to.
    permission_mode: str = "plan"
    health_port: int = 8080
    model: str | None = None
    soul_parse_model: str = "claude-haiku-4-5-20251001"

    # External file paths (mountable from a Secret); default under home.
    identity_path: Path | None = None
    mcp_config_path: Path | None = None
    plugins_path: Path | None = None
    marketplace_cache_dir: Path | None = None
    marketplace_token: str | None = None

    # Postgres retention cleanup, swept daily by whichever worker claims the cycle.
    # Sessions and approvals expire on separate schedules: a thread's memory going
    # stale is not the same event as an audit record aging out. Deleting a session
    # row also drops its transcript (FK cascade) -- and its engaged_with/permission_mode,
    # so a thread quiet this long needs a fresh mention to re-engage jean.
    cleanup_enabled: bool = True
    session_retention_days: int = 3
    approval_retention_days: int = 30
    cleanup_interval_hours: int = 24
    # Refuse to archive a pathological transcript rather than let one thread bloat
    # the database. Such a thread keeps working, but only on the worker holding it.
    transcript_max_mb: int = 32

    # The CLI writes a turn to its .jsonl write-behind, so jean waits for the file to
    # settle before archiving it (JeanSession._settle). All three are seconds.
    #   settle_quiet    -- how long the file must stay unchanged to count as finished.
    #                      Sized against the CLI's flush lag (~0.5s to the final
    #                      `assistant` record, ~0.1s more for the `system` records that
    #                      trail it), with room to spare: too short and jean archives a
    #                      turn missing its answer, which a cold worker then resumes.
    #   settle_interval -- how often to sample the file while waiting.
    #   settle_timeout  -- the ceiling. Hitting it archives whatever is on disk anyway
    #                      and logs loudly; the user's turn is never failed over it.
    settle_timeout: float = 10.0
    settle_interval: float = 0.1
    settle_quiet: float = 1.0

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
