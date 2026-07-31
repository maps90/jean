from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import RateLimitEvent as RealRateLimitEvent
from claude_agent_sdk import ResultMessage as RealResultMessage

from jean.db.memory import MemoryStore
from jean.session.session import (
    RATE_LIMIT_EVENT_CLASS_NAME,
    RESULT_MESSAGE_CLASS_NAME,
    JeanSession,
)
from jean.session.transcript import LocalTranscripts

MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
FAST_SETTLE = {"settle_timeout": 0.05, "settle_interval": 0.002, "settle_quiet": 0.004}


def test_sdk_class_names_match_the_real_sdk_classes():
    """session/ may not import claude_agent_sdk (CLAUDE.md's layering rule), so
    run_turn recognises these two messages by class NAME -- the same soft
    dependency `ASSISTANT_MESSAGE_CLASS_NAME` carries. If the SDK renames either,
    the metrics silently flatline: no token counts, and no rate-limit warning
    before the window that actually takes the agent down. This test turns that
    drift into a loud failure instead."""
    assert RealResultMessage.__name__ == RESULT_MESSAGE_CLASS_NAME
    assert RealRateLimitEvent.__name__ == RATE_LIMIT_EVENT_CLASS_NAME


class RecordingMetrics:
    """Fake at the MetricsSink port -- records calls, asserts nothing itself."""

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.token_calls: list[dict] = []
        self.started = 0
        self.resumed: list[str] = []
        self.incomplete = 0
        self.schedule: list[str] = []
        self.rate_limits: list[dict] = []

    def turn_done(self, *, trigger: str, outcome: str, seconds: float) -> None:
        self.turns.append({"trigger": trigger, "outcome": outcome, "seconds": seconds})

    def tokens(self, *, trigger: str, usage: dict | None, cost_usd: float | None) -> None:
        self.token_calls.append({"trigger": trigger, "usage": usage, "cost_usd": cost_usd})

    def session_started(self) -> None:
        self.started += 1

    def session_resumed(self, *, outcome: str) -> None:
        self.resumed.append(outcome)

    def transcript_incomplete(self) -> None:
        self.incomplete += 1

    def schedule_run(self, *, status: str) -> None:
        self.schedule.append(status)

    def rate_limit(self, *, window: str, utilization, resets_at) -> None:
        self.rate_limits.append(
            {"window": window, "utilization": utilization, "resets_at": resets_at}
        )

    @property
    def outcomes(self) -> list[str]:
        return [t["outcome"] for t in self.turns]


# -- SDK message stand-ins, named exactly as the SDK names them ----------------


@dataclass
class ResultMessage:
    session_id: str
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    is_error: bool = False
    api_error_status: int | None = None


@dataclass
class RateLimitInfo:
    status: str = "allowed_warning"
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    session_id: str = "sdk-1"


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str


@dataclass
class AssistantMessage:
    content: list = field(default_factory=list)


def _assistant(*, text: str | None = None, tool: str | None = None) -> AssistantMessage:
    blocks: list = []
    if tool is not None:
        blocks.append(ToolUseBlock(name=tool))
    if text is not None:
        blocks.append(TextBlock(text=text))
    return AssistantMessage(content=blocks)


class FakeChat:
    def __init__(self, *, reply_raises: bool = False):
        self.replies: list[tuple[str, str, str]] = []
        self.reply_raises = reply_raises

    async def set_status(self, channel, thread_ts, status):
        pass

    async def reply(self, channel, thread_ts, text):
        if self.reply_raises:
            raise RuntimeError("slack is down")
        self.replies.append((channel, thread_ts, text))
        return "999.0"

    async def edit(self, *a, **k):
        raise NotImplementedError

    async def upload(self, *a, **k):
        raise NotImplementedError

    async def react(self, *a, **k):
        raise NotImplementedError

    async def unreact(self, *a, **k):
        raise NotImplementedError


def _client_factory(messages, *, open_raises_for=None):
    """A client whose receive_response() yields `messages`.

    `open_raises_for` makes construction fail when options carry that resume id,
    which is how the CLI signals a transcript it cannot resume.
    """

    def factory(*, options):
        resume = options.get("resume") if isinstance(options, dict) else None
        if open_raises_for is not None and resume == open_raises_for:
            raise RuntimeError("cli exited 1")
        return FakeSdkClient(messages)

    return factory


class FakeSdkClient:
    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass

    async def query(self, text: str) -> None:
        pass

    async def receive_response(self):
        for m in self._messages:
            yield m


def _session(tmp_path: Path, messages, *, chat=None, metrics=None, factory=None, **kw):
    store = MemoryStore()
    chat = chat or FakeChat()
    metrics = metrics or RecordingMetrics()
    kw.setdefault("max_transcript_bytes", MAX_TRANSCRIPT_BYTES)
    for k, v in FAST_SETTLE.items():
        kw.setdefault(k, v)
    session = JeanSession(
        "C1",
        "111.0",
        store=store,
        chat=chat,
        options_factory=lambda resume, mode=None: {"resume": resume, "mode": mode},
        client_factory=factory or _client_factory(messages),
        transcripts=store,
        local=LocalTranscripts(cli_home=tmp_path, cwd=Path("/w")),
        metrics=metrics,
        **kw,
    )
    return session, store, chat, metrics


# -- outcome classification ---------------------------------------------------


async def test_outcome_ok_when_the_agent_called_a_speaking_tool(tmp_path: Path):
    messages = [
        _assistant(tool="mcp__jean_slack__reply", text="posted"),
        ResultMessage(session_id="s1"),
    ]
    session, _s, chat, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.outcomes == ["ok"]
    assert chat.replies == []  # the agent spoke for itself; no _deliver


async def test_outcome_ok_when_deliver_posts_the_final_text(tmp_path: Path):
    """`_deliver` is the NORMAL path, not a rescue -- a turn that ends in real
    text delivered to the thread is a success, not a defect."""
    messages = [_assistant(text="here is your answer"), ResultMessage(session_id="s1")]
    session, _s, chat, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.outcomes == ["ok"]
    assert chat.replies[0][2] == "here is your answer"


async def test_outcome_notice_when_the_turn_produced_no_answer_at_all(tmp_path: Path):
    """The JEAN_EFFORT=low failure: the agent does the work, never replies, and
    nothing raises. A success counter that only watched for exceptions would
    score this 100% healthy."""
    messages = [_assistant(tool="Bash"), ResultMessage(session_id="s1")]
    session, _s, chat, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.outcomes == ["notice"]
    assert len(chat.replies) == 1  # the notice itself did go out


async def test_outcome_undelivered_when_the_reply_fails(tmp_path: Path):
    """chat.reply raising is caught and logged inside _deliver, so the turn
    still returns clean -- but the thread is silent, which is the worst
    user-visible outcome of the lot."""
    messages = [_assistant(text="an answer nobody will see"), ResultMessage(session_id="s1")]
    session, _s, _c, metrics = _session(tmp_path, messages, chat=FakeChat(reply_raises=True))
    await session.run_turn("hi")

    assert metrics.outcomes == ["undelivered"]


async def test_outcome_error_when_the_turn_raises(tmp_path: Path):
    def factory(*, options):
        raise RuntimeError("connect failed")

    session, _s, _c, metrics = _session(tmp_path, [], factory=factory)
    with pytest.raises(RuntimeError):
        await session.run_turn("hi")

    assert metrics.outcomes == ["error"]


async def test_outcome_rate_limited_beats_the_delivery_outcome(tmp_path: Path):
    """A 429 does not raise: the stream completes with an error ResultMessage and
    no text, which would otherwise be scored `notice` and blamed on the prompt."""
    messages = [
        _assistant(tool="Bash"),
        ResultMessage(session_id="s1", is_error=True, api_error_status=429),
    ]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.outcomes == ["rate_limited"]


async def test_an_error_result_that_is_not_a_rate_limit_is_not_mislabelled(tmp_path: Path):
    """`is_error` covers every failed API call. Only 429/529 are capacity; a 400
    is a bug and must not be filed under 'we are being throttled'."""
    messages = [
        _assistant(text="partial"),
        ResultMessage(session_id="s1", is_error=True, api_error_status=400),
    ]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.outcomes == ["ok"]


# -- tokens -------------------------------------------------------------------


async def test_tokens_and_cost_are_read_off_the_result_message(tmp_path: Path):
    usage = {
        "input_tokens": 120,
        "output_tokens": 40,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 500,
    }
    messages = [
        _assistant(text="done"),
        ResultMessage(session_id="s1", usage=usage, total_cost_usd=0.42),
    ]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.token_calls == [{"trigger": "human", "usage": usage, "cost_usd": 0.42}]


async def test_a_result_message_without_usage_still_reports_the_turn(tmp_path: Path):
    messages = [_assistant(text="done"), ResultMessage(session_id="s1")]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.token_calls == [{"trigger": "human", "usage": None, "cost_usd": None}]
    assert metrics.outcomes == ["ok"]


async def test_trigger_label_follows_the_turn(tmp_path: Path):
    messages = [_assistant(text="done"), ResultMessage(session_id="s1", usage={})]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("cron prompt", trigger="schedule")

    assert metrics.turns[0]["trigger"] == "schedule"
    assert metrics.token_calls[0]["trigger"] == "schedule"


# -- session lifecycle --------------------------------------------------------


async def test_a_first_turn_counts_as_a_started_session(tmp_path: Path):
    messages = [_assistant(text="done"), ResultMessage(session_id="s1")]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.started == 1
    assert metrics.resumed == []


async def test_a_refused_resume_is_recorded_as_fresh_fallback(tmp_path: Path):
    """The thread silently lost its whole history. Today the only symptom is a
    confused human; this is the metric that makes it visible."""
    messages = [_assistant(text="done"), ResultMessage(session_id="s2")]
    factory = _client_factory(messages, open_raises_for="dead-id")
    session, store, _c, metrics = _session(tmp_path, messages, factory=factory)
    await store.upsert_session("C1", "111.0", sdk_session_id="dead-id")

    await session.run_turn("hi")

    assert metrics.resumed == ["fresh_fallback"]
    assert metrics.started == 0


async def test_a_successful_resume_is_recorded_as_ok(tmp_path: Path):
    messages = [_assistant(text="done"), ResultMessage(session_id="good-id")]
    session, store, _c, metrics = _session(tmp_path, messages)
    await store.upsert_session("C1", "111.0", sdk_session_id="good-id")

    await session.run_turn("hi")

    assert metrics.resumed == ["ok"]


async def test_a_connect_that_fails_for_a_non_resume_reason_is_not_memory_loss(tmp_path: Path):
    """If connecting WITHOUT resume fails too, it was never the resume -- it is
    bad auth or a bad --plugin-dir. Counting it as fresh_fallback would put a
    permanent floor under the memory-fidelity SLI during any auth outage."""

    def factory(*, options):
        raise RuntimeError("bad auth")

    session, store, _c, metrics = _session(tmp_path, [], factory=factory)
    await store.upsert_session("C1", "111.0", sdk_session_id="some-id")

    with pytest.raises(RuntimeError):
        await session.run_turn("hi")

    assert metrics.resumed == []
    assert metrics.outcomes == ["error"]


# -- rate limit ---------------------------------------------------------------


async def test_rate_limit_events_reach_the_sink(tmp_path: Path):
    messages = [
        RateLimitEvent(
            RateLimitInfo(utilization=0.87, resets_at=1750000000, rate_limit_type="five_hour")
        ),
        _assistant(text="done"),
        ResultMessage(session_id="s1"),
    ]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.rate_limits == [
        {"window": "five_hour", "utilization": 0.87, "resets_at": 1750000000}
    ]


async def test_a_rate_limit_event_without_a_window_falls_back_to_unknown(tmp_path: Path):
    """`rate_limit_type` is optional in the SDK. A None label value would be
    rejected by prometheus_client and take the whole turn's metrics with it."""
    messages = [
        RateLimitEvent(RateLimitInfo(utilization=0.1)),
        _assistant(text="done"),
        ResultMessage(session_id="s1"),
    ]
    session, _s, _c, metrics = _session(tmp_path, messages)
    await session.run_turn("hi")

    assert metrics.rate_limits[0]["window"] == "unknown"
