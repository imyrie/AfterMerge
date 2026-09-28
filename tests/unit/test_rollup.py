"""Rollup DDL splitting, equivalence checking, and the cost arithmetic."""

from __future__ import annotations

from aftermerge.warehouse.rollup import (
    QueryProfile,
    RollupBenchmark,
    RollupComparison,
    _normalise,
    statements,
)

DDL = """
-- A leading comment block explaining the design.
CREATE TABLE a (x Int64) ENGINE = Memory;

-- Another comment, this time before a view.
CREATE MATERIALIZED VIEW b TO a AS SELECT 1;
"""


def test_statements_survive_leading_comments() -> None:
    """Regression: dropping any chunk starting with `--` skipped half the DDL.

    Each statement here is preceded by an explanatory comment, so rejecting
    comment-led chunks silently applied two of four -- and looked fine, because
    the tables already existed from an earlier manual run.
    """
    found = statements(DDL)
    assert len(found) == 2
    assert found[0].startswith("CREATE TABLE a")
    assert found[1].startswith("CREATE MATERIALIZED VIEW b")


def test_comment_only_chunks_are_dropped() -> None:
    assert statements("-- just a comment\n") == []
    assert statements("") == []


def profile(rows: int, byts: int, ms: float) -> QueryProfile:
    return QueryProfile(rows_read=rows, bytes_read=byts, elapsed_ms=ms)


def comparison(equivalent: bool = True) -> RollupComparison:
    return RollupComparison(
        question="db work",
        raw=profile(57_726, 23_000_000, 120.0),
        rollup=profile(4, 1_000, 12.0),
        equivalent=equivalent,
        detail="",
    )


def test_reduction_ratios() -> None:
    c = comparison()
    assert round(c.rows_reduction) == 14_432
    assert round(c.bytes_reduction) == 23_000
    assert c.speedup == 10.0


def test_a_zero_denominator_does_not_divide_by_zero() -> None:
    c = RollupComparison(
        question="q",
        raw=profile(100, 100, 10.0),
        rollup=profile(0, 0, 0.0),
        equivalent=True,
        detail="",
    )
    assert c.rows_reduction == float("inf")
    assert c.speedup == float("inf")


def test_a_disagreeing_rollup_fails_the_benchmark() -> None:
    """A rollup that is fast and wrong is worse than no rollup."""
    benchmark = RollupBenchmark(comparisons=(comparison(True), comparison(False)))
    assert not benchmark.all_equivalent


def test_an_empty_benchmark_is_not_a_pass() -> None:
    assert not RollupBenchmark(comparisons=()).all_equivalent


def test_normalise_ignores_row_order() -> None:
    assert _normalise([("b", 2), ("a", 1)]) == _normalise([("a", 1), ("b", 2)])


def test_normalise_tolerates_float_noise_but_not_real_differences() -> None:
    assert _normalise([("a", 1.2345)]) == _normalise([("a", 1.2348)])
    assert _normalise([("a", 1.2)]) != _normalise([("a", 9.9)])


def test_as_dict_is_json_safe() -> None:
    import json

    payload = json.loads(json.dumps(RollupBenchmark(comparisons=(comparison(),)).as_dict()))
    assert payload["all_equivalent"] is True
    assert payload["comparisons"][0]["rows_reduction"] == 14431.5
