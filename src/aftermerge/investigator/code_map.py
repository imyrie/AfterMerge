"""Mapping a diff onto the code sites telemetry reports.

Spans carry `code.file.path` relative to the deployed source root (`/app` in the
container), so a span says `orders/repository.py`. Git says
`fixtures/shopdemo/orders/repository.py`. Correlation is only a set intersection
once both are expressed in the same coordinates, so the prefix is stripped here
rather than fudged at comparison time.

A real deployment configures the same mapping; the alternative is fuzzy path
matching, which would turn a deterministic join back into a guess.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOURCE_PREFIX = "fixtures/shopdemo"


@dataclass(frozen=True)
class ChangedFile:
    repo_path: str
    """Path as git reports it, e.g. fixtures/shopdemo/orders/repository.py."""

    source_path: str | None
    """Path as spans report it, e.g. orders/repository.py. None if outside the deployed tree."""

    insertions: int
    deletions: int

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str
    author: str
    committed_at: str


class GitUnavailable(Exception):
    """Raised when the repository cannot answer, e.g. an unreachable commit."""


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GitUnavailable(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def changed_files(
    base: str,
    head: str,
    *,
    repo_root: Path,
    source_prefix: str = DEFAULT_SOURCE_PREFIX,
) -> list[ChangedFile]:
    """Files changed between two commits, in both coordinate systems."""
    output = _git(repo_root, "diff", "--numstat", f"{base}..{head}")

    changes: list[ChangedFile] = []
    prefix = source_prefix.rstrip("/") + "/" if source_prefix else ""
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        # Binary files report "-" rather than a count.
        insertions = int(added) if added.isdigit() else 0
        deletions = int(removed) if removed.isdigit() else 0
        source_path = path[len(prefix) :] if prefix and path.startswith(prefix) else None
        changes.append(
            ChangedFile(
                repo_path=path,
                source_path=source_path,
                insertions=insertions,
                deletions=deletions,
            )
        )
    return changes


def commit_info(sha: str, *, repo_root: Path) -> CommitInfo:
    raw = _git(repo_root, "show", "-s", "--format=%H%x1f%s%x1f%an%x1f%cI", sha).strip()
    full_sha, subject, author, committed = raw.split("\x1f")
    return CommitInfo(sha=full_sha[:7], subject=subject, author=author, committed_at=committed)


def diff_for(base: str, head: str, *, repo_root: Path, path: str | None = None) -> str:
    args = ["diff", f"{base}..{head}"]
    if path:
        args += ["--", path]
    return _git(repo_root, *args)
