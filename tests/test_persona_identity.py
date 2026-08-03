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


def test_baseline_requires_creating_artifacts_somewhere_the_reader_can_reach():
    """Reported in production: the agent created a Google Doc, posted the URL, and the
    requester got "you need access" -- the doc was owned by the agent's own identity and
    nobody else could open it.

    The rule is create-it-shared, NOT grant-access-after: jean never tells the agent who
    it is talking to (`author_id` is used for engagement in gateway/app.py and dropped
    before dispatch), and there is no Slack-id-to-email lookup on ChatSurface, so an
    instruction to share with the requester names someone the agent cannot identify.
    Creating in a pre-configured shared location needs no identity at all.

    The fallback is half the rule: an agent that cannot place it somewhere readable must
    say so and deliver the content anyway, not post a link it cannot vouch for."""
    # Flattened: the template is hard-wrapped, so a phrase the reader sees as one
    # sentence may straddle a newline. Asserting on the raw text would pin the wrap
    # rather than the wording, and rewrapping a paragraph would fail the test.
    composed = " ".join(compose_system_prompt("persona").split())
    assert "shared location" in composed
    assert "not in a space only you can reach" in composed
    assert "could not make it readable" in composed
    assert "mcp__jean_slack__upload" in composed


def test_compose_system_prompt_names_the_agent():
    """The persona's name -- not the project's -- is who the agent is told it is."""
    composed = compose_system_prompt("Name: Anya", name="Anya")
    assert "You are Anya," in composed
    assert "You are jean," not in composed


def test_compose_system_prompt_defaults_to_jean():
    assert "You are jean," in compose_system_prompt("persona")


def test_baseline_tells_the_agent_it_can_read_channel_history():
    composed = compose_system_prompt("persona text")
    assert "mcp__jean_slack__read_channel" in composed
    assert "mcp__jean_slack__read_thread" in composed
