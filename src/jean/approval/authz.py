from __future__ import annotations

import re
from collections.abc import Iterable

from jean.persona.model import ApproverEntry

_SPLIT_RE = re.compile(r"[,\s]+")


def _keywords(scope: str) -> list[str]:
    return [kw for kw in _SPLIT_RE.split(scope.lower().strip()) if kw]


def select_approvers(
    summary: str,
    approvers: list[ApproverEntry],
    *,
    env_fallback: Iterable[str] = (),
    manager: str | None = None,
) -> set[str]:
    """Pick who must approve `summary`. Priority: keyword match against each
    non-catchall approver's `scope` > any catchall approver > `env_fallback`
    > `manager` > empty set. Pure and code-side by design -- this is the trust
    boundary's authorization logic, never something the model decides.

    The `manager` rung exists because an empty result is not a safe default but
    a dead end: the gate authorizes clicks against this set, so nobody -- not
    even the manager -- can resolve an approval it did not name. Falling back to
    the person jean already answers to keeps a scoped-only IDENTITY.md (no
    catch-all) from silently making every off-scope action unapprovable.
    """
    summary_lower = summary.lower()

    keyword_matches = {
        a.user_id
        for a in approvers
        if not a.catchall and any(kw in summary_lower for kw in _keywords(a.scope))
    }
    if keyword_matches:
        return keyword_matches

    catchall = {a.user_id for a in approvers if a.catchall}
    if catchall:
        return catchall

    env_fallback_set = set(env_fallback)
    if env_fallback_set:
        return env_fallback_set

    return {manager} if manager else set()
