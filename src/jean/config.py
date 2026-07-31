from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from jean.persona.model import USER_ID_RE

# The CLI's `--effort` levels (claude-agent-sdk `EffortLevel`). Validated here so
# a typo fails the boot rather than the first turn, where it would surface as a
# dead reply after a human already waited on one.
ALLOWED_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


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
    # Postgres schema this instance's tables live in (via search_path). The
    # default "public" is single-agent-per-database: exactly the old behavior.
    # Set a distinct value per agent (e.g. "anya", "damian") to host MULTIPLE
    # agents in ONE database -- each gets its own schema, and the per-thread
    # lock / cleanup gate / approval NOTIFY channel are namespaced by it, so
    # agents never read, write, or prune each other's rows. All sharing agents
    # point JEAN_DATABASE_URL at the same DB and differ only by JEAN_DB_SCHEMA.
    db_schema: str = "public"
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
    # jean gates only *risky* tool calls (agent_options.classify_risk): routine
    # mutations run unattended, the four risky categories ask a human, and
    # "Always allow" silences a repeated pattern for the session. "default" is
    # the mode where the CLI calls the permission hook for every mutating tool
    # so the classifier can decide. Reachable per-thread via `/mode`:
    # "plan" makes the agent present a plan first; "bypassPermissions" skips the
    # hook entirely, leaving only the agent-chosen request_approval tool.
    permission_mode: str = "default"
    health_port: int = 8080
    # The `agent` label on every exported metric. Defaults to db_schema, which is
    # already the per-agent discriminator in a shared database (see above), so a
    # multi-agent deployment gets correct labels with no extra config. Set this
    # explicitly on a single-agent install, where db_schema is the useless
    # "public". Prometheus also attaches job/pod labels of its own; this one makes
    # a dashboard portable across scrape configs.
    metrics_agent: str | None = None
    model: str | None = None
    # How hard the model works per turn. Unset = let the CLI pick its own default
    # (`xhigh`), which is what every deployment ran before this existed. This is
    # the primary latency/cost lever: a turn's wall clock is dominated by the
    # tokens the model generates, and effort is what governs how many of those
    # there are -- lower effort means less thinking, less preamble, and more
    # consolidated tool calls. Lower it far enough and the model under-thinks
    # multi-step work, so it belongs in config where a deployment can sweep it
    # against its own traffic, not hardcoded here.
    effort: str | None = None
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
    # How often a worker looks for due schedules, and how late a firing may be and
    # still run. A summary forty minutes late is fine; two days late carries a
    # "weekly" framing that is no longer true, so it is recorded as missed instead.
    schedule_poll_seconds: float = 30.0
    schedule_grace_seconds: float = 3600.0

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
    # Post a heads-up in the thread if a turn is still running after this many
    # seconds; 0 disables. A turn's wall clock is dominated by model generation --
    # 149s for a metrics-and-logs investigation is normal and 99% of it is tokens
    # jean cannot speed up -- and `set_status` ("is thinking...") only renders in
    # Slack's Assistant pane, so a channel shows nothing at all until the answer
    # lands. Silence for minutes reads as broken; this makes the wait legible.
    slow_turn_seconds: float = 20.0

    @field_validator("effort", mode="before")
    @classmethod
    def _valid_effort(cls, value: object) -> str | None:
        """Normalize and validate the effort level at boot.

        Empty is treated as unset: clearing a key in a mounted Secret leaves an
        empty string, and that should mean "let the CLI decide" rather than a
        refusal to start. Case and padding are forgiven because the value is
        typically pasted by a human; an unknown level is not, because the CLI
        would only reject it once a turn was already running.
        """
        if value is None:
            return None
        level = str(value).strip().lower()
        if not level:
            return None
        if level not in ALLOWED_EFFORT_LEVELS:
            raise ValueError(
                f"JEAN_EFFORT must be one of {', '.join(sorted(ALLOWED_EFFORT_LEVELS))}: {value!r}"
            )
        return level

    @field_validator("db_schema")
    @classmethod
    def _valid_schema(cls, value: str) -> str:
        """db_schema is interpolated into DDL (CREATE SCHEMA), the search_path,
        the cleanup advisory-lock key, and the LISTEN/NOTIFY channel name -- none
        of which can be a bound parameter. So it must be a plain lowercase SQL
        identifier, validated at boot, never trusted from the environment."""
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
            raise ValueError(
                f"JEAN_DB_SCHEMA must be a lowercase identifier [a-z_][a-z0-9_]*: {value!r}"
            )
        return value

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
