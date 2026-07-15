from __future__ import annotations

from jean.approval.policy import deny_reason, summarize
from jean.ports import ApprovalDecision


def test_bash_summary_shows_the_command_and_its_description():
    text = summarize("Bash", {"command": "kubectl delete pod api-7f9", "description": "drop a pod"})
    assert "drop a pod" in text
    assert "kubectl delete pod api-7f9" in text


def test_bash_summary_survives_a_missing_description():
    text = summarize("Bash", {"command": "rm -rf /tmp/x"})
    assert "rm -rf /tmp/x" in text


def test_file_writes_name_the_path():
    assert "/etc/app.conf" in summarize("Write", {"file_path": "/etc/app.conf", "content": "x"})
    assert "/srv/main.py" in summarize("Edit", {"file_path": "/srv/main.py"})


def test_unknown_tool_falls_back_to_its_name_and_arguments():
    text = summarize("mcp__kubernetes__apply", {"manifest": "kind: Deployment"})
    assert "mcp__kubernetes__apply" in text
    assert "kind: Deployment" in text


def test_a_huge_argument_is_clipped_so_slack_accepts_the_block():
    # Slack rejects a section block over 3000 chars, and an approval that fails
    # to post leaves the tool call hanging until it times out. A heredoc or a
    # base64 blob in a Bash command would otherwise blow straight past it.
    text = summarize("Bash", {"command": f"echo {'A' * 10_000}"})
    assert len(text) < 3000
    assert "…" in text


def test_a_file_write_never_dumps_the_file_body_into_the_approval():
    text = summarize("Write", {"file_path": "/tmp/big", "content": "A" * 10_000})
    assert len(text) < 3000
    assert "AAAA" not in text


def test_a_plan_approval_shows_the_plan_text():
    # ExitPlanMode is the one approval jean asks for under the default plan mode:
    # the human reads the plan itself, not a rendered tool call, before clicking.
    text = summarize("ExitPlanMode", {"plan": "1. rollout restart api\n2. tail the logs"})
    assert "rollout restart api" in text
    assert "tail the logs" in text


def test_a_huge_plan_is_clipped_so_slack_accepts_the_block():
    text = summarize("ExitPlanMode", {"plan": "step\n" * 5_000})
    assert len(text) < 3000
    assert "…" in text


def test_a_plan_with_no_text_still_summarizes():
    # The exact input key is confirmed at runtime; a missing/renamed key must not
    # produce an empty approval the human cannot reason about.
    text = summarize("ExitPlanMode", {})
    assert text.strip() != ""


def test_denied_by_a_human_names_the_approver():
    reason = deny_reason(ApprovalDecision(approved=False, by="U123"))
    assert "U123" in reason


def test_a_timeout_is_reported_as_a_timeout_not_as_a_human_denial():
    # The coordinator resolves an expired request as by="system"; telling the
    # model "a human denied this" would be a lie it then repeats in Slack.
    reason = deny_reason(ApprovalDecision(approved=False, by="system"))
    assert "system" not in reason
    assert "no approver" in reason.lower() or "timed out" in reason.lower()
