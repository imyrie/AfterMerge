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


# --- incremental loading -----------------------------------------------------

from aftermerge.warehouse.rollup import (  # noqa: E402
    DEFAULT_LOOKBACK_DAYS,
    RefreshReport,
    TableLoad,
    days_to_load,
    watermark,
)


class FakeCH:
    """Answers queries by matching a fragment of the SQL."""

    def __init__(self, answers: dict[str, list[tuple]]) -> None:
        self.answers = answers
        self.commands: list[str] = []
        self.queries: list[str] = []

    def query(self, sql: str, parameters: dict | None = None):
        self.queries.append(sql)
        for fragment, rows in self.answers.items():
            if fragment in sql:
                return type("R", (), {"result_rows": rows, "summary": {}})()
        return type("R", (), {"result_rows": [], "summary": {}})()

    def command(self, sql: str) -> None:
        self.commands.append(sql)


def test_an_empty_rollup_has_no_watermark() -> None:
    """ClickHouse returns the zero date for max() over an empty table, not NULL."""
    assert watermark(FakeCH({"max(day)": [("1970-01-01",)]}), "t") is None
    assert watermark(FakeCH({"max(day)": [(None,)]}), "t") is None


def test_a_populated_rollup_reports_its_newest_day() -> None:
    assert watermark(FakeCH({"max(day)": [("2026-09-28",)]}), "t") == "2026-09-28"


def test_an_empty_rollup_loads_every_day() -> None:
    ch = FakeCH(
        {
            "max(day)": [("1970-01-01",)],
            "DISTINCT": [("2026-09-26",), ("2026-09-27",), ("2026-09-28",)],
        }
    )
    assert len(days_to_load(ch, "t")) == 3


def test_full_ignores_the_watermark() -> None:
    ch = FakeCH(
        {
            "max(day)": [("2026-09-28",)],
            "DISTINCT": [("2026-09-26",), ("2026-09-27",), ("2026-09-28",)],
        }
    )
    assert len(days_to_load(ch, "t", full=True)) == 3


def test_the_watermark_day_is_reloaded_not_skipped() -> None:
    """The boundary day was almost certainly incomplete when it was written.

    Spans for a day keep arriving until it ends, so loading strictly after the
    watermark leaves every boundary day permanently short.
    """
    ch = FakeCH({"max(day)": [("2026-09-28",)], "DISTINCT": [("2026-09-28",)]})
    assert days_to_load(ch, "t", lookback_days=0) == ["2026-09-28"]

    # The generated predicate must be >=, not >, or the boundary day is skipped.
    predicate = next(q for q in ch.queries if "DISTINCT" in q)
    assert ">= toDate('2026-09-28')" in predicate
    assert "> toDate('2026-09-28')" not in predicate.replace(">= toDate", "")


def test_a_day_is_dropped_before_it_is_inserted() -> None:
    """AggregatingMergeTree merges equal keys, so inserting over a day adds to it.

    Dropping first is what makes the load idempotent, and therefore safe for a
    scheduler to retry.
    """
    from aftermerge.warehouse.rollup import load_day

    ch = FakeCH({"count()": [(100,)]})
    load_day(ch, "otel_route_rollup", "2026-09-28")

    assert any(c.startswith("ALTER TABLE") and "DROP PARTITION" in c for c in ch.commands)
    assert ch.commands.index(
        next(c for c in ch.commands if "DROP PARTITION" in c)
    ) < ch.commands.index(next(c for c in ch.commands if c.startswith("INSERT")))


def report(scanned: int, raw: int, tables: int = 2) -> RefreshReport:
    loads = tuple(
        TableLoad(
            table=f"t{i}", days=("2026-09-28",), rows_scanned=scanned // tables, rows_written=1
        )
        for i in range(tables)
    )
    return RefreshReport(loads=loads, full=False, raw_rows=raw)


def test_the_full_rebuild_baseline_counts_every_table() -> None:
    """Comparing both tables' scans to one table's rows reported 200% for a
    full rebuild, which is how this was found."""
    assert report(scanned=200, raw=100, tables=2).full_rebuild_rows == 200
    assert report(scanned=200, raw=100, tables=2).fraction_reprocessed == 1.0


def test_a_partial_refresh_reports_its_share() -> None:
    assert report(scanned=50, raw=100, tables=2).fraction_reprocessed == 0.25


def test_a_refresh_with_nothing_to_do_says_so() -> None:
    empty = RefreshReport(
        loads=(TableLoad(table="t", days=(), rows_scanned=0, rows_written=0),),
        full=False,
        raw_rows=100,
    )
    assert "already up to date" in empty.summary


def test_the_default_lookback_tolerates_late_arrivals() -> None:
    """Strictly-forward loading would never see a span that arrived late."""
    assert DEFAULT_LOOKBACK_DAYS >= 1
