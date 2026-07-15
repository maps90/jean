from __future__ import annotations

from dataclasses import dataclass, field

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from jean.agent_options import build_can_use_tool
from jean.ports import ApprovalDecision


class _RecordingGate:
    """Fake gate: records whether it was asked, returns a fixed decision."""

    def __init__(self, decision: ApprovalDecision | None) -> None:
        self._decision = decision
        self.asked = False
        self.channel: str | None = None
        self.thread_ts: str | None = None
        self.summary: str | None = None

    async def request(self, channel: str, thread_ts: str, summary: str) -> ApprovalDecision:
        self.asked = True
        self.channel = channel
        self.thread_ts = thread_ts
        self.summary = summary
        assert self._decision is not None, "gate asked when it should not have been"
        return self._decision


async def test_safe_tool_runs_without_asking():
    gate = _RecordingGate(None)
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "pytest -q"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert gate.asked is False


async def test_denied_class_tool_is_refused_without_asking():
    gate = _RecordingGate(None)
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("mcp__plugin_x__authenticate", {}, None)
    assert isinstance(result, PermissionResultDeny)
    assert gate.asked is False


async def test_risky_tool_approved_once_runs():
    gate = _RecordingGate(ApprovalDecision(True, "U1", "once"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is None
    assert gate.asked is True


async def test_risky_tool_always_allow_adds_a_session_rule():
    gate = _RecordingGate(ApprovalDecision(True, "U1", "always"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions is not None
    update = result.updated_permissions[0]
    assert update.type == "addRules"
    assert update.behavior == "allow"
    assert update.destination == "session"
    assert update.rules[0].tool_name == "Bash"


async def test_risky_tool_denied_is_refused():
    gate = _RecordingGate(ApprovalDecision(False, "U1", "once"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    result = await hook("Bash", {"command": "rm -rf /data"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is False


async def test_risky_call_forwards_the_bound_channel_and_thread():
    gate = _RecordingGate(ApprovalDecision(True, "U1", "once"))
    hook = build_can_use_tool(gate, channel="C_BOUND", thread_ts="1111.2222")
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert gate.channel == "C_BOUND"
    assert gate.thread_ts == "1111.2222"
    assert gate.summary is not None and "kubectl delete pod api-0" in gate.summary


@dataclass
class _FakeContext:
    """Stands in for the SDK's ToolPermissionContext -- only `suggestions`
    matters to build_can_use_tool."""

    suggestions: list[PermissionUpdate] = field(default_factory=list)


async def test_always_allow_honors_the_sdk_suggested_narrow_pattern():
    """One 'Always allow' on `kubectl delete` must not silence ALL future Bash
    for the session -- it should use the SDK's suggested narrow rule
    (e.g. `Bash(kubectl delete:*)`) when the CLI offers one via context."""
    gate = _RecordingGate(ApprovalDecision(True, "U1", "always"))
    hook = build_can_use_tool(gate, channel="C1", thread_ts="1.0")
    suggestions = [
        PermissionUpdate(
            type="addRules",
            rules=[PermissionRuleValue(tool_name="Bash", rule_content="kubectl delete:*")],
            behavior="allow",
            destination="session",
        )
    ]
    context = _FakeContext(suggestions=suggestions)
    result = await hook("Bash", {"command": "kubectl delete pod api-0"}, context)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_permissions == suggestions
