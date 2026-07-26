from __future__ import annotations

import os

import pytest

from jean.agent_options import build_agent_options
from jean.config import ALLOWED_EFFORT_LEVELS, Settings


@pytest.fixture
def env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("JEAN_") or key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JEAN_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("JEAN_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("JEAN_HOME", str(tmp_path))
    return monkeypatch


def _opts(settings: Settings):
    return build_agent_options(
        persona_text="p",
        slack_server=object(),
        slack_tool_names=["mcp__jean_slack__reply"],
        mcp_servers={},
        plugins=[],
        settings=settings,
        resume=None,
        can_use_tool=lambda *a, **k: None,
    )


def test_effort_defaults_to_none_so_the_cli_picks_its_own(env):
    """Unset must stay unset -- jean passing a level the operator did not choose
    would silently override the CLI's own default for every deployment."""
    s = Settings()
    assert s.effort is None
    assert _opts(s).effort is None


def test_effort_reaches_the_agent_options(env):
    env.setenv("JEAN_EFFORT", "low")
    s = Settings()
    assert s.effort == "low"
    assert _opts(s).effort == "low"


@pytest.mark.parametrize("level", sorted(ALLOWED_EFFORT_LEVELS))
def test_every_documented_level_is_accepted(env, level):
    env.setenv("JEAN_EFFORT", level)
    assert Settings().effort == level


def test_unknown_level_fails_at_boot_not_mid_turn(env):
    """A typo must not survive to the first turn: the CLI would reject it there,
    after a human already waited on a reply."""
    env.setenv("JEAN_EFFORT", "lo")
    with pytest.raises(ValueError, match="JEAN_EFFORT"):
        Settings()


def test_effort_is_case_and_whitespace_tolerant(env):
    """A value pasted from a doc or a Vault UI often carries case or padding;
    that is not a reason to refuse to boot."""
    env.setenv("JEAN_EFFORT", "  HIGH ")
    assert Settings().effort == "high"


def test_empty_effort_is_treated_as_unset(env):
    """`JEAN_EFFORT=''` is how an operator clears the key in a Secret -- it must
    mean 'let the CLI decide', not 'boot-fail on an empty string'."""
    env.setenv("JEAN_EFFORT", "")
    assert Settings().effort is None
