"""Run a detection and persist what it observed.

Order matters here. The comparison is computed first and the incident is opened
only if the rules fire, so the audit trail never fills with non-incidents. Facts
are then recorded against that incident, each carrying the query and parameters
that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from aftermerge.detector import windows
from aftermerge.detector.rules import SLO, Amplification, Detection, ErrorRate, evaluate
from aftermerge.detector.stats import DEFAULT_MIN_SAMPLES, Comparison, compare
from aftermerge.investigator import evidence
from aftermerge.store.repositories import IncidentRepository
from aftermerge.store.tables import Incident
from aftermerge.telemetry import client

DEFAULT_MAX_SAMPLES = 5000


@dataclass
class DetectionOutcome:
    window: windows.ComparisonWindow
    detection: Detection
    incident: Incident | None
    fact_count: int


def _split_samples(
    result: client.FactResult, baseline: str, candidate: str
) -> tuple[list[float], list[float]]:
    """Separate duration samples by version.

    An empty result has no columns at all, so indexing into them raises
    `ValueError: 'version' is not in list` -- which reads like a schema bug when
    the truth is simply that no telemetry fell inside the lookback window.
    Returning empty samples lets the comparison report `insufficient_data`,
    which is the honest answer.
    """
    if not {"version", "duration_ms"} <= set(result.columns):
        return [], []

    version_idx = result.columns.index("version")
    duration_idx = result.columns.index("duration_ms")
    base: list[float] = []
    cand: list[float] = []
    for row in result.rows:
        if row[version_idx] == baseline:
            base.append(float(row[duration_idx]))
        elif row[version_idx] == candidate:
            cand.append(float(row[duration_idx]))
    return base, cand


def _amplification(
    result: client.FactResult, baseline: str, candidate: str
) -> Amplification | None:
    """Total database spans per request on each side, and where they come from.

    Rows are grouped by (version, code_site), so totals are summed per version.
    The reported code site is the one contributing the largest increase, which is
    the file a human should look at first.
    """
    if "version" not in result.columns:
        return None
    v_idx = result.columns.index("version")
    site_idx = result.columns.index("code_site")
    per_idx = result.columns.index("spans_per_request")

    totals = {baseline: 0.0, candidate: 0.0}
    by_site: dict[str, dict[str, float]] = {}
    for row in result.rows:
        version = str(row[v_idx])
        if version not in totals:
            continue
        value = float(row[per_idx])
        totals[version] += value
        by_site.setdefault(str(row[site_idx]), {baseline: 0.0, candidate: 0.0})[version] += value

    if not totals[baseline] and not totals[candidate]:
        return None

    worst_site = max(
        by_site,
        key=lambda site: by_site[site][candidate] - by_site[site][baseline],
        default=None,
    )
    return Amplification(
        baseline_per_request=totals[baseline],
        candidate_per_request=totals[candidate],
        code_site=worst_site,
    )


def _error_rate(result: client.FactResult, baseline: str, candidate: str) -> ErrorRate | None:
    """Failed requests as a fraction of the total, for each version."""
    if not {"version", "requests", "errors"} <= set(result.columns):
        return None
    v_idx = result.columns.index("version")
    r_idx = result.columns.index("requests")
    e_idx = result.columns.index("errors")

    rates: dict[str, float] = {}
    for row in result.rows:
        requests = float(row[r_idx])
        rates[str(row[v_idx])] = float(row[e_idx]) / requests if requests else 0.0

    if baseline not in rates and candidate not in rates:
        return None
    return ErrorRate(baseline=rates.get(baseline, 0.0), candidate=rates.get(candidate, 0.0))


def _scalar_for(result: client.FactResult, version: str, column: str) -> float | None:
    """Pull one column's value for one version out of a per-version result."""
    if "version" not in result.columns or column not in result.columns:
        return None
    v_idx = result.columns.index("version")
    c_idx = result.columns.index(column)
    for row in result.rows:
        if row[v_idx] == version:
            return float(row[c_idx])
    return None


def detect(
    session: Session,
    *,
    service: str,
    route_service: str,
    slo: SLO,
    lookback_minutes: int = 120,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    ch: Any | None = None,
) -> DetectionOutcome:
    """Compare the two most recently deployed versions of `service`."""
    window = windows.from_latest_deployment(session, service)
    ch = ch or client.get_client()

    common = {
        "baseline": window.baseline_version,
        "candidate": window.candidate_version,
        "lookback_minutes": lookback_minutes,
    }

    durations = client.run(
        "route_durations",
        client=ch,
        service=route_service,
        route=slo.route,
        max_samples=max_samples,
        **common,
    )
    baseline_samples, candidate_samples = _split_samples(
        durations, window.baseline_version, window.candidate_version
    )
    comparison: Comparison = compare(baseline_samples, candidate_samples, min_samples=min_samples)

    # Both fetched before the decision. Work amplification and error rate are
    # inputs to the verdict, not evidence gathered after one has been reached.
    spans = client.run(
        "span_count_per_trace", client=ch, service=service, lookback_minutes=lookback_minutes
    )
    quantiles = client.run(
        "latency_quantiles",
        client=ch,
        service=route_service,
        route=slo.route,
        lookback_minutes=lookback_minutes,
    )
    amplification = _amplification(spans, window.baseline_version, window.candidate_version)
    errors = _error_rate(quantiles, window.baseline_version, window.candidate_version)
    detection = evaluate(comparison, slo, amplification=amplification, error_rate=errors)

    if not detection.triggered:
        return DetectionOutcome(window=window, detection=detection, incident=None, fact_count=0)

    incident = IncidentRepository(session).open(
        service=service,
        route=slo.route,
        baseline_version=window.baseline_version,
        candidate_version=window.candidate_version,
        severity=detection.severity or "minor",
        summary="; ".join(detection.reasons),
        onset_at=window.onset_at,
        deployment_id=window.deployment_id,
    )

    # The duration samples are passed through rather than re-queried: they are
    # the observation the verdict was computed from, so the incident should cite
    # exactly those rows.
    facts = evidence.gather(
        session,
        incident,
        ch=ch,
        route_service=route_service,
        lookback_minutes=lookback_minutes,
        extra=[
            (
                evidence.EvidenceSpec(
                    kind="route_duration_samples",
                    query_name="route_durations",
                    value_column="duration_ms",
                    unit="ms p95",
                    aggregate="p95",
                ),
                durations,
            )
        ],
    )

    return DetectionOutcome(
        window=window, detection=detection, incident=incident, fact_count=len(facts)
    )
