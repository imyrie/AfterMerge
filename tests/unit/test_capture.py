"""Reconstructing requests from span attributes."""

from __future__ import annotations

from aftermerge.reproducer.capture import _split_target, candidate_from_row

# The FastAPI instrumentation records http.target WITHOUT the query string, so a
# capture that trusted it alone would replay /orders when production served
# /orders?limit=50 -- quietly changing the workload under test.
ROW = {
    "method": "GET",
    "url": "http://gateway:8000/orders?limit=50",
    "target": "/orders",
    "route": "/orders",
    "user_agent": "Grafana k6/2.2.0",
    "status_code": 200,
    "observations": 1801,
    "exemplar_trace_id": "abc123",
    "max_ms": 2141.4,
}


def test_query_is_recovered_from_url_when_target_omits_it() -> None:
    path, query = _split_target("http://gateway:8000/orders?limit=50", "/orders")
    assert path == "/orders"
    assert query == {"limit": "50"}


def test_query_is_read_from_target_when_url_is_missing() -> None:
    path, query = _split_target("", "/orders?limit=50")
    assert path == "/orders"
    assert query == {"limit": "50"}


def test_path_only_request_has_no_query() -> None:
    assert _split_target("http://gateway:8000/orders", "/orders") == ("/orders", {})


def test_missing_attributes_still_yield_an_absolute_path() -> None:
    path, query = _split_target("", "")
    assert path == "/"
    assert query == {}


def test_a_get_request_is_replayable() -> None:
    candidate = candidate_from_row(ROW)

    assert candidate.replayable
    assert candidate.envelope.method == "GET"
    assert candidate.envelope.target == "/orders?limit=50"
    assert candidate.envelope.replay_safe is True
    assert candidate.observations == 1801
    assert candidate.envelope.source_trace_id == "abc123"


def test_mutating_requests_are_captured_but_flagged_unreplayable() -> None:
    """Spans carry no body, so a POST cannot be faithfully reproduced.

    Recording it with a stated reason is more useful than dropping it: "we saw
    this and cannot replay it" is information.
    """
    candidate = candidate_from_row({**ROW, "method": "POST"})

    assert candidate.replayable is False
    assert "body" in (candidate.unreplayable_reason or "")
    assert candidate.envelope.method == "POST"


def test_only_allowlisted_headers_survive_capture() -> None:
    candidate = candidate_from_row(ROW)
    assert candidate.envelope.headers == {"user-agent": "Grafana k6/2.2.0"}
