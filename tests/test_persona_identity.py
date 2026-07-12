from __future__ import annotations

from pathlib import Path

from jean.persona.identity import BASELINE_TEMPLATE, compose_system_prompt, load_identity


def test_load_identity_missing_file_returns_empty(tmp_path: Path):
    assert load_identity(tmp_path / "nope" / "IDENTITY.md") == ""


def test_load_identity_reads_file(tmp_path: Path):
    p = tmp_path / "IDENTITY.md"
    p.write_text("I am jean, teammate to <@U11111>.")
    assert load_identity(p) == "I am jean, teammate to <@U11111>."


def test_compose_system_prompt_contains_persona_and_baseline():
    persona = "I am jean, teammate to <@U11111>."
    composed = compose_system_prompt(persona)
    assert persona in composed
    assert "mcp__jean_slack__reply" in composed
    assert "request_approval" in composed
    for line in BASELINE_TEMPLATE.format(name="jean").splitlines():
        if line.strip():
            assert line in composed
            break


def test_compose_system_prompt_names_the_agent():
    """The persona's name -- not the project's -- is who the agent is told it is."""
    composed = compose_system_prompt("Name: Anya", name="Anya")
    assert "You are Anya," in composed
    assert "You are jean," not in composed


def test_compose_system_prompt_defaults_to_jean():
    assert "You are jean," in compose_system_prompt("persona")
