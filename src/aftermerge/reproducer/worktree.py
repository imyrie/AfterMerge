"""Checked-out commits and built images, cached by SHA.

Rebuilding shopdemo for every replay turns a twenty-second check into a
three-minute one, so both the worktree and the image are cached and reused.

Worktrees are always **detached**. Checking out a branch name would fail the
moment that branch is checked out anywhere else, which is exactly the situation
during development.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKTREE_DIR = ".worktrees"
IMAGE_PREFIX = "shopdemo"


class WorktreeError(Exception):
    pass


def _run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or f"{' '.join(args)} failed")
    return result.stdout


def resolve(ref: str, *, repo_root: Path) -> str:
    return _run(["git", "-C", str(repo_root), "rev-parse", "--short", ref]).strip()


def ensure_worktree(sha: str, *, repo_root: Path) -> Path:
    """A detached worktree at `sha`, created once and reused."""
    path = repo_root / WORKTREE_DIR / sha
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(repo_root), "worktree", "add", "--detach", "--quiet", str(path), sha])
    return path


def ensure_image(sha: str, *, repo_root: Path) -> str:
    """Build `shopdemo:<sha>` from the worktree if it is not already present."""
    tag = f"{IMAGE_PREFIX}:{sha}"
    existing = _run(["docker", "images", "-q", tag]).strip()
    if existing:
        return tag

    worktree = ensure_worktree(sha, repo_root=repo_root)
    _run(["docker", "build", "-q", "-t", tag, str(worktree / "fixtures" / "shopdemo")])
    return tag


def remove_worktree(sha: str, *, repo_root: Path) -> None:
    path = repo_root / WORKTREE_DIR / sha
    if path.is_dir():
        _run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(path)])
