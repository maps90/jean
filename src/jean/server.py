from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from jean.approval.gate import ApprovalGate
from jean.config import Settings
from jean.db.postgres import PostgresStore
from jean.gateway.app import Gateway, register
from jean.health import make_health_app
from jean.persona.extract import load_soul_data
from jean.persona.identity import compose_system_prompt, load_identity
from jean.persona.model import SoulData
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


async def _allow_all_tools(
    tool_name: str, tool_input: dict[str, Any], context: Any
) -> PermissionResultAllow:
    del tool_name, tool_input, context  # v1: the gate + persona are the discipline, not the SDK
    return PermissionResultAllow()


async def run() -> None:
    settings = Settings.load()
    settings.home.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.home / "workspaces").mkdir(parents=True, exist_ok=True)

    store = await PostgresStore.connect(settings.database_url)

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

    gate = ApprovalGate(
        post_blocks,
        store,
        approvers_provider=lambda: soul_provider().approvers,
        timeout_seconds=settings.approval_ttl,
    )

    routing = RoutingContext()
    server_mcp, tool_names, _tools = build_slack_mcp(
        chat, gate, channel_of=lambda: routing.channel, thread_of=lambda: routing.thread_ts
    )

    def options_factory(resume: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=compose_system_prompt(persona_text),
            mcp_servers={"jean_slack": server_mcp},
            allowed_tools=tool_names,
            permission_mode=settings.permission_mode,
            can_use_tool=_allow_all_tools,
            resume=resume,
            model=settings.model,
            cwd=str(settings.home / "workspaces"),
        )

    def session_factory(channel: str, thread_ts: str) -> JeanSession:
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

    health_app = make_health_app(ready_check=store.ping)
    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.health_port)
    await site.start()
    logger.info("jean health server listening on :%d", settings.health_port)

    try:
        await asyncio.gather(
            AsyncSocketModeHandler(app, settings.slack_app_token).start_async(),
            manager.run_sweeper(),
        )
    finally:
        await runner.cleanup()
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
