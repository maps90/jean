from __future__ import annotations

import json
from pathlib import Path

import pytest

from jean.approval.authz import select_approvers
from jean.config import Settings
from jean.persona.extract import assert_ids_grounded, load_soul_data, regex_fallback
from jean.persona.model import ApproverEntry, Identity, Manager, SoulData

PERSONA = (
    "I am jean. My manager is <@U11111>. If you need to approve a deploy, "
    "ping approver <@U22222> (scope: deploy). Channels I live in: <#C33333>."
)


def _settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("JEAN_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    s = Settings.load()
    s.identity_path.write_text(PERSONA)
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_assert_ids_grounded_passes_when_all_ids_present():
    soul = SoulData(
        identity=Identity(name="jean"),
        manager=Manager(user_id="U11111"),
        approvers=[ApproverEntry(user_id="U22222", scope="deploy")],
        allowed_channels=["C33333"],
    )
    assert_ids_grounded(soul, PERSONA)  # does not raise


def test_assert_ids_grounded_rejects_invented_id():
    soul = SoulData(
        identity=Identity(name="jean"),
        manager=Manager(user_id="U99999"),  # not in PERSONA
    )
    with pytest.raises(ValueError, match="U99999"):
        assert_ids_grounded(soul, PERSONA)


def test_regex_fallback_finds_grounded_manager_and_approver():
    soul = regex_fallback(PERSONA)
    assert soul.manager is not None
    assert soul.manager.user_id == "U11111"
    assert any(a.user_id == "U22222" for a in soul.approvers)
    assert_ids_grounded(soul, PERSONA)  # everything the fallback found must be grounded


# The IDENTITY.md format README tells every operator to write, verbatim.
README_PERSONA = """\
# jean

I am jean, an AI teammate for the engineering team.

My manager is <@U0123ABCD>. I take direction from them and keep them
informed of anything important.

Approvers:
- <@U0456EFGH> approves deploys and infra changes (scope: deploy, release, infra)
- <@U0123ABCD> is the catch-all approver for anything else

I live in #eng-jean and can be DMed directly.
"""


def test_regex_fallback_reads_the_readme_catchall_approver():
    """The fallback must be able to express a catch-all approver. It could not:
    it never set catchall=True, so an approval whose summary missed every scope
    keyword resolved to nobody and could not be approved by anyone."""
    soul = regex_fallback(README_PERSONA)
    catchalls = {a.user_id for a in soul.approvers if a.catchall}
    assert catchalls == {"U0123ABCD"}


def test_regex_fallback_keeps_an_approver_who_is_also_the_manager():
    """The manager is usually the catch-all approver. The fallback used to skip
    any approver whose id matched the manager's, dropping exactly that person."""
    soul = regex_fallback(README_PERSONA)
    assert soul.manager is not None and soul.manager.user_id == "U0123ABCD"
    assert "U0123ABCD" in {a.user_id for a in soul.approvers}


def test_regex_fallback_keeps_scoped_approver_scoped():
    soul = regex_fallback(README_PERSONA)
    scoped = next(a for a in soul.approvers if a.user_id == "U0456EFGH")
    assert scoped.catchall is False
    assert "deploy" in scoped.scope and "infra" in scoped.scope


def test_readme_persona_always_yields_an_approver():
    """End to end over the documented format: an off-scope summary (the one that
    broke in production -- a doc upload matches no deploy/infra keyword) must
    still resolve to a human, not to an empty set."""
    soul = regex_fallback(README_PERSONA)
    chosen = select_approvers(
        'Upload a Markdown file "Grafana_Production_Datasources_HowTo.md" to this Slack thread',
        soul.approvers,
        manager=soul.manager.user_id if soul.manager else None,
    )
    assert chosen == {"U0123ABCD"}


def test_regex_fallback_prose_manager_does_not_become_an_approver():
    """Splitting on sentence ends, not just lines: the manager sentence and the
    approver sentence are separate, so the manager is not swept in by proximity."""
    soul = regex_fallback(PERSONA)
    assert {a.user_id for a in soul.approvers} == {"U22222"}


def test_regex_fallback_on_empty_persona_has_no_manager():
    soul = regex_fallback("")
    assert soul.manager is None
    assert soul.approvers == []


def test_regex_fallback_reads_the_persona_name():
    """A degraded extraction must not silently rename the agent back to jean."""
    soul = regex_fallback("# Persona\n\n## Identity\n- Name: Anya\n- Role: SRE\n")
    assert soul.identity.name == "Anya"


def test_regex_fallback_without_a_name_keeps_the_default():
    assert regex_fallback(PERSONA).identity.name == "jean"


async def test_load_soul_data_happy_path_extracts_and_caches(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    calls = []

    async def fake_extractor(system: str, prompt: str) -> str:
        calls.append((system, prompt))
        return json.dumps(
            {
                "identity": {"name": "jean", "role": "teammate"},
                "manager": {"user_id": "U11111", "name": "Boss"},
                "allowed_channels": ["C33333"],
                "dm_allowed_users": [],
                "blocked_users": [],
                "approvers": [{"user_id": "U22222", "scope": "deploy", "catchall": False}],
                "mandate": "help the team",
                "values": ["care"],
                "approval_timeout_seconds": 600,
            }
        )

    soul = await load_soul_data(settings, extractor=fake_extractor)
    assert soul.manager.user_id == "U11111"
    assert soul.approvers[0].user_id == "U22222"
    assert soul.mandate == "help the team"
    assert len(calls) == 1

    # second call hits the sha256 cache -- extractor must not be invoked again.
    soul2 = await load_soul_data(settings, extractor=fake_extractor)
    assert soul2.manager.user_id == "U11111"
    assert len(calls) == 1


async def test_load_soul_data_falls_back_on_bad_json(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)

    async def bad_extractor(system: str, prompt: str) -> str:
        return "not json at all"

    soul = await load_soul_data(settings, extractor=bad_extractor)
    assert soul.manager is not None
    assert soul.manager.user_id == "U11111"
    assert any(a.user_id == "U22222" for a in soul.approvers)


async def test_load_soul_data_falls_back_on_ungrounded_id(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)

    async def lying_extractor(system: str, prompt: str) -> str:
        return json.dumps(
            {
                "identity": {"name": "jean", "role": ""},
                "manager": {"user_id": "U99999", "name": "invented"},
                "allowed_channels": [],
                "dm_allowed_users": [],
                "blocked_users": [],
                "approvers": [],
                "mandate": "",
                "values": [],
                "approval_timeout_seconds": 0,
            }
        )

    soul = await load_soul_data(settings, extractor=lying_extractor)
    # the invented manager must be rejected; fallback finds the real grounded one.
    assert soul.manager.user_id == "U11111"


async def test_load_soul_data_missing_persona_returns_empty_soul(tmp_path, monkeypatch):
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("JEAN_HOME", str(tmp_path / "nonexistent"))
    settings = Settings.load()

    async def unused_extractor(system: str, prompt: str) -> str:
        raise AssertionError("extractor should not be called with no persona text")

    soul = await load_soul_data(settings, extractor=unused_extractor)
    assert soul.manager is None
    assert soul.approvers == []
