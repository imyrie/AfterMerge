"""Decoding OTLP JSON and folding it into rolling windows."""

from __future__ import annotations

import json

from aftermerge.detector.rules import SLO
from aftermerge.streaming.decode import SpanRecord, decode
from aftermerge.streaming.windows import StreamState, VersionWindow

SLO_500 = SLO(route="GET /orders", p95_ms=500.0)


def message(spans: list[dict], service: str = "gateway", version: str = "v1") -> str:
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": service}},
                            {"key": "service.version", "value": {"stringValue": version}},
                        ]
                    },
                    "scopeSpans": [{"spans": spans}],
                }
            ]
        }
    )


def span(name="GET /orders", kind=2, ms=10.0, trace="t1", error=False, code_site=None) -> dict:
    attrs = [{"key": "code.file.path", "value": {"stringValue": code_site}}] if code_site else []
    return {
        "name": name,
        "kind": kind,
        "traceId": trace,
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": str(int(1000000000 + ms * 1e6)),
        "status": {"code": 2} if error else {},
        "attributes": attrs,
    }


# --- decoding ----------------------------------------------------------------


def test_a_server_span_decodes() -> None:
    (record,) = decode(message([span(ms=42.5)]))
    assert record.service == "gateway"
    assert record.version == "v1"
    assert record.kind == "Server"
    assert record.duration_ms == 42.5
    assert record.is_error is False


def test_span_kind_is_an_integer_enum_in_otlp_json() -> None:
    """The JSON encoding emits the number, not the name."""
    assert decode(message([span(kind=2)]))[0].kind == "Server"
    assert decode(message([span(kind=3)]))[0].kind == "Client"


def test_an_error_status_is_read() -> None:
    assert decode(message([span(error=True)]))[0].is_error is True


def test_code_site_is_lifted_from_attributes() -> None:
    record = decode(message([span(kind=3, code_site="orders/repository.py")]))[0]
    assert record.code_site == "orders/repository.py"
    assert decode(message([span()]))[0].code_site is None


def test_a_malformed_message_yields_nothing_rather_than_raising() -> None:
    """One bad payload must not stop an otherwise healthy consumer."""
    assert decode(b"not json") == []
    assert decode("{}") == []
    assert decode(json.dumps({"resourceSpans": None})) == []


# --- windows -----------------------------------------------------------------


def test_windows_are_bounded() -> None:
    """Unbounded state is a memory leak with a deploy-shaped trigger."""
    window = VersionWindow(version="v1", max_samples=10)
    for i in range(100):
        window.durations.append(float(i))
    assert len(window.durations) == 10


def test_spans_per_request_counts_distinct_traces() -> None:
    window = VersionWindow(version="v1")
    for trace in ("a", "a", "a", "b", "b", "b"):
        window.db_trace_ids.append(trace)
    assert window.db_spans_per_request == 3.0


def test_only_the_watched_route_feeds_the_latency_window() -> None:
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    state.observe(SpanRecord("gateway", "v1", "GET /orders", "Server", "t", 10.0, False, None))
    state.observe(SpanRecord("gateway", "v1", "GET /other", "Server", "t", 99.0, False, None))
    assert list(state.windows["v1"].durations) == [10.0]


def test_only_attributed_client_spans_count_as_database_work() -> None:
    """Driver-internal work carries no application frame and is not per-request work."""
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    state.observe(SpanRecord("orders", "v1", "SELECT", "Client", "t1", 1.0, False, "repo.py"))
    state.observe(SpanRecord("orders", "v1", "SELECT", "Client", "t1", 1.0, False, None))
    assert len(state.windows["v1"].db_trace_ids) == 1


def test_one_version_is_not_yet_a_comparison() -> None:
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    for _ in range(100):
        state.observe(SpanRecord("gateway", "v1", "GET /orders", "Server", "t", 10.0, False, None))
    assert state.compare(SLO_500, min_samples=10) is None


def test_work_amplification_is_caught_in_the_stream() -> None:
    """The same rules as the batch detector, applied to rolling windows."""
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    for version, db_per_request in (("good", 2), ("bad", 51)):
        for n in range(60):
            trace = f"{version}-{n}"
            state.observe(
                SpanRecord("gateway", version, "GET /orders", "Server", trace, 10.0, False, None)
            )
            for _ in range(db_per_request):
                state.observe(
                    SpanRecord("orders", version, "SELECT", "Client", trace, 1.0, False, "repo.py")
                )

    outcome = state.compare(SLO_500, min_samples=30)
    assert outcome is not None
    baseline, candidate, detection = outcome
    assert (baseline, candidate) == ("good", "bad")
    assert detection.triggered
    assert any("database work per request rose" in r for r in detection.reasons)


def test_the_newest_version_is_the_candidate() -> None:
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    state.observe(SpanRecord("gateway", "first", "GET /orders", "Server", "t", 1.0, False, None))
    state.observe(SpanRecord("gateway", "second", "GET /orders", "Server", "t", 1.0, False, None))
    assert state.versions_by_recency() == ["first", "second"]


def test_a_redeployed_version_becomes_the_candidate_again() -> None:
    """Ordering is by last activity, not first sighting.

    A topic replayed from the start sees the previously-live version first. With
    first-seen ordering it stays "newest" forever, so a rollback or a redeploy is
    compared the wrong way round -- naming the fix as the regression.
    """
    state = StreamState(service="orders", route_service="gateway", route="GET /orders")
    for version in ("old", "new", "old"):
        state.observe(
            SpanRecord("gateway", version, "GET /orders", "Server", "t", 1.0, False, None)
        )
    assert state.versions_by_recency()[-1] == "old"
