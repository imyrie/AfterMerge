"""Turning a validated patch into a branch, and optionally offering it outward.

Local by default. Opening a pull request notifies people and is awkward to
retract, so it never happens as a side effect of an investigation: the branch and
body are produced on disk, and going outward takes an explicit flag.

The branch is built in a detached worktree and then pointed at by name, so the
user's working tree and current checkout are never touched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from aftermerge.patcher.patch import Patch
from aftermerge.patcher.tree import patched_commit

#: Branches AfterMerge created itself. They descend from the bad commit, so
#: `git branch --contains` lists them -- and inferring one as the base would aim
#: a pull request at its own head.
FIX_BRANCH_PREFIX = "aftermerge/"


class PullRequestError(Exception):
    pass


@dataclass(frozen=True)
class FixBranch:
    name: str
    sha: str
    base: str
    diffstat: str


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise PullRequestError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def infer_base(bad_ref: str, *, repo_root: Path, exclude: frozenset[str] = frozenset()) -> str:
    """The branch the regression actually lives on.

    A fix has to target wherever the bad commit is, which is not necessarily the
    default branch -- in this repository the regression sits on its own branch.
    Guessing "main" would open a PR against a branch that never contained the bug.
    """
    raw = _git(repo_root, "branch", "--contains", bad_ref, "--format=%(refname:short)")
    names = [
        n.strip()
        for n in raw.splitlines()
        if n.strip()
        and "HEAD" not in n
        and not n.strip().startswith(FIX_BRANCH_PREFIX)
        and n.strip() not in exclude
    ]
    if not names:
        raise PullRequestError(f"no local branch contains {bad_ref}; pass --base explicitly")
    # Prefer a branch that is not the default, since the regression branch is the
    # more specific answer when a commit is on both.
    preferred = [n for n in names if n not in {"main", "master"}]
    return (preferred or names)[0]


def create_branch(
    patch: Patch,
    *,
    bad_ref: str,
    branch_name: str,
    repo_root: Path,
    message: str,
) -> FixBranch:
    """Commit the patch onto a new branch without disturbing the working tree."""
    existing = _git(repo_root, "branch", "--list", branch_name).strip()
    if existing:
        raise PullRequestError(f"branch {branch_name!r} already exists; delete it or pass --branch")

    with patched_commit(patch, base_ref=bad_ref, repo_root=repo_root, message=message) as sha:
        # Name the commit before the worktree goes away, so the branch holds the
        # exact tree that was validated rather than a rebuilt equivalent.
        _git(repo_root, "branch", branch_name, sha)

    diffstat = _git(repo_root, "diff", "--stat", f"{bad_ref}..{branch_name}")
    return FixBranch(name=branch_name, sha=sha, base=bad_ref, diffstat=diffstat)


def push_branch(branch: FixBranch, *, repo_root: Path, remote: str = "origin") -> str:
    return _git(repo_root, "push", "-u", remote, branch.name)


def gh_command(branch: FixBranch, base: str, title: str, body_path: Path) -> list[str]:
    """The command that would open the PR. Printed, not run, unless asked."""
    return [
        "gh",
        "pr",
        "create",
        "--base",
        base,
        "--head",
        branch.name,
        "--title",
        title,
        "--body-file",
        str(body_path),
        "--draft",
    ]
