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

#: Rollup table -> the SELECT that rebuilds it from raw. Materialised views only
#: see new inserts, so history has to be filled in explicitly.
BACKFILL: dict[str, str] = {
    "otel_route_rollup": """
        SELECT toDate(Timestamp), ServiceName, SpanName,
               ResourceAttributes['service.version'],
               countState(), uniqExactState(TraceId),
               quantilesState(0.5, 0.95, 0.99)(Duration / 1e6),
               sumState(toUInt64(StatusCode = 'Error'))
        FROM otel_traces
        WHERE SpanKind = 'Server'
        GROUP BY 1, 2, 3, 4
    """,
    "otel_db_work_rollup": """
        SELECT toDate(Timestamp), ServiceName,
               ResourceAttributes['service.version'],
               SpanAttributes['code.file.path'],
               countState(), uniqExactState(TraceId)
        FROM otel_traces
        WHERE SpanKind = 'Client' AND SpanAttributes['code.file.path'] != ''
        GROUP BY 1, 2, 3, 4
    """,
}

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


def backfill(ch: Any) -> dict[str, int]:
    """Rebuild rollups from raw.

    Truncates first, because re-inserting over existing buckets would double
    count: AggregatingMergeTree combines rows with equal keys rather than
    replacing them. Assumes ingest is quiet -- a span arriving between the
    truncate and the insert is counted by the materialised view and by the
    backfill both.
    """
    counts: dict[str, int] = {}
    for table, select in BACKFILL.items():
        ch.command(f"TRUNCATE TABLE {table}")
        ch.command(f"INSERT INTO {table} {select}")
        counts[table] = int(ch.query(f"SELECT count() FROM {table}").result_rows[0][0])
    return counts


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
