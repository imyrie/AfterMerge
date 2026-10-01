# Metric requests to SQL

`aftermerge ask` turns a question about telemetry into ClickHouse SQL, gates it, and runs it.

```bash
uv run aftermerge ask "p95 server latency per deployed version for GET /orders in the last 4 hours"
```

```
attempt 1: accepted
SELECT
    ResourceAttributes['service.version'] AS version,
    count()                               AS requests,
    round(quantile(0.95)(Duration) / 1e6, 1) AS p95_ms
FROM otel_traces
WHERE ServiceName = 'gateway'
  AND SpanKind = 'Server'
  AND SpanName = 'GET /orders'
  AND Timestamp >= now() - INTERVAL 4 HOUR
GROUP BY version
ORDER BY min(Timestamp) ASC

┏━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┓
┃ version ┃ requests ┃ p95_ms ┃
┡━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━┩
│ cbb4790 │ 1789     │ 515.0  │
│ 8b4fd77 │ 930      │ 2510.5 │
└─────────┴──────────┴────────┘
read 8,192 rows
```

## Why this needs a gate at all

Generated SQL fails in a way that looks like success. `StatusCode = 'STATUS_CODE_ERROR'` is valid
SQL against a real column; it parses, runs, returns zero, and zero reads as a measurement. This
repository shipped that exact defect by hand and it survived three slices (see
[data-quality.md](data-quality.md)). A gate that only asks "did it execute" would accept it every
time.

So a generated statement is treated the way a generated patch is: proposed, gated, and discarded if
the gate refuses.

## Three layers, each doing what it is actually good at

**Static guards** ([guards.py](../src/aftermerge/nl2sql/guards.py)) decide policy, before the
statement reaches the server:

| guard | refuses |
|---|---|
| `single_statement` | a second statement after an innocent read |
| `read_only` | anything not starting SELECT/WITH, and any side-effecting keyword |
| `allowed_tables` | `system.*`, other databases, anything outside the telemetry tables |
| `no_external_functions` | `url()`, `remote()`, `s3()` — exfiltration primitives, not metrics |
| `bounded` | no time predicate and no LIMIT, i.e. all retained history |
| `literal_vocabulary` | a filter literal the exporter can never write |

Keywords inside strings and comments are blanked first, so a route named `/cart/drop` is not a
`DROP`. All rejections are reported together: one reason per attempt would mean one round trip per
mistake.

**`EXPLAIN SYNTAX`** asks the server whether the statement parses and every identifier resolves. A
hallucinated column is the most common failure in generated SQL, and ClickHouse detects it exactly
— `Missing columns: 'latency_ms_p95'` — without executing anything. Re-implementing that against a
scraped schema would be strictly worse, so it is not attempted.

**`readonly=1`** makes the read-only guard true rather than merely intended, and is applied to the
`EXPLAIN`s too. Verified against this server: it refuses `TRUNCATE` with `READONLY`. The row,
time and result caps ride along, and `readonly=1` also forbids changing settings, so a statement
cannot lift the limits that constrain it.

## The schema card

Built by introspecting `system.columns` rather than kept as a literal, because a card that drifts
from the database produces hallucinations that are not the model's fault.

Columns alone are not enough. Everything interesting in an OTLP span lives inside a `Map` —
`ResourceAttributes['service.version']`, `SpanAttributes['code.file.path']` — and a model reading
only `DESCRIBE TABLE` sees `Map(LowCardinality(String), String)` and has to guess the keys. A
guessed key returns an empty string for every row rather than failing: the silent-zero failure in a
different costume. So the card carries the observed keys by frequency, the enum vocabularies, and
two catalog queries as worked examples.

## The differential oracle

Safe, bounded, well-formed and referencing real columns still does not mean *correct*. A query can
clear every one of those bars and measure the wrong thing.

So each reference case pairs a metric request with a hand-written catalog query that already answers
it, and acceptance means the generated SQL returns the same values — the same oracle the patch
validator uses, rather than inspecting the artifact and forming an opinion.

Its limits, stated plainly:

- The comparison is ordered and element-wise, so each request spells out its output contract
  (which columns, in what order, in what unit, sorted how). A query computing the right metric with
  columns in a different order counts as a disagreement.
- `Outcome.answer` ignores column *names* and rounds floats, so `AS p95_latency` versus `AS p95_ms`
  is not treated as an error about the data.
- An empty reference result cannot prove agreement. Two empty answers are not evidence.

## What measuring it actually found

Three bugs, all mine, none of them the models':

1. **A truncated reply.** One model pads `AS` clauses with alignment whitespace, hit the token
   ceiling mid-statement, and left an unclosed ``` fence. The fence regex required a closing fence,
   so the literal ` ```sql ` text reached the gate, which reported *"statement starts with SQL"* —
   true, and useless. Truncation is now detected from `stop_reason` and named as itself.
2. **A CTE read as an unknown table.** A model answered with `WITH client_spans AS (...) SELECT ...
   FROM client_spans`. The allowlist called `client_spans` an unknown table and refused a correct
   query, while the prompt explicitly permits `WITH`. CTE names are now recognised; the CTE's own
   `FROM` is still checked, so naming one lets nothing through.
3. **A drifted allowlist.** Its first version named two rollup tables that do not exist, which would
   have rejected every correct query against the real ones. It is now derived from the rollup
   loader.

Each of those had scored a model zero for a defect in the harness. The first benchmark run reported
Sonnet and Haiku at 0% — a number that said nothing about the models.

## Benchmark

`nl2sql` is a task in the same harness as `testgen` and `patch`, so one run measures all three
against the gate ([evaluation.md](evaluation.md)):

```bash
uv run aftermerge evaluate --tasks nl2sql --models claude-opus-5,claude-sonnet-5,claude-haiku-4-5-20251001
```

Three reference cases, one attempt each, after the harness bugs above were fixed:

| model | agreement | tokens | cost | cost / accepted |
|---|---|---|---|---|
| claude-opus-5 | 3/3 | 8,639 | $0.1765 | $0.1765 |
| claude-sonnet-5 | 3/3 | 8,654 | $0.0355 | $0.0355 |
| claude-haiku-4-5-20251001 | 3/3 | 6,247 | $0.0084 | $0.0084 |

A run counts as accepted only if *every* case agrees. Averaging inside a run would let a model that
got one of three right look partly correct, and every other gate here is pass/fail on the whole
artifact.

**Read this result carefully.** All three models agree with the catalog on all three cases, so the
reference set no longer discriminates between them — 100% across the board is a statement about the
difficulty of the set, not a ranking. The finding that does survive is the **21× spread in cost per
accepted result at identical correctness**: on questions of this shape, the expensive model buys
nothing. Distinguishing the models would need harder cases, and the honest version of that claim is
that they have not been written yet.

## Why the answer is not recorded as evidence

Deliberately no audit-trail write. A fact in this pipeline requires an incident it is evidence
*for*, and an ad-hoc metric question is not evidence about anything — recording one would mean
inventing an incident to hang it on, which is the kind of convenient fiction the trust ladder exists
to prevent.

Generated SQL is an exploration tool. The catalog produces evidence, and a query earns a place in
the catalog by being reviewed and named by a person.
