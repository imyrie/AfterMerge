# Slice 3 — plan

**Goal:** close the loop. Propose a fix, prove it works without breaking anything, and open a pull
request whose body is the evidence.

**Status:** not started. Every table below is an exit condition, not a result.

**What makes this slice hard is not writing the patch.** A language model will happily produce a
plausible diff. The work is establishing that the diff is *correct* — and the regression test alone
cannot establish that, because a patch can satisfy the test while breaking the feature.

---

## Part 1 — Patch proposal

| | |
|---|---|
| Input | the causing diff, the implicated code site, the incident's facts, the certified test |
| Output | a unified diff against the bad commit |
| Exit | `aftermerge patch` writes a diff touching only files the causing commit touched |

### Repair or revert, and say which

Two legitimate strategies, and they are not equivalent:

- **Revert** — restore the previous implementation. Always safe, but discards whatever the commit
  was *also* trying to do. For slice 0's fixture the commit did nothing else, so a revert is a
  complete fix.
- **Repair** — keep the commit's intent, remove the pathology. Preserves the author's goal but risks
  being subtly wrong.

The patcher should attempt a repair and fall back to a revert, and **the PR must state which it
did**. "I reverted your change" and "I rewrote your change" ask very different things of a reviewer,
and blurring them is how an automated PR loses trust.

### Constrain the blast radius

A proposed patch that touches files outside the causing diff is rejected without being run. The
incident implicates specific code sites; a fix wandering beyond them is not a fix, it is a rewrite,
and reviewing it costs more than writing the fix by hand.

**The patch must not touch the test.** Hash the certified test file before and after. If a patcher
can edit the test, it can make it pass trivially — this is the single cheapest way for the whole
pipeline to start lying, and it costs one comparison to prevent.

---

## Part 2 — Patch validation

The slice-2 gate, inverted, plus a correctness oracle it cannot supply.

| Check | Requirement | Why |
|---|---|---|
| Certified regression test at patched | **passes** | the specific pathology is gone |
| Existing test suite at patched | **passes** | nothing else broke |
| Differential replay patched vs good | work within tolerance | the fix restores the actual behaviour |
| Response equivalence patched vs good | identical bodies | the fix did not change what users get |

### The fourth row is the one that matters

**A patch can satisfy the regression test while being wrong.** The test asserts that database work
does not scale with page size. A patch that returns fewer items, caches stale results, drops the
line-item join, or silently truncates the response satisfies it perfectly — and is broken.

This is Goodhart's law arriving inside the pipeline: the moment a measure becomes the target of an
optimiser, it stops measuring what it measured. The regression test is *necessary and not
sufficient*, and a pipeline that stops at "the test passes" will eventually ship something confidently
wrong.

The oracle is **differential response comparison**: replay the same captured request against a
sandbox at `good_sha` and a sandbox at the patched tree, and require byte-identical response bodies.
That is cheap here because the reproducer already does exactly this for span counts, and because the
seed is deterministic — which is why part 1 of slice 2 insisted on `setseed()` rather than accepting
"close enough".

Where bodies legitimately differ (timestamps, ids), the comparison needs a declared normalisation,
and that normalisation belongs in the scenario file where it can be reviewed — not buried in the
comparison code where it silently widens over time.

### Sandboxing an uncommitted tree

`sandbox()` currently takes a ref. Validation needs a sandbox at *patched*, which is not a commit
yet. The clean route is to commit the patch to a scratch branch off the bad commit and sandbox that
SHA — reusing the whole existing mechanism rather than teaching the sandbox about working trees. The
scratch branch is also what the PR will eventually be opened from.

Each check runs as a **subprocess**, so each produces a `CompletedProcess` and a `verifications` row.
Level 3 for this slice should read as four rows, not one.

---

## Part 3 — The pull request

| | |
|---|---|
| Produces | a branch, a commit, and a PR body assembled from the incident report |
| Exit | `aftermerge pr` produces all three **locally**; `--push` is required to go outward |

### Local by default

Opening a PR is outward-facing and hard to retract — it notifies people, and a bad automated PR
costs a team more than no PR. `aftermerge pr` should default to writing the branch and the body and
printing them, with pushing behind an explicit flag. The tool should never be able to surprise
someone with a PR as a side effect of an investigation.

### The body is the evidence, in trust order

The PR body is the incident report: facts, then hypotheses, then verifications, with the commands
that produced each. A reviewer should be able to re-run every number in it without asking anyone.

It must also state plainly:

- that it was generated, and by what
- whether it is a **repair or a revert**
- what was verified, and — just as importantly — **what was not**

A PR that overstates its own confidence is worse than one that admits a gap, because the gap gets
discovered later and at higher cost.

### Never auto-merge

Not behind a flag, not with a confirmation. A human merges. The entire value proposition is a
reviewer who can check the evidence quickly, and that requires there to be a reviewer.

---

## Build order

1. **Patch validation** (part 2) — write it first, against a **hand-written** patch. It is the hard
   half and the part that has to be trustworthy; validating a known-good fix proves the harness
   before any generation exists to confuse the picture. This mirrors slice 2, where differential
   replay was built before capture.
2. **Patch proposal** (part 1) — feed the validated harness.
3. **Pull request** (part 3) — mostly assembly once the evidence exists.

If time runs short, a validated hand-written patch with four green verification rows is already the
thing slice 2 could not do.

---

## Risks, ranked

| Risk | Why it matters | Mitigation |
|---|---|---|
| Patch satisfies the test but breaks the feature | Ships confident, wrong code — the worst outcome available | Response-equivalence oracle, not just the test |
| Patcher edits the test | The pipeline starts lying and nothing catches it | Hash the test file; reject any change |
| Patch touches unrelated files | Unreviewable; defeats the purpose | Restrict to the causing diff's files, reject otherwise |
| Revert presented as a repair | Reviewer misjudges what they are approving | Label the strategy explicitly in the PR |
| A PR opened unintentionally | Outward-facing and hard to retract | Local by default; `--push` explicit; never auto-merge |
| Validation passes on stale sandboxes | Green for the wrong build | Sandbox the patched SHA; never trust an earlier gate result |
| Retry loop burns sandbox builds | Slow and expensive | Attempt limit; giving up is a supported outcome |

---

## What "done" looks like

```
## Verified conclusions

| method                    | verdict   | exit code |
|---------------------------|-----------|-----------|
| differential_replay       | confirmed |         0 |
| regression_test_gate      | confirmed |         0 |
| patch_regression_test     | confirmed |         0 |
| patch_suite               | confirmed |         0 |
| patch_response_equivalence| confirmed |         0 |

Strategy: repair (the batched item fetch was restored; no other behaviour changed).
Replaying /orders?limit=50 against cbb4790 and the patched tree produced 2.0 vs 2.0
database operations per request, with byte-identical response bodies.

Not verified: behaviour under concurrent writes; the captured request is read-only.
```

The last line matters as much as the five above it.
