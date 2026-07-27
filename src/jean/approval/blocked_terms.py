from __future__ import annotations

import re

# A hard rule, not an approval: some words must never leave this deployment, no
# matter how convincingly the agent argues for it. Which is exactly why it is here
# and not in the system prompt -- a prompt directive is a request the model may
# rationalise past, and jean's trust boundary says the LLM never makes a security
# decision. This check is pure, deterministic, and runs on the way OUT.
#
# Scope is deliberately narrow: it stops jean from PRODUCING a blocked term --
# posting it to Slack, writing it into a file, putting it in a commit message.
# It cannot stop a term that is already in a file jean did not write from being
# committed by a later `git add -A`, and it does not pretend to: see
# _PUBLISHING_BASH for what a shell command is checked against.


def find_blocked(text: str, terms: frozenset[str]) -> str | None:
    """The first blocked term appearing in `text`, or None if it is clean.

    Case-insensitive substring match, on purpose: `Okadoc`, `OKADOC` and
    `okadoc-deck` must all trip, and a word-boundary match would miss the last
    one. Substring matching over-blocks rather than under-blocks, which is the
    right direction for a rule that exists to prevent a leak.
    """
    if not terms or not text:
        return None
    haystack = text.lower()
    # Sorted for determinism: two terms may both be present, and the same input
    # must always name the same one, or the refusal message would flap.
    for term in sorted(terms):
        if term and term in haystack:
            return term
    return None


def refusal(term: str) -> str:
    """What the agent is told when it trips the rule.

    Names the term so the agent can actually fix its draft -- a refusal that says
    only "something is wrong" produces a retry loop. It also says not to quote the
    term back, because the obvious next move ("I can't say X") would trip the rule
    again and read as jean being broken rather than as a rule doing its job.
    """
    return (
        f"Blocked: this text contains {term!r}, which this deployment does not allow in "
        f"anything jean sends or writes. Rewrite it without that term -- a generic "
        f"description works -- and do not quote the term back, including to explain "
        f"this refusal. This is enforced in code and cannot be approved or overridden."
    )


# Fields whose VALUE is content jean is about to author. Checked for Write/Edit and
# friends; the file path itself is not checked, because a path is not published and
# reading a badly-named file is legitimate work.
CONTENT_FIELDS = ("content", "new_string", "new_source", "prompt")

# Shell commands that either publish text or author a file. A blocked term is only
# fatal in one of these -- `grep -ri <term> .` is how you AUDIT for the term, and
# denying that would make the rule impossible to verify.
_PUBLISHING_BASH = re.compile(
    r"""
      \bgit\s+(?:commit|tag)\b        # message goes into history
    | \bgit\s+push\b
    | \bgh\s+(?:pr|issue|release|gist)\b   # anything gh publishes
    | \bglab\s+
    | >>? | \btee\b | <<-?\s*['\"]?\w*EOF  # redirection / heredoc authors a file
    | \bcurl\b .* (?:-d\b|--data) | \bwget\b .* --post
    """,
    re.IGNORECASE | re.VERBOSE,
)


def bash_publishes(command: str) -> bool:
    """Would this shell command put text somewhere it outlives the turn?"""
    return bool(_PUBLISHING_BASH.search(command))
