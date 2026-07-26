from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from jean.approval.gate import ACTION_RE
from jean.gateway.dispatch import dispatch
from jean.gateway.engagement import decide, mentions_in
from jean.persona.model import SoulData
from jean.ports import ChatSurface, SessionStore

logger = logging.getLogger("jean.gateway")

# Added to a message the moment engagement accepts it, before the model runs.
# `set_status` ("is thinking...") only renders in Slack's Assistant pane, so in a
# channel there was nothing at all between the question and the first reply.
ACK_EMOJI = "eyes"

ALLOWED_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
    "auto",
}


def ephemeral_for(result: str) -> str | None:
    """What to tell the clicker privately, if anything. A decided request has
    its buttons rewritten away (ApprovalGate._retire), so "gone" here means the
    click raced the rewrite or hit a stale message -- say so, rather than
    leaving them clicking a button that silently does nothing."""
    if result == "unauthorized":
        return "You are not authorized to approve or deny this request."
    if result == "gone":
        return "This request was already decided or has expired."
    return None


def help_text(agent_name: str) -> str:
    return (
        f"{agent_name} commands:\n"
        f"/mode <mode> - set this thread's permission mode "
        f"({', '.join(sorted(ALLOWED_PERMISSION_MODES))})\n"
        "/help - show this message"
    )


class _Manager(Protocol):
    async def handle(self, channel: str, thread_ts: str, text: str) -> None: ...


class _Gate(Protocol):
    async def handle_action(self, action_id: str, user_id: str) -> str: ...


class Gateway:
    """Slack-free coordination point: engagement decisions (gateway/engagement.py),
    turn dispatch (gateway/dispatch.py), approval actions, and slash commands.
    `register()` below is the only place this touches slack_bolt."""

    def __init__(
        self,
        *,
        store: SessionStore,
        manager: _Manager,
        gate: _Gate,
        bot_id: str,
        soul_provider: Callable[[], SoulData],
        chat: ChatSurface | None = None,
    ) -> None:
        self._store = store
        self._manager = manager
        self._gate = gate
        self._bot_id = bot_id
        self._soul_provider = soul_provider
        # Optional: the ack is a nicety, and the single-process/test wiring may
        # not have a surface. Never let its absence decide whether a turn runs.
        self._chat = chat

    async def on_mention(
        self,
        *,
        channel: str,
        thread_ts: str,
        text: str,
        author_id: str | None,
        message_ts: str | None = None,
    ) -> None:
        """Owns bot-@mentions: routes through `decide()` so authorization (the
        blocked-user check) and partner assignment have one source of truth,
        shared with `on_message`. Slack also delivers the same @mention as a
        plain `message` event (see `on_message`, which skips bot-mentions to
        avoid running the turn twice)."""
        await self._engage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            author_id=author_id,
            is_dm=False,
            message_ts=message_ts,
        )

    async def on_message(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        author_id: str | None,
        is_dm: bool,
        message_ts: str | None = None,
    ) -> None:
        if self._bot_id in mentions_in(text):
            # `on_mention` (app_mention event) already engages + dispatches
            # this turn; handling it here too would run it twice.
            return
        await self._engage(
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            author_id=author_id,
            is_dm=is_dm,
            message_ts=message_ts,
        )

    async def _engage(
        self,
        *,
        channel: str,
        thread_ts: str,
        text: str,
        author_id: str | None,
        is_dm: bool,
        message_ts: str | None = None,
    ) -> None:
        partner = await self._store.get_partner(channel, thread_ts)
        decision = decide(
            bot_id=self._bot_id,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
            is_dm=is_dm,
            soul=self._soul_provider(),
            partner=partner,
            author_id=author_id,
        )
        # Write only on a real change: a bystander's message must cost nothing --
        # no turn, and no database write either.
        if decision.partner != partner:
            await self._store.set_partner(channel, thread_ts, decision.partner)
        # One line per inbound message, handled or not. Ignoring is a legitimate
        # outcome here, so without this an ignored message and an event that
        # never arrived produced identical output: nothing.
        logger.info(
            "message %s/%s from %s is_dm=%s handle=%s reason=%s partner=%s->%s",
            channel,
            thread_ts,
            author_id,
            is_dm,
            decision.handle,
            decision.reason,
            partner,
            decision.partner,
        )
        if not decision.handle:
            return
        await self._ack(channel, message_ts)
        await dispatch(self._manager, channel=channel, thread_ts=thread_ts, text=text)

    async def _ack(self, channel: str, message_ts: str | None) -> None:
        """React the instant the message is accepted, so a channel shows the
        difference between "working on it" and "never heard you".

        Best-effort by design (the documented exception to never swallowing in
        domain code): a missing `reactions:write` scope, a deleted message or an
        already-present reaction must not cost the user their answer.
        """
        if self._chat is None or message_ts is None:
            return
        try:
            await self._chat.react(channel, message_ts, ACK_EMOJI)
        except Exception as exc:
            logger.debug("ack reaction failed on %s/%s: %s", channel, message_ts, exc)

    async def on_action(self, action_id: str, user_id: str) -> str:
        return await self._gate.handle_action(action_id, user_id)

    async def on_command(
        self, command: str, channel: str, thread_ts: str, user_id: str, text: str
    ) -> str:
        del user_id  # not needed today; kept for future auditing/authz on commands
        if command == "/mode":
            mode = text.strip()
            if mode not in ALLOWED_PERMISSION_MODES:
                return (
                    f"unknown permission mode {mode!r}. allowed: "
                    f"{', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
                )
            await self._store.upsert_session(channel, thread_ts, permission_mode=mode, touch=False)
            return f"permission mode set to {mode!r} for this thread"
        if command == "/help":
            return help_text(self._soul_provider().identity.name)
        return f"unknown command {command!r}"


def register(app: Any, gw: Gateway) -> None:
    """Wire the Gateway into a slack_bolt AsyncApp. Not unit-tested (it's the
    bolt seam) -- exercise it via a running app; Gateway's methods above carry
    all the logic and are fully covered by tests/test_gateway_app.py."""

    @app.event("app_mention")
    async def _on_app_mention(event: dict) -> None:
        # Only drop other bots' @mentions here -- unlike `message`, a `subtype`
        # (e.g. `file_share`, `thread_broadcast`) can still be a real, addressed
        # mention and must reach `on_mention`.
        if event.get("bot_id") is not None:
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        await gw.on_mention(
            channel=channel,
            thread_ts=thread_ts,
            text=event.get("text", ""),
            author_id=event.get("user"),
            # `ts` is THIS message; `thread_ts` is the thread's opening message.
            # The ack belongs on the message that was actually addressed to jean.
            message_ts=event.get("ts"),
        )

    @app.event("message")
    async def _on_message(event: dict) -> None:
        if event.get("subtype") is not None or event.get("bot_id") is not None:
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        is_dm = event.get("channel_type") == "im"
        await gw.on_message(
            channel,
            thread_ts,
            event.get("text", ""),
            event.get("user"),
            is_dm,
            message_ts=event.get("ts"),
        )

    @app.action(ACTION_RE)
    async def _on_action(ack: Callable, body: dict, client: Any) -> None:
        await ack()
        action_id = body["actions"][0]["action_id"]
        user_id = body["user"]["id"]
        result = await gw.on_action(action_id, user_id)
        message = ephemeral_for(result)
        if message is not None:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"], user=user_id, text=message
            )

    @app.command("/mode")
    async def _on_mode(ack: Callable, command: dict, respond: Callable) -> None:
        await ack()
        result = await gw.on_command(
            "/mode",
            command["channel_id"],
            command.get("thread_ts", command["channel_id"]),
            command["user_id"],
            command.get("text", ""),
        )
        await respond(result)

    @app.command("/help")
    async def _on_help(ack: Callable, command: dict, respond: Callable) -> None:
        await ack()
        result = await gw.on_command(
            "/help",
            command["channel_id"],
            command.get("thread_ts", command["channel_id"]),
            command["user_id"],
            "",
        )
        await respond(result)
