"""The schema card handed to the model.

Built by introspecting the live server rather than kept as a literal, because a
schema card that drifts from the database produces hallucinations that are not
the model's fault. `system.columns` is the source of truth for columns.

Columns alone are not enough here. Everything interesting in an OTLP span lives
inside `Map` columns -- `ResourceAttributes['service.version']`,
`SpanAttributes['code.file.path']` -- and a model reading only `DESCRIBE TABLE`
sees `Map(LowCardinality(String), String)` and has to guess the keys. Guessed
keys are valid SQL that returns an empty string for every row, which is the
silent-zero failure in a different costume. So the card carries the observed
keys, and the enum vocabularies, and two worked examples from the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aftermerge.dataquality.checks import COLUMN_VOCABULARIES
from aftermerge.nl2sql.guards import ALLOWED_TABLES
from aftermerge.telemetry import catalog

#: Map columns whose keys have to be shown rather than described.
MAP_COLUMNS = ("ResourceAttributes", "SpanAttributes")

#: Keys are listed by frequency and cut off, because the tail of an attribute map
#: is long, uninteresting and would crowd out the examples.
MAX_MAP_KEYS = 24

#: Catalog queries shown as worked examples. These two carry the conventions that
#: matter: read the SERVER span for user-visible latency, split by
#: `ResourceAttributes['service.version']`, and bound on `Timestamp`.
EXAMPLE_QUERIES = ("latency_quantiles", "span_count_per_trace")


@dataclass(frozen=True)
class SchemaCard:
    tables: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    map_keys: dict[str, list[str]] = field(default_factory=dict)
    vocabularies: dict[str, list[str]] = field(default_factory=dict)
    examples: list[tuple[str, str]] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []
        for table, columns in self.tables.items():
            parts.append(f"TABLE {table}")
            parts.extend(f"    {name}  {type_}" for name, type_ in columns)
            parts.append("")

        for column, keys in self.map_keys.items():
            if keys:
                parts.append(f"Observed keys in {column} (use exactly these):")
                parts.extend(f"    {column}['{key}']" for key in keys)
                parts.append("")

        for column, values in self.vocabularies.items():
            parts.append(f"{column} is one of: {', '.join(repr(v) for v in values)}")
        if self.vocabularies:
            parts.append("")

        for name, sql in self.examples:
            parts.append(f"-- example: {name}")
            parts.append(sql.strip())
            parts.append("")

        return "\n".join(parts).strip()


def _columns(client: Any, table: str) -> list[tuple[str, str]]:
    result = client.query(
        "SELECT name, type FROM system.columns "
        "WHERE database = currentDatabase() AND table = {table:String} ORDER BY position",
        parameters={"table": table},
    )
    return [(str(r[0]), str(r[1])) for r in result.result_rows]


def _map_keys(client: Any, column: str, *, limit: int = MAX_MAP_KEYS) -> list[str]:
    """Attribute keys actually present, most frequent first."""
    result = client.query(
        f"SELECT k FROM otel_traces ARRAY JOIN mapKeys({column}) AS k "
        "GROUP BY k ORDER BY count() DESC LIMIT {limit:UInt32}",
        parameters={"limit": limit},
    )
    return [str(r[0]) for r in result.result_rows]


def build(client: Any) -> SchemaCard:
    """Introspect the server and assemble the card.

    A table named in `ALLOWED_TABLES` but absent from this deployment is skipped
    rather than raising: the rollups are optional, and a card describing a table
    the model cannot read would invite queries the gate then rejects.
    """
    tables: dict[str, list[tuple[str, str]]] = {}
    for table in sorted(ALLOWED_TABLES):
        columns = _columns(client, table)
        if columns:
            tables[table] = columns

    map_keys = {column: _map_keys(client, column) for column in MAP_COLUMNS}
    vocabularies = {column: sorted(values) for column, values in COLUMN_VOCABULARIES.items()}
    examples = [(name, catalog.load(name).sql) for name in EXAMPLE_QUERIES]
    return SchemaCard(
        tables=tables, map_keys=map_keys, vocabularies=vocabularies, examples=examples
    )
