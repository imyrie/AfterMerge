"""Benchmarking models against the gate.

The interesting question is not whether a model writes plausible code, it is how
often its output survives validation. Every run here goes through the same gate
the pipeline uses, so "accepted" means the same thing it means in production: a
test that failed on the defect and passed on its predecessor, or a patch that
restored the behaviour and the response bytes.

Runs default to a single attempt. First-attempt acceptance is the honest
headline -- retries measure the feedback loop, which is a different question --
and each extra attempt costs two or three container builds.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aftermerge.llm import TokenUsage

TESTGEN = "testgen"
PATCH = "patch"
NL2SQL = "nl2sql"
TASKS = (TESTGEN, PATCH, NL2SQL)

#: Tasks that need a detected incident to evaluate against. `nl2sql` does not:
#: a metric request stands on its own, which also makes it the one task here
#: that can be benchmarked without first reproducing a regression.
INCIDENT_TASKS = (TESTGEN, PATCH)


@dataclass(frozen=True)
class Pricing:
    """US dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


#: Approximate published list prices, kept here so the arithmetic is visible and
#: editable. Tokens are what this harness measures; cost is derived from them, so
#: a stale row understates or overstates spend without affecting the token counts.
#: Update from the current pricing page before quoting a dollar figure.
PRICES: dict[str, Pricing] = {
    "claude-opus-5": Pricing(15.0, 75.0),
    "claude-sonnet-5": Pricing(3.0, 15.0),
    "claude-fable-5-1": Pricing(3.0, 15.0),
    "claude-haiku-4-5-20251001": Pricing(1.0, 5.0),
}


@dataclass(frozen=True)
class EvalRun:
    model: str
    task: str
    accepted: bool
    attempts: int
    usage: TokenUsage
    seconds: float
    detail: str

    @property
    def cost_usd(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        return (
            self.usage.input_tokens / 1_000_000 * price.input_per_mtok
            + self.usage.output_tokens / 1_000_000 * price.output_per_mtok
        )


@dataclass(frozen=True)
class EvalReport:
    runs: tuple[EvalRun, ...] = field(default_factory=tuple)

    @property
    def models(self) -> tuple[str, ...]:
        seen: list[str] = []
        for run in self.runs:
            if run.model not in seen:
                seen.append(run.model)
        return tuple(seen)

    def for_model(self, model: str) -> tuple[EvalRun, ...]:
        return tuple(r for r in self.runs if r.model == model)

    def acceptance_rate(self, model: str | None = None) -> float:
        runs = self.for_model(model) if model else self.runs
        return sum(1 for r in runs if r.accepted) / len(runs) if runs else 0.0

    def total_usage(self, model: str | None = None) -> TokenUsage:
        runs = self.for_model(model) if model else self.runs
        total = TokenUsage()
        for run in runs:
            total = total + run.usage
        return total

    def cost(self, model: str | None = None) -> float | None:
        runs = self.for_model(model) if model else self.runs
        costs = [r.cost_usd for r in runs]
        if any(c is None for c in costs):
            return None
        return sum(c for c in costs if c is not None)

    def cost_per_accepted(self, model: str | None = None) -> float | None:
        """Spend divided by results that survived the gate.

        The metric that matters. A cheap model that is never accepted costs
        infinitely more per useful result than an expensive one that is.
        """
        runs = self.for_model(model) if model else self.runs
        accepted = sum(1 for r in runs if r.accepted)
        total = self.cost(model)
        if total is None or accepted == 0:
            return None
        return total / accepted

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": [
                {
                    "model": r.model,
                    "task": r.task,
                    "accepted": r.accepted,
                    "attempts": r.attempts,
                    "input_tokens": r.usage.input_tokens,
                    "output_tokens": r.usage.output_tokens,
                    "cost_usd": r.cost_usd,
                    "seconds": round(r.seconds, 1),
                    "detail": r.detail,
                }
                for r in self.runs
            ],
            "summary": {
                model: {
                    "acceptance_rate": self.acceptance_rate(model),
                    "tokens": self.total_usage(model).total,
                    "cost_usd": self.cost(model),
                    "cost_per_accepted_usd": self.cost_per_accepted(model),
                }
                for model in self.models
            },
        }


#: Runs one (model, task) pair and reports what the gate decided.
TaskRunner = Callable[[str, str], EvalRun]


def run_matrix(
    models: Sequence[str],
    tasks: Sequence[str],
    runner: TaskRunner,
    *,
    on_start: Callable[[str, str], None] | None = None,
) -> EvalReport:
    """Run every (model, task) pair and collect the results.

    A run that raises is recorded as a failure rather than aborting the matrix:
    one unavailable model should not discard the results already paid for.
    """
    runs: list[EvalRun] = []
    for model in models:
        for task in tasks:
            if on_start is not None:
                on_start(model, task)
            started = time.monotonic()
            try:
                runs.append(runner(model, task))
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                runs.append(
                    EvalRun(
                        model=model,
                        task=task,
                        accepted=False,
                        attempts=0,
                        usage=TokenUsage(),
                        seconds=time.monotonic() - started,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
    return EvalReport(runs=tuple(runs))
