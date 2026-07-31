from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest

# jean turns run seconds to minutes -- config.py records ~149s for a
# metrics-and-logs investigation as normal, not slow. prometheus_client's default
# buckets stop at 10s, which would collapse every real turn into +Inf and make the
# p95 latency SLI meaningless. These give resolution across the actual spread.
DURATION_BUCKETS = (1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, float("inf"))

# Our label value <- the key the CLI puts in `ResultMessage.usage`. Kept as an
# explicit map (rather than passing the SDK's keys through) so a rename upstream
# shows up as a flat zero on one series instead of silently inventing a new one
# and blowing up cardinality.
TOKEN_KINDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}


class NullMetrics:
    """Does nothing, satisfies `MetricsSink`.

    The default for every collaborator that takes a sink, so tests and any run
    that does not scrape wire nothing and no call site needs an `if`.
    """

    def turn_done(self, *, trigger: str, outcome: str, seconds: float) -> None:
        pass

    def tokens(self, *, trigger: str, usage: dict[str, Any] | None, cost_usd: float | None) -> None:
        pass

    def session_started(self) -> None:
        pass

    def session_resumed(self, *, outcome: str) -> None:
        pass

    def transcript_incomplete(self) -> None:
        pass

    def schedule_run(self, *, status: str) -> None:
        pass

    def rate_limit(
        self, *, window: str, utilization: float | None, resets_at: float | None
    ) -> None:
        pass


class PrometheusMetrics:
    """`MetricsSink` backed by a private prometheus_client registry.

    The registry is an instance attribute, never the library's module-level
    default `REGISTRY`: that global is exactly the stateful singleton the
    conventions forbid, and sharing it would make two agents in one process (or
    two instances in one test run) collide on metric registration.

    `agent` is applied here, once, rather than passed by every call site --
    domain code has no business knowing which deployment it is running as.
    """

    def __init__(self, *, agent: str) -> None:
        self._agent = agent
        self._registry = CollectorRegistry()
        r = self._registry

        self._turns = Counter(
            "jean_turns",
            "Turns completed, by what triggered them and whether the human got an answer.",
            ["agent", "trigger", "outcome"],
            registry=r,
        )
        self._duration = Histogram(
            "jean_turn_duration_seconds",
            "Wall-clock seconds per turn.",
            ["agent", "trigger"],
            buckets=DURATION_BUCKETS,
            registry=r,
        )
        self._tokens = Counter(
            "jean_tokens",
            "Tokens consumed, by kind.",
            ["agent", "trigger", "kind"],
            registry=r,
        )
        self._cost = Counter(
            "jean_cost_usd",
            "Cost as reported by the CLI. On a subscription token this is a "
            "notional API-equivalent price, not a bill.",
            ["agent", "trigger"],
            registry=r,
        )
        self._sessions_started = Counter(
            "jean_sessions_started",
            "Agent sessions opened without a resume id (new conversations).",
            ["agent"],
            registry=r,
        )
        self._sessions_resumed = Counter(
            "jean_sessions_resumed",
            "Resume attempts; outcome=fresh_fallback means the transcript was gone "
            "and the thread lost its memory.",
            ["agent", "outcome"],
            registry=r,
        )
        self._transcript_incomplete = Counter(
            "jean_transcript_incomplete",
            "Transcripts archived before they settled -- a later cold resume may "
            "be missing that turn's answer.",
            ["agent"],
            registry=r,
        )
        self._schedule_runs = Counter(
            "jean_schedule_runs",
            "Scheduled prompts fired, by status.",
            ["agent", "status"],
            registry=r,
        )
        self._rate_limit_utilization = Gauge(
            "jean_rate_limit_utilization",
            "Fraction of the rate-limit window consumed (0-1), as last reported.",
            ["agent", "window"],
            registry=r,
        )
        self._rate_limit_resets_at = Gauge(
            "jean_rate_limit_resets_at",
            "Unix time the rate-limit window resets, as last reported.",
            ["agent", "window"],
            registry=r,
        )

        # Touch the series that carry no dimension beyond `agent` so they exist at
        # zero from boot. A counter that has never fired is absent from the
        # exposition entirely, and `rate()` over an absent series yields no data
        # rather than 0 -- which reads on a dashboard as "no signal" instead of
        # "nothing has gone wrong yet".
        self._sessions_started.labels(agent=agent)
        self._transcript_incomplete.labels(agent=agent)

    def render(self) -> tuple[bytes, str]:
        """The exposition body and its content type, for the /metrics route."""
        return _generate_latest(self._registry), CONTENT_TYPE_LATEST

    def turn_done(self, *, trigger: str, outcome: str, seconds: float) -> None:
        self._turns.labels(agent=self._agent, trigger=trigger, outcome=outcome).inc()
        self._duration.labels(agent=self._agent, trigger=trigger).observe(seconds)

    def tokens(self, *, trigger: str, usage: dict[str, Any] | None, cost_usd: float | None) -> None:
        for kind, key in TOKEN_KINDS.items():
            value = _as_number((usage or {}).get(key))
            # Always touch the series, even at zero, so all four kinds appear from
            # the first turn -- see the boot-touch note above. Incrementing by 0 is
            # what creates the child; `.inc(0)` is not a no-op at registry level.
            self._tokens.labels(agent=self._agent, trigger=trigger, kind=kind).inc(value)
        cost = _as_number(cost_usd)
        if cost:
            self._cost.labels(agent=self._agent, trigger=trigger).inc(cost)

    def session_started(self) -> None:
        self._sessions_started.labels(agent=self._agent).inc()

    def session_resumed(self, *, outcome: str) -> None:
        self._sessions_resumed.labels(agent=self._agent, outcome=outcome).inc()

    def transcript_incomplete(self) -> None:
        self._transcript_incomplete.labels(agent=self._agent).inc()

    def schedule_run(self, *, status: str) -> None:
        self._schedule_runs.labels(agent=self._agent, status=status).inc()

    def rate_limit(
        self, *, window: str, utilization: float | None, resets_at: float | None
    ) -> None:
        # Publish only what the event actually carried: both fields are optional in
        # the SDK, and a defaulted 0.0 utilization would read as "plenty of
        # headroom" -- the opposite of the truth this metric exists to tell.
        if utilization is not None:
            self._rate_limit_utilization.labels(agent=self._agent, window=window).set(
                float(utilization)
            )
        if resets_at is not None:
            self._rate_limit_resets_at.labels(agent=self._agent, window=window).set(
                float(resets_at)
            )


def _as_number(value: Any) -> float:
    """`value` as a float, or 0.0 if it is missing or not a number.

    `ResultMessage.usage` is `dict[str, Any]` -- the CLI's shape, not ours -- so a
    key can be absent, null, or a string. A metrics gap must never fail a turn.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)
