"""Did the fix change what users actually receive?

The regression test cannot answer this. It asserts that database work does not
scale with page size, and a patch that returns fewer rows, caches stale results,
or drops a join satisfies it perfectly while being broken. That is Goodhart's law
arriving inside the pipeline: once a measure becomes an optimiser's target it
stops measuring what it measured.

So the fix is also required to produce byte-identical responses to the known-good
build. This is only meaningful because the sandbox seed is deterministic --
verified: two independent sandboxes at the same commit return identical bodies.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import httpx

from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.sandbox import Sandbox


@dataclass(frozen=True)
class ResponseSnapshot:
    ref: str
    status_code: int
    body: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class EquivalenceResult:
    good: ResponseSnapshot
    patched: ResponseSnapshot
    normalisations: tuple[str, ...] = ()
    difference: str | None = field(default=None)

    @property
    def equivalent(self) -> bool:
        return self.difference is None

    @property
    def summary(self) -> str:
        if self.equivalent:
            note = (
                f" after {len(self.normalisations)} normalisation(s)" if self.normalisations else ""
            )
            return (
                f"responses from {self.good.ref} and {self.patched.ref} are identical{note} "
                f"({len(self.good.body)} bytes, sha256 {self.good.digest[:12]})"
            )
        return f"responses differ: {self.difference}"


def snapshot(
    sandbox: Sandbox, envelope: RequestEnvelope, *, timeout: float = 120.0
) -> ResponseSnapshot:
    response = httpx.get(
        f"{sandbox.base_url}{envelope.path}",
        params=envelope.query or None,
        headers=envelope.headers or None,
        timeout=timeout,
    )
    return ResponseSnapshot(
        ref=sandbox.sha, status_code=response.status_code, body=response.content
    )


def _normalise(body: bytes, patterns: tuple[str, ...]) -> bytes:
    text = body.decode("utf-8", errors="replace")
    for pattern in patterns:
        text = re.sub(pattern, "<normalised>", text)
    return text.encode("utf-8")


def compare(
    good: ResponseSnapshot,
    patched: ResponseSnapshot,
    *,
    normalisations: tuple[str, ...] = (),
) -> EquivalenceResult:
    """Require the patched build to answer exactly as the known-good build does.

    `normalisations` are declared in the scenario file rather than buried here,
    so that the set of tolerated differences is reviewable and cannot quietly
    widen over time.
    """
    difference: str | None = None

    if good.status_code != patched.status_code:
        difference = f"status {good.status_code} became {patched.status_code}"
    else:
        left = _normalise(good.body, normalisations)
        right = _normalise(patched.body, normalisations)
        if left != right:
            if len(left) != len(right):
                difference = (
                    f"body length {len(left)} became {len(right)} "
                    f"({len(right) - len(left):+d} bytes)"
                )
            else:
                at = next(
                    (i for i, (a, b) in enumerate(zip(left, right, strict=True)) if a != b), 0
                )
                difference = f"bodies differ at byte {at} (same length, {len(left)})"

    return EquivalenceResult(
        good=good, patched=patched, normalisations=normalisations, difference=difference
    )
