"""Pre-aggregated rollups, and proof they answer the same questions.

A rollup that is fast and wrong is worse than no rollup, so the benchmark checks
equivalence against the raw query before it reports a speedup. Same principle as
the patch validator: a number is not evidence until something has checked it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aftermerge.telemetry import client

DDL_PATH = Path("infra/clickhouse/rollups.sql")

#: Rollup table -> the SELECT that builds it, parameterised by day. `{day}` is
#: substituted with the partition being loaded, so one day can be rebuilt without
#: touching any other.
LOADS: dict[str, str] = {
    "otel_route_rollup": """
        SELECT toDate(Timestamp), ServiceName, SpanName,
               ResourceAttributes['service.version'],
               countState(), uniqExactState(TraceId),
               quantilesState(0.5, 0.95, 0.99)(Duration / 1e6),
               sumState(toUInt64(StatusCode = 'Error'))
        FROM otel_traces
        WHERE SpanKind = 'Server' AND toDate(Timestamp) = toDate('{day}')
        GROUP BY 1, 2, 3, 4
    """,
    "otel_db_work_rollup": """
        SELECT toDate(Timestamp), ServiceName,
               ResourceAttributes['service.version'],
               SpanAttributes['code.file.path'],
               countState(), uniqExactState(TraceId)
        FROM otel_traces
        WHERE SpanKind = 'Client' AND SpanAttributes['code.file.path'] != ''
          AND toDate(Timestamp) = toDate('{day}')
        GROUP BY 1, 2, 3, 4
    """,
}

#: Days behind the watermark that are reloaded anyway.
#:
#: A span can arrive after its own day has been rolled up -- a delayed export, a
#: collector restart, a backfilled queue. Loading strictly forward of the
#: watermark would never see it, and the rollup would disagree with raw forever
#: in a way no query reveals. Reloading a trailing window costs little and is
#: how incremental loads stay correct.
DEFAULT_LOOKBACK_DAYS = 2


#: Questions answered both ways, for the equivalence and cost comparison.
EQUIVALENTS: tuple[tuple[str, str, str], ...] = (
    ("route latency", "latency_quantiles", "latency_quantiles_rollup"),
    ("db work per request", "span_count_per_trace", "span_count_per_trace_rollup"),
)


@dataclass(frozen=True)
class QueryProfile:
    rows_read: int
    bytes_read: int
    elapsed_ms: float


@dataclass(frozen=True)
class RollupComparison:
    question: str
    raw: QueryProfile
    rollup: QueryProfile
    equivalent: bool
    detail: str

    def _ratio(self, raw: float, rollup: float) -> float:
        return raw / rollup if rollup else float("inf")

    @property
    def rows_reduction(self) -> float:
        return self._ratio(self.raw.rows_read, self.rollup.rows_read)

    @property
    def bytes_reduction(self) -> float:
        return self._ratio(self.raw.bytes_read, self.rollup.bytes_read)

    @property
    def speedup(self) -> float:
        return self._ratio(self.raw.elapsed_ms, self.rollup.elapsed_ms)


@dataclass(frozen=True)
class RollupBenchmark:
    comparisons: tuple[RollupComparison, ...]

    @property
    def all_equivalent(self) -> bool:
        return bool(self.comparisons) and all(c.equivalent for c in self.comparisons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "all_equivalent": self.all_equivalent,
            "comparisons": [
                {
                    "question": c.question,
                    "equivalent": c.equivalent,
                    "detail": c.detail,
                    "raw": {
                        "rows_read": c.raw.rows_read,
                        "bytes_read": c.raw.bytes_read,
                        "elapsed_ms": round(c.raw.elapsed_ms, 1),
                    },
                    "rollup": {
                        "rows_read": c.rollup.rows_read,
                        "bytes_read": c.rollup.bytes_read,
                        "elapsed_ms": round(c.rollup.elapsed_ms, 1),
                    },
                    "rows_reduction": round(c.rows_reduction, 1),
                    "bytes_reduction": round(c.bytes_reduction, 1),
                    "speedup": round(c.speedup, 1),
                }
                for c in self.comparisons
            ],
        }


def statements(ddl: str) -> list[str]:
    """Split DDL into executable statements, dropping comment-only chunks.

    Leading comments are stripped rather than used to reject the whole chunk.
    Discarding any chunk that *starts* with `--` silently skipped two of the four
    statements here, because each is preceded by an explanatory comment -- and it
    looked fine, because the tables already existed from an earlier manual run.
    """
    result: list[str] = []
    for chunk in ddl.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            result.append(body)
    return result


def apply(ch: Any, *, ddl_path: Path = DDL_PATH) -> int:
    """Create the rollup tables and their materialised views. Idempotent."""
    applied = 0
    for statement in statements(ddl_path.read_text()):
        ch.command(statement)
        applied += 1
    return applied


@dataclass(frozen=True)
class TableLoad:
    table: str
    days: tuple[str, ...]
    rows_scanned: int
    rows_written: int

    @property
    def skipped(self) -> bool:
        return not self.days


@dataclass(frozen=True)
class RefreshReport:
    loads: tuple[TableLoad, ...]
    full: bool
    raw_rows: int

    @property
    def rows_scanned(self) -> int:
        return sum(load.rows_scanned for load in self.loads)

    @property
    def days_loaded(self) -> int:
        return max((len(load.days) for load in self.loads), default=0)

    @property
    def full_rebuild_rows(self) -> int:
        """What a full rebuild would read: every raw row, once per rollup table."""
        return self.raw_rows * len(self.loads)

    @property
    def fraction_reprocessed(self) -> float:
        """Share of a full rebuild's work this refresh actually did.

        Measured against every table's scan, not the raw row count: each rollup
        reads the same days independently, so comparing their sum to a single
        table's worth of rows reported 200% for a two-table full rebuild.
        """
        total = self.full_rebuild_rows
        return self.rows_scanned / total if total else 0.0

    @property
    def summary(self) -> str:
        if not self.days_loaded:
            return "already up to date; nothing reprocessed"
        mode = "full rebuild" if self.full else "incremental"
        return (
            f"{mode}: {self.days_loaded} day(s), {self.rows_scanned:,} of "
            f"{self.full_rebuild_rows:,} rows a full rebuild would read "
            f"({self.fraction_reprocessed:.1%})"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "full": self.full,
            "raw_rows": self.raw_rows,
            "rows_scanned": self.rows_scanned,
            "full_rebuild_rows": self.full_rebuild_rows,
            "fraction_reprocessed": round(self.fraction_reprocessed, 4),
            "summary": self.summary,
            "tables": [
                {
                    "table": load.table,
                    "days": list(load.days),
                    "rows_scanned": load.rows_scanned,
                    "rows_written": load.rows_written,
                }
                for load in self.loads
            ],
        }


def watermark(ch: Any, table: str) -> str | None:
    """The newest day present in a rollup, or None when it is empty."""
    rows = ch.query(f"SELECT max(day) FROM {table}").result_rows
    value = rows[0][0] if rows and rows[0] else None
    # ClickHouse returns the zero date for an empty table rather than NULL.
    if value is None or str(value).startswith("1970-01-01"):
        return None
    return str(value)[:10]


def days_to_load(
    ch: Any,
    table: str,
    *,
    full: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[str]:
    """Which day partitions need rebuilding.

    The watermark day itself is always reloaded: it was almost certainly
    incomplete when it was written, since spans for a day keep arriving until it
    ends. Loading strictly after the watermark would leave every boundary day
    permanently short.
    """
    mark = None if full else watermark(ch, table)
    where = "" if mark is None else f"WHERE toDate(Timestamp) >= toDate('{mark}') - {lookback_days}"

    rows = ch.query(
        f"SELECT DISTINCT toDate(Timestamp) AS day FROM otel_traces {where} ORDER BY day"
    ).result_rows
    return [str(row[0])[:10] for row in rows]


def load_day(ch: Any, table: str, day: str) -> tuple[int, int]:
    """Rebuild one day partition. Returns (rows scanned, rows written).

    Drop-then-insert rather than insert-on-top: AggregatingMergeTree merges rows
    with equal keys, so inserting over an existing day would add to it rather
    than replace it. Dropping first is what makes this safe to re-run, which is
    what a scheduler needs when it retries a failed task.
    """
    scanned = int(
        ch.query(
            "SELECT count() FROM otel_traces WHERE toDate(Timestamp) = toDate({day:String})",
            parameters={"day": day},
        ).result_rows[0][0]
    )
    ch.command(f"ALTER TABLE {table} DROP PARTITION '{day}'")
    ch.command(f"INSERT INTO {table} {LOADS[table].format(day=day)}")
    written = int(
        ch.query(
            f"SELECT count() FROM {table} WHERE day = toDate({{day:String}})",
            parameters={"day": day},
        ).result_rows[0][0]
    )
    return scanned, written


def refresh(
    ch: Any,
    *,
    full: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> RefreshReport:
    """Bring the rollups up to date, reprocessing only what changed."""
    raw_rows = int(ch.query("SELECT count() FROM otel_traces").result_rows[0][0])

    loads: list[TableLoad] = []
    for table in LOADS:
        days = days_to_load(ch, table, full=full, lookback_days=lookback_days)
        scanned = written = 0
        for day in days:
            day_scanned, day_written = load_day(ch, table, day)
            scanned += day_scanned
            written += day_written
        loads.append(
            TableLoad(table=table, days=tuple(days), rows_scanned=scanned, rows_written=written)
        )

    return RefreshReport(loads=tuple(loads), full=full, raw_rows=raw_rows)


def _profile(
    ch: Any, name: str, params: dict[str, Any], repeats: int = 3
) -> tuple[QueryProfile, list[tuple[Any, ...]]]:
    """Best-of-N, so a cold cache on the first run does not decide the result."""
    best: QueryProfile | None = None
    rows: list[tuple[Any, ...]] = []
    for _ in range(repeats):
        result = client.run(name, client=ch, **params)
        summary = result.summary
        profile = QueryProfile(
            rows_read=int(summary.get("read_rows", 0)),
            bytes_read=int(summary.get("read_bytes", 0)),
            elapsed_ms=float(summary.get("elapsed_ns", 0)) / 1e6,
        )
        rows = [tuple(r) for r in result.rows]
        if best is None or profile.elapsed_ms < best.elapsed_ms:
            best = profile
    assert best is not None
    return best, rows


def _normalise(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Compare on value, not on ordering or float noise."""

    def cell(value: Any) -> Any:
        return round(float(value), 1) if isinstance(value, float) else value

    return sorted(tuple(cell(c) for c in row) for row in rows)


def compare(
    ch: Any,
    question: str,
    raw_name: str,
    raw_params: dict[str, Any],
    rollup_name: str,
    rollup_params: dict[str, Any],
) -> RollupComparison:
    """Profile both sides, and check they agree before reporting a speedup."""
    raw_profile, raw_rows = _profile(ch, raw_name, raw_params)
    rollup_profile, rollup_rows = _profile(ch, rollup_name, rollup_params)

    left, right = _normalise(raw_rows), _normalise(rollup_rows)
    equivalent = left == right
    detail = (
        f"{len(left)} row(s), identical"
        if equivalent
        else f"raw returned {left!r}, rollup returned {right!r}"
    )
    return RollupComparison(
        question=question,
        raw=raw_profile,
        rollup=rollup_profile,
        equivalent=equivalent,
        detail=detail,
    )
