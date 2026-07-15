from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from jean.agent_options import build_agent_options, build_can_use_tool
from jean.approval.gate import ApprovalGate
from jean.config import Settings
from jean.db.postgres import PostgresStore
from jean.gateway.app import Gateway, register
from jean.health import ErrorOnlyAccessLogger, make_health_app
from jean.maintenance.cleanup import CleanupScheduler
from jean.persona.extract import load_soul_data
from jean.persona.identity import load_identity
from jean.persona.model import SoulData
from jean.plugins.git_resolver import GitMarketplaceResolver
from jean.plugins.manifest import load_mcp_config, load_plugin_manifest
from jean.plugins.mcp_client import start_clients
from jean.plugins.mcp_config import remote_servers, stdio_servers, take_over_plugin_mcp
from jean.plugins.mcp_proxy import build_proxy_servers
from jean.ports import ChatSurface, MaintenanceStore, SessionStore, TranscriptStore
from jean.session.manager import SessionManager
from jean.session.session import JeanSession
from jean.session.transcript import LocalTranscripts
from jean.slack.client import SlackSurface
from jean.slack.mcp import build_slack_mcp

logger = logging.getLogger("jean.server")

# What JeanSession calls to build a client's options: (resume, permission_mode).
OptionsFactory = Callable[[str | None, str | None], ClaudeAgentOptions]


class _Store(SessionStore, TranscriptStore, MaintenanceStore, Protocol):
    """The one adapter (PostgresStore in production; FakeStore in tests)
    that structurally satisfies all three ports at once, so it can be passed
    as all three without a cast."""


@dataclass
class _SoulCell:
    """Mutable holder so `soul_provider` always reflects the latest loaded
    SoulData (a seam for a future hot-reload command; v1 loads it once)."""

    soul: SoulData


def build_local_transcripts(settings: Settings) -> LocalTranscripts:
    """`cwd` MUST equal the `cwd` build_agent_options hands the CLI
    (settings.home / "workspaces", see agent_options.py) -- LocalTranscripts
    derives the CLI's on-disk project directory by slugifying it, and a
    mismatch here makes transcript hydration silently look in the wrong
    directory (see JeanSession._archive's warning log)."""
    return LocalTranscripts(cli_home=Path.home(), cwd=settings.home / "workspaces")


def build_session_factory(
    *,
    settings: Settings,
    store: _Store,
    chat: ChatSurface,
    options_factory_for: Callable[[str, str], OptionsFactory],
    client_factory: Callable[..., Any],
    local_transcripts: LocalTranscripts,
) -> Callable[[str, str], JeanSession]:
    """`store` is handed to JeanSession as both `store=` and `transcripts=`:
    PostgresStore satisfies SessionStore and TranscriptStore structurally, so
    one object serves both roles.

    The options factory is built PER SESSION (`options_factory_for`), not once
    for the process: it closes over the SDK's permission hook, which must know
    which Slack thread to ask for approval in (see run()).
    """

    def session_factory(channel: str, thread_ts: str) -> JeanSession:
        return JeanSession(
            channel,
            thread_ts,
            store=store,
            chat=chat,
            options_factory=options_factory_for(channel, thread_ts),
            client_factory=client_factory,
            transcripts=store,
            local=local_transcripts,
            max_transcript_bytes=settings.transcript_max_mb * 1024 * 1024,
            settle_timeout=settings.settle_timeout,
            settle_interval=settings.settle_interval,
            settle_quiet=settings.settle_quiet,
        )

    return session_factory


def build_cleanup_scheduler(store: MaintenanceStore, settings: Settings) -> CleanupScheduler:
    """Sessions and approvals expire on separate windows -- a thread's memory
    going stale and an audit record aging out are different concerns."""
    return CleanupScheduler(
        store,
        session_retention_seconds=settings.session_retention_days * 86400,
        approval_retention_seconds=settings.approval_retention_days * 86400,
        interval_seconds=settings.cleanup_interval_hours * 3600,
    )


async def run() -> None:
    settings = Settings.load()
    settings.home.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.home / "workspaces").mkdir(parents=True, exist_ok=True)

    store = await PostgresStore.connect(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )

    persona_text = load_identity(settings.identity_path)
    cell = _SoulCell(soul=await load_soul_data(settings))
    soul_provider = lambda: cell.soul  # noqa: E731

    app = AsyncApp(token=settings.slack_bot_token)
    auth = await app.client.auth_test()
    bot_id = auth["user_id"]

    chat = SlackSurface(app.client)

    async def post_blocks(channel: str, thread_ts: str, text: str, blocks: list[dict]) -> str:
        resp = await app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text, blocks=blocks
        )
        return resp["ts"]

    async def update_blocks(channel: str, ts: str, text: str, blocks: list[dict]) -> None:
        await app.client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)

    def manager_of() -> str | None:
        manager = soul_provider().manager
        return manager.user_id if manager else None

    gate = ApprovalGate(
        post_blocks,
        store,
        update_blocks=update_blocks,
        approvers_provider=lambda: soul_provider().approvers,
        manager_provider=manager_of,
        timeout_seconds=settings.approval_ttl,
        env_approvers=settings.approvers,
    )
    if not soul_provider().approvers and not settings.approvers and manager_of() is None:
        # Every approval will fail closed until this is fixed -- say so at boot
        # rather than at the first tool call a human is waiting on.
        logger.error(
            "no approvers configured: %s names no approver and no manager, and JEAN_APPROVERS "
            "is unset. jean will refuse every action that needs approval.",
            settings.identity_path,
        )

    extra_mcp = load_mcp_config(settings.mcp_config_path)
    resolver = GitMarketplaceResolver(
        token=settings.marketplace_token, cache_dir=settings.marketplace_cache_dir
    )
    plugins = await resolver.resolve(load_plugin_manifest(settings.plugins_path))

    health_app = make_health_app(ready_check=store.ping)
    # Probe traffic is constant and uninteresting; log only 4xx/5xx (see
    # ErrorOnlyAccessLogger) so it does not bury everything else.
    runner = web.AppRunner(health_app, access_log_class=ErrorOnlyAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    logger.info("jean health server listening on :%d", settings.health_port)

    # Run every stdio MCP server ONCE, here, for the whole worker -- and re-expose
    # their tools in-process (mcp_proxy) so the CLI never spawns any of its own.
    #
    # stdio transport *is* a child process, so a per-session ClaudeSDKClient cannot
    # share one: the CLI used to fork a full set (kubernetes + grafana +
    # elasticsearch, ~250 MB) for every Slack thread. Two live threads plus their
    # CLI children pinned the pod against its 1 GiB limit, and the next thread's
    # CLI then stalled in memory reclaim and never answered the SDK's `initialize`
    # -- surfacing as "Control request timeout: initialize". A hang, not a crash:
    # nothing was OOM-killed, the cgroup was simply full.
    #
    # This doubles as the preflight: the CLI never reports whether a server it
    # spawned came up (its init message calls them all "pending" and carries none
    # of their tools), so connecting here is what makes a broken server visible in
    # the log -- and now the connection that proved it works is the one every
    # thread uses.
    #
    # Placement is deliberate: *after* the health server binds, so a slow cold
    # `npx` cannot stall the liveness probe into killing the pod, and *before*
    # Slack connects, so no thread starts against a server that is still coming up.
    mcp_clients = await start_clients(stdio_servers(extra_mcp, plugins))
    # Read the plugins' http servers BEFORE the takeover renames the files they are
    # declared in. `remote_servers` can still read a renamed config (_mcp_json looks
    # for both names), but depending on that would be a trap for the next reader.
    remote = remote_servers(extra_mcp, plugins)
    take_over_plugin_mcp(plugins)
    mcp_servers = {**build_proxy_servers(mcp_clients), **remote}

    def options_factory_for(channel: str, thread_ts: str) -> OptionsFactory:
        # Everything the MCP tools and the permission hook need to know which
        # Slack thread they act in is bound HERE, per session -- never read from
        # a process-wide slot at call time. Both the reply tools and the approval
        # hook fire lazily, long after the turn began (the hook can wait on a
        # human for up to approval_ttl); a shared routing slot would let another
        # thread's turn repoint it in between, sending this thread's reply -- or
        # its approval request -- into the wrong thread. The channel/thread are
        # known here, so close over them. A per-session Slack server is cheap:
        # in-process closures, not a child process.
        can_use_tool = build_can_use_tool(gate, channel=channel, thread_ts=thread_ts)
        slack_server, slack_tool_names, _tools = build_slack_mcp(
            chat, gate, channel=channel, thread_ts=thread_ts
        )

        def options_factory(resume: str | None, permission_mode: str | None) -> ClaudeAgentOptions:
            return build_agent_options(
                persona_text=persona_text,
                agent_name=soul_provider().identity.name,
                slack_server=slack_server,
                slack_tool_names=slack_tool_names,
                mcp_servers=mcp_servers,
                plugins=plugins,
                settings=settings,
                resume=resume,
                permission_mode=permission_mode,
                can_use_tool=can_use_tool,
            )

        return options_factory

    local_transcripts = build_local_transcripts(settings)
    session_factory = build_session_factory(
        settings=settings,
        store=store,
        chat=chat,
        options_factory_for=options_factory_for,
        client_factory=ClaudeSDKClient,
        local_transcripts=local_transcripts,
    )

    manager = SessionManager(
        session_factory=session_factory,
        lock=store,
        idle_seconds=settings.idle_minutes * 60,
    )

    gw = Gateway(
        store=store, manager=manager, gate=gate, bot_id=bot_id, soul_provider=soul_provider
    )
    register(app, gw)

    tasks = [
        AsyncSocketModeHandler(app, settings.slack_app_token).start_async(),
        manager.run_sweeper(),
    ]
    if settings.cleanup_enabled:
        scheduler = build_cleanup_scheduler(store, settings)
        tasks.append(scheduler.run())

    try:
        await asyncio.gather(*tasks)
    finally:
        # The MCP servers are jean's children now, so jean is what reaps them --
        # nothing else will, and a stray npx child would outlive the pod's grace
        # period and be SIGKILLed.
        for client in mcp_clients:
            await client.close()
        await runner.cleanup()
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
