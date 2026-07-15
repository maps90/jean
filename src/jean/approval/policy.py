from __future__ import annotations

import json
from typing import Any

from jean.ports import ApprovalDecision

# Slack rejects a section block whose text exceeds 3000 chars, so an approval
# carrying a whole file body or a base64 blob would fail to post -- and a tool
# call whose approval never posts hangs until it times out. Clip the argument,
# not the request: the human is deciding whether to let jean do this at all,
# and the first lines say what it is.
_MAX_ARG = 1200

_FILE_VERBS = {
    "Write": "Write",
    "Edit": "Edit",
    "MultiEdit": "Edit",
    "NotebookEdit": "Edit notebook",
}


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_ARG:
        return text
    return f"{text[:_MAX_ARG]}\n… (+{len(text) - _MAX_ARG} more chars)"


def summarize(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Render a tool call as the one thing a human is being asked to approve.

    This is what the approver reads before clicking, so it must describe the
    *actual* call -- never a paraphrase the model supplied. The model's own
    words (Bash's `description`) are shown as context, but the command itself
    is always printed verbatim underneath it.
    """
    if tool_name == "ExitPlanMode":
        # Under the default plan mode this is the ONE thing a human approves: the
        # agent's own plan for the whole task, not a single rendered tool call.
        # The key is confirmed against the CLI at runtime (agent_options logs the
        # raw input on first sight); fall back to the tool args so a missing or
        # renamed key never posts an empty, unreviewable approval.
        plan = str(tool_input.get("plan") or "").strip()
        if plan:
            return _clip(plan)
        args = json.dumps(tool_input, indent=2, default=str, ensure_ascii=False)
        return f"Approve this plan\n```{_clip(args)}```"

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        description = str(tool_input.get("description", "")).strip()
        headline = f"Run a shell command — {description}" if description else "Run a shell command"
        return f"{headline}\n```{_clip(command)}```"

    if tool_name in _FILE_VERBS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or "(unknown path)"
        return f"{_FILE_VERBS[tool_name]} `{path}`"

    args = json.dumps(tool_input, indent=2, default=str, ensure_ascii=False)
    return f"Use `{tool_name}`\n```{_clip(args)}```"


def deny_reason(decision: ApprovalDecision) -> str:
    """What the agent is told when a tool call is not approved.

    An expired request comes back as by="system" (see the coordinator's wait):
    reporting that as a human denial would have the agent tell the thread it
    was denied by someone who never saw it.
    """
    if decision.by == "system":
        return (
            "No approver responded, so this timed out and was not run. "
            "Say so in the thread instead of retrying."
        )
    return f"Denied by <@{decision.by}>. Do not retry this; ask in the thread instead."
