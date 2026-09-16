"""Turning a proposed patch into something the sandbox can build.

The sandbox takes a commit, not a working tree, so a patch is committed to a
detached worktree and the resulting SHA is handed on. Reusing the existing
mechanism is much cheaper than teaching the sandbox about dirty trees, and it
means a patched build is verified exactly the way every other build is.

The commit is made on a detached HEAD, so nothing is left on a branch. It stays
reachable by SHA for as long as validation needs it, and the object database
collects it later if the PR step never claims it.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aftermerge.patcher.patch import Patch, PatchRejected


class PatchApplyFailed(PatchRejected):
    """The patch does not apply cleanly to the base commit."""


def _git(repo_root: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        input=stdin,
    )
    if result.returncode != 0:
        raise PatchApplyFailed(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


@contextmanager
def patched_commit(patch: Patch, *, base_ref: str, repo_root: Path) -> Iterator[str]:
    """Apply `patch` on top of `base_ref` and yield the resulting commit SHA."""
    scratch = repo_root / ".worktrees" / f"patch-{uuid.uuid4().hex[:8]}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", "--detach", "--quiet", str(scratch), base_ref)

    try:
        # --index so the change is staged; a patch that does not apply cleanly is
        # rejected here rather than producing a half-patched tree that builds.
        _git(scratch, "apply", "--index", "-", stdin=patch.diff)
        _git(
            scratch,
            "-c",
            "user.name=AfterMerge",
            "-c",
            "user.email=aftermerge@localhost",
            "commit",
            "--quiet",
            "-m",
            f"Candidate fix ({patch.strategy}) from {patch.origin}",
        )
        yield _git(scratch, "rev-parse", "--short", "HEAD").strip()
    finally:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(scratch)],
            capture_output=True,
            text=True,
        )
