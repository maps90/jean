from __future__ import annotations

import re
from collections.abc import Callable

# Order matters: carve anything whose *content* must survive verbatim (code,
# links, bare urls, headings-once-rendered) out to sentinels first, run the
# text-level conversions (bullets/italic/bold/strike) on what's left, then
# restore the sentinels last. Within the text-level conversions, italic runs
# BEFORE bold: a single-star italic regex must never eat half of a `**bold**`
# pair, so the italic pattern explicitly excludes stars adjacent to another
# star (protecting `**...**`) and bold is only converted afterward.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((\S+?)\)")
_BARE_URL_RE = re.compile(r"(?<![<|])\bhttps?://[^\s<>]+")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)

_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

_SENTINEL_RE = re.compile(r"\x00(\d+)\x00")


def md_to_mrkdwn(md: str) -> str:
    """Convert common markdown to Slack mrkdwn."""
    stash: list[str] = []

    def carve(pattern: re.Pattern, text: str, transform: Callable[[re.Match], str]) -> str:
        def repl(m: re.Match) -> str:
            stash.append(transform(m))
            return f"\x00{len(stash) - 1}\x00"

        return pattern.sub(repl, text)

    text = md
    text = carve(_FENCE_RE, text, lambda m: m.group(0))
    text = carve(_INLINE_CODE_RE, text, lambda m: m.group(0))
    text = carve(_MD_LINK_RE, text, lambda m: f"<{m.group(2)}|{m.group(1)}>")
    text = carve(_BARE_URL_RE, text, lambda m: f"<{m.group(0)}>")
    text = carve(_HEADING_RE, text, lambda m: f"*{m.group(2)}*")

    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _ITALIC_STAR_RE.sub(r"_\1_", text)
    text = _BOLD_RE.sub(r"*\2*", text)
    text = _STRIKE_RE.sub(r"~\1~", text)

    return _SENTINEL_RE.sub(lambda m: stash[int(m.group(1))], text)


def chunk_text(text: str, max: int = 39000) -> list[str]:
    """Split text into pieces of at most `max` chars, preferring to break on
    the last newline within the limit. Never loses or reorders characters:
    `"".join(chunk_text(text, max=n)) == text` always holds."""
    if len(text) <= max:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max:
        split_at = remaining.rfind("\n", 0, max)
        split_at = split_at + 1 if split_at > 0 else max
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks
