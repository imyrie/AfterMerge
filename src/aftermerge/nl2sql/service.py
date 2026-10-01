"""Orchestrating a metric request end to end.

Deliberately does not touch the audit trail. A fact in this pipeline requires an
incident it is evidence *for*, and an ad-hoc metric question is not evidence
about anything -- recording one would mean inventing an incident to hang it on,
which is exactly the kind of convenient fiction the trust ladder exists to
prevent. Generated SQL is an exploration tool; the catalog is what produces
evidence, and a query that earns a place in the catalog is one a person has
reviewed and named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aftermerge.nl2sql import execute, schema
from aftermerge.nl2sql.agent import DEFAULT_MAX_ATTEMPTS, Proposal, propose
from aftermerge.nl2sql.benchmark import Comparison, Reference, compare


@dataclass(frozen=True)
class AskResult:
    proposal: Proposal
    outcome: execute.Outcome | None = None

    @property
    def accepted(self) -> bool:
        return self.proposal.accepted

    @property
    def answered(self) -> bool:
        return self.outcome is not None and self.outcome.ok


def ask(
    request: str,
    *,
    ch: Any,
    client: Any,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> AskResult:
    """Generate SQL for one request, then run it if the gate accepted it."""
    card = schema.build(ch)
    proposal = propose(
        request,
        card=card,
        client=client,
        ch=ch,
        model=model,
        max_attempts=max_attempts,
    )
    if not proposal.accepted:
        return AskResult(proposal=proposal)
    return AskResult(proposal=proposal, outcome=execute.run(ch, proposal.sql))


def answer_reference(
    reference: Reference,
    *,
    ch: Any,
    client: Any,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[Proposal, Comparison]:
    """Generate SQL for a reference case and diff it against the catalog query.

    A proposal the gate refused is reported as a disagreement rather than
    skipped: from the benchmark's point of view "would not pass the gate" and
    "passed the gate but computed the wrong thing" are both failures to answer
    the question, and collapsing them would flatter whichever model fails early.
    """
    card = schema.build(ch)
    proposal = propose(
        reference.request,
        card=card,
        client=client,
        ch=ch,
        model=model,
        max_attempts=max_attempts,
    )
    if not proposal.accepted:
        return proposal, Comparison(
            reference.name, False, f"never passed the gate: {proposal.rejection_summary}"
        )
    return proposal, compare(reference, proposal.sql, ch)
