"""Rolling per-version aggregates, evaluated with the batch detector's rules.

The point of consuming the stream is to reach a verdict while a rollout is still
happening, rather than after it. What must not happen is the streaming path
drifting into a second, subtly different definition of "regression" -- so this
builds the same `Comparison`, `Amplification` and `ErrorRate` the batch detector
builds, and calls the same `evaluate`.

Every window is bounded by sample count. A consumer that accumulates unbounded
state is a memory leak with a deploy-shaped trigger.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from aftermerge.detector.rules import SLO, Amplification, Detection, ErrorRate, evaluate
from aftermerge.detector.stats import compare
from aftermerge.streaming.decode import SpanRecord

DEFAULT_MAX_SAMPLES = 2000


@dataclass
class VersionWindow:
    """What has been seen recently for one deployed version."""

    version: str
    max_samples: int = DEFAULT_MAX_SAMPLES
    first_seen: float = field(default_factory=time.monotonic)
    #: Ordering uses this, not first_seen. A version that appeared early in the
    #: topic stays "earliest" forever, so first_seen names the wrong candidate the
    #: moment a version is redeployed or rolled back to -- which is exactly when
    #: a streaming verdict matters most.
    last_seen: float = field(default_factory=time.monotonic)
    durations: deque[float] = field(default_factory=deque)
    errored: deque[bool] = field(default_factory=deque)
    #: One entry per database span, holding its trace id. Distinct traces come
    #: from the set of this deque, which keeps "spans per request" exact without
    #: an unbounded set.
    db_trace_ids: deque[str] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.durations = deque(self.durations, maxlen=self.max_samples)
        self.errored = deque(self.errored, maxlen=self.max_samples)
        self.db_trace_ids = deque(self.db_trace_ids, maxlen=self.max_samples * 60)

    @property
    def requests(self) -> int:
        return len(self.durations)

    @property
    def error_rate(self) -> float:
        return (sum(self.errored) / len(self.errored)) if self.errored else 0.0

    @property
    def db_spans_per_request(self) -> float:
        distinct = len(set(self.db_trace_ids))
        return len(self.db_trace_ids) / distinct if distinct else 0.0


class StreamState:
    """Windows per version for one service and route."""

    def __init__(
        self,
        *,
        service: str,
        route_service: str,
        route: str,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> None:
        self.service = service
        self.route_service = route_service
        self.route = route
        self.max_samples = max_samples
        self.windows: dict[str, VersionWindow] = {}

    def _window(self, version: str) -> VersionWindow:
        if version not in self.windows:
            self.windows[version] = VersionWindow(version=version, max_samples=self.max_samples)
        window = self.windows[version]
        window.last_seen = time.monotonic()
        return window

    def observe(self, span: SpanRecord) -> None:
        if not span.version:
            return

        if span.service == self.route_service and span.kind == "Server" and span.name == self.route:
            window = self._window(span.version)
            window.durations.append(span.duration_ms)
            window.errored.append(span.is_error)

        # Driver-internal work carries no application frame, and counting it
        # would put the pool's reset query into the per-request figure.
        if span.service == self.service and span.kind == "Client" and span.code_site:
            self._window(span.version).db_trace_ids.append(span.trace_id)

    def versions_by_recency(self) -> list[str]:
        """Least recently active first, so the candidate is the live version."""
        return sorted(self.windows, key=lambda v: self.windows[v].last_seen)

    def compare(self, slo: SLO, *, min_samples: int) -> tuple[str, str, Detection] | None:
        """Evaluate the newest version against the one before it.

        Returns None until two versions have been seen: a single version is a
        deploy that has not happened yet, not a regression.
        """
        versions = self.versions_by_recency()
        if len(versions) < 2:
            return None

        baseline_version, candidate_version = versions[-2], versions[-1]
        baseline, candidate = self.windows[baseline_version], self.windows[candidate_version]

        comparison = compare(
            list(baseline.durations), list(candidate.durations), min_samples=min_samples
        )
        amplification = Amplification(
            baseline_per_request=baseline.db_spans_per_request,
            candidate_per_request=candidate.db_spans_per_request,
        )
        errors = ErrorRate(baseline=baseline.error_rate, candidate=candidate.error_rate)

        detection = evaluate(comparison, slo, amplification=amplification, error_rate=errors)
        return baseline_version, candidate_version, detection
