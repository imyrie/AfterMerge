"""Gathering level-1 evidence for an incident.

One declared set of queries, run against the incident window, each persisted as
a Fact carrying the statement and parameters that produced it. Keeping the set
declarative means an incident's evidence is enumerable and reproducible rather
than whatever the code happened to ask for that day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from aftermerge.store.repositories import FactRepository
from aftermerge.store.tables import Fact, Incident
from aftermerge.telemetry import client


@dataclass(frozen=True)
class EvidenceSpec:
    """One query to run, and how to reduce it to a headline number."""

    kind: str
    query_name: str
    params: dict[str, Any] = field(default_factory=dict)
    value_column: str | None = None
    unit: str | None = None
    #: How to reduce the candidate's rows to one headline number:
    #: "first" (already-aggregated queries), "sum" (rows are per code site or
    #: per operation), or "p95" (rows are raw per-request samples).
    aggregate: str = "first"


def _headline(result: client.FactResult, version: str, spec: EvidenceSpec) -> float | None:
    if spec.value_column is None or spec.value_column not in result.columns:
        return None
    if "version" not in result.columns:
        return None

    v_idx = result.columns.index("version")
    c_idx = result.columns.index(spec.value_column)
    values = [float(row[c_idx]) for row in result.rows if row[v_idx] == version]

    if not values:
        return None
    if spec.aggregate == "sum":
        return sum(values)
    if spec.aggregate == "p95":
        # Raw samples: a single row is an arbitrary request, not a summary.
        return float(np.percentile(np.asarray(values, dtype=float), 95))
    return values[0]


def specs_for(
    service: str, route_service: str, route: str, lookback_minutes: int
) -> list[EvidenceSpec]:
    common = {"lookback_minutes": lookback_minutes}
    return [
        EvidenceSpec(
            kind="route_latency_quantiles",
            query_name="latency_quantiles",
            params={"service": route_service, "route": route, **common},
            value_column="p95_ms",
            unit="ms",
        ),
        EvidenceSpec(
            kind="db_spans_per_request",
            query_name="span_count_per_trace",
            params={"service": service, **common},
            value_column="spans_per_request",
            unit="spans",
            # Rows are per (version, code_site); the headline is the total.
            aggregate="sum",
        ),
        EvidenceSpec(
            kind="dependency_attribution",
            query_name="dependency_attribution",
            params={"service": service, **common},
            value_column="total_ms_per_request",
            unit="ms",
            aggregate="sum",
        ),
    ]


def gather(
    session: Session,
    incident: Incident,
    *,
    ch: Any,
    route_service: str,
    lookback_minutes: int = 120,
    extra: list[tuple[EvidenceSpec, client.FactResult]] | None = None,
) -> list[Fact]:
    """Run the evidence set and persist each result as a Fact."""
    facts = FactRepository(session)
    recorded: list[Fact] = []

    for spec in specs_for(incident.service, route_service, incident.route, lookback_minutes):
        result = client.run(spec.query_name, client=ch, **spec.params)
        recorded.append(
            facts.record(
                incident_id=incident.id,
                kind=spec.kind,
                result=result,
                value=_headline(result, incident.candidate_version, spec),
                unit=spec.unit,
            )
        )

    # Results the caller already fetched (the detector's duration samples), so
    # they are not queried twice.
    for spec, result in extra or []:
        recorded.append(
            facts.record(
                incident_id=incident.id,
                kind=spec.kind,
                result=result,
                value=_headline(result, incident.candidate_version, spec),
                unit=spec.unit,
            )
        )

    return recorded
