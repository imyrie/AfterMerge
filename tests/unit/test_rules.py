"""Trigger logic: significance alone must never be enough."""

from __future__ import annotations

from aftermerge.detector.rules import SLO, Amplification, evaluate
from aftermerge.detector.stats import Comparison

SLO_500 = SLO(route="GET /orders", p95_ms=500.0)


def comparison(
    *,
    ratio: float,
    p_value: float,
    candidate_p95: float,
    sufficient: bool = True,
    baseline_n: int = 500,
    candidate_n: int = 500,
) -> Comparison:
    return Comparison(
        baseline_n=baseline_n,
        candidate_n=candidate_n,
        baseline_p95_ms=candidate_p95 / ratio if ratio else 0.0,
        candidate_p95_ms=candidate_p95,
        baseline_median_ms=0.0,
        candidate_median_ms=0.0,
        ratio=ratio,
        p_value=p_value,
        effect_size=0.5,
        sufficient_data=sufficient,
        min_samples=100,
    )


def test_insufficient_data_is_not_an_all_clear() -> None:
    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=100, sufficient=False), SLO_500
    )

    assert result.triggered is False
    assert result.insufficient_data is True
    assert result.headline == "insufficient data to decide"
    assert result.severity is None


def test_significant_but_trivial_change_does_not_trigger() -> None:
    """At high volume a 3% slowdown is significant and irrelevant.

    Paging on this is the noise failure mode the project exists to avoid.
    """
    result = evaluate(comparison(ratio=1.03, p_value=1e-12, candidate_p95=103), SLO_500)

    assert result.triggered is False
    assert result.insufficient_data is False


def test_large_change_without_significance_does_not_trigger() -> None:
    result = evaluate(comparison(ratio=3.0, p_value=0.4, candidate_p95=300), SLO_500)
    assert result.triggered is False


def test_significant_and_material_triggers() -> None:
    result = evaluate(comparison(ratio=2.0, p_value=1e-9, candidate_p95=300), SLO_500)

    assert result.triggered is True
    assert result.severity == "major"
    assert any("Mann-Whitney" in r for r in result.reasons)


def test_slo_breach_triggers_even_without_a_ratio_change() -> None:
    """A service that was always too slow is still in breach."""
    result = evaluate(comparison(ratio=1.0, p_value=1.0, candidate_p95=900), SLO_500)

    assert result.triggered is True
    assert any("breaches SLO" in r for r in result.reasons)


def test_severity_ladder() -> None:
    minor = evaluate(comparison(ratio=1.6, p_value=1e-9, candidate_p95=160), SLO_500)
    major = evaluate(comparison(ratio=2.5, p_value=1e-9, candidate_p95=250), SLO_500)
    critical = evaluate(comparison(ratio=11.0, p_value=1e-9, candidate_p95=1700), SLO_500)

    assert minor.severity == "minor"
    assert major.severity == "major"
    assert critical.severity == "critical"


def test_every_verdict_gives_a_reason() -> None:
    for c in (
        comparison(ratio=1.0, p_value=1.0, candidate_p95=100),
        comparison(ratio=9.0, p_value=1e-20, candidate_p95=900),
        comparison(ratio=1.0, p_value=1.0, candidate_p95=100, sufficient=False),
    ):
        assert evaluate(c, SLO_500).reasons, "a verdict with no stated reason is unusable"


# --- work amplification ------------------------------------------------------
#
# Regression test for a real miss: against a warm database the N+1 fixture was
# only 1.45x slower (it was 11.2x against a cold one) and a latency-only
# detector reported "no regression" while every request made 51 database round
# trips instead of 2.


def test_work_amplification_triggers_when_latency_does_not() -> None:
    flat_latency = comparison(ratio=1.45, p_value=0.0, candidate_p95=50.8)
    amp = Amplification(baseline_per_request=2.0, candidate_per_request=51.0)

    assert evaluate(flat_latency, SLO_500).triggered is False
    result = evaluate(flat_latency, SLO_500, amplification=amp)

    assert result.triggered is True
    assert result.severity == "critical"
    assert any("25.5x" in r for r in result.reasons)


def test_amplification_reports_the_responsible_code_site() -> None:
    amp = Amplification(2.0, 51.0, code_site="orders/repository.py")
    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=50), SLO_500, amplification=amp
    )

    assert any("orders/repository.py" in r for r in result.reasons)


def test_unchanged_work_does_not_trigger() -> None:
    amp = Amplification(baseline_per_request=2.0, candidate_per_request=2.0)
    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=50), SLO_500, amplification=amp
    )

    assert result.triggered is False
    assert any("unchanged" in r for r in result.reasons)


def test_modest_amplification_is_below_threshold() -> None:
    amp = Amplification(baseline_per_request=2.0, candidate_per_request=4.0)  # 2x, under 3x
    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=50), SLO_500, amplification=amp
    )
    assert result.triggered is False


def test_amplification_triggers_despite_thin_latency_samples() -> None:
    """Work amplification is a ratio of counts and does not need the sample floor."""
    thin = comparison(ratio=1.0, p_value=1.0, candidate_p95=50, sufficient=False)
    amp = Amplification(baseline_per_request=2.0, candidate_per_request=51.0)
    result = evaluate(thin, SLO_500, amplification=amp)

    assert result.triggered is True
    assert result.insufficient_data is False


def test_zero_baseline_work_does_not_divide_by_zero() -> None:
    amp = Amplification(baseline_per_request=0.0, candidate_per_request=51.0)
    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=50), SLO_500, amplification=amp
    )
    assert result.triggered is False


# --- error rate --------------------------------------------------------------
#
# SLO.max_error_rate was declared from the start and read by nothing. Scenario
# 002 failed 62% of its requests and the only thing that noticed was latency.


def test_a_breached_error_budget_triggers() -> None:
    from aftermerge.detector.rules import ErrorRate

    healthy_latency = comparison(ratio=1.0, p_value=1.0, candidate_p95=100)
    result = evaluate(healthy_latency, SLO_500, error_rate=ErrorRate(baseline=0.0, candidate=0.62))

    assert result.triggered
    assert any("62.0% of requests are failing" in r for r in result.reasons)


def test_errors_within_budget_do_not_trigger() -> None:
    from aftermerge.detector.rules import ErrorRate

    result = evaluate(
        comparison(ratio=1.0, p_value=1.0, candidate_p95=100),
        SLO_500,
        error_rate=ErrorRate(baseline=0.002, candidate=0.005),
    )
    assert not result.triggered


def test_errors_trigger_even_with_thin_latency_samples() -> None:
    """A service that is failing does not need a significance test."""
    from aftermerge.detector.rules import ErrorRate

    thin = comparison(ratio=1.0, p_value=1.0, candidate_p95=100, sufficient=False)
    result = evaluate(thin, SLO_500, error_rate=ErrorRate(baseline=0.0, candidate=0.62))

    assert result.triggered
    assert result.insufficient_data is False


def test_failing_requests_raise_severity() -> None:
    from aftermerge.detector.rules import ErrorRate

    result = evaluate(
        comparison(ratio=1.1, p_value=1.0, candidate_p95=100),
        SLO_500,
        error_rate=ErrorRate(baseline=0.0, candidate=0.62),
    )
    assert result.severity == "major"
