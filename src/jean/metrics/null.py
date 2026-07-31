from __future__ import annotations

from typing import Any


class NullMetrics:
    """Does nothing, satisfies `MetricsSink`.

    The default for every collaborator that takes a sink, so tests and any run
    that does not scrape wire nothing and no call site needs an `if`.

    It lives here, apart from the Prometheus adapter, precisely so that domain
    code can default to it: importing it from `metrics/prometheus.py` would pull
    `prometheus_client` into `session/` and `schedule/` through the back door,
    which is the layering rule this codebase enforces in review.
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
