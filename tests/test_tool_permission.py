from __future__ import annotations

from dataclasses import dataclass

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from jean.agent_options import build_can_use_tool
from jean.ports import ApprovalDecision

EXIT_PLAN = "ExitPlanMode"


@dataclass
class _FakeGate:
    """Stands in for ApprovalGate: records what it was asked to approve and
    answers with a canned decision."""

    decision: ApprovalDecision
    calls: list[tuple[str, str, str]]

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.calls.append((channel, thread_ts, summary))
        return self.decision


def _gate(approved: bool, by: str = "U123") -> _FakeGate:
    return _FakeGate(ApprovalDecision(approved=approved, by=by), [])


async def test_an_approved_tool_call_runs():
    gate = _gate(True)
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    result = await can_use("Bash", {"command": "kubectl rollout restart deploy/api"}, None)

    assert isinstance(result, PermissionResultAllow)


async def test_a_denied_tool_call_is_blocked_and_the_model_is_told_who_denied_it():
    gate = _gate(False, by="U999")
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    result = await can_use("Bash", {"command": "rm -rf /"}, None)

    assert isinstance(result, PermissionResultDeny)
    assert "U999" in result.message
    # The turn continues so the agent can say it was denied, rather than the
    # whole thread dying on an interrupt.
    assert result.interrupt is False


async def test_the_approval_describes_the_actual_tool_call():
    gate = _gate(True)
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    await can_use("Bash", {"command": "helm upgrade api ./chart"}, None)

    _channel, _thread, summary = gate.calls[0]
    assert "helm upgrade api ./chart" in summary


async def test_an_approved_plan_unlocks_the_turn_by_switching_off_prompting():
    """Under the default plan mode the agent presents its plan by calling
    ExitPlanMode. Approving it is the ONE approval: jean returns allow AND flips
    the session to bypassPermissions so the remaining planned steps run without
    another prompt. Re-arming plan mode for the next turn is the session's job."""
    gate = _gate(True)
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    result = await can_use(EXIT_PLAN, {"plan": "1. restart api\n2. verify"}, None)

    assert isinstance(result, PermissionResultAllow)
    updates = result.updated_permissions or []
    assert any(getattr(u, "type", None) == "setMode" for u in updates)
    mode = next(getattr(u, "mode", None) for u in updates if getattr(u, "type", None) == "setMode")
    assert mode == "bypassPermissions"


async def test_the_plan_the_human_approves_is_the_plan_the_agent_wrote():
    gate = _gate(True)
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    await can_use(EXIT_PLAN, {"plan": "delete the staging namespace"}, None)

    _channel, _thread, summary = gate.calls[0]
    assert "delete the staging namespace" in summary


async def test_a_denied_plan_does_not_unlock_and_keeps_the_turn_alive():
    gate = _gate(False, by="U999")
    can_use = build_can_use_tool(gate, channel="C1", thread_ts="111.0")

    result = await can_use(EXIT_PLAN, {"plan": "drop prod"}, None)

    assert isinstance(result, PermissionResultDeny)
    assert "U999" in result.message
    # interrupt=False: the agent stays in plan mode and can say it was denied,
    # rather than the thread dying; a later attempt re-plans.
    assert result.interrupt is False


async def test_the_request_goes_to_the_session_thread_not_to_shared_routing_state():
    """The channel/thread are bound per session at build time. Reading a
    process-wide routing slot here would misroute the approval whenever a
    second thread starts a turn while this one waits -- asking the wrong
    people, in the wrong thread, to approve a mutation."""
    gate = _gate(True)
    can_use = build_can_use_tool(gate, channel="C-ops", thread_ts="222.0")

    await can_use("Write", {"file_path": "/etc/x", "content": "y"}, None)

    assert gate.calls[0][0] == "C-ops"
    assert gate.calls[0][1] == "222.0"
