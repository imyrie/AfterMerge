"""Benchmark arithmetic, and what happens when a run blows up."""

from __future__ import annotations

from aftermerge.evaluation.harness import PATCH, TESTGEN, EvalReport, EvalRun, run_matrix
from aftermerge.llm import TokenUsage


def run(model: str, task: str, accepted: bool, tokens: tuple[int, int] = (1000, 500)) -> EvalRun:
    return EvalRun(
        model=model,
        task=task,
        accepted=accepted,
        attempts=1,
        usage=TokenUsage(*tokens),
        seconds=30.0,
        detail="",
    )


def test_token_usage_adds() -> None:
    assert (TokenUsage(10, 5) + TokenUsage(1, 2)) == TokenUsage(11, 7)
    assert TokenUsage(10, 5).total == 15


def test_acceptance_rate_is_per_model_and_overall() -> None:
    report = EvalReport(
        runs=(
            run("a", TESTGEN, True),
            run("a", PATCH, False),
            run("b", TESTGEN, True),
            run("b", PATCH, True),
        )
    )
    assert report.acceptance_rate("a") == 0.5
    assert report.acceptance_rate("b") == 1.0
    assert report.acceptance_rate() == 0.75


def test_cost_uses_the_price_table() -> None:
    # sonnet: $3/Mtok in, $15/Mtok out -> 1M in + 1M out = $18
    report = EvalReport(runs=(run("claude-sonnet-5", TESTGEN, True, (1_000_000, 1_000_000)),))
    assert report.cost("claude-sonnet-5") == 18.0


def test_an_unpriced_model_reports_no_cost_rather_than_zero() -> None:
    """Zero would read as free, which is worse than admitting the table is stale."""
    report = EvalReport(runs=(run("some-unlisted-model", TESTGEN, True),))
    assert report.cost("some-unlisted-model") is None
    assert report.cost_per_accepted("some-unlisted-model") is None


def test_cost_per_accepted_is_the_metric_that_matters() -> None:
    """A model accepted half the time costs twice as much per useful result."""
    always = EvalReport(
        runs=(
            run("claude-sonnet-5", TESTGEN, True, (1_000_000, 0)),
            run("claude-sonnet-5", PATCH, True, (1_000_000, 0)),
        )
    )
    sometimes = EvalReport(
        runs=(
            run("claude-sonnet-5", TESTGEN, True, (1_000_000, 0)),
            run("claude-sonnet-5", PATCH, False, (1_000_000, 0)),
        )
    )
    assert always.cost_per_accepted("claude-sonnet-5") == 3.0
    assert sometimes.cost_per_accepted("claude-sonnet-5") == 6.0


def test_a_model_never_accepted_has_no_cost_per_accepted() -> None:
    report = EvalReport(runs=(run("claude-sonnet-5", TESTGEN, False),))
    assert report.cost_per_accepted("claude-sonnet-5") is None


def test_a_failing_run_is_recorded_and_the_matrix_continues() -> None:
    """One unavailable model must not discard results already paid for."""

    def runner(model: str, task: str) -> EvalRun:
        if model == "broken":
            raise RuntimeError("model not available")
        return run(model, task, True)

    report = run_matrix(["broken", "good"], [TESTGEN], runner)

    assert len(report.runs) == 2
    assert report.runs[0].accepted is False
    assert "model not available" in report.runs[0].detail
    assert report.runs[1].accepted is True


def test_model_order_is_preserved_for_reporting() -> None:
    report = EvalReport(runs=(run("z", TESTGEN, True), run("a", TESTGEN, True)))
    assert report.models == ("z", "a")


def test_as_dict_is_json_safe() -> None:
    import json

    report = EvalReport(runs=(run("claude-sonnet-5", TESTGEN, True),))
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["summary"]["claude-sonnet-5"]["acceptance_rate"] == 1.0
    assert payload["runs"][0]["input_tokens"] == 1000
