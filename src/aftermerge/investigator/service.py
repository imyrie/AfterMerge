"""Run an investigation over an already-detected incident."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from aftermerge.investigator.code_map import DEFAULT_SOURCE_PREFIX, GitUnavailable
from aftermerge.investigator.correlation import Correlation, correlate
from aftermerge.store.repositories import FactRepository, HypothesisRepository
from aftermerge.store.tables import Fact, Hypothesis, Incident


@dataclass
class Investigation:
    incident: Incident
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    correlation: Correlation | None
    notes: list[str]


def investigate(
    session: Session,
    incident: Incident,
    *,
    repo_root: Path,
    source_prefix: str = DEFAULT_SOURCE_PREFIX,
) -> Investigation:
    """Correlate an incident's evidence with the deploy that preceded it."""
    facts = FactRepository(session).for_incident(incident.id)
    notes: list[str] = []
    correlation: Correlation | None = None

    try:
        correlation = correlate(incident, facts, repo_root=repo_root, source_prefix=source_prefix)
    except GitUnavailable as exc:
        # A missing commit is a real, reportable limitation -- not a reason to
        # invent a conclusion, and not something to swallow silently.
        notes.append(f"change correlation unavailable: {exc}")

    if correlation is not None:
        span_fact = next((f for f in facts if f.kind == "db_spans_per_request"), None)
        supporting = [f.id for f in facts if f.kind == "db_spans_per_request"] or [
            f.id for f in facts[:1]
        ]
        if supporting:
            HypothesisRepository(session).supersede(
                incident_id=incident.id,
                statement=correlation.statement,
                kind=correlation.kind,
                score=correlation.score,
                supporting_fact_ids=supporting,
                generated_by="correlation.correlate",
            )
        elif span_fact is None:
            notes.append("no evidence recorded, so no hypothesis could be proposed")

    hypotheses = HypothesisRepository(session).for_incident(incident.id)
    return Investigation(
        incident=incident,
        facts=facts,
        hypotheses=hypotheses,
        correlation=correlation,
        notes=notes,
    )
