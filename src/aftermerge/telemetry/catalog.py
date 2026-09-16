"""The fact catalog: named, parameterised SQL.

Queries live as .sql files rather than inline strings so that every number
AfterMerge reports can be traced back to an exact, re-runnable statement. A fact
row stores the query name and its parameters; anyone can reproduce the value.

This also means production and replay sandboxes run byte-identical SQL, which is
what makes a differential comparison meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

QUERIES_DIR = Path(__file__).parent / "queries"


@dataclass(frozen=True)
class Query:
    name: str
    sql: str
    description: str

    @property
    def path(self) -> Path:
        return QUERIES_DIR / f"{self.name}.sql"


@cache
def load(name: str) -> Query:
    path = QUERIES_DIR / f"{name}.sql"
    if not path.is_file():
        raise KeyError(f"unknown query {name!r}; available: {', '.join(names())}")

    sql = path.read_text()
    # First comment line after the "-- fact:" marker documents the query.
    description = ""
    for line in sql.splitlines():
        if line.startswith("--") and "fact:" not in line:
            description = line.lstrip("- ").strip()
            break
    return Query(name=name, sql=sql, description=description)


def names() -> list[str]:
    return sorted(p.stem for p in QUERIES_DIR.glob("*.sql"))
