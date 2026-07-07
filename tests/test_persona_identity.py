from __future__ import annotations

from pathlib import Path

from jean.persona.identity import BASELINE_PROMPT, compose_system_prompt, load_identity


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
    for line in BASELINE_PROMPT.splitlines():
        if line.strip():
            assert line in composed
            break
