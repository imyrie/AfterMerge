# Slice 2 — plan

**Goal:** make level 3 stop being empty. Reproduce the regression in isolation, prove the cause by
differential replay, and generate a regression test that provably fails on the bad commit and passes
on the good one.

**Status:** parts 1-3 done (2026-09-16). Parts 4-5 are still a plan.

**Why this slice is the differentiator.** Slice 1 produces a persuasive report, but every claim in it
is still level 1 or 2 — measured or inferred. Plenty of tools stop there. What almost nothing does is
close the loop: re-run the failure in a sandbox, confirm the diff actually causes it, and leave behind
a test that would catch it again. Do not skip ahead to patching (slice 3) before this works.

---

## Part 1 — Sandbox

An ephemeral shopdemo at an arbitrary commit, isolated from the running stack.

| | |
|---|---|
| Builds | `docker compose -p repro-<sha>` from a `git worktree` at that SHA |
| Exit | `sandbox.up("8b4fd77")` returns a handle; `curl` against it reports that version |

**DONE.** `sandbox("8b4fd77", repo_root=...)` is a context manager yielding a handle with
`base_url`, `trace_database`, `health()`, `span_count()` and `wait_for_spans()`. Verified: it serves
the requested commit, publishes an ephemeral port, writes traces to its own database, leaves
production's `otel` database untouched, reproduces **51 database operations per request** from
`orders/repository.py` in isolation, and tears down containers, volumes and trace database.

Implementation notes from building it:

- **The request envelope type was defined first**, before anything captures into it
  (`reproducer/envelope.py`). Replay will be written against that type, so capture in part 2 becomes
  another producer of an existing shape rather than a refactor of the replay path. Credential headers
  raise on construction, header handling is an allowlist, and `replay_safe` defaults to `False`.
- **Separate database, shared ClickHouse server.** A whole ClickHouse instance per sandbox is slow and
  large; a separate *database* on the running server gives the same structural isolation for free.
- **The seed is owned by the reproducer, not the commit.** `infra/sandbox/seed.sql` calls `setseed()`
  and uses a fixed epoch instead of `now()`. The schema still comes from the commit under test, since
  schema is part of the application, but the data must be identical on both sides or a differential
  compares two databases rather than two code paths.
- **A flush race, encoded rather than documented.** Querying the trace database straight after a
  request reliably returns zero: the collector batches, and the table is created at collector startup,
  so an empty result is indistinguishable from a broken pipeline. `wait_for_spans()` exists so every
  caller does not rediscover this with an arbitrary `sleep`.
- **`infra/sandbox/compose.yml` cannot be run by hand.** It requires the `REPRO_*` variables that
  `sandbox.py` supplies; a bare `docker compose -p <project> logs` fails with an unhelpful
  `invalid spec` error. Debug through the module, not the compose file.

**Telemetry isolation is the design decision here.** Two options:

1. Reuse the running collector and tag replay traffic with a resource attribute, filtering by it.
   Cheap, but a single forgotten filter silently mixes replay traces into production evidence.
2. Give each sandbox its own collector writing to its own ClickHouse database.
   One extra container per sandbox.

**Take option 2.** This project's whole claim is that its evidence is trustworthy; contamination should
be impossible by construction, not avoided by discipline.

**Gotchas**
- Do not publish fixed ports. Two concurrent sandboxes, or a sandbox alongside the dev stack, will
  collide on 8000/8001/5432. Use ephemeral ports and read them back from `docker compose port`.
- Teardown must be `try/finally` **and** label-based, so a crashed run does not leave orphaned
  containers and volumes holding gigabytes.
- Worktrees and built images should be cached by SHA. Rebuilding shopdemo for every replay turns a
  20-second check into a 3-minute one.

---

## Part 2 — Request capture

Store real requests so a failure can be re-sent rather than guessed at.

| | |
|---|---|
| Captures | method, route, query params, allowlisted headers, body |
| Exit | a `captured_requests` row that replays byte-identically |

**DONE — and built differently than planned.** Requests are reconstructed from **telemetry**, not
from application middleware. That keeps AfterMerge read-only (no redeploy, no code in the request
path) and works retroactively on traffic already recorded, including the commits pinned in a
scenario, which were built long before capture existed.

Two consequences, both stated rather than hidden:

- **Spans carry no request body**, so a mutating request can be recorded but not faithfully
  reproduced. Those are stored with `replay_safe = False` and a reason, not dropped: "we saw this and
  cannot replay it" is useful information.
- **Spans also carry no credential headers**, because OpenTelemetry does not record them by default.
  Capturing from telemetry is therefore *safer* than a middleware that sees real headers and must be
  trusted to drop them.

**Gotcha that would have silently corrupted every replay:** the FastAPI instrumentation records
`http.target` **without** the query string — it reads `/orders` for a request to `/orders?limit=50`.
Only `http.url` preserves parameters. A capture trusting `http.target` would replay a different
workload than production served, and `limit` is exactly what drives this regression's magnitude.
`_split_target` prefers `http.url` and falls back to `http.target`.

Shapes are grouped, not listed: 1,801 identical `GET /orders?limit=50` calls are one row carrying an
observation count and the slowest exemplar trace, rather than 1,801 near-duplicates.

`replay_safe` is computed by the repository, never accepted from the caller, so the flag and the
stated reason cannot drift apart.

**Sanitisation is not optional and not an afterthought.** Drop `Authorization` and `Cookie` outright,
redact configured body fields, and set `replay_safe` explicitly. A request that cannot be proven safe
is not replayed.

**Enforce sandbox-only replay in the type system.** The replay client should accept a *sandbox handle*,
not a URL string. Replaying a captured mutating request against production would be catastrophic, and
"we were careful" is not a control. This is the same move as `VerificationRepository` requiring a
`CompletedProcess`: make the dangerous thing unrepresentable.

**Honest scope note:** slice 0's fixture is `GET /orders?limit=50` — no body, no auth. Capture is nearly
trivial *for this demo*. Build it properly anyway, because the design is the point.

---

## Part 3 — Differential replay

| | |
|---|---|
| Does | replay the same captured request against sandbox@good and sandbox@bad, N times each |
| Then | runs **the same named fact queries** from `telemetry/queries/` against both |
| Exit | a `verifications` row with a real exit code — **the first level-3 evidence in the project** |

**DONE.** `aftermerge verify` produces:

```
differential_replay: confirmed (exit code 0)
Replaying /orders?limit=50 against cbb4790 and 8b4fd77 in isolation produced
2.0 vs 51.0 database operations per request (25.5x).
```

**The verification genuinely runs as a subprocess.** `VerificationRepository` requires a
`CompletedProcess`, and satisfying that by fabricating one in-process would hollow out the whole
invariant. So `aftermerge replay` is a real command with exit-code semantics — 0 reproduced,
1 not reproduced, 2 could not run — and `verify` records what that process actually did. The stored
evidence is an exit code and stdout anyone can reproduce by re-running the printed command.

**Three verdicts, not two.** Exit 2 records as `errored`, never `refuted`. A harness that could not
start is not evidence against a hypothesis, and letting broken tooling silently discredit a correct
conclusion would be worse than recording nothing. Callers declare which codes mean "could not run";
they still cannot declare the outcome.

**Two measurement bugs, both of which would have produced quietly wrong numbers:**

1. `wait_for_spans(minimum=repeat)` returned as soon as 15 spans existed, but 15 requests against the
   N+1 build produce ~765 spans. Measuring then gave **47.6** operations per request instead of 51.0.
2. Waiting for the span count to *stabilise* was not enough either: the collector batches on a
   one-second timer, so the total plateaus between bursts and a stability check declares victory with
   a third of the requests still in flight — 476 spans over 10 traces rather than 765 over 15.

The fix is that the request count is **known exactly**, so `wait_for_traces(expected)` waits for it
rather than inferring from stability. Part 4's generated test will assert on this number; a flaky
count here would have poisoned it.

**A `python -m` bug worth knowing:** commands appended after the `if __name__ == "__main__"` block
are registered too late, so `python -m aftermerge.cli replay` reported "No such command" while
`uv run aftermerge replay` worked fine — the console script imports the module fully first. Only the
subprocess path exposed it.

Reusing the identical SQL in replay and production is what makes the comparison meaningful. If replay
used different queries, a difference between them would prove nothing.

**Expected result on the fixture:** 2 database spans per request at `cbb4790`, 51 at `8b4fd77`,
reproduced in isolation with no production traffic involved.

**Gotcha — the seed is not deterministic.** `fixtures/shopdemo/db/02-seed.sql` uses `random()` for
`created_at`, so `ORDER BY created_at DESC LIMIT 50` returns a *different* 50 orders in each sandbox.
Span counts survive this (50 orders is still 51 queries), but anything data-dependent will not.
Fix with `setseed()` or a fixed dataset before trusting any comparison finer than a count.

---

## Part 4 — Test generation

An LLM writes a pytest regression test from the incident's facts and the diff.

| | |
|---|---|
| Input | level-1 facts + the diff hunk, as structured output |
| Output | a test file under `tests/regression/` |

**The generated test must assert on work, not time.** A latency-based assertion would be flaky, and —
per slice 1 — would have failed to catch this very regression against a warm database, where p95 rose
only 1.45×. The test should assert database operations per request, which was stable at 2 → 51 across
both a cold and a warm cache.

Practically: the test issues an HTTP request against the sandbox, then queries the replay trace store
for the span count. That works unchanged at both SHAs, with no need to import version-specific code.

---

## Part 5 — The validation gate

The part that makes slice 2 worth more than a prompt.

| Check | Requirement |
|---|---|
| Run at `bad_sha` | must exit **non-zero** |
| Run at `good_sha` | must exit **zero** |
| Otherwise | reject the candidate, retry at most twice, then give up and say so |

A test that passes everywhere proves nothing. A test that fails everywhere proves nothing. Only a test
that discriminates between the two commits has demonstrated it encodes the regression — and that is
established by two recorded exit codes, not by the model's opinion of its own output.

**Giving up must be a supported outcome.** If no candidate passes the gate, ship the report without a
test and state that plainly. A pipeline that always produces something is less trustworthy than one
that can say "I could not."

---

## Build order

1. **Sandbox** (part 1) — nothing else can be tested until a sandbox comes up and tears down cleanly.
2. **Differential replay** (part 3, using a hand-written request) — this alone produces the project's
   first level-3 verification, and is independently demoable.
3. **Capture** (part 2) — replaces the hand-written request with a real one.
4. **Test generation + gate** (parts 4 and 5) — the gate is the deliverable; the generation is the
   easy half.

Parts 1 and 3 are the substance. If time runs short, a working sandbox plus a recorded differential
verification is already the thing slice 1 could not do.

---

## Risks, ranked

| Risk | Why it matters | Mitigation |
|---|---|---|
| Sandbox startup cost | 30-60s per side makes iteration painful and the demo slow | Cache worktrees and images by SHA |
| Orphaned containers | A crashed run silently eats disk | `try/finally` plus label-based sweep |
| Non-deterministic seed | Invalidates any fine-grained comparison | `setseed()` before trusting more than counts |
| Port collisions | Breaks concurrent or alongside-dev runs | Ephemeral ports only |
| LLM writes a test that passes everywhere | Would be worthless evidence | The gate catches it by construction |
| Replay hitting production | Catastrophic and irreversible | Type-level: replay accepts a sandbox handle, never a URL |

---

## What "done" looks like

```
## Verified conclusions

| method                | verdict   | exit code |
|-----------------------|-----------|-----------|
| differential_replay   | confirmed |         0 |
| regression_test_gate  | confirmed |         0 |

Replayed 50 captured requests against cbb4790 and 8b4fd77 in isolation:
2.0 vs 51.0 database operations per request.

tests/regression/test_orders_n_plus_one.py fails at 8b4fd77 (exit 1) and
passes at cbb4790 (exit 0).
```

That block is the whole point of the project. It is the sentence no
"LLM reads your logs" tool can write.
