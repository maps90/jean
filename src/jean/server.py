from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

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
from jean.session.manager import SessionManager
from jean.session.session import JeanSession, RoutingContext
from jean.slack.client import SlackSurface
from jean.slack.mcp import build_slack_mcp

logger = logging.getLogger("jean.server")


@dataclass
class _SoulCell:
    """Mutable holder so `soul_provider` always reflects the latest loaded
    SoulData (a seam for a future hot-reload command; v1 loads it once)."""

    soul: SoulData


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

    def manager_of() -> str | None:
        manager = soul_provider().manager
        return manager.user_id if manager else None

    gate = ApprovalGate(
        post_blocks,
        store,
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

    routing = RoutingContext()
    server_mcp, tool_names, _tools = build_slack_mcp(
        chat, gate, channel_of=lambda: routing.channel, thread_of=lambda: routing.thread_ts
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
    take_over_plugin_mcp(plugins)
    mcp_servers = {**build_proxy_servers(mcp_clients), **remote_servers(extra_mcp)}

    def session_factory(channel: str, thread_ts: str) -> JeanSession:
        # Bound per session: the permission hook waits on a human for up to
        # approval_ttl, and the RoutingContext the MCP tools read is process-wide
        # -- another thread starting a turn during that wait would repoint it and
        # send this approval to the wrong thread. The channel/thread are known
        # here, so close over them instead.
        can_use_tool = build_can_use_tool(gate, channel=channel, thread_ts=thread_ts)

        def options_factory(resume: str | None, permission_mode: str | None) -> ClaudeAgentOptions:
            return build_agent_options(
                persona_text=persona_text,
                agent_name=soul_provider().identity.name,
                slack_server=server_mcp,
                slack_tool_names=tool_names,
                mcp_servers=mcp_servers,
                plugins=plugins,
                settings=settings,
                resume=resume,
                permission_mode=permission_mode,
                can_use_tool=can_use_tool,
            )

        return JeanSession(
            channel,
            thread_ts,
            store=store,
            chat=chat,
            routing=routing,
            options_factory=options_factory,
            client_factory=ClaudeSDKClient,
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
        scheduler = CleanupScheduler(
            store, retention_seconds=settings.cleanup_retention_days * 86400
        )
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
