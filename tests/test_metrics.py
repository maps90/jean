from __future__ import annotations

from jean.metrics.null import NullMetrics
from jean.metrics.prometheus import PrometheusMetrics
from jean.ports import MetricsSink


def _series(text: str, name: str, **labels: str) -> float | None:
    """The value of one sample line, found by name + exact label set.

    Parses the exposition text rather than reaching into the registry: the
    rendered bytes ARE the contract with Prometheus, so that is what the tests
    assert on.
    """
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    prefix = f"{name}{{{label_str}}} " if label_str else f"{name} "
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        # Label order in the output follows declaration order, not sorted order,
        # so match on the parsed set instead of the raw substring.
        if not line.startswith(f"{name}{{") and not line.startswith(f"{name} "):
            continue
        head, _, value = line.rpartition(" ")
        if head == prefix.rstrip():
            return float(value)
        if "{" in head:
            body = head[head.index("{") + 1 : head.rindex("}")]
            got = dict(
                (k, v.strip('"'))
                for k, v in (part.split("=", 1) for part in body.split(",") if part)
            )
            if head[: head.index("{")] == name and got == labels:
                return float(value)
    return None


def _render(m: PrometheusMetrics) -> str:
    body, content_type = m.render()
    assert "text/plain" in content_type
    return body.decode()


def test_adapter_satisfies_the_port():
    assert isinstance(PrometheusMetrics(agent="anya"), MetricsSink)
    assert isinstance(NullMetrics(), MetricsSink)


def test_null_metrics_accepts_every_call_and_does_nothing():
    m = NullMetrics()
    m.turn_done(trigger="human", outcome="ok", seconds=1.0)
    m.tokens(trigger="human", usage={"input_tokens": 1}, cost_usd=0.5)
    m.session_started()
    m.session_resumed(outcome="ok")
    m.transcript_incomplete()
    m.schedule_run(status="ok")
    m.rate_limit(window="five_hour", utilization=0.5, resets_at=1.0)


def test_every_series_carries_the_agent_label():
    m = PrometheusMetrics(agent="damian")
    m.turn_done(trigger="human", outcome="ok", seconds=2.0)
    text = _render(m)
    assert _series(text, "jean_turns_total", agent="damian", trigger="human", outcome="ok") == 1.0


def test_turns_and_duration_are_recorded_per_trigger_and_outcome():
    m = PrometheusMetrics(agent="anya")
    m.turn_done(trigger="human", outcome="ok", seconds=3.0)
    m.turn_done(trigger="human", outcome="ok", seconds=5.0)
    m.turn_done(trigger="schedule", outcome="error", seconds=1.0)
    text = _render(m)

    assert _series(text, "jean_turns_total", agent="anya", trigger="human", outcome="ok") == 2.0
    assert (
        _series(text, "jean_turns_total", agent="anya", trigger="schedule", outcome="error") == 1.0
    )
    assert _series(text, "jean_turn_duration_seconds_sum", agent="anya", trigger="human") == 8.0
    assert _series(text, "jean_turn_duration_seconds_count", agent="anya", trigger="human") == 2.0


def test_duration_buckets_span_jeans_real_turn_times():
    """A jean turn runs 5s-300s (config.py records ~149s as normal for an
    investigation), so the histogram must have resolution up there -- the
    library default tops out at 10s and would collapse every real turn into
    +Inf, making the p95 SLI meaningless."""
    m = PrometheusMetrics(agent="anya")
    m.turn_done(trigger="human", outcome="ok", seconds=149.0)
    text = _render(m)

    # 149s lands under the 180 bucket but not under 120.
    assert (
        _series(
            text, "jean_turn_duration_seconds_bucket", agent="anya", trigger="human", le="120.0"
        )
        == 0.0
    )
    assert (
        _series(
            text, "jean_turn_duration_seconds_bucket", agent="anya", trigger="human", le="180.0"
        )
        == 1.0
    )


def test_tokens_are_split_by_kind():
    m = PrometheusMetrics(agent="anya")
    m.tokens(
        trigger="human",
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 300,
        },
        cost_usd=0.125,
    )
    text = _render(m)

    assert _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="input") == 100.0
    assert _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="output") == 20.0
    assert (
        _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="cache_read")
        == 4000.0
    )
    assert (
        _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="cache_creation")
        == 300.0
    )
    assert _series(text, "jean_cost_usd_total", agent="anya", trigger="human") == 0.125


def test_missing_or_partial_usage_adds_nothing_and_does_not_raise():
    """`ResultMessage.usage` is `dict | None` in the SDK and its keys are not
    guaranteed. A metrics gap must never fail a turn."""
    m = PrometheusMetrics(agent="anya")
    m.tokens(trigger="human", usage=None, cost_usd=None)
    m.tokens(trigger="human", usage={}, cost_usd=None)
    m.tokens(trigger="human", usage={"output_tokens": 7}, cost_usd=None)
    m.tokens(trigger="human", usage={"input_tokens": "not-a-number"}, cost_usd=None)
    text = _render(m)

    assert _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="output") == 7.0
    assert _series(text, "jean_tokens_total", agent="anya", trigger="human", kind="input") == 0.0


def test_session_lifecycle_counters():
    m = PrometheusMetrics(agent="anya")
    m.session_started()
    m.session_resumed(outcome="ok")
    m.session_resumed(outcome="fresh_fallback")
    m.transcript_incomplete()
    text = _render(m)

    assert _series(text, "jean_sessions_started_total", agent="anya") == 1.0
    assert _series(text, "jean_sessions_resumed_total", agent="anya", outcome="ok") == 1.0
    assert (
        _series(text, "jean_sessions_resumed_total", agent="anya", outcome="fresh_fallback") == 1.0
    )
    assert _series(text, "jean_transcript_incomplete_total", agent="anya") == 1.0


def test_schedule_runs_by_status():
    m = PrometheusMetrics(agent="anya")
    m.schedule_run(status="ok")
    m.schedule_run(status="missed")
    text = _render(m)

    assert _series(text, "jean_schedule_runs_total", agent="anya", status="ok") == 1.0
    assert _series(text, "jean_schedule_runs_total", agent="anya", status="missed") == 1.0


def test_rate_limit_gauges_hold_the_latest_value():
    m = PrometheusMetrics(agent="anya")
    m.rate_limit(window="five_hour", utilization=0.4, resets_at=1000.0)
    m.rate_limit(window="five_hour", utilization=0.9, resets_at=2000.0)
    text = _render(m)

    # A gauge, not a counter: the second reading replaces the first.
    assert _series(text, "jean_rate_limit_utilization", agent="anya", window="five_hour") == 0.9
    assert _series(text, "jean_rate_limit_resets_at", agent="anya", window="five_hour") == 2000.0


def test_rate_limit_ignores_absent_values():
    """The SDK models utilization and resets_at as optional; an event that
    carries neither must not publish a zero that reads as 'plenty of headroom'."""
    m = PrometheusMetrics(agent="anya")
    m.rate_limit(window="seven_day", utilization=None, resets_at=None)
    text = _render(m)

    assert _series(text, "jean_rate_limit_utilization", agent="anya", window="seven_day") is None


def test_registries_are_per_instance_not_global():
    """The adapter must not touch prometheus_client's default REGISTRY -- that
    global is exactly the module-level singleton the conventions forbid, and it
    makes two instances in one test process collide on registration."""
    a = PrometheusMetrics(agent="anya")
    b = PrometheusMetrics(agent="damian")
    a.turn_done(trigger="human", outcome="ok", seconds=1.0)

    assert (
        _series(_render(a), "jean_turns_total", agent="anya", trigger="human", outcome="ok") == 1.0
    )
    assert (
        _series(_render(b), "jean_turns_total", agent="damian", trigger="human", outcome="ok")
        is None
    )
