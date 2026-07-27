from __future__ import annotations

import pytest

from jean.approval.blocked_terms import bash_publishes, find_blocked, refusal
from jean.approval.risk import Risk, classify_risk
from jean.config import Settings

TERMS = frozenset({"acmecorp", "widgetco"})


def _risk(tool: str, **inp) -> Risk:
    return classify_risk(tool, inp, blocked_terms=TERMS)


# --- the matcher -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "acmecorp",
        "AcmeCorp",
        "ACMECORP",
        "the acmecorp deployment",
        "acmecorp-deck.pptx",  # substring, not a whole word
        "see https://grafana.acmecorp.net/x",
    ],
)
def test_the_term_is_found_however_it_is_written(text: str):
    assert find_blocked(text, TERMS) == "acmecorp"


def test_clean_text_passes():
    assert find_blocked("a perfectly ordinary sentence", TERMS) is None
    assert find_blocked("", TERMS) is None


def test_no_configured_terms_blocks_nothing():
    """The default. An unconfigured deployment behaves exactly as before."""
    assert find_blocked("acmecorp", frozenset()) is None


def test_the_named_term_is_stable_when_several_match():
    """Two terms present must always name the same one, or the refusal flaps
    between retries and the agent cannot tell whether it made progress."""
    both = "widgetco and acmecorp"
    assert find_blocked(both, TERMS) == find_blocked(both, TERMS) == "acmecorp"


def test_the_refusal_tells_the_agent_what_to_do():
    msg = refusal("acmecorp")
    assert "acmecorp" in msg  # it must be actionable
    assert "do not quote the term back" in msg  # or the explanation trips the rule
    assert "cannot be approved" in msg  # not a RISKY prompt -- a hard rule


# --- authoring a file ------------------------------------------------------


def test_writing_the_term_into_a_file_is_denied():
    assert _risk("Write", file_path="/w/README.md", content="built for acmecorp") is Risk.DENY
    assert _risk("Edit", file_path="/w/a.py", new_string="# acmecorp internal") is Risk.DENY


def test_writing_clean_content_is_unaffected():
    assert _risk("Write", file_path="/w/README.md", content="built for a customer") is Risk.SAFE


def test_a_path_naming_the_term_is_not_blocked():
    """Reading or editing a badly-named file is legitimate work -- it is the
    CONTENT jean authors that must not carry the term. Blocking the path would
    make cleaning such a file impossible."""
    assert _risk("Write", file_path="/w/acmecorp-notes.md", content="clean text") is Risk.SAFE


# --- publishing via the shell ---------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix the acmecorp integration"',
        'git tag -a v1 -m "acmecorp release"',
        'gh pr create --title "acmecorp" --body x',
        'gh issue comment 4 --body "acmecorp asked for this"',
        'echo "acmecorp" > notes.md',
        'echo "acmecorp" >> notes.md',
        'echo "acmecorp" | tee notes.md',
        "curl -d 'org=acmecorp' https://example.test/x",
    ],
)
def test_publishing_the_term_through_the_shell_is_denied(command: str):
    assert _risk("Bash", command=command) is Risk.DENY


@pytest.mark.parametrize(
    "command",
    [
        "grep -ri acmecorp .",
        "rg acmecorp src/",
        "git log --oneline -S acmecorp",
        "ls acmecorp-notes.md",
    ],
)
def test_searching_for_the_term_is_allowed(command: str):
    """This is how you AUDIT for the term. Denying it would make the rule
    impossible to verify, and searching publishes nothing."""
    assert _risk("Bash", command=command) is not Risk.DENY


def test_a_clean_commit_is_unaffected():
    assert _risk("Bash", command='git commit -m "fix the integration"') is not Risk.DENY


def test_bash_publishes_recognises_authoring_without_a_term():
    """The publish test is independent of the term, so it is worth pinning."""
    assert bash_publishes('git commit -m "x"')
    assert bash_publishes("cat <<EOF > f")
    assert not bash_publishes("grep -r x .")


# --- the rule outranks everything else -----------------------------------


def test_deny_beats_risky():
    """A blocked term in a command that is ALSO risky must deny, not prompt --
    otherwise an approver could click the leak through."""
    assert _risk("Bash", command="git push && echo acmecorp > /etc/motd") is Risk.DENY


def test_without_configured_terms_nothing_changes():
    """Same inputs, no terms: the classifier's existing verdicts stand."""
    assert classify_risk("Write", {"file_path": "/w/a", "content": "acmecorp"}) is Risk.SAFE
    assert classify_risk("Bash", {"command": 'git commit -m "acmecorp"'}) is not Risk.DENY


# --- config parsing --------------------------------------------------------


def _terms(raw: str) -> frozenset[str]:
    return Settings(slack_bot_token="x", slack_app_token="y", blocked_terms=raw).blocked_term_set


def test_the_setting_is_split_and_folded():
    assert _terms("AcmeCorp, WidgetCo") == {"acmecorp", "widgetco"}


def test_an_unset_setting_is_off():
    assert _terms("") == frozenset()


def test_a_stray_comma_cannot_produce_an_empty_term():
    """An empty term is a substring of every string, so it would deny everything --
    including jean's own replies. A trailing comma must not be able to do that."""
    assert _terms("acmecorp,,  ,") == {"acmecorp"}
    assert find_blocked("totally unrelated text", _terms("acmecorp, ,")) is None
