"""Distribution comparison behaviour."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aftermerge.detector.stats import compare

RNG = np.random.default_rng(20260916)


def lognormal(n: int, mean: float = 3.0, sigma: float = 0.5) -> list[float]:
    """Latency-shaped samples: right-skewed, strictly positive."""
    return list(RNG.lognormal(mean=mean, sigma=sigma, size=n))


def test_identical_distributions_are_not_flagged() -> None:
    result = compare(lognormal(500), lognormal(500))
    assert result.sufficient_data
    assert result.p_value > 0.01
    assert 0.7 < result.ratio < 1.4


def test_a_clear_slowdown_is_detected() -> None:
    baseline = lognormal(500)
    candidate = [v * 10 for v in lognormal(500)]
    result = compare(baseline, candidate)

    assert result.sufficient_data
    assert result.p_value < 1e-10
    assert result.ratio > 5
    assert result.effect_size > 0.9


def test_too_few_samples_reports_insufficient_not_negative() -> None:
    """The critical distinction: 'could not tell' must not read as 'all clear'."""
    result = compare(lognormal(10), lognormal(10), min_samples=100)

    assert result.sufficient_data is False
    assert math.isnan(result.p_value), "no test should be reported when data is thin"


def test_threshold_is_per_side_not_combined() -> None:
    assert compare(lognormal(500), lognormal(10), min_samples=100).sufficient_data is False
    assert compare(lognormal(10), lognormal(500), min_samples=100).sufficient_data is False


def test_constant_samples_do_not_raise() -> None:
    """scipy raises on zero variance; that is a real answer, not a crash."""
    result = compare([100.0] * 200, [100.0] * 200)

    assert result.sufficient_data
    assert result.p_value == 1.0
    assert result.effect_size == 0.0
    assert result.ratio == pytest.approx(1.0)


def test_empty_input_yields_nan_rather_than_dividing_by_zero() -> None:
    result = compare([], [])
    assert result.sufficient_data is False
    assert math.isnan(result.baseline_p95_ms)
