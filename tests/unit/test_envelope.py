"""Request envelopes must be safe by construction."""

from __future__ import annotations

import pytest

from aftermerge.reproducer.envelope import RequestEnvelope, UnsafeRequest


def test_credential_headers_are_refused_outright() -> None:
    """Replayed credentials are still credentials, sitting in a database and a log."""
    for header in ("Authorization", "Cookie", "X-API-Key"):
        with pytest.raises(UnsafeRequest, match="credential headers"):
            RequestEnvelope(method="GET", path="/orders", headers={header: "secret"})


def test_sanitised_keeps_only_allowlisted_headers() -> None:
    """Allowlist, not denylist: a header invented next year must not sail through."""
    envelope = RequestEnvelope.sanitised(
        method="get",
        path="/orders",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer hunter2",
            "X-Internal-Tenant": "acme",
        },
    )

    assert envelope.headers == {"accept": "application/json"}
    assert "authorization" not in envelope.headers
    assert "x-internal-tenant" not in envelope.headers


def test_method_is_normalised() -> None:
    assert RequestEnvelope.sanitised(method="post", path="/orders").method == "POST"


def test_relative_paths_are_rejected() -> None:
    with pytest.raises(UnsafeRequest, match="absolute"):
        RequestEnvelope(method="GET", path="orders")


def test_mutating_requests_are_identifiable() -> None:
    """Replay must be able to treat these differently; it cannot if it cannot see them."""
    assert RequestEnvelope.sanitised(method="POST", path="/orders").is_mutating
    assert RequestEnvelope.sanitised(method="DELETE", path="/orders").is_mutating
    assert not RequestEnvelope.get("/orders").is_mutating


def test_replay_safe_defaults_to_false() -> None:
    """An envelope nobody sanitised is not replayable by default."""
    assert RequestEnvelope(method="GET", path="/orders").replay_safe is False
    assert RequestEnvelope.sanitised(method="GET", path="/orders").replay_safe is True


def test_target_renders_the_query_string() -> None:
    assert RequestEnvelope.get("/orders", limit=50).target == "/orders?limit=50"
    assert RequestEnvelope.get("/orders").target == "/orders"
