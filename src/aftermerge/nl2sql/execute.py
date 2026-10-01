"""Validating and running a generated statement against ClickHouse.

Two layers, because the static gate in `guards.py` is a policy decision made by
code that can be wrong:

`EXPLAIN SYNTAX` asks the server whether the statement parses and whether every
identifier resolves. A hallucinated column is the single most common failure in
generated SQL, and ClickHouse detects it exactly -- `Missing columns:
'latency_ms_p95'` -- without executing anything. Re-implementing that against a
scraped schema would be strictly worse.

`readonly=1` then makes the read-only guard true rather than merely intended. It
is applied to every statement this module sends, including the `EXPLAIN`s, so a
statement that slipped past the gate still cannot write: verified against this
server, where it refuses `TRUNCATE` with READONLY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Caps applied to every generated statement. A metric request that genuinely
#: needs to read more than this wants a catalog query written by a person, not
#: an ad-hoc one.
MAX_ROWS_TO_READ = 50_000_000
MAX_EXECUTION_SECONDS = 20
MAX_RESULT_ROWS = 1_000

#: `readonly=1` refuses writes *and* refuses to change settings, so the caps
#: above cannot be lifted by the statement they constrain.
READ_ONLY_SETTINGS: dict[str, Any] = {
    "readonly": 1,
    "max_rows_to_read": MAX_ROWS_TO_READ,
    "max_execution_time": MAX_EXECUTION_SECONDS,
    "max_result_rows": MAX_RESULT_ROWS,
    "result_overflow_mode": "break",
}


@dataclass(frozen=True)
class Explanation:
    """Whether the server accepts the statement, without running it."""

    ok: bool
    error: str = ""
    estimated_rows: int | None = None

    @property
    def feedback(self) -> str:
        return f"- [explain] {self.error}" if self.error else ""


@dataclass(frozen=True)
class Outcome:
    """A generated statement's result, plus what it cost to produce."""

    columns: tuple[str, ...] = field(default_factory=tuple)
    rows: tuple[tuple[Any, ...], ...] = field(default_factory=tuple)
    summary: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def read_rows(self) -> int | None:
        value = self.summary.get("read_rows")
        return int(value) if value is not None else None

    @property
    def answer(self) -> tuple[tuple[Any, ...], ...]:
        """Rows normalised for comparison against a reference query.

        Column *names* are deliberately excluded: a model that writes
        `AS p95_latency` where the catalog writes `AS p95_ms` has not made a
        mistake about the data. Values and their order are what must agree.
        """
        return tuple(tuple(_normalise(v) for v in row) for row in self.rows)


def _normalise(value: Any) -> Any:
    """Collapse differences that are representational rather than semantic."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        # A reference query rounds to one decimal; a generated one may not.
        return round(value, 1)
    return value


def _error_text(exc: Exception) -> str:
    """ClickHouse errors carry a stack-ish tail that adds nothing for a model."""
    text = str(exc).strip().replace("\n", " ")
    for marker in (": While processing", " (version "):
        head, _, _ = text.partition(marker)
        text = head or text
    return text[:600]


def explain(client: Any, sql: str) -> Explanation:
    """Ask the server to analyse the statement without executing it."""
    try:
        client.query(f"EXPLAIN SYNTAX {sql}", settings=READ_ONLY_SETTINGS)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        return Explanation(ok=False, error=_error_text(exc))
    return Explanation(ok=True, estimated_rows=estimate_rows(client, sql))


def estimate_rows(client: Any, sql: str) -> int | None:
    """Rows ClickHouse expects to read, from `EXPLAIN ESTIMATE`.

    Advisory only. The estimate is per-table and absent for a statement that
    reads nothing from disk, so it informs the report rather than gating it --
    `max_rows_to_read` is what actually stops a runaway scan.
    """
    try:
        result = client.query(f"EXPLAIN ESTIMATE {sql}", settings=READ_ONLY_SETTINGS)
    except Exception:  # noqa: BLE001 - an estimate is a nicety, not a gate
        return None
    rows = list(result.result_rows)
    if not rows:
        return None
    # Columns: database, table, parts, rows, marks.
    try:
        return sum(int(r[3]) for r in rows)
    except (IndexError, TypeError, ValueError):
        return None


def run(client: Any, sql: str) -> Outcome:
    """Execute under the read-only caps and record what it cost."""
    try:
        result = client.query(sql, settings=READ_ONLY_SETTINGS)
    except Exception as exc:  # noqa: BLE001 - an error is a result here
        return Outcome(error=_error_text(exc))
    return Outcome(
        columns=tuple(result.column_names),
        rows=tuple(tuple(r) for r in result.result_rows),
        summary=dict(result.summary or {}),
    )
