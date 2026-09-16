"""Distribution comparison for regression detection.

Everything here is deterministic and free of language models. Two ideas matter:

1. **Significance is not sufficiency.** With thousands of requests, a 3%
   slowdown is statistically significant and operationally irrelevant. The
   detector therefore requires both a significant result *and* a material effect
   size before it will call something a regression.

2. **Too little data is not the same as no regression.** Below the sample
   threshold the comparison reports `sufficient_data=False` rather than a
   negative result, so the caller cannot mistake silence for an all-clear.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

DEFAULT_MIN_SAMPLES = 100


@dataclass(frozen=True)
class Comparison:
    baseline_n: int
    candidate_n: int
    baseline_p95_ms: float
    candidate_p95_ms: float
    baseline_median_ms: float
    candidate_median_ms: float
    ratio: float
    p_value: float
    effect_size: float
    sufficient_data: bool
    min_samples: int

    @property
    def degraded(self) -> bool:
        return self.ratio > 1.0


def _percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q)) if values else math.nan


def compare(
    baseline: Sequence[float],
    candidate: Sequence[float],
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> Comparison:
    """Compare two sets of latency samples.

    Uses Mann-Whitney U rather than a t-test: latency distributions are heavily
    right-skewed and nowhere near normal, so a test that assumes normality would
    be answering a different question than the one asked.

    Effect size is the rank-biserial correlation, in [-1, 1]. Positive means the
    candidate is slower.
    """
    baseline_n, candidate_n = len(baseline), len(candidate)
    baseline_p95 = _percentile(baseline, 95)
    candidate_p95 = _percentile(candidate, 95)
    baseline_med = _percentile(baseline, 50)
    candidate_med = _percentile(candidate, 50)

    ratio = candidate_p95 / baseline_p95 if baseline_p95 else math.nan
    sufficient = baseline_n >= min_samples and candidate_n >= min_samples

    p_value, effect = math.nan, math.nan
    if sufficient:
        try:
            result = stats.mannwhitneyu(candidate, baseline, alternative="greater")
            p_value = float(result.pvalue)
            # rank-biserial correlation from U
            effect = float(2.0 * result.statistic / (candidate_n * baseline_n) - 1.0)
        except ValueError:
            # Raised when every observation is identical; there is no rank
            # information to test, which is a real answer rather than an error.
            p_value, effect = 1.0, 0.0

    return Comparison(
        baseline_n=baseline_n,
        candidate_n=candidate_n,
        baseline_p95_ms=baseline_p95,
        candidate_p95_ms=candidate_p95,
        baseline_median_ms=baseline_med,
        candidate_median_ms=candidate_med,
        ratio=ratio,
        p_value=p_value,
        effect_size=effect,
        sufficient_data=sufficient,
        min_samples=min_samples,
    )
