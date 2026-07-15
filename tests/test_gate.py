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


def _make_gate(
    coordinator,
    posted,
    *,
    approvers=(),
    timeout_seconds=1.0,
    posted_event=None,
    manager=None,
    env_approvers=(),
    updated=None,
    update_blocks=None,
):
    async def post_blocks(channel, thread_ts, text, blocks):
        posted.append(blocks)
        if posted_event is not None:
            posted_event.set()
        return "999.0"

    async def _record_update(channel, ts, text, blocks):
        if updated is not None:
            updated.append((channel, ts, text, blocks))

    return ApprovalGate(
        post_blocks,
        coordinator,
        update_blocks=update_blocks or _record_update,
        approvers_provider=lambda: list(approvers),
        manager_provider=lambda: manager,
        timeout_seconds=timeout_seconds,
        env_approvers=env_approvers,
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
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(coordinator, posted, approvers=approvers, timeout_seconds=0.05)

    decision = await gate.request("C1", "111.0", "deploy please")
    assert decision.approved is False
    assert decision.by == "system"


async def test_no_approver_fails_closed_without_posting_buttons():
    """With nobody authorized, every click on an Approve button returns
    'unauthorized' -- including the manager's. Posting buttons no one can press
    and then hanging for the full approval_ttl is the worst outcome: refuse
    immediately and say why."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    gate = _make_gate(coordinator, posted, approvers=(), timeout_seconds=30)

    decision = await asyncio.wait_for(gate.request("C1", "111.0", "deploy please"), timeout=1)

    assert decision.approved is False
    assert decision.by == "system"
    assert _action_ids(posted[0]) == []  # no dead Approve/Deny buttons
    text = " ".join(
        el["text"] for b in posted[0] for el in [b["text"]] if b.get("type") == "section"
    )
    assert "no approver" in text.lower()


async def test_manager_is_the_last_resort_approver():
    """An off-scope summary with no catch-all still resolves to the manager."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="deploy")]
    gate = _make_gate(
        coordinator,
        posted,
        approvers=approvers,
        timeout_seconds=5,
        posted_event=posted_event,
        manager="U12345",
    )

    async def click_approve():
        await posted_event.wait()
        action_id = _action_id_for(posted[0], "approve")
        assert await gate.handle_action(action_id, "U12345") == "approved"

    decision, _ = await asyncio.gather(
        gate.request("C1", "111.0", "upload a file"), click_approve()
    )
    assert decision.approved is True
    assert decision.by == "U12345"


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

    async def update_blocks(channel, ts, text, blocks):
        return None

    gate = ApprovalGate(
        post_blocks,
        coordinator,
        update_blocks=update_blocks,
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


async def test_approved_message_is_rewritten_without_buttons():
    """The clicker's only feedback is the message itself: once decided, the
    buttons must be gone (nothing left to click) and the decision visible."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    updated: list[tuple] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator,
        posted,
        approvers=approvers,
        timeout_seconds=5,
        posted_event=posted_event,
        updated=updated,
    )

    async def click_approve():
        await posted_event.wait()
        await gate.handle_action(_action_id_for(posted[0], "approve"), "U11111")

    await asyncio.gather(gate.request("C1", "111.0", "deploy please"), click_approve())

    assert len(updated) == 1
    channel, ts, text, blocks = updated[0]
    assert (channel, ts) == ("C1", "999.0")
    assert _action_ids(blocks) == []
    assert not any(b["type"] == "actions" for b in blocks)
    rendered = str(blocks)
    assert "Approved" in rendered
    assert "<@U11111>" in rendered
    assert "Approved" in text


async def test_denied_message_is_rewritten_without_buttons():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    updated: list[tuple] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator,
        posted,
        approvers=approvers,
        timeout_seconds=5,
        posted_event=posted_event,
        updated=updated,
    )

    async def click_deny():
        await posted_event.wait()
        await gate.handle_action(_action_id_for(posted[0], "deny"), "U11111")

    await asyncio.gather(gate.request("C1", "111.0", "deploy please"), click_deny())

    assert len(updated) == 1
    blocks = updated[0][3]
    assert _action_ids(blocks) == []
    rendered = str(blocks)
    assert "Denied" in rendered
    assert "<@U11111>" in rendered


async def test_timed_out_message_is_rewritten_without_buttons():
    """A request nobody answers is system-denied -- its buttons must not stay
    live and clickable forever."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    updated: list[tuple] = []
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=0.05, updated=updated
    )

    decision = await gate.request("C1", "111.0", "deploy please")

    assert decision.approved is False
    assert len(updated) == 1
    blocks = updated[0][3]
    assert _action_ids(blocks) == []
    assert "expired" in str(blocks).lower()
    # "system" is a sentinel, not a Slack id -- never @-mention it.
    assert "<@system>" not in str(blocks)


async def test_failing_message_update_does_not_lose_the_decision():
    """The decision is authoritative (it lives in the store); a Slack hiccup
    while rewriting the message must not turn an approval into an error."""
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U11111", scope="", catchall=True)]

    async def boom(channel, ts, text, blocks):
        raise RuntimeError("slack is having a day")

    gate = _make_gate(
        coordinator,
        posted,
        approvers=approvers,
        timeout_seconds=5,
        posted_event=posted_event,
        update_blocks=boom,
    )

    async def click_approve():
        await posted_event.wait()
        await gate.handle_action(_action_id_for(posted[0], "approve"), "U11111")

    decision, _ = await asyncio.gather(
        gate.request("C1", "111.0", "deploy please"), click_approve()
    )
    assert decision.approved is True
    assert decision.by == "U11111"


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


async def test_blocks_include_an_always_allow_button():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(coordinator, posted, approvers=approvers, timeout_seconds=0.05)

    await gate.request("C1", "111.0", "kubectl delete pod")

    ids = _action_ids(posted[0])
    assert any(a.startswith("jean_appr:always:") for a in ids)


async def test_always_click_resolves_with_always_scope():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5.0, posted_event=posted_event
    )

    waiter = asyncio.create_task(gate.request("C1", "111.0", "kubectl delete pod"))
    await posted_event.wait()
    always_id = _action_id_for(posted[0], "always")
    result = await gate.handle_action(always_id, "U1")
    decision = await waiter

    assert result == "approved"
    assert decision.approved is True
    assert decision.scope == "always"


async def test_always_click_by_a_non_approver_is_unauthorized():
    coordinator = MemoryStore()
    posted: list[list[dict]] = []
    posted_event = asyncio.Event()
    approvers = [ApproverEntry(user_id="U1", scope="", catchall=True)]
    gate = _make_gate(
        coordinator, posted, approvers=approvers, timeout_seconds=5.0, posted_event=posted_event
    )

    waiter = asyncio.create_task(gate.request("C1", "111.0", "kubectl delete pod"))
    await posted_event.wait()
    always_id = _action_id_for(posted[0], "always")
    result = await gate.handle_action(always_id, "INTRUDER")

    assert result == "unauthorized"
    # Still pending -- resolve it so the waiter doesn't hang the test.
    approve_id = _action_id_for(posted[0], "approve")
    await gate.handle_action(approve_id, "U1")
    await waiter
