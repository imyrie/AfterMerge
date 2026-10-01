"""Static policy gate for SQL a model wrote.

The division of labour here is deliberate. ClickHouse itself is a far better
judge of whether a statement parses and whether every identifier resolves than
any regex could be, so that question is asked by running `EXPLAIN` (see
`execute.py`) rather than re-implemented here.

What ClickHouse will *not* object to is the set of things this module checks. It
will happily run a `DROP`, happily read `system.tables`, happily scan every row
ever written, and happily return zero for a filter whose literal can never
match. Those are policy, not syntax, and policy has to be decided before the
statement reaches the server -- an `EXPLAIN` on a destructive statement is
already too much trust.

So: guards for what the database does not mind, `EXPLAIN` for what it minds
better than we could.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aftermerge.dataquality.checks import COLUMN_VOCABULARIES, literals_in
from aftermerge.warehouse.rollup import LOADS as ROLLUP_LOADS

#: Statements that may begin a read. Anything else is refused outright rather
#: than inspected further.
READ_PREFIXES = ("SELECT", "WITH")

#: Tokens that cannot appear in a read-only statement. `SYSTEM`, `KILL` and
#: `OPTIMIZE` are here because they are side-effecting even though they read
#: like administration; `SET` because it can lift the very limits applied below.
FORBIDDEN_TOKENS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "ALTER",
        "DROP",
        "CREATE",
        "TRUNCATE",
        "ATTACH",
        "DETACH",
        "RENAME",
        "EXCHANGE",
        "GRANT",
        "REVOKE",
        "OPTIMIZE",
        "SYSTEM",
        "KILL",
        "SET",
        "USE",
    }
)

#: Table functions that reach outside this server entirely. A generated query
#: that can call `url()` or `remote()` is an exfiltration primitive, not a
#: metric.
FORBIDDEN_FUNCTIONS = frozenset(
    {"url", "remote", "remoteSecure", "file", "s3", "hdfs", "mysql", "postgresql", "jdbc", "odbc"}
)

#: Tables a generated query may read. Narrower than "whatever exists": the
#: question being answered is always about telemetry, so the span table and the
#: rollups are the whole legitimate surface.
#:
#: Taken from the rollup loader rather than written out, because a hand-kept
#: list drifts -- the first version of this named two tables that do not exist,
#: and the gate would have rejected every correct query against the real ones.
ALLOWED_TABLES = frozenset({"otel_traces", *ROLLUP_LOADS})

#: A statement with no time predicate and no LIMIT reads all retained history.
#: Either bound is enough; neither is not.
_TIME_PREDICATE = re.compile(
    r"\bTimestamp\b\s*(>=|>|<=|<|BETWEEN)|\bday\b\s*(>=|>|=|BETWEEN)", re.I
)
_LIMIT = re.compile(r"\bLIMIT\s+\d+", re.I)

_FROM_OR_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", re.I)
#: Names bound by a WITH clause. These are not tables, and refusing them would
#: reject a perfectly good query -- which it did: a model used a CTE and the
#: allowlist called the CTE an unknown table. The CTE's own FROM is still
#: checked, so nothing is let through by naming it.
_CTE_NAME = re.compile(r"(?:\bWITH|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", re.I)
_FUNCTION_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_WORD = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class Rejection:
    guard: str
    reason: str


@dataclass(frozen=True)
class GateResult:
    sql: str
    rejections: tuple[Rejection, ...] = field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return not self.rejections

    @property
    def feedback(self) -> str:
        """What to tell the model so its next attempt is better.

        Named reasons rather than a bare "invalid": a repair attempt given only
        "rejected" is a second guess, not a correction.
        """
        return "\n".join(f"- [{r.guard}] {r.reason}" for r in self.rejections)


def strip_strings_and_comments(sql: str) -> str:
    """Blank out string literals and comments, preserving length-independent shape.

    Every guard below scans for keywords, and a route named '/cart/drop' or a
    comment mentioning DELETE must not trip them. Replacing literals with empty
    quotes rather than removing them keeps the surrounding syntax intact.
    """
    out = re.sub(r"--[^\n]*", " ", sql)
    out = re.sub(r"/\*.*?\*/", " ", out, flags=re.S)
    out = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", out)
    out = re.sub(r"`[^`]*`", "``", out)
    return out


def _statements(stripped: str) -> list[str]:
    return [part for part in (p.strip() for p in stripped.split(";")) if part]


def check_single_statement(stripped: str) -> Rejection | None:
    if len(_statements(stripped)) > 1:
        return Rejection(
            "single_statement",
            "more than one statement; a metric request is answered by exactly one SELECT",
        )
    return None


def check_read_only(stripped: str) -> Rejection | None:
    body = stripped.lstrip().lstrip("(").lstrip()
    if not body.upper().startswith(READ_PREFIXES):
        match = _WORD.search(body)
        token = match.group(0).upper() if match else "(empty)"
        return Rejection("read_only", f"statement starts with {token}, not SELECT or WITH")

    found = sorted(FORBIDDEN_TOKENS.intersection(w.upper() for w in _WORD.findall(stripped)))
    if found:
        return Rejection("read_only", f"contains side-effecting keyword(s): {', '.join(found)}")
    return None


def check_tables(stripped: str) -> Rejection | None:
    referenced = {m.group(1) for m in _FROM_OR_JOIN.finditer(stripped)}
    cte_names = {m.group(1).lower() for m in _CTE_NAME.finditer(stripped)}
    # A bare name is read in the connection's database; a dotted name names one
    # explicitly, and only the configured telemetry database is in scope.
    unqualified = {name.split(".")[-1] for name in referenced}
    unknown = sorted(n for n in unqualified - ALLOWED_TABLES if n.lower() not in cte_names)
    if unknown:
        return Rejection(
            "allowed_tables",
            f"reads {', '.join(unknown)}; only {', '.join(sorted(ALLOWED_TABLES))} are in scope",
        )
    dotted = sorted(n for n in referenced if "." in n and not n.startswith(("otel.", "default.")))
    if dotted:
        return Rejection("allowed_tables", f"reads another database: {', '.join(dotted)}")
    return None


def check_functions(stripped: str) -> Rejection | None:
    called = {m.group(1) for m in _FUNCTION_CALL.finditer(stripped)}
    forbidden = sorted({c for c in called if c.lower() in {f.lower() for f in FORBIDDEN_FUNCTIONS}})
    if forbidden:
        return Rejection(
            "no_external_functions",
            f"calls {', '.join(forbidden)}, which reaches outside this server",
        )
    return None


def check_bounded(stripped: str) -> Rejection | None:
    if _TIME_PREDICATE.search(stripped) or _LIMIT.search(stripped):
        return None
    return Rejection(
        "bounded",
        "no time predicate and no LIMIT, so the query reads all retained history",
    )


def check_literal_vocabulary(sql: str) -> Rejection | None:
    """The defect this repo already shipped once, now applied to generated SQL.

    `StatusCode = 'STATUS_CODE_ERROR'` is valid SQL against a real column and
    returns zero rows. A gate that only asks "does it run" accepts it happily,
    and the answer is a confident, plausible, wrong number. Literals are read
    from the unstripped SQL because the literals *are* the strings.
    """
    for column, vocabulary in COLUMN_VOCABULARIES.items():
        impossible = sorted(literals_in(sql, column) - set(vocabulary))
        if impossible:
            quoted = ", ".join(f"{column}='{lit}'" for lit in impossible)
            return Rejection(
                "literal_vocabulary",
                f"{quoted} cannot match; {column} is one of {sorted(vocabulary)}",
            )
    return None


def gate(sql: str) -> GateResult:
    """Every guard, collected rather than short-circuited.

    One rejection per attempt would mean one round trip per mistake. Reporting
    all of them lets a repair attempt fix the statement in one pass.
    """
    stripped = strip_strings_and_comments(sql)
    rejections = [
        r
        for r in (
            check_single_statement(stripped),
            check_read_only(stripped),
            check_tables(stripped),
            check_functions(stripped),
            check_bounded(stripped),
            check_literal_vocabulary(sql),
        )
        if r is not None
    ]
    return GateResult(sql=sql.strip(), rejections=tuple(rejections))
