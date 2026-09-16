"""Proposing a candidate fix.

Two proposers behind one protocol, and the deterministic one is the default --
the same shape as test generation, for the same reason: what makes a patch
acceptable is that it passes validation, not what wrote it.

Here the deterministic option is a **revert**, which is not a fallback so much as
a legitimate answer. Restoring the previous implementation always removes the
regression; what it costs is whatever else the commit was trying to do. For a
commit that did nothing else, a revert is simply the correct fix.

Both proposers return whole file contents rather than a diff, and the diff is
computed from them. Models are unreliable at emitting valid unified diffs --
line counts, context, offsets -- and a malformed patch fails at `git apply` for
reasons that have nothing to do with whether the fix was right.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aftermerge.patcher.patch import Patch, PatchRejected


@dataclass(frozen=True)
class PatchContext:
    good_ref: str
    bad_ref: str
    changed_files: tuple[str, ...]
    code_site: str | None
    baseline_spans_per_request: float
    candidate_spans_per_request: float
    causing_diff: str


@dataclass(frozen=True)
class Proposal:
    """New contents for each file the fix touches."""

    files: dict[str, str]
    strategy: str
    origin: str


class PatchProposer(Protocol):
    name: str
    deterministic: bool

    def propose(self, context: PatchContext) -> Proposal: ...


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise PatchRejected(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def to_patch(proposal: Proposal, *, base_ref: str, repo_root: Path) -> Patch:
    """Turn proposed file contents into a diff against `base_ref`.

    Computed by git in a scratch worktree, so the result is always a valid patch
    that applies cleanly -- the proposer never has to get diff syntax right.
    """
    scratch = repo_root / ".worktrees" / f"propose-{uuid.uuid4().hex[:8]}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "--detach", "--quiet", str(scratch), base_ref)
    try:
        for relative, content in proposal.files.items():
            target = scratch / relative
            if not target.resolve().is_relative_to(scratch.resolve()):
                raise PatchRejected(f"proposed path escapes the repository: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        diff = _git(scratch, "diff")
        if not diff.strip():
            raise PatchRejected("proposal is identical to the commit it is meant to fix")
        return Patch(diff=diff, strategy=proposal.strategy, origin=proposal.origin)
    finally:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(scratch)],
            capture_output=True,
            text=True,
        )


class RevertProposer:
    """Restore each implicated file to its known-good contents."""

    name = "revert"
    deterministic = True

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    def propose(self, context: PatchContext) -> Proposal:
        files = {
            path: _git(self._repo_root, "show", f"{context.good_ref}:{path}")
            for path in context.changed_files
        }
        return Proposal(files=files, strategy="revert", origin=self.name)


PROMPT = """\
A production regression was introduced by the diff below and has been measured and
reproduced. Write a fix.

Measured database operations per request:
  before the change ({good_ref}): {baseline:.1f}
  after the change ({bad_ref}):  {candidate:.1f}
  originating in: {code_site}

The diff that caused it:

{causing_diff}

Current contents of {path}:

{content}

Requirements:
- Keep whatever the change was trying to achieve; remove only the pathology.
- The response body must be byte-identical to the pre-change build. A fix that
  returns fewer rows, empties a field, or caches results will be rejected.
- Do not change any test.
- Return the complete corrected contents of {path} and nothing else: no markdown
  fences, no commentary, no diff.
"""


class AnthropicProposer:
    """Attempt a repair that preserves the commit's intent."""

    name = "anthropic"
    deterministic = False

    def __init__(self, client: Any, model: str = "claude-sonnet-5", max_tokens: int = 4000) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def propose(self, context: PatchContext) -> Proposal:
        files: dict[str, str] = {}
        for path in context.changed_files:
            prompt = PROMPT.format(
                good_ref=context.good_ref,
                bad_ref=context.bad_ref,
                baseline=context.baseline_spans_per_request,
                candidate=context.candidate_spans_per_request,
                code_site=context.code_site or "unknown",
                causing_diff=context.causing_diff,
                path=path,
                content=_read_at(context.bad_ref, path),
            )
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            ).strip()
            files[path] = _strip_fences(text)

        return Proposal(files=files, strategy="repair", origin=f"{self.name}:{self._model}")


def _read_at(ref: str, path: str, repo_root: Path | None = None) -> str:
    return _git(repo_root or Path.cwd(), "show", f"{ref}:{path}")


def _strip_fences(text: str) -> str:
    """Models add code fences despite being asked not to."""
    if not text.startswith("```"):
        return text.rstrip() + "\n"
    lines = text.splitlines()
    end = -1 if lines and lines[-1].strip() == "```" else None
    return "\n".join(lines[1:end]).rstrip() + "\n"
