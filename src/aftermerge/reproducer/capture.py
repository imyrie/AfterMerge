"""Reconstructing replayable requests from telemetry.

Captured from spans rather than from application middleware, which keeps
AfterMerge read-only: no redeploy, no code in the request path, and it works on
traffic that has already happened -- including the commits pinned in a scenario,
which were built long before capture existed.

Two consequences follow, and both are stated rather than hidden:

* Spans carry no request body, so a mutating request can be recorded but not
  faithfully reproduced. It is stored `replay_safe = False` with a reason.
* Spans also carry no credential headers, because OpenTelemetry does not record
  them by default. Capturing from telemetry is therefore *safer* than a
  middleware that sees the real headers and has to be trusted to drop them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from aftermerge.reproducer.envelope import SAFE_METHODS, RequestEnvelope

DEFAULT_MAX_SHAPES = 20


@dataclass(frozen=True)
class CaptureCandidate:
    envelope: RequestEnvelope
    observations: int
    status_code: int | None
    max_duration_ms: float | None
    unreplayable_reason: str | None

    @property
    def replayable(self) -> bool:
        return self.unreplayable_reason is None


def _split_target(url: str, target: str) -> tuple[str, dict[str, str]]:
    """Recover path and query.

    `http.target` is preferred for the path but the FastAPI instrumentation
    records it without the query string, so `http.url` is the only attribute
    that preserves parameters. Replaying `/orders` when production served
    `/orders?limit=50` would silently change the workload under test.
    """
    parsed = urlparse(url) if url else None
    query = dict(parse_qsl(parsed.query)) if parsed and parsed.query else {}

    path = (target.split("?", 1)[0] if target else "") or (parsed.path if parsed else "")
    if not path.startswith("/"):
        path = f"/{path}" if path else "/"

    if not query and target and "?" in target:
        query = dict(parse_qsl(target.split("?", 1)[1]))
    return path, query


def candidate_from_row(row: dict[str, Any]) -> CaptureCandidate:
    """Turn one exemplar-request row into a capture candidate."""
    method = str(row.get("method") or "GET").upper()
    path, query = _split_target(str(row.get("url") or ""), str(row.get("target") or ""))

    user_agent = str(row.get("user_agent") or "")
    envelope = RequestEnvelope.sanitised(
        method=method,
        path=path,
        query=query,
        headers={"user-agent": user_agent} if user_agent else {},
        source_trace_id=str(row.get("exemplar_trace_id") or "") or None,
    )

    reason: str | None = None
    if method not in SAFE_METHODS:
        reason = (
            f"{method} requests carry a body that spans do not record, so this cannot be "
            "replayed faithfully from telemetry alone"
        )

    return CaptureCandidate(
        envelope=envelope,
        observations=int(row.get("observations") or 0),
        status_code=int(row["status_code"]) if row.get("status_code") else None,
        max_duration_ms=float(row["max_ms"]) if row.get("max_ms") is not None else None,
        unreplayable_reason=reason,
    )


def candidates_from_result(
    columns: list[str], rows: list[tuple[Any, ...]]
) -> list[CaptureCandidate]:
    return [candidate_from_row(dict(zip(columns, row, strict=True))) for row in rows]


def capture_for_incident(
    session: Any,
    incident: Any,
    *,
    ch: Any,
    route_service: str,
    lookback_minutes: int = 120,
    max_shapes: int = DEFAULT_MAX_SHAPES,
) -> list[Any]:
    """Capture the distinct request shapes the regressed version served.

    Scoped to the candidate version: the point is to reproduce what the *broken*
    deploy was actually being asked to do.
    """
    from aftermerge.store.repositories import CapturedRequestRepository
    from aftermerge.telemetry import client

    result = client.run(
        "exemplar_requests",
        client=ch,
        service=route_service,
        version=incident.candidate_version,
        lookback_minutes=lookback_minutes,
        max_shapes=max_shapes,
    )
    candidates = candidates_from_result(result.columns, list(result.rows))

    repo = CapturedRequestRepository(session)
    return [
        repo.record(
            envelope=candidate.envelope,
            incident_id=incident.id,
            observations=candidate.observations,
            status_code=candidate.status_code,
            max_duration_ms=candidate.max_duration_ms,
            unreplayable_reason=candidate.unreplayable_reason,
        )
        for candidate in candidates
    ]
