from __future__ import annotations

import asyncio

from jean.approval.gate import ApprovalGate
from jean.db.memory import MemoryStore
from jean.persona.model import ApproverEntry


def _action_ids(blocks: list[dict]) -> list[str]:
    ids = []
    for block in blocks:
        if block.get("type") == "actions":
            ids.extend(el["action_id"] for el in block["elements"])
    return ids


def _action_id_for(blocks: list[dict], verb: str) -> str:
    return next(a for a in _action_ids(blocks) if a.startswith(f"jean_appr:{verb}:"))


def _make_gate(coordinator, posted, *, approvers=(), timeout_seconds=1.0, posted_event=None):
    async def post_blocks(channel, thread_ts, text, blocks):
        posted.append(blocks)
        if posted_event is not None:
            posted_event.set()
        return "999.0"

    return ApprovalGate(
        post_blocks,
        coordinator,
        approvers_provider=lambda: list(approvers),
        timeout_seconds=timeout_seconds,
    )


async def test_blocks_contain_action_ids_and_approver_mention():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(coordinator, posted, approvers=approvers, timeout_seconds=0.05)

    await gate.request("C1", "111.0", "deploy please")

    ids = _action_ids(posted[0])
    assert any(a.startswith("jean_appr:approve:") for a in ids)
    assert any(a.startswith("jean_appr:deny:") for a in ids)
    context = next(b for b in posted[0] if b["type"] == "context")
    assert "<@U11111>" in context["elements"][0]["text"]


async def test_approve_flow_resolves_the_waiter():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5, posted_event=posted_event
    )

    async def click_approve():
        await posted_event.wait()
        action_id = _action_id_for(posted[0], "approve")
        result = await gate.handle_action(action_id, "U11111")
        assert result == "approved"

    decision, _ = await asyncio.gather(
        gate.request("C1", "111.0", "deploy please"), click_approve()
    )
    assert decision.approved is True
    assert decision.by == "U11111"


async def test_deny_flow_resolves_the_waiter():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5, posted_event=posted_event
    )

    async def click_deny():
        await posted_event.wait()
        action_id = _action_id_for(posted[0], "deny")
        result = await gate.handle_action(action_id, "U11111")
        assert result == "denied"

    decision, _ = await asyncio.gather(gate.request("C1", "111.0", "deploy please"), click_deny())
    assert decision.approved is False
    assert decision.by == "U11111"


async def test_unauthorized_clicker_keeps_it_pending_and_it_times_out():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=0.1, posted_event=posted_event
    )

    async def click_wrong_user():
        await posted_event.wait()
        action_id = _action_id_for(posted[0], "approve")
        result = await gate.handle_action(action_id, "U99999")
        assert result == "unauthorized"

    decision, _ = await asyncio.gather(
        gate.request("C1", "111.0", "deploy please"), click_wrong_user()
    )
    assert decision.approved is False
    assert decision.by == "system"


async def test_timeout_auto_denies_with_no_click():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    gate = _make_gate(coordinator, posted, approvers=(), timeout_seconds=0.05)

    decision = await gate.request("C1", "111.0", "deploy please")
    assert decision.approved is False
    assert decision.by == "system"


async def test_row_and_approvers_exist_before_blocks_are_posted():
    """I4: create()/set_approvers() must happen before post_blocks -- a click
    racing in right after the post succeeds must be able to resolve
    immediately instead of getting 'gone'/'unauthorized'."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]

    async def post_blocks(channel, thread_ts, text, blocks):
        # By the time blocks are posted, the row + approvers must already be
        # queryable -- simulate a click landing right here.
        posted.append(blocks)
        action_id = _action_id_for(blocks, "approve")
        approval_id = action_id.split(":")[-1]
        assert await coordinator.get_pending(approval_id) is not None
        assert await coordinator.approvers_of(approval_id) == {"U11111"}
        result = await gate.handle_action(action_id, "U11111")
        assert result == "approved"
        return "999.0"

    gate = ApprovalGate(
        post_blocks,
        coordinator,
        approvers_provider=lambda: list(approvers),
        timeout_seconds=5,
    )

    decision = await gate.request("C1", "111.0", "deploy please")
    assert decision.approved is True
    assert decision.by == "U11111"


async def test_handle_action_garbage_id_returns_gone():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    gate = _make_gate(coordinator, posted)
    assert await gate.handle_action("not-a-jean-action", "U11111") == "gone"


async def test_handle_action_unknown_approval_id_returns_gone():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    gate = _make_gate(coordinator, posted)
    assert await gate.handle_action("jean_appr:approve:doesnotexist", "U11111") == "gone"


async def test_double_click_second_click_returns_gone():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5, posted_event=posted_event
    )

    async def click_twice():
        await posted_event.wait()
        action_id = _action_id_for(posted[0], "approve")
        first = await gate.handle_action(action_id, "U11111")
        second = await gate.handle_action(action_id, "U11111")
        assert first == "approved"
        assert second == "gone"

    await asyncio.gather(gate.request("C1", "111.0", "deploy please"), click_twice())
