from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_regex_fallback_on_empty_persona_has_no_manager():
    soul = regex_fallback("")
    assert soul.manager is None
    assert soul.approvers == []


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
