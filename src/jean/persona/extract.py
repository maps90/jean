from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from jean.persona.model import EXTRACTION_PROMPT, ApproverEntry, Identity, Manager, SoulData

if TYPE_CHECKING:
    from jean.config import Settings

# extractor(system, prompt) -> raw model text (expected to be a JSON object)
Extractor = Callable[[str, str], Awaitable[str]]

_SYSTEM_PROMPT = (
    "You are a precise JSON extraction engine. Reply with a single JSON object "
    "and nothing else -- no prose, no markdown code fences."
)

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
_CHANNEL_RE = re.compile(r"<#([CGD][A-Z0-9]+)(?:\|[^>]*)?>")


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


def regex_fallback(persona: str) -> SoulData:
    """Deterministic, dependency-free extraction used when the LLM extractor
    is unavailable or fails validation/grounding. Every id it returns is read
    directly out of `persona`, so it is grounded by construction."""
    mentions = _MENTION_RE.findall(persona)
    channels = _CHANNEL_RE.findall(persona)

    manager: Manager | None = None
    approvers: list[ApproverEntry] = []
    for match in re.finditer(r"manager[^.]*?<@([UW][A-Z0-9]+)>", persona, re.IGNORECASE):
        manager = Manager(user_id=match.group(1))
        break
    if manager is None and mentions:
        manager = Manager(user_id=mentions[0])

    for match in re.finditer(
        r"approv\w*[^.]*?<@([UW][A-Z0-9]+)>(?:[^.]*?scope:\s*([\w, -]+))?", persona, re.IGNORECASE
    ):
        uid, scope = match.group(1), (match.group(2) or "").strip()
        if manager is not None and uid == manager.user_id:
            continue
        approvers.append(ApproverEntry(user_id=uid, scope=scope))

    return SoulData(
        identity=Identity(),
        manager=manager,
        allowed_channels=list(dict.fromkeys(channels)),
        approvers=approvers,
    )


def _soul_from_json(raw: str) -> SoulData:
    data = json.loads(raw)
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
    except Exception:
        return regex_fallback(persona)

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(raw)
    return soul


def _load_persona_text(settings: Settings) -> str:
    from jean.persona.identity import load_identity

    return load_identity(settings.identity_path)
