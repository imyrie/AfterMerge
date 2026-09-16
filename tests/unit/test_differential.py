"""Differential verdict logic."""

from __future__ import annotations

import pytest

from aftermerge.reproducer.differential import DifferentialResult
from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.replay import ReplayMeasurement, ReplayRefused, replay


def measurement(sha: str, spans: float, failures: int = 0) -> ReplayMeasurement:
    return ReplayMeasurement(
        sha=sha,
        requests_sent=20,
        failures=failures,
        db_spans_per_request=spans,
        code_site="orders/repository.py",
        trace_database=f"repro_{sha}",
    )


def result(good: float, bad: float, *, good_fail: int = 0, bad_fail: int = 0) -> DifferentialResult:
    return DifferentialResult(
        target="/orders?limit=50",
        good=measurement("cbb4790", good, good_fail),
        bad=measurement("8b4fd77", bad, bad_fail),
        threshold=3.0,
    )


def test_a_large_amplification_reproduces() -> None:
    r = result(2.0, 51.0)
    assert r.reproduced
    assert r.ratio == pytest.approx(25.5)
    assert "25.5x" in r.summary


def test_no_amplification_does_not_reproduce() -> None:
    r = result(2.0, 2.0)
    assert not r.reproduced
    assert "did not reproduce" in r.summary


def test_amplification_below_threshold_does_not_reproduce() -> None:
    r = result(2.0, 4.0)  # 2x, under the 3x threshold
    assert not r.reproduced


def test_failed_requests_invalidate_the_comparison() -> None:
    """A replay that errored proves nothing either way, so say so rather than guess."""
    r = result(2.0, 51.0, bad_fail=3)
    assert not r.reproduced
    assert "not trustworthy" in r.summary


def test_zero_baseline_work_does_not_divide_by_zero() -> None:
    assert result(0.0, 51.0).ratio == float("inf")
    assert result(0.0, 0.0).ratio == 1.0


def test_as_dict_is_json_safe() -> None:
    import json

    payload = json.loads(json.dumps(result(2.0, 51.0).as_dict()))
    assert payload["reproduced"] is True
    assert payload["good"]["db_spans_per_request"] == 2.0


def test_replay_refuses_an_unsafe_envelope() -> None:
    """Replay does not second-guess what capture flagged."""
    unsafe = RequestEnvelope(method="POST", path="/orders", replay_safe=False)
    with pytest.raises(ReplayRefused, match="not marked replay-safe"):
        replay(object(), unsafe)  # type: ignore[arg-type]
