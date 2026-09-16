"""Turning a statistical comparison into a decision.

Deliberately boring and fully deterministic: given the same comparison and the
same SLO, this always returns the same verdict, and every reason it gives names
a number that can be checked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aftermerge.detector.stats import Comparison

DEFAULT_ALPHA = 0.01
DEFAULT_MIN_RATIO = 1.5
DEFAULT_MIN_AMPLIFICATION = 3.0


@dataclass(frozen=True)
class SLO:
    route: str
    p95_ms: float
    max_error_rate: float = 0.01


@dataclass(frozen=True)
class Amplification:
    """Change in units of work per request, independent of how fast that work ran.

    Latency is environment-dependent: the same N+1 measured 11.2x slower against
    a cold database and 1.45x against a warm one. The count of database round
    trips was 51 per request in both cases.

    Work amplification is therefore the sturdier signal, and a detector watching
    only latency will miss real regressions whenever the infrastructure happens
    to be fast enough to absorb them.
    """

    baseline_per_request: float
    candidate_per_request: float
    code_site: str | None = None

    @property
    def ratio(self) -> float:
        if not self.baseline_per_request:
            return math.nan
        return self.candidate_per_request / self.baseline_per_request


@dataclass(frozen=True)
class Detection:
    triggered: bool
    insufficient_data: bool
    severity: str | None
    reasons: tuple[str, ...]
    comparison: Comparison
    amplification: Amplification | None = None

    @property
    def headline(self) -> str:
        if self.insufficient_data:
            return "insufficient data to decide"
        if not self.triggered:
            return "no regression detected"
        return f"{self.severity} regression detected"


def _severity(ratio: float, slo_breached: bool, amplification_ratio: float) -> str:
    """Severity reflects the worst of the signals, not only latency."""
    worst = max(
        ratio if not math.isnan(ratio) else 0.0,
        amplification_ratio if not math.isnan(amplification_ratio) else 0.0,
    )
    if worst >= 5.0 or (slo_breached and worst >= 3.0):
        return "critical"
    if worst >= 2.0 or slo_breached:
        return "major"
    return "minor"


def evaluate(
    comparison: Comparison,
    slo: SLO,
    *,
    amplification: Amplification | None = None,
    alpha: float = DEFAULT_ALPHA,
    min_ratio: float = DEFAULT_MIN_RATIO,
    min_amplification: float = DEFAULT_MIN_AMPLIFICATION,
) -> Detection:
    """Decide whether a comparison constitutes a regression.

    Requires significance *and* a material effect. At high request volumes a
    trivial slowdown is statistically significant, so p-value alone would page
    on noise -- exactly the failure mode this project is meant to avoid.
    """
    amp_ratio = amplification.ratio if amplification else math.nan
    amplified = not math.isnan(amp_ratio) and amp_ratio >= min_amplification

    if not comparison.sufficient_data and not amplified:
        return Detection(
            triggered=False,
            insufficient_data=True,
            severity=None,
            reasons=(
                f"need {comparison.min_samples} samples per side, have "
                f"{comparison.baseline_n} baseline / {comparison.candidate_n} candidate",
            ),
            comparison=comparison,
            amplification=amplification,
        )

    significant = comparison.sufficient_data and comparison.p_value < alpha
    material = comparison.ratio >= min_ratio
    slo_breached = comparison.candidate_p95_ms > slo.p95_ms

    reasons: list[str] = []
    if amplified and amplification is not None:
        where = f" in {amplification.code_site}" if amplification.code_site else ""
        reasons.append(
            f"database work per request rose {amp_ratio:.1f}x "
            f"({amplification.baseline_per_request:.1f} -> "
            f"{amplification.candidate_per_request:.1f} spans){where}"
        )
    if significant:
        reasons.append(f"candidate is slower (Mann-Whitney p={comparison.p_value:.2e} < {alpha})")
    if material:
        reasons.append(
            f"p95 rose {comparison.ratio:.1f}x "
            f"({comparison.baseline_p95_ms:.0f}ms -> {comparison.candidate_p95_ms:.0f}ms)"
        )
    if slo_breached:
        reasons.append(
            f"p95 {comparison.candidate_p95_ms:.0f}ms breaches SLO of {slo.p95_ms:.0f}ms"
        )

    # Any one signal is enough. Work amplification is deliberately independent
    # of the latency test, because that is precisely the case latency misses.
    triggered = (significant and material) or slo_breached or amplified

    if not triggered and not reasons:
        reasons.append(
            f"p95 ratio {comparison.ratio:.2f}x is below the {min_ratio}x threshold and within SLO"
        )
    if not triggered and not math.isnan(amp_ratio):
        reasons.append(f"database work per request unchanged ({amp_ratio:.2f}x)")

    return Detection(
        triggered=triggered,
        insufficient_data=False,
        severity=_severity(comparison.ratio, slo_breached, amp_ratio) if triggered else None,
        reasons=tuple(reasons),
        comparison=comparison,
        amplification=amplification,
    )
