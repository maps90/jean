from __future__ import annotations

from jean.approval.authz import select_approvers
from jean.persona.model import ApproverEntry


def test_keyword_match_wins_over_catchall():
    approvers = [
        ApproverEntry(user_id="U11111", scope="deploy, release"),
        ApproverEntry(user_id="U22222", scope="", catchall=True),
    ]
    assert select_approvers("please deploy the new build", approvers) == {"U11111"}


def test_falls_back_to_catchall_when_no_keyword_matches():
    approvers = [
        ApproverEntry(user_id="U11111", scope="deploy, release"),
        ApproverEntry(user_id="U22222", scope="", catchall=True),
    ]
    assert select_approvers("please delete the database", approvers) == {"U22222"}


def test_falls_back_to_env_when_no_approvers_match():
    approvers = [ApproverEntry(user_id="U11111", scope="deploy")]
    result = select_approvers("please delete the database", approvers, env_fallback=("U99999",))
    assert result == {"U99999"}


def test_empty_when_nothing_matches_and_no_fallback():
    approvers = [ApproverEntry(user_id="U11111", scope="deploy")]
    assert select_approvers("please delete the database", approvers) == set()


def test_empty_approvers_list_uses_env_fallback():
    assert select_approvers("anything", [], env_fallback=("U99999",)) == {"U99999"}


def test_falls_back_to_manager_when_nothing_else_matches():
    """Last resort before nobody: the manager. An approval with no approver is
    unresolvable -- every click is 'unauthorized' -- so it must not happen while
    IDENTITY.md names someone jean answers to."""
    approvers = [ApproverEntry(user_id="U11111", scope="deploy")]
    assert select_approvers("upload a file", approvers, manager="U12345") == {"U12345"}


def test_manager_used_when_no_approvers_at_all():
    assert select_approvers("anything", [], manager="U12345") == {"U12345"}


def test_env_fallback_wins_over_manager():
    result = select_approvers("anything", [], env_fallback=("U99999",), manager="U12345")
    assert result == {"U99999"}


def test_catchall_wins_over_manager():
    approvers = [ApproverEntry(user_id="U22222", scope="", catchall=True)]
    assert select_approvers("anything", approvers, manager="U12345") == {"U22222"}


def test_empty_when_nothing_matches_and_no_manager():
    approvers = [ApproverEntry(user_id="U11111", scope="deploy")]
    assert select_approvers("upload a file", approvers, manager=None) == set()


def test_multiple_keyword_approvers_all_returned():
    approvers = [
        ApproverEntry(user_id="U11111", scope="deploy"),
        ApproverEntry(user_id="U22222", scope="deploy, release"),
        ApproverEntry(user_id="U33333", scope="delete"),
    ]
    assert select_approvers("please deploy now", approvers) == {"U11111", "U22222"}
