"""A proposed fix, and the rules it has to obey before anything runs it.

Two guards, both cheap, both checked before a single container is built.

The scope guard keeps a fix from becoming a rewrite: a patch wandering outside
the files the causing commit touched costs a reviewer more than writing the fix
by hand would have.

The test guard is the important one. If whatever proposes a patch can also edit
the test that judges it, it can make the test pass trivially and the entire
pipeline starts lying. One comparison prevents that, so it is not optional and
not configurable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: How the fix relates to the change that caused the regression. A reviewer
#: needs this: "I reverted your change" and "I rewrote your change" ask very
#: different things of them.
STRATEGIES = ("repair", "revert")

_DIFF_TARGET = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_DIFF_SOURCE = re.compile(r"^--- a/(.+)$", re.MULTILINE)


class PatchRejected(Exception):
    """Raised when a patch must not be applied, let alone validated."""


@dataclass(frozen=True)
class Patch:
    diff: str
    strategy: str
    origin: str

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise PatchRejected(f"strategy must be one of {STRATEGIES}, got {self.strategy!r}")
        if not self.diff.strip():
            raise PatchRejected("patch is empty")

    @property
    def touched_files(self) -> frozenset[str]:
        """Every path the diff writes to or reads from."""
        return frozenset(_DIFF_TARGET.findall(self.diff)) | frozenset(
            _DIFF_SOURCE.findall(self.diff)
        )

    @classmethod
    def from_file(cls, path: Path, *, strategy: str, origin: str = "hand-written") -> Patch:
        return cls(diff=path.read_text(), strategy=strategy, origin=origin)


def enforce_scope(patch: Patch, allowed: frozenset[str]) -> None:
    """Reject a patch that reaches beyond the files implicated by the incident."""
    stray = {f for f in patch.touched_files if f not in allowed and f != "/dev/null"}
    if stray:
        raise PatchRejected(
            "patch touches files the causing commit did not: "
            + ", ".join(sorted(stray))
            + ". A fix that wanders is a rewrite, not a fix."
        )


def enforce_test_untouched(patch: Patch, test_paths: frozenset[str]) -> None:
    """Reject any patch that edits the test judging it."""
    edited = patch.touched_files & test_paths
    if edited:
        raise PatchRejected(
            "patch edits the test that validates it: "
            + ", ".join(sorted(edited))
            + ". A patch that can rewrite its own examiner proves nothing."
        )
