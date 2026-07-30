# Prometheus metrics export

**Status:** approved design, not yet implemented
**Date:** 2026-07-30

## Goal

Expose jean's per-turn cost and health as Prometheus counters so a Grafana dashboard
can answer, per agent: how many tokens are we burning, on what, how often does a
human not get an answer, and are we about to hit a rate limit.

Two agents (`anya`, `damian`) run as separate deployments against one Postgres,
distinguished today by `JEAN_DB_SCHEMA`. Both are scraped as separate targets.

## Non-goals

- **No DB reads on scrape.** Every metric is a per-pod counter or histogram
  incremented in code jean already runs. `/metrics` never queries Postgres. This
  rules out any DB-derived gauge — "sessions alive across the fleet" is a `sum()`
  over counters in Grafana, or a question for the DB, not for a scrape. (The two
  rate-limit gauges below are not DB-derived; they are last-seen values pushed by
  the SDK's own event stream.)
- **No per-thread or per-user labels.** Channel and thread ids are unbounded
  cardinality and would be a Prometheus incident of their own.
- **No multiprocess collector.** One asyncio process per pod; scale is N pods.
  `prometheus_client`'s default in-process registry is correct here.

## Architecture

Ports and adapters, exactly like every other boundary in this codebase.

```
session/  gateway/  schedule/     ->  MetricsSink (Protocol, ports.py)
                                          ^
                                          | injected by server.py
                                          |
                                   metrics/prometheus.py  (adapter, owns CollectorRegistry)
                                          |
                                   health.py  GET /metrics
```

- **Port** — `MetricsSink` in `src/jean/ports.py`. Domain code calls it; domain code
  never imports `prometheus_client`. This is CLAUDE.md's layering rule, unchanged.
- **Adapter** — new `src/jean/metrics/prometheus.py`. Constructs its own
  `CollectorRegistry()` (an instance, injected — never the library's global default
  `REGISTRY`, which would be exactly the module-level singleton the conventions
  forbid). Owns the `agent` label so no call site has to know its own deployment name.
- **Null object** — `NullMetrics` in the same module, all methods `pass`. Default for
  every constructor that takes a sink, so tests and single-process runs that do not
  care about metrics wire nothing.
- **Composition root** — `server.py` builds the adapter and passes it into
  `JeanSession`, `SessionManager`, and `ScheduleRunner`, and hands its `render()` to
  `make_health_app`.

### Sink methods are synchronous

Every `MetricsSink` method is a plain `def`, not `async def` — the one deliberate
departure from "async everywhere on I/O paths". A counter increment is an in-memory
dict update, not I/O. Making them `async` would add an await point (and therefore a
cancellation point) inside `run_turn`'s `finally` block, where a cancelled scrape
bookkeeping call could mask the real exception being propagated. Metrics must never
be able to fail or reorder a turn.

### The `agent` label

Set once by the adapter from `JEAN_METRICS_AGENT`, falling back to `JEAN_DB_SCHEMA`.
The fallback exists because the two are the same string in the current deployment;
the override exists because `db_schema` defaults to `public`, which is a useless
label value for a single-agent install. Prometheus service discovery also supplies
`job`/`pod`, but an in-app `agent` label makes dashboards portable across scrape
configs.

## Metrics

All labelled `agent`. `trigger` is `human` | `schedule`.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `jean_tokens_total` | counter | `trigger`, `kind` | `ResultMessage.usage`; kind = `input`/`output`/`cache_read`/`cache_creation` |
| `jean_turns_total` | counter | `trigger`, `outcome` | end of `run_turn` |
| `jean_turn_duration_seconds` | histogram | `trigger` | the elapsed time already computed at `session.py:718` |
| `jean_cost_usd_total` | counter | `trigger` | `ResultMessage.total_cost_usd` |
| `jean_sessions_started_total` | counter | — | `_connect` opened without `resume` |
| `jean_sessions_resumed_total` | counter | `outcome` | `ok` \| `fresh_fallback` |
| `jean_transcript_incomplete_total` | counter | — | `_settle` hit its timeout |
| `jean_schedule_runs_total` | counter | `status` | `ok` \| `error` \| `missed` |
| `jean_rate_limit_utilization` | gauge | `window` | `RateLimitEvent.rate_limit_info` |
| `jean_rate_limit_resets_at` | gauge | `window` | same, unix seconds |

Cardinality ceiling: ~30 series per pod. Negligible.

`jean_cost_usd_total` on an OAuth/subscription token is the CLI's notional
API-equivalent price, not a bill. Useful as a trend and for comparing the two
agents; not a finance number. Documented on the dashboard, not enforced in code.

The two rate-limit gauges are last-seen-value per pod, so panels must use `max()`,
not `sum()`. They are the only gauges in the set.

### `outcome`: what counts as a failure

A turn that raises is not the interesting failure. `session.py:467` posts
`MUTE_TURN_NOTICE` when a turn produces neither a tool call nor any text, and the
turn completes normally — that is the observed `JEAN_EFFORT=low` regression, where
the agent does the work and never replies. `session.py:499` is worse: if
`chat.reply` itself raises, it is logged and swallowed and the turn still returns
clean. Both would score as success under "did not throw".

So `ok` means **the human got a real answer**:

| `outcome` | Condition | User sees |
|---|---|---|
| `ok` | a speaking tool was called, or `_deliver` posted real `final_text` | the answer |
| `notice` | `final_text is None` → `MUTE_TURN_NOTICE` | an apology |
| `undelivered` | `chat.reply` raised inside `_deliver` | nothing |
| `error` | the turn raised (`run_turn`'s `except BaseException`) | nothing |
| `rate_limited` | `ResultMessage.is_error` and `api_error_status` in {429, 529} | nothing |

`ok` deliberately **includes** the `_deliver` path. Per its own docstring, delivering
the final assistant message is the normal route, not a rescue — only the `None`-text
case is a defect.

Exactly one label is emitted per turn, resolved by this precedence — most
user-visible failure wins:

1. `error` — the turn raised; no other outcome was reached.
2. `undelivered` — `chat.reply` raised. Beats `notice`: if the notice itself failed
   to post, the thread is silent, which is the worse fact.
3. `rate_limited` — a `ResultMessage` arrived with `is_error` and `api_error_status`
   in {429, 529}. Beats `notice`/`ok`, because such a turn does not raise: the stream
   completes with an error result, and without this rule a rate-limited turn would be
   scored `notice` and blamed on the prompt.
4. `notice` — `final_text is None` and no speaking tool was called.
5. `ok` — everything else.

To report this, `_deliver` changes from returning `None` to returning the outcome
string it produced (`ok` | `notice` | `undelivered`). `run_turn` already knows
`spoke`; combining the two yields the label without new state.

## SLIs

**SLI-1 — Answer delivery rate.** Target 99% over 30d.

```promql
sum(rate(jean_turns_total{outcome="ok"}[30m])) by (agent)
  / sum(rate(jean_turns_total[30m])) by (agent)
```

Everything not `ok` burns error budget while staying separable by cause:
`rate_limited` is capacity, `notice` is a prompt/effort regression, `error` is infra.

**SLI-2 — Turn latency, human-triggered only.** Target p95 < 180s.

```promql
histogram_quantile(0.95,
  sum(rate(jean_turn_duration_seconds_bucket{trigger="human"}[30m])) by (le, agent))
```

Filtered to `trigger="human"` on purpose: nobody waits on a cron firing, and
`config.py:122` records that ~149s for a metrics-and-logs investigation is normal
behaviour, not a fault. A global latency SLO would page on health.

Buckets: `[1, 2.5, 5, 10, 30, 60, 120, 180, 300, 600, +Inf]`, matching jean's real
5s–300s spread.

**SLI-3 — Memory fidelity.** Target 99.9%.

```promql
1 - (
  sum(rate(jean_sessions_resumed_total{outcome="fresh_fallback"}[6h])) by (agent)
    / sum(rate(jean_sessions_resumed_total[6h])) by (agent))
```

A `fresh_fallback` is a thread that silently lost its whole history — today the only
symptom is a confused human. `jean_transcript_incomplete_total` is the latent form of
the same loss: archived truncated, so the *next* cold resume drops an answer.

**Saturation alert (not an SLI).** `max(jean_rate_limit_utilization) by (agent, window) > 0.85`.
On a Max subscription, exhausting the five-hour window is the actual outage mode, and
there is no warning today.

## Instrumentation sites

| Site | Change |
|---|---|
| `session.py` receive loop | match `ResultMessage` and `RateLimitEvent` by class name, as `ASSISTANT_MESSAGE_CLASS_NAME` already does for `AssistantMessage`; read `usage`, `total_cost_usd`, `is_error`, `api_error_status`, `rate_limit_info` |
| `session.py` `_deliver` | return the outcome string instead of `None` |
| `session.py` `run_turn` finally | emit `turn_done(trigger, outcome, seconds)` next to the existing log line |
| `session.py` `_connect` | `session_started()` on the no-resume path; `session_resumed(ok\|fresh_fallback)` on the resume paths |
| `session.py` `_settle` | `transcript_incomplete()` at the timeout warning |
| `schedule/runner.py` | `schedule_run(status)` at each of the three `record_run` calls |
| `session/manager.py` | `handle(..., *, trigger: str = "human")`; `ScheduleRunner` passes `trigger="schedule"` |
| `health.py` | `make_health_app(ready_check=, render_metrics=None)`; route added only when supplied |

### Class-name matching

`RESULT_MESSAGE_CLASS_NAME = "ResultMessage"` and `RATE_LIMIT_EVENT_CLASS_NAME =
"RateLimitEvent"` follow the existing `ASSISTANT_MESSAGE_CLASS_NAME` pattern:
`session/` is domain code and must not import `claude_agent_sdk`, so SDK messages
arrive duck-typed through the injected client factory. Each constant gets the same
pinning test as `tests/test_session.py:62` — import the real SDK class in the test
and assert `__name__` matches — so an SDK rename fails the suite instead of silently
zeroing the metrics.

Missing usage fields degrade to zero rather than raising: `usage` is
`dict[str, Any] | None` in the SDK, and a metrics gap must never fail a turn.

## Testing

Unit tests inject a `FakeMetrics` recording sink at the port — no registry, no
scraping, no network, consistent with the existing fake-at-the-port tests.

- each `outcome` value is produced by the condition that should produce it,
  including `notice` (no text, no tool call) and `undelivered` (`chat.reply` raises)
- token counters read a realistic `usage` dict, and a `None`/partial one adds nothing
  and raises nothing
- `trigger="schedule"` reaches the sink when `ScheduleRunner` fires a turn
- `fresh_fallback` is recorded when the resume is refused and the fallback connect
  succeeds — and is **not** recorded when both fail (that is not a lost transcript,
  it is a propagating infra error)
- schedule `ok` / `error` / `missed` each recorded
- the SDK class-name pins, as above

Adapter tests cover the adapter alone: increment through the port, assert the
rendered exposition text contains the expected series and label values.

## Deployment

- New dep `prometheus-client>=0.20`.
- No new port or Service: `/metrics` is served by the existing health app on
  `JEAN_HEALTH_PORT` (8080).
- Add `JEAN_METRICS_AGENT` to each agent's config (`anya`, `damian`).
- Scrape config / PodMonitor lives in flux-infra, not this repo.
