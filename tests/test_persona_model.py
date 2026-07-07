from __future__ import annotations

import pytest

from jean.persona.model import EXTRACTION_PROMPT, ApproverEntry, Identity, Manager, SoulData


def test_approver_entry_accepts_valid_user_id():
    a = ApproverEntry(user_id="U12345", scope="deploys")
    assert a.user_id == "U12345"
    assert a.catchall is False


def test_approver_entry_accepts_workspace_shared_id():
    a = ApproverEntry(user_id="W98765", scope="", catchall=True)
    assert a.user_id == "W98765"
    assert a.catchall is True


@pytest.mark.parametrize("junk", ["", "not-a-user-id", "12345", "u12345", "Xabc"])
def test_approver_entry_rejects_junk_user_id(junk):
    with pytest.raises(ValueError, match="user_id"):
        ApproverEntry(user_id=junk)


def test_manager_rejects_junk_user_id():
    with pytest.raises(ValueError, match="user_id"):
        Manager(user_id="nope")


def test_manager_accepts_valid_user_id():
    m = Manager(user_id="U11111", name="Boss")
    assert m.user_id == "U11111"


def test_soul_data_defaults():
    soul = SoulData(identity=Identity(name="jean"), manager=Manager(user_id="U11111"))
    assert soul.allowed_channels == []
    assert soul.dm_allowed_users == []
    assert soul.blocked_users == []
    assert soul.approvers == []
    assert soul.mandate == ""
    assert soul.values == []
    assert soul.approval_timeout_seconds == 0


def test_soul_data_full_parse():
    soul = SoulData(
        identity=Identity(name="jean", role="engineering teammate"),
        manager=Manager(user_id="U11111", name="Boss"),
        allowed_channels=["C22222"],
        dm_allowed_users=["U33333"],
        blocked_users=["U44444"],
        approvers=[ApproverEntry(user_id="U55555", scope="deploy", catchall=False)],
        mandate="ship jean",
        values=["honesty", "care"],
        approval_timeout_seconds=900,
    )
    assert soul.allowed_channels == ["C22222"]
    assert soul.approvers[0].user_id == "U55555"
    assert soul.approval_timeout_seconds == 900


def test_soul_data_rejects_junk_channel_id():
    with pytest.raises(ValueError, match="channel"):
        SoulData(
            identity=Identity(name="jean"),
            manager=Manager(user_id="U11111"),
            allowed_channels=["not-a-channel"],
        )


def test_extraction_prompt_mentions_key_fields():
    for field in ("manager", "approvers", "allowed_channels", "mandate"):
        assert field in EXTRACTION_PROMPT
