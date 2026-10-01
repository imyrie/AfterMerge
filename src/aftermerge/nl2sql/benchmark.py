"""Does the generated query return the right answer, not just a runnable one?

The gate in `guards.py` and the `EXPLAIN` in `execute.py` together establish
that a statement is safe, bounded, well-formed and references real columns.
None of that establishes that it answers the question. A query can clear every
one of those bars and still measure the wrong thing.

So the strongest available check is differential: each reference case pairs a
metric request with a hand-written catalog query that already answers it, and
acceptance means the generated SQL returns *the same values*. This is the same
oracle the patch validator uses -- compare against known-good behaviour rather
than inspect the artifact and form an opinion.

Its limit is worth stating plainly. The comparison is ordered and element-wise,
so a generated query that computes the right metric but emits columns in a
different order counts as a disagreement. Each request therefore spells out its
output contract, and `Outcome.answer` ignores column *names* and rounds floats,
so what remains is a genuine difference in values rather than in presentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aftermerge.nl2sql import execute
from aftermerge.telemetry import client as ch_client

#: Defaults matching the rest of the CLI: the user-facing route is served by the
#: gateway, while the database spans being counted belong to the orders service.
#: Two services, not one, because that split is the whole point of the scenario
#: -- latency is observed at the edge and attributed further in.
DEFAULT_ROUTE_SERVICE = "gateway"
DEFAULT_DB_SERVICE = "orders"
DEFAULT_ROUTE = "GET /orders"
DEFAULT_LOOKBACK_MINUTES = 240


@dataclass(frozen=True)
class Reference:
    """A metric request and the catalog query that already answers it."""

    name: str
    request: str
    query_name: str
    params: dict[str, Any] = field(default_factory=dict)


def references(
    *,
    route_service: str = DEFAULT_ROUTE_SERVICE,
    db_service: str = DEFAULT_DB_SERVICE,
    route: str = DEFAULT_ROUTE,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> tuple[Reference, ...]:
    """The reference set.

    Each request states its output contract -- which columns, in what order,
    in what unit, sorted how -- because an ordered comparison against a fixed
    reference is only fair if the shape being asked for is unambiguous.
    """
    return (
        Reference(
            name="route_latency_quantiles",
            request=(
                f"For service '{route_service}' and route '{route}', return one row per "
                "deployed "
                "version with exactly these columns in this order: the version, the number of "
                "requests, the 50th, 95th and 99th percentile of server span duration in "
                "milliseconds each rounded to 1 decimal place, and the number of spans whose "
                f"status is an error. Use only the last {lookback_minutes} minutes. Sort by "
                "each version's earliest timestamp, ascending."
            ),
            query_name="latency_quantiles",
            params={
                "service": route_service,
                "route": route,
                "lookback_minutes": lookback_minutes,
            },
        ),
        Reference(
            name="db_spans_per_request",
            request=(
                f"For service '{db_service}', measure database work per request. Consider "
                "only "
                "client spans that carry a non-empty code file path attribute. Return one row "
                "per deployed version and code file path, with exactly these columns in this "
                "order: the version, the code file path, the number of such spans, the number "
                "of distinct traces, and spans divided by distinct traces rounded to 2 decimal "
                f"places. Use only the last {lookback_minutes} minutes. Sort by each group's "
                "earliest timestamp, ascending."
            ),
            query_name="span_count_per_trace",
            params={"service": db_service, "lookback_minutes": lookback_minutes},
        ),
        Reference(
            name="dependency_attribution",
            request=(
                f"For service '{db_service}', show where a request's time goes, split by "
                "deployed "
                "version. Consider only client spans. Return one row per version and span name "
                "with exactly these columns in this order: the version, the span name, the "
                "number of calls, calls divided by distinct traces rounded to 2 decimal places, "
                "the mean duration in milliseconds rounded to 3 decimal places, and the summed "
                "duration divided by distinct traces in milliseconds rounded to 2 decimal "
                f"places. Use only the last {lookback_minutes} minutes. Sort by version "
                "ascending, then by that last column descending."
            ),
            query_name="dependency_attribution",
            params={"service": db_service, "lookback_minutes": lookback_minutes},
        ),
    )


@dataclass(frozen=True)
class Comparison:
    """Whether a generated statement agreed with its reference."""

    reference: str
    agreed: bool
    detail: str
    generated_rows: int = 0
    reference_rows: int = 0


def reference_answer(reference: Reference, ch: Any) -> execute.Outcome:
    """Run the catalog query that the generated SQL has to match."""
    result = ch_client.run(reference.query_name, client=ch, **reference.params)
    return execute.Outcome(
        columns=tuple(result.columns),
        rows=tuple(result.rows),
        summary=dict(result.summary),
    )


def compare(reference: Reference, sql: str, ch: Any) -> Comparison:
    """Execute the generated statement and diff its values against the reference."""
    expected = reference_answer(reference, ch)
    actual = execute.run(ch, sql)

    if not actual.ok:
        return Comparison(reference.name, False, f"generated query failed: {actual.error}")

    want, got = expected.answer, actual.answer
    if want == got:
        return Comparison(
            reference.name,
            True,
            f"{len(got)} row(s) identical to {reference.query_name}",
            len(got),
            len(want),
        )

    if not want:
        return Comparison(
            reference.name,
            False,
            f"reference {reference.query_name} returned no rows, so nothing can be compared",
            len(got),
            0,
        )
    if len(want) != len(got):
        return Comparison(
            reference.name,
            False,
            f"row count differs: reference {len(want)}, generated {len(got)}",
            len(got),
            len(want),
        )

    for index, (want_row, got_row) in enumerate(zip(want, got, strict=True)):
        if want_row != got_row:
            return Comparison(
                reference.name,
                False,
                f"row {index} differs: reference {want_row!r}, generated {got_row!r}",
                len(got),
                len(want),
            )

    # Unreachable while `answer` is a plain tuple comparison, but a future
    # normalisation change should not silently report agreement.
    return Comparison(reference.name, False, "values differ in a way the diff did not locate")
