from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from jean.persona.model import EXTRACTION_PROMPT, ApproverEntry, Identity, Manager, SoulData

if TYPE_CHECKING:
    from jean.config import Settings

logger = logging.getLogger("jean.persona")

# extractor(system, prompt) -> raw model text (expected to be a JSON object)
Extractor = Callable[[str, str], Awaitable[str]]

_SYSTEM_PROMPT = (
    "You are a precise JSON extraction engine. Reply with a single JSON object "
    "and nothing else -- no prose, no markdown code fences."
)

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
_CHANNEL_RE = re.compile(r"<#([CGD][A-Z0-9]+)(?:\|[^>]*)?>")
_NAME_RE = re.compile(r"^\s*[-*]?\s*Name:\s*(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)

_APPROVER_HEADING_RE = re.compile(r"^#*\s*approvers?\s*:?\s*$", re.IGNORECASE)
_APPROVE_WORD_RE = re.compile(r"approv", re.IGNORECASE)
_SCOPE_RE = re.compile(r"scope:\s*([\w, -]+)", re.IGNORECASE)
_CATCHALL_RE = re.compile(
    r"catch[\s-]?all|any(?:thing)?\s+else|everything\s+else|all\s+other", re.IGNORECASE
)


def _approver_segments(persona: str) -> list[str]:
    """The pieces of `persona` that describe an approver: every bullet under an
    `Approvers:` heading, plus every *sentence* that talks about approving and
    names someone.

    Sentences, not lines, because a persona written as prose puts the manager and
    the approver in one paragraph ("My manager is <@U1>. Approver: <@U2>.") -- a
    line-wide match would sweep the manager in as an approver of everything.
    """
    segments: list[str] = []
    in_section = False
    for line in persona.splitlines():
        stripped = line.strip()
        if _APPROVER_HEADING_RE.match(stripped):
            in_section = True
            continue
        if in_section:
            if stripped and _MENTION_RE.search(stripped):
                segments.append(stripped)
                continue
            # A blank line or a line naming nobody ends the list.
            in_section = False
        for sentence in stripped.split("."):
            if _APPROVE_WORD_RE.search(sentence) and _MENTION_RE.search(sentence):
                segments.append(sentence)
    return segments


def assert_ids_grounded(soul: SoulData, persona: str) -> None:
    """Trust boundary: every Slack id in `soul` must appear verbatim in the raw
    persona text. Raises ValueError naming the first ungrounded id found."""
    ids: list[str] = []
    if soul.manager is not None:
        ids.append(soul.manager.user_id)
    ids.extend(a.user_id for a in soul.approvers)
    ids.extend(soul.allowed_channels)
    ids.extend(soul.dm_allowed_users)
    ids.extend(soul.blocked_users)
    for id_ in ids:
        if id_ not in persona:
            raise ValueError(f"ungrounded id {id_!r} not found verbatim in IDENTITY.md")


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_comments(persona: str) -> str:
    """Drop HTML comments before any id is read out of the persona.

    The documented soul.md template explains each section with a commented-out
    *example* -- `<!-- "- <@U0123ABCD> approves deploys and infra (scope:
    deploy, release, infra)" -->`. Read literally, that granted the template's
    placeholder id scoped approval authority on every real instance: an
    authorization nobody wrote on purpose. A comment is documentation for the
    human editing the file, never a grant, so it is removed before parsing
    rather than filtered afterwards -- the same reasoning applies to a
    commented-out manager, channel or blocked user.
    """
    return _HTML_COMMENT_RE.sub("", persona)


def regex_fallback(persona: str) -> SoulData:
    """Deterministic, dependency-free extraction used when the LLM extractor
    is unavailable or fails validation/grounding. Every id it returns is read
    directly out of `persona`, so it is grounded by construction."""
    persona = strip_comments(persona)
    mentions = _MENTION_RE.findall(persona)
    channels = _CHANNEL_RE.findall(persona)

    manager: Manager | None = None
    approvers: list[ApproverEntry] = []
    for match in re.finditer(r"manager[^.]*?<@([UW][A-Z0-9]+)>", persona, re.IGNORECASE):
        manager = Manager(user_id=match.group(1))
        break
    if manager is None and mentions:
        manager = Manager(user_id=mentions[0])

    # The manager is deliberately NOT excluded here: in the documented format
    # (see README) the manager *is* the catch-all approver, and skipping them
    # left an approval nobody was authorized to click.
    seen: set[str] = set()
    for segment in _approver_segments(persona):
        scope_match = _SCOPE_RE.search(segment)
        scope = scope_match.group(1).strip() if scope_match else ""
        catchall = bool(_CATCHALL_RE.search(segment))
        for uid in _MENTION_RE.findall(segment):
            if uid in seen:
                continue
            seen.add(uid)
            approvers.append(ApproverEntry(user_id=uid, scope=scope, catchall=catchall))

    name_match = _NAME_RE.search(persona)
    identity = Identity(name=name_match.group(1)) if name_match else Identity()

    return SoulData(
        identity=identity,
        manager=manager,
        allowed_channels=list(dict.fromkeys(channels)),
        approvers=approvers,
    )


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _json_object(raw: str) -> str:
    """The JSON object out of a model reply that may be dressed up.

    `claude-haiku-4-5` answers this prompt with a ```json fence around the
    object, so a bare `json.loads` raised on every boot and every instance ran
    on the degraded regex fallback -- silently, because the failure path logged
    no reason. Rather than fight the model with prompt wording, take the widest
    `{...}` span: that survives a fence, a bare object, and any "Here is the
    extraction:" preamble. Nothing is trusted on the strength of having parsed
    -- `assert_ids_grounded` still has to pass afterwards.
    """
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        raise ValueError("model reply contained no JSON object")
    return match.group(0)


def _soul_from_json(raw: str) -> SoulData:
    data = json.loads(_json_object(raw))
    identity_data = data.get("identity") or {}
    manager_data = data.get("manager")
    return SoulData(
        identity=Identity(
            name=identity_data.get("name", "jean"), role=identity_data.get("role", "")
        ),
        manager=(
            Manager(user_id=manager_data["user_id"], name=manager_data.get("name", ""))
            if manager_data
            else None
        ),
        allowed_channels=list(data.get("allowed_channels") or []),
        dm_allowed_users=list(data.get("dm_allowed_users") or []),
        blocked_users=list(data.get("blocked_users") or []),
        approvers=[
            ApproverEntry(
                user_id=a["user_id"], scope=a.get("scope", ""), catchall=a.get("catchall", False)
            )
            for a in (data.get("approvers") or [])
        ],
        mandate=data.get("mandate", ""),
        values=list(data.get("values") or []),
        approval_timeout_seconds=int(data.get("approval_timeout_seconds") or 0),
    )


def _default_extractor(settings: Settings) -> Extractor:
    from anthropic import AsyncAnthropic

    if settings.anthropic_api_key:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    elif settings.claude_code_oauth_token:
        client = AsyncAnthropic(
            auth_token=settings.claude_code_oauth_token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
    else:
        raise RuntimeError(
            "no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set; cannot run the soul extractor"
        )

    async def extractor(system: str, prompt: str) -> str:
        resp = await client.messages.create(
            model=settings.soul_parse_model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    return extractor


async def load_soul_data(settings: Settings, *, extractor: Extractor | None = None) -> SoulData:
    """Load + parse IDENTITY.md into a grounded SoulData.

    Cache-hit (sha256 of the raw persona text) -> return cached. Otherwise
    extract -> validate -> ground; any failure at any step (bad JSON, invalid
    id shape, ungrounded id) falls back to `regex_fallback`, which can never
    produce an ungrounded id.
    """
    persona = _load_persona_text(settings)
    if not persona:
        return SoulData(identity=Identity(), manager=None)

    digest = hashlib.sha256(persona.encode()).hexdigest()
    cache_path = settings.cache_dir / f"{digest}.json"
    if cache_path.exists():
        return _soul_from_json(cache_path.read_text())

    resolved = extractor or _default_extractor(settings)
    try:
        raw = await resolved(_SYSTEM_PROMPT, EXTRACTION_PROMPT + persona)
        soul = _soul_from_json(raw)
        assert_ids_grounded(soul, persona)
    except Exception as exc:
        # Loudly, and WITH the reason: the fallback is a *degraded* parse, and a
        # soul that silently loses its approvers turns every approval into one
        # nobody can click. A bare "extraction failed" hid a one-line
        # JSONDecodeError (an unstripped ```json fence) for as long as it took
        # someone to reproduce the call by hand -- the reason belongs in the log,
        # not in a debugging session.
        logger.warning(
            "soul extraction failed (%s: %s); falling back to regex parse of %s",
            type(exc).__name__,
            exc,
            settings.identity_path,
        )
        soul = regex_fallback(persona)
        logger.warning(
            "regex fallback found manager=%s approvers=%s",
            soul.manager.user_id if soul.manager else None,
            [(a.user_id, a.scope, a.catchall) for a in soul.approvers],
        )
        return soul

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(raw)
    return soul


def _load_persona_text(settings: Settings) -> str:
    from jean.persona.identity import load_identity

    return load_identity(settings.identity_path)
