"""The fact catalog must stay loadable and parameterised.

These run without Docker: they guard the invariant that every reported number
traces back to a real, named, re-runnable statement.
"""

from __future__ import annotations

import re

import pytest

from aftermerge.telemetry import catalog

EXPECTED = {"span_count_per_trace", "latency_quantiles"}


def test_catalog_contains_the_slice_0_queries() -> None:
    assert set(catalog.names()) >= EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_query_loads_and_is_documented(name: str) -> None:
    query = catalog.load(name)
    assert query.sql.strip()
    assert query.description, f"{name}.sql has no description comment"
    assert query.path.is_file()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_queries_are_parameterised_not_interpolated(name: str) -> None:
    """Guards against someone reintroducing f-string SQL."""
    sql = catalog.load(name).sql
    assert re.search(r"\{[a-z_]+:[A-Za-z0-9()]+\}", sql), "no ClickHouse bound parameters found"
    assert "%s" not in sql
    assert "format(" not in sql.lower()


def test_unknown_query_names_the_alternatives() -> None:
    with pytest.raises(KeyError) as excinfo:
        catalog.load("no_such_query")
    assert "span_count_per_trace" in str(excinfo.value)
