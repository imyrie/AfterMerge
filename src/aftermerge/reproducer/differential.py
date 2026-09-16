"""Replaying the same request against two commits and comparing the result.

This is the step that turns "the diff correlates with the regression" into "the
diff causes it". Correlation is an inference from production telemetry; a
differential is an experiment with a control.

Sandboxes are brought up one at a time rather than together. Two full stacks
competing for CPU would distort exactly the measurement being taken.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.replay import ReplayMeasurement, replay
from aftermerge.reproducer.sandbox import sandbox

DEFAULT_REPEAT = 20
DEFAULT_THRESHOLD = 3.0


@dataclass(frozen=True)
class DifferentialResult:
    target: str
    good: ReplayMeasurement
    bad: ReplayMeasurement
    threshold: float

    @property
    def ratio(self) -> float:
        if not self.good.db_spans_per_request:
            return float("inf") if self.bad.db_spans_per_request else 1.0
        return self.bad.db_spans_per_request / self.good.db_spans_per_request

    @property
    def reproduced(self) -> bool:
        """Did the regression appear in isolation, away from production traffic?"""
        return self.good.clean and self.bad.clean and self.ratio >= self.threshold

    @property
    def summary(self) -> str:
        if self.reproduced:
            return (
                f"Replaying {self.target} against {self.good.sha} and {self.bad.sha} in isolation "
                f"produced {self.good.db_spans_per_request:.1f} vs "
                f"{self.bad.db_spans_per_request:.1f} database operations per request "
                f"({self.ratio:.1f}x)."
            )
        if not (self.good.clean and self.bad.clean):
            return (
                f"Replay did not complete cleanly "
                f"({self.good.failures} / {self.bad.failures} failed requests); "
                "the comparison is not trustworthy."
            )
        return (
            f"Replaying {self.target} produced {self.good.db_spans_per_request:.1f} vs "
            f"{self.bad.db_spans_per_request:.1f} operations per request ({self.ratio:.1f}x), "
            f"below the {self.threshold}x threshold. The regression did not reproduce."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "good": asdict(self.good),
            "bad": asdict(self.bad),
            "ratio": round(self.ratio, 2),
            "threshold": self.threshold,
            "reproduced": self.reproduced,
            "summary": self.summary,
        }


def run_differential(
    envelope: RequestEnvelope,
    *,
    good_ref: str,
    bad_ref: str,
    repo_root: Path,
    repeat: int = DEFAULT_REPEAT,
    threshold: float = DEFAULT_THRESHOLD,
) -> DifferentialResult:
    with sandbox(good_ref, repo_root=repo_root) as good_box:
        good = replay(good_box, envelope, repeat=repeat)

    with sandbox(bad_ref, repo_root=repo_root) as bad_box:
        bad = replay(bad_box, envelope, repeat=repeat)

    return DifferentialResult(target=envelope.target, good=good, bad=bad, threshold=threshold)
