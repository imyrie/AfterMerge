"""Generated SQL, and the gate that decides whether to believe it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aftermerge.nl2sql import execute
from aftermerge.nl2sql.agent import extract_sql, propose
from aftermerge.nl2sql.benchmark import Reference, compare
from aftermerge.nl2sql.guards import ALLOWED_TABLES, gate, strip_strings_and_comments
from aftermerge.nl2sql.schema import SchemaCard

BOUNDED = "WHERE Timestamp >= now() - INTERVAL 60 MINUTE"


class FakeCH:
    """Answers EXPLAIN and ordinary queries from canned values."""

    def __init__(
        self,
        *,
        explain_error: str | None = None,
        rows: list[tuple[Any, ...]] | None = None,
        columns: tuple[str, ...] = ("a",),
        run_error: str | None = None,
    ) -> None:
        self.explain_error = explain_error
        self.rows = rows if rows is not None else []
        self.columns = columns
        self.run_error = run_error
        self.seen: list[str] = []

    def query(self, sql: str, parameters: dict | None = None, settings: dict | None = None) -> Any:
        self.seen.append(sql)
        if sql.startswith("EXPLAIN SYNTAX"):
            if self.explain_error:
                raise RuntimeError(self.explain_error)
            return SimpleNamespace(column_names=["explain"], result_rows=[("ok",)], summary={})
        if sql.startswith("EXPLAIN ESTIMATE"):
            return SimpleNamespace(
                column_names=["database", "table", "parts", "rows", "marks"],
                result_rows=[("otel", "otel_traces", 2, 72784, 10)],
                summary={},
            )
        if self.run_error:
            raise RuntimeError(self.run_error)
        return SimpleNamespace(
            column_names=list(self.columns),
            result_rows=list(self.rows),
            summary={"read_rows": "72784"},
        )


class FakeLLM:
    """Replies in order, recording the prompts it was given."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[list[dict[str, Any]]] = []
        self.messages = self
        self.stop_reason = "end_turn"

    def create(self, **kwargs: Any) -> Any:
        self.prompts.append([dict(m) for m in kwargs["messages"]])
        text = self._replies.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
            stop_reason=self.stop_reason,
        )


# --- the gate -----------------------------------------------------------------


def test_a_plain_bounded_read_is_accepted() -> None:
    assert gate(f"SELECT count() FROM otel_traces {BOUNDED}").accepted


def test_ddl_is_refused() -> None:
    result = gate("DROP TABLE otel_traces")
    assert not result.accepted
    assert "read_only" in {r.guard for r in result.rejections}


def test_a_second_statement_is_refused() -> None:
    """The shape where the read is innocent and the payload follows it."""
    result = gate(f"SELECT 1 FROM otel_traces {BOUNDED}; DROP TABLE otel_traces")
    assert "single_statement" in {r.guard for r in result.rejections}


def test_system_tables_are_out_of_scope() -> None:
    result = gate("SELECT * FROM system.tables LIMIT 10")
    assert "allowed_tables" in {r.guard for r in result.rejections}


def test_table_functions_that_leave_the_server_are_refused() -> None:
    """A generated query that can call url() is an exfiltration primitive."""
    result = gate("SELECT * FROM url('http://elsewhere/x', 'CSV', 'a String') LIMIT 1")
    assert "no_external_functions" in {r.guard for r in result.rejections}


def test_an_unbounded_scan_is_refused() -> None:
    result = gate("SELECT count() FROM otel_traces")
    assert "bounded" in {r.guard for r in result.rejections}


def test_either_bound_is_enough() -> None:
    assert gate("SELECT TraceId FROM otel_traces LIMIT 10").accepted
    assert gate(f"SELECT TraceId FROM otel_traces {BOUNDED}").accepted


def test_a_literal_the_exporter_never_writes_is_refused() -> None:
    """The defect this repo shipped by hand, now applied to generated SQL.

    Valid SQL, real column, runs cleanly, returns zero -- and zero reads as a
    measurement. A gate that only asked "does it execute" would accept it.
    """
    result = gate(f"SELECT countIf(StatusCode = 'STATUS_CODE_ERROR') FROM otel_traces {BOUNDED}")
    assert not result.accepted
    rejection = next(r for r in result.rejections if r.guard == "literal_vocabulary")
    assert "cannot match" in rejection.reason
    assert "'Error'" in rejection.reason


def test_the_correct_literal_passes() -> None:
    assert gate(f"SELECT countIf(StatusCode = 'Error') FROM otel_traces {BOUNDED}").accepted


def test_keywords_inside_strings_and_comments_do_not_trip_the_guards() -> None:
    """A route named '/cart/drop' is not a DROP."""
    assert gate(f"SELECT count() FROM otel_traces {BOUNDED} AND SpanName = '/cart/drop'").accepted
    assert gate(f"SELECT count() FROM otel_traces -- DELETE nothing\n{BOUNDED}").accepted


def test_stripping_preserves_surrounding_syntax() -> None:
    stripped = strip_strings_and_comments("SELECT 'a' AS x -- note\nFROM t")
    assert "note" not in stripped
    assert "AS x" in stripped


def test_every_rejection_is_reported_at_once() -> None:
    """One reason per attempt would mean one round trip per mistake."""
    result = gate("SELECT * FROM system.tables")
    assert len(result.rejections) >= 2
    assert result.feedback.count("- [") == len(result.rejections)


def test_the_allowlist_tracks_the_rollup_loader() -> None:
    """A hand-kept list drifted once already and rejected correct queries."""
    assert "otel_traces" in ALLOWED_TABLES
    assert any(name.endswith("_rollup") for name in ALLOWED_TABLES)


# --- extraction and normalisation ---------------------------------------------


def test_a_fenced_reply_is_tolerated() -> None:
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_a_trailing_semicolon_is_stripped() -> None:
    assert extract_sql("SELECT 1;") == "SELECT 1"


def test_an_echoed_sql_label_is_stripped() -> None:
    """Measured, not hypothetical: a model echoed the prompt's trailing "SQL:"
    label and the gate read the statement as starting with the word SQL, scoring
    it zero for a punctuation habit rather than for anything about the data."""
    assert extract_sql("SQL:\nSELECT 1") == "SELECT 1"
    assert extract_sql("sql\nSELECT 1") == "SELECT 1"
    assert gate(f"SELECT count() FROM otel_traces {BOUNDED}").accepted


def test_an_unclosed_fence_from_a_truncated_reply_is_handled() -> None:
    """Measured: a reply hit the token ceiling mid-statement, leaving no closing
    fence, so the leading "```sql" reached the gate and was reported as the
    statement starting with the word SQL -- true, and entirely unhelpful."""
    assert extract_sql("```sql\nSELECT count() FROM otel_traces").startswith("SELECT")


def test_truncation_is_named_rather_than_disguised_as_a_syntax_error() -> None:
    sql = f"SELECT count() FROM otel_traces {BOUNDED}"
    llm_client = FakeLLM([sql])
    llm_client.stop_reason = "max_tokens"
    proposal = propose("count", card=SchemaCard(), client=llm_client, ch=FakeCH(), max_attempts=1)
    assert not proposal.accepted
    assert "truncated" in proposal.rejection_summary


def test_a_cte_is_not_mistaken_for_an_unknown_table() -> None:
    """Also measured: a model answered with a CTE and the allowlist refused it.

    The CTE's own FROM is still checked, so recognising the name lets nothing
    through -- a CTE reading a forbidden table is still rejected.
    """
    good = (
        "WITH client_spans AS ("
        f"  SELECT TraceId, Duration FROM otel_traces {BOUNDED}"
        ") SELECT count() FROM client_spans"
    )
    assert gate(good).accepted

    sneaky = "WITH leak AS (SELECT * FROM system.tables) SELECT * FROM leak LIMIT 1"
    assert "allowed_tables" in {r.guard for r in gate(sneaky).rejections}


def test_answers_compare_on_values_not_column_names() -> None:
    """`AS p95_latency` versus `AS p95_ms` is not a mistake about the data."""
    a = execute.Outcome(columns=("p95_ms",), rows=((1,),))
    b = execute.Outcome(columns=("p95_latency",), rows=((1.0,),))
    assert a.answer == b.answer


def test_floats_are_compared_at_the_precision_the_catalog_reports() -> None:
    a = execute.Outcome(columns=("x",), rows=((12.34,),))
    b = execute.Outcome(columns=("x",), rows=((12.31,),))
    assert a.answer == b.answer


def test_error_text_drops_the_server_tail() -> None:
    cleaned = execute._error_text(
        RuntimeError("Code: 47. Missing columns: 'nope': While processing SELECT (version 25.3)")
    )
    assert "Missing columns" in cleaned
    assert "version 25.3" not in cleaned


# --- the agent ----------------------------------------------------------------


def test_a_rejection_is_fed_back_and_the_repair_is_accepted() -> None:
    """A retry told only "rejected" is a second guess, not a correction."""
    bad = "SELECT countIf(StatusCode = 'STATUS_CODE_ERROR') FROM otel_traces"
    good = f"SELECT countIf(StatusCode = 'Error') FROM otel_traces {BOUNDED}"
    llm_client = FakeLLM([bad, good])

    proposal = propose(
        "error count", card=SchemaCard(), client=llm_client, ch=FakeCH(), max_attempts=3
    )

    assert proposal.accepted
    assert len(proposal.attempts) == 2
    repair_prompt = llm_client.prompts[1][-1]["content"]
    assert "STATUS_CODE_ERROR" in repair_prompt
    assert "literal_vocabulary" in repair_prompt
    assert "bounded" in repair_prompt


def test_attempts_are_bounded() -> None:
    llm_client = FakeLLM(["DROP TABLE otel_traces"] * 2)
    proposal = propose(
        "anything", card=SchemaCard(), client=llm_client, ch=FakeCH(), max_attempts=2
    )
    assert not proposal.accepted
    assert len(proposal.attempts) == 2


def test_usage_accumulates_across_attempts() -> None:
    llm_client = FakeLLM(["DROP TABLE t", "DROP TABLE t"])
    proposal = propose(
        "anything", card=SchemaCard(), client=llm_client, ch=FakeCH(), max_attempts=2
    )
    assert proposal.usage.input_tokens == 200
    assert proposal.usage.output_tokens == 40


def test_a_hallucinated_column_is_caught_by_the_server_not_the_regex() -> None:
    """ClickHouse resolves identifiers better than any scraped-schema check."""
    sql = f"SELECT latency_ms_p95 FROM otel_traces {BOUNDED}"
    ch = FakeCH(explain_error="Code: 47. Missing columns: 'latency_ms_p95'")
    proposal = propose("p95", card=SchemaCard(), client=FakeLLM([sql, sql]), ch=ch, max_attempts=1)
    assert not proposal.accepted
    assert "Missing columns" in proposal.rejection_summary


def test_without_a_warehouse_nothing_is_accepted() -> None:
    """No EXPLAIN means no confirmation the identifiers resolve.

    "Accepted" has to mean the same thing here as everywhere else in this
    pipeline, so it is withheld rather than assumed.
    """
    sql = f"SELECT count() FROM otel_traces {BOUNDED}"
    proposal = propose("count", card=SchemaCard(), client=FakeLLM([sql]), ch=None, max_attempts=1)
    assert proposal.attempts[0].gate_result.accepted
    assert not proposal.accepted


# --- the differential oracle --------------------------------------------------


def _reference() -> Reference:
    return Reference(
        name="demo", request="...", query_name="latency_quantiles", params={"service": "gateway"}
    )


def test_identical_values_agree(monkeypatch: Any) -> None:
    rows = [("v1", 10, 1.0), ("v2", 10, 2.0)]
    monkeypatch.setattr(
        "aftermerge.nl2sql.benchmark.reference_answer",
        lambda ref, ch: execute.Outcome(columns=("a", "b", "c"), rows=tuple(rows)),
    )
    result = compare(_reference(), "SELECT 1", FakeCH(rows=rows, columns=("x", "y", "z")))
    assert result.agreed


def test_a_differing_row_is_located(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "aftermerge.nl2sql.benchmark.reference_answer",
        lambda ref, ch: execute.Outcome(columns=("a",), rows=(("v1", 10), ("v2", 20))),
    )
    result = compare(_reference(), "SELECT 1", FakeCH(rows=[("v1", 10), ("v2", 99)]))
    assert not result.agreed
    assert "row 1 differs" in result.detail


def test_a_row_count_mismatch_is_named(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "aftermerge.nl2sql.benchmark.reference_answer",
        lambda ref, ch: execute.Outcome(columns=("a",), rows=(("v1",), ("v2",))),
    )
    result = compare(_reference(), "SELECT 1", FakeCH(rows=[("v1",)]))
    assert not result.agreed
    assert "row count differs" in result.detail


def test_an_empty_reference_cannot_prove_agreement(monkeypatch: Any) -> None:
    """Two empty results are not evidence that the generated query is right."""
    monkeypatch.setattr(
        "aftermerge.nl2sql.benchmark.reference_answer",
        lambda ref, ch: execute.Outcome(columns=(), rows=()),
    )
    result = compare(_reference(), "SELECT 1", FakeCH(rows=[("v1",)]))
    assert not result.agreed
    assert "no rows" in result.detail


def test_a_generated_query_that_fails_to_run_disagrees(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "aftermerge.nl2sql.benchmark.reference_answer",
        lambda ref, ch: execute.Outcome(columns=("a",), rows=(("v1",),)),
    )
    result = compare(_reference(), "SELECT 1", FakeCH(run_error="TOO_MANY_ROWS"))
    assert not result.agreed
    assert "TOO_MANY_ROWS" in result.detail


# --- the schema card ----------------------------------------------------------


def test_the_card_shows_map_keys_rather_than_describing_the_map() -> None:
    """A model cannot guess map keys, and a guessed key returns '' per row."""
    card = SchemaCard(
        tables={"otel_traces": [("Timestamp", "DateTime64(9)")]},
        map_keys={"ResourceAttributes": ["service.version"]},
        vocabularies={"StatusCode": ["Error", "Ok", "Unset"]},
        examples=[("example", "SELECT 1")],
    )
    rendered = card.render()
    assert "ResourceAttributes['service.version']" in rendered
    assert "StatusCode is one of: 'Error', 'Ok', 'Unset'" in rendered
    assert "-- example: example" in rendered
