from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from jean.approval.authz import select_approvers
from jean.persona.model import ApproverEntry
from jean.ports import ApprovalCoordinator, ApprovalDecision

logger = logging.getLogger("jean.approval")

ACTION_RE = re.compile(r"^jean_appr:(approve|always|deny):(.+)$")

# (channel, thread_ts, text, blocks) -> posted message ts. Kept as a plain
# callable (not the concrete Slack client) so approval/ stays free of
# slack_sdk imports (ports & adapters: the domain layer stays infra-free).
PostBlocks = Callable[[str, str, str, list[dict]], Awaitable[str]]
# (channel, ts, text, blocks) -> rewrite an already-posted message in place.
UpdateBlocks = Callable[[str, str, str, list[dict]], Awaitable[None]]
ApproversProvider = Callable[[], list[ApproverEntry]]
ManagerProvider = Callable[[], str | None]


class ApprovalGate:
    """Posts a Block Kit approval request and waits on the ApprovalCoordinator
    port. Authorization (`select_approvers`) and resolution both live in code
    -- the persona/LLM never decides who may approve or self-approves."""

    def __init__(
        self,
        post_blocks: PostBlocks,
        coordinator: ApprovalCoordinator,
        *,
        update_blocks: UpdateBlocks,
        approvers_provider: ApproversProvider,
        timeout_seconds: float,
        manager_provider: ManagerProvider = lambda: None,
        env_approvers: tuple[str, ...] = (),
    ) -> None:
        self._post_blocks = post_blocks
        self._update_blocks = update_blocks
        self._coordinator = coordinator
        self._approvers_provider = approvers_provider
        self._manager_provider = manager_provider
        self._timeout_seconds = timeout_seconds
        self._env_approvers = env_approvers

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        approvers = select_approvers(
            summary,
            self._approvers_provider(),
            env_fallback=self._env_approvers,
            manager=self._manager_provider(),
        )
        if not approvers:
            # Fail closed, now, rather than posting buttons that authorize
            # nobody: handle_action checks the clicker against this set, so an
            # empty one makes EVERY click "unauthorized" -- the request would
            # hang for the full approval_ttl and then auto-deny as "system",
            # with the thread none the wiser. Say so instead.
            logger.error(
                "no approver resolved for %r in %s/%s -- refusing. Set JEAN_APPROVERS or name "
                "a catch-all approver (or a manager) in IDENTITY.md.",
                summary,
                channel,
                thread_ts,
            )
            await self._post_blocks(
                channel, thread_ts, "Cannot approve: no approver configured.", _no_approver_blocks()
            )
            return ApprovalDecision(False, "system")

        approval_id = uuid.uuid4().hex
        # Row + approver set must exist BEFORE the blocks (which embed
        # approval_id in their action_ids) are posted -- otherwise a click
        # that lands between posting and set_approvers finds no pending row
        # ("gone") or no authorized approvers ("unauthorized") and the
        # request dead-ends at timeout instead of resolving.
        await self._coordinator.create(approval_id, channel, thread_ts, summary)
        await self._coordinator.set_approvers(approval_id, approvers)
        blocks = _build_blocks(approval_id, summary, approvers)
        ts = await self._post_blocks(channel, thread_ts, f"Approval requested: {summary}", blocks)
        decision = await self._coordinator.wait(approval_id, self._timeout_seconds)
        # The buttons are the clicker's only feedback, so retire them here --
        # on whichever worker is waiting, for every outcome (approve, deny,
        # timeout). Without this the message keeps its live buttons and a
        # decided request looks untouched, so people click it again.
        await self._retire(channel, ts, summary, decision)
        return decision

    async def _retire(
        self, channel: str, ts: str, summary: str, decision: ApprovalDecision
    ) -> None:
        text, blocks = _resolved_message(summary, decision)
        try:
            await self._update_blocks(channel, ts, text, blocks)
        except Exception:  # noqa: BLE001 -- best-effort Slack nicety
            # The decision is already durable in the coordinator and is what
            # the agent acts on; a failed rewrite must not turn it into an
            # error. Loud in the log, harmless to the turn.
            logger.warning("could not rewrite approval message %s/%s", channel, ts, exc_info=True)

    async def handle_action(self, action_id: str, user_id: str) -> str:
        match = ACTION_RE.match(action_id)
        if not match:
            return "gone"
        verb, approval_id = match.group(1), match.group(2)

        pending = await self._coordinator.get_pending(approval_id)
        if pending is None:
            return "gone"

        authorized = await self._coordinator.approvers_of(approval_id)
        if user_id not in authorized:
            return "unauthorized"

        approved = verb != "deny"
        scope = "always" if verb == "always" else "once"
        resolved = await self._coordinator.resolve(approval_id, approved, user_id, scope)
        if not resolved:
            return "gone"
        return "approved" if approved else "denied"


def _no_approver_blocks() -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Cannot approve: no approver configured.*\n"
                    "I need a human who is allowed to approve this, and nobody is. "
                    "Name a catch-all approver (or a manager) in `IDENTITY.md`, or set "
                    "`JEAN_APPROVERS`. Not running the action."
                ),
            },
        }
    ]


def _resolved_message(summary: str, decision: ApprovalDecision) -> tuple[str, list[dict]]:
    """The decided form of the request: same summary, no actions block. `by` is
    the sentinel "system" on a timeout -- not a Slack id, so never mention it."""
    if decision.by == "system":
        headline, footer = "Approval expired", "No answer in time -- treated as denied."
    elif decision.approved and decision.scope == "always":
        headline = "Always-allowed for this session"
        footer = f"Always-allowed by <@{decision.by}>"
    elif decision.approved:
        headline, footer = "Approved", f"Approved by <@{decision.by}>"
    else:
        headline, footer = "Denied", f"Denied by <@{decision.by}>"
    text = f"{headline}: {summary}"
    return text, [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{headline}*\n{summary}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": footer}],
        },
    ]


def _build_blocks(approval_id: str, summary: str, approvers: set[str]) -> list[dict]:
    # `approvers` is never empty here -- request() fails closed before this.
    mentions = " ".join(f"<@{uid}>" for uid in sorted(approvers))
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Approval requested*\n{summary}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": f"jean_appr:approve:{approval_id}",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Always allow"},
                    "action_id": f"jean_appr:always:{approval_id}",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": f"jean_appr:deny:{approval_id}",
                    "value": approval_id,
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Approver(s): {mentions}"}],
        },
    ]
