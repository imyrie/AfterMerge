# Slice 3 — plan

**Goal:** close the loop. Propose a fix, prove it works without breaking anything, and open a pull
request whose body is the evidence.

**Status:** complete (2026-09-16). All three parts done.

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

**DONE.** `aftermerge fix` proposes and validates in one loop:

```
attempt 1 (revert, revert): accepted
  passed   patch_regression_test: passes at c82d515
  skipped  patch_suite: no suite command configured
  passed   patch_work_restored: 2.0 vs 2.0 database operations per request
  passed   patch_response_equivalence: identical (17503 bytes, sha256 ea0d29b720d0)

validated .aftermerge/candidate.patch (revert)
```

**Both proposers return whole file contents, and git computes the diff.** Models are unreliable at
emitting valid unified diffs -- line counts, context, offsets -- and a malformed patch fails at
`git apply` for reasons unrelated to whether the fix was right. Letting git produce the diff from
proposed contents removes that failure mode entirely, and the revert proposer uses the same path.

**The deterministic default is a revert, and that is a real answer rather than a fallback.**
Restoring the previous implementation always removes the regression; the only cost is whatever else
the commit was trying to do. For a commit that did nothing else, it *is* the correct fix. The
strategy is recorded on the patch and stated in the output, because "I reverted your change" and
"I rewrote your change" ask different things of a reviewer.

**The loop never decides whether a patch is good.** It shells out to `aftermerge validate` and takes
the exit code, exactly as `certify` does with the gate. A deterministic proposer is not retried --
three sandbox builds to regenerate an identical diff -- and a rejected candidate is deleted rather
than left in `.aftermerge/`.

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

**DONE.** `aftermerge validate --patch <file> --strategy {repair,revert}` exits 0 validated,
1 rejected, 2 could not run.

On the hand-written revert (`fixtures/regressions/001-n-plus-one/fix.patch`):

```
passed   patch_regression_test: passes at ed455b5
skipped  patch_suite: no suite command configured; response equivalence is the oracle
passed   patch_work_restored: 2.0 vs 2.0 database operations per request
passed   patch_response_equivalence: identical (17503 bytes, sha256 ea0d29b720d0)
```

**And proven in the direction that matters.** `cheat.patch` stops the per-order query by returning
every order with an empty `items` list. Database work becomes *constant -- better than baseline* --
so the certified regression test passes and two of three substantive checks go green:

```
passed   patch_regression_test: passes at 7638e5f
passed   patch_work_restored: 2.0 vs 1.0 database operations per request
failed   patch_response_equivalence: body length 17503 became 6320 (-11183 bytes)
```

Exit 1. Without the equivalence oracle that patch would have been certified and shipped.

**Byte-identical responses were verified before the design was committed to.** Two independent
sandboxes at the same commit return the same 17,503 bytes with the same sha256, because slice 2's
seed uses `setseed()` and a fixed epoch. Had that not held, equivalence would have needed a
normalisation for every varying field and the oracle would have been much weaker.

**A skipped check is not a passing check.** shopdemo has no test suite of its own, so `patch_suite`
records as skipped with the reason stated, is named in the summary, and a result consisting only of
skips does not validate. Pretending absent evidence is positive evidence is precisely the
overstatement this project exists to avoid.

**One deviation from the sketch above:** validation is a *single* subprocess boundary, not one per
check. Four separate commands would each rebuild sandboxes to gather evidence two can supply --
roughly four extra minutes for no additional truth. Per-check results live in the verification's
metrics and render in the report either way.

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

**DONE.** `aftermerge pr` produces the branch and body on disk:

```
branch    aftermerge/fix-8b4fd77 at 1e84d68 (off 8b4fd77)
base      regression/001-n-plus-one
title     Fix critical regression in orders GET /orders
body      .aftermerge/pull_request.md  (152 lines)

not pushed. To push:  git push -u origin aftermerge/fix-8b4fd77
not opened. To open a draft PR:
  gh pr create --base regression/001-n-plus-one --head aftermerge/fix-8b4fd77 ... --draft

Nothing merges automatically. A person reviews and merges.
```

**The base is inferred from the commit, not assumed to be `main`.** The regression lives on its own
branch here, and a PR against `main` would target a branch that never contained the bug.

**A bug this surfaced immediately:** `git branch --contains <bad_sha>` also lists AfterMerge's *own*
fix branch, because that branch descends from the bad commit. Inferring it as the base would aim a
pull request at its own head -- no diff, nothing to review. Branches under `aftermerge/` are now
excluded, with a test pinning it.

**The branch is built in a detached worktree and then named**, so the user's checkout and working
tree are never touched, and the branch holds the exact tree that was validated.

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
