from __future__ import annotations

import json
import logging

from jean.persona.extract import _soul_from_json, load_soul_data, regex_fallback

# The commented-out examples ship in the documented soul.md template, so every
# real persona file has them.
SOUL_WITH_COMMENTED_EXAMPLES = """# Persona

## Identity
- Name: Anya

## Reporting
- Manager: <@UCTP0TWQ0>

<!-- Optional. Add as:
     "- Backup manager: <@U0123ABCD>" — use a real Slack member id. -->

## Approvers

- <@UCTP0TWQ0> is the catch-all approver — approves deploys, releases, and infra
  changes, and anything else.
  <!-- Add more approvers with their scope, e.g.
       "- <@U0123ABCD> approves deploys and infra (scope: deploy, release, infra)" -->
"""

VALID_PAYLOAD = {
    "identity": {"name": "Anya", "role": "SRE"},
    "manager": {"user_id": "UCTP0TWQ0", "name": "Manager"},
    "approvers": [{"user_id": "UCTP0TWQ0", "scope": "infra", "catchall": True}],
}


def test_fenced_json_parses():
    """claude-haiku-4-5 wraps its answer in a ```json fence, so a bare
    json.loads failed on every boot and silently demoted every instance to the
    regex fallback."""
    raw = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    soul = _soul_from_json(raw)
    assert soul.identity.name == "Anya"
    assert soul.manager is not None and soul.manager.user_id == "UCTP0TWQ0"


def test_unfenced_json_still_parses():
    soul = _soul_from_json(json.dumps(VALID_PAYLOAD))
    assert soul.identity.name == "Anya"


def test_fence_without_a_language_tag_parses():
    soul = _soul_from_json("```\n" + json.dumps(VALID_PAYLOAD) + "\n```")
    assert soul.identity.name == "Anya"


def test_json_with_prose_around_it_parses():
    raw = "Here is the extraction:\n\n" + json.dumps(VALID_PAYLOAD) + "\n\nLet me know."
    soul = _soul_from_json(raw)
    assert soul.identity.name == "Anya"


def test_fallback_ignores_ids_inside_html_comments():
    """A placeholder in a template comment must never become an approver: it is
    an authorization grant nobody wrote on purpose."""
    soul = regex_fallback(SOUL_WITH_COMMENTED_EXAMPLES)
    ids = {a.user_id for a in soul.approvers}
    assert "U0123ABCD" not in ids, "template placeholder was granted approval authority"
    assert ids == {"UCTP0TWQ0"}


def test_fallback_still_finds_the_real_approver_and_manager():
    soul = regex_fallback(SOUL_WITH_COMMENTED_EXAMPLES)
    assert soul.manager is not None and soul.manager.user_id == "UCTP0TWQ0"
    assert soul.approvers[0].catchall is True


def test_fallback_ignores_a_commented_out_manager():
    soul = regex_fallback(
        "## Reporting\n<!-- - Manager: <@UDEADBEEF> -->\n- Manager: <@UCTP0TWQ0>\n"
    )
    assert soul.manager is not None and soul.manager.user_id == "UCTP0TWQ0"


async def test_extraction_failure_logs_why(tmp_path, caplog, monkeypatch):
    """`except Exception` logged a bare 'soul extraction failed', so the reason
    (a JSONDecodeError from an unstripped fence) was invisible in production."""
    identity = tmp_path / "soul.md"
    identity.write_text(SOUL_WITH_COMMENTED_EXAMPLES)

    class _Settings:
        identity_path = identity
        cache_dir = tmp_path / "cache"
        soul_parse_model = "x"
        anthropic_api_key = None
        claude_code_oauth_token = None

    async def malformed_json(system: str, prompt: str) -> str:
        # A complete `{...}` span that is not valid JSON (trailing comma).
        return '```json\n{"identity": {"name": "Anya"}, }\n```'

    with caplog.at_level(logging.WARNING, logger="jean.persona"):
        soul = await load_soul_data(_Settings(), extractor=malformed_json)

    assert soul.manager is not None  # fell back, still usable
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "JSONDecodeError" in joined, joined
    # The whole point: the log names the cause, not just that there was one.
    assert "soul extraction failed;" not in joined, "reason-free message came back"


async def test_truncated_reply_says_so(tmp_path, caplog):
    """A max_tokens cutoff leaves an unclosed object -- a distinct cause that
    should read as its own sentence in the log, not as a JSON error."""
    identity = tmp_path / "soul.md"
    identity.write_text(SOUL_WITH_COMMENTED_EXAMPLES)

    class _Settings:
        identity_path = identity
        cache_dir = tmp_path / "cache"
        soul_parse_model = "x"
        anthropic_api_key = None
        claude_code_oauth_token = None

    async def truncated(system: str, prompt: str) -> str:
        return '```json\n{"identity": {"name": "Anya"\n'

    with caplog.at_level(logging.WARNING, logger="jean.persona"):
        await load_soul_data(_Settings(), extractor=truncated)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "no JSON object" in joined, joined
