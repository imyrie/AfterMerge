"""Tying a regression to the change that caused it.

The join is deterministic. Spans record the source file responsible for each
unit of work; git records which files a deploy changed. Intersecting the two
answers "does the diff explain the new work?" with a number, not an opinion.

The honest cases matter as much as the happy one:

* new work, explained by the diff       -> strong, mechanical correlation
* new work, NOT explained by the diff   -> weak, and the report says so
* no new work at all                    -> timing alone, capped low
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aftermerge.investigator.code_map import ChangedFile, CommitInfo, changed_files, commit_info
from aftermerge.store.tables import Fact, Incident

#: Correlation resting on timing alone can never outrank a mechanical match.
TEMPORAL_ONLY_CEILING = 0.35


@dataclass(frozen=True)
class SiteAttribution:
    source_path: str
    baseline_per_request: float
    candidate_per_request: float
    changed: bool

    @property
    def delta(self) -> float:
        return self.candidate_per_request - self.baseline_per_request


@dataclass(frozen=True)
class Correlation:
    commit: CommitInfo
    changed_files: tuple[ChangedFile, ...]
    attributions: tuple[SiteAttribution, ...]
    new_work_total: float
    new_work_attributed: float
    basis: str
    score: float
    statement: str

    @property
    def implicated_files(self) -> tuple[str, ...]:
        return tuple(a.source_path for a in self.attributions if a.changed and a.delta > 0)


def _site_deltas(fact: Fact, baseline: str, candidate: str) -> dict[str, tuple[float, float]]:
    """Per-code-site work per request, from a recorded span-count fact."""
    columns: list[str] = fact.raw_result["columns"]
    rows: list[list[object]] = fact.raw_result["rows"]
    if not {"version", "code_site", "spans_per_request"} <= set(columns):
        return {}

    v_idx = columns.index("version")
    s_idx = columns.index("code_site")
    p_idx = columns.index("spans_per_request")

    sites: dict[str, tuple[float, float]] = {}
    for row in rows:
        site = str(row[s_idx])
        base, cand = sites.get(site, (0.0, 0.0))
        value = float(row[p_idx])  # type: ignore[arg-type]
        if row[v_idx] == baseline:
            sites[site] = (base + value, cand)
        elif row[v_idx] == candidate:
            sites[site] = (base, cand + value)
    return sites


def correlate(
    incident: Incident,
    facts: list[Fact],
    *,
    repo_root: Path,
    source_prefix: str,
) -> Correlation | None:
    """Explain an incident's new work in terms of the deploy's diff."""
    commit = commit_info(incident.candidate_version, repo_root=repo_root)
    changes = tuple(
        changed_files(
            incident.baseline_version,
            incident.candidate_version,
            repo_root=repo_root,
            source_prefix=source_prefix,
        )
    )
    changed_sources = {c.source_path for c in changes if c.source_path}

    span_fact = next((f for f in facts if f.kind == "db_spans_per_request"), None)
    sites = (
        _site_deltas(span_fact, incident.baseline_version, incident.candidate_version)
        if span_fact
        else {}
    )

    attributions = tuple(
        sorted(
            (
                SiteAttribution(
                    source_path=site,
                    baseline_per_request=base,
                    candidate_per_request=cand,
                    changed=site in changed_sources,
                )
                for site, (base, cand) in sites.items()
            ),
            key=lambda a: a.delta,
            reverse=True,
        )
    )

    new_work_total = sum(a.delta for a in attributions if a.delta > 0)
    new_work_attributed = sum(a.delta for a in attributions if a.changed and a.delta > 0)

    if new_work_total > 0:
        basis = "code_site_overlap"
        score = new_work_attributed / new_work_total
        if score > 0:
            files = ", ".join(a.source_path for a in attributions if a.changed and a.delta > 0)
            statement = (
                f"Commit {commit.sha} ({commit.subject!r}) modified {files}, which accounts for "
                f"{new_work_attributed:.0f} of the {new_work_total:.0f} additional database "
                f"operations per request."
            )
        else:
            statement = (
                f"Commit {commit.sha} ({commit.subject!r}) deployed immediately before onset, but "
                f"none of the {new_work_total:.0f} additional database operations per request "
                f"originate in the files it changed."
            )
    else:
        basis = "temporal_only"
        score = TEMPORAL_ONLY_CEILING
        statement = (
            f"Commit {commit.sha} ({commit.subject!r}) deployed immediately before onset. "
            "No change in work per request was observed, so this rests on timing alone."
        )

    return Correlation(
        commit=commit,
        changed_files=changes,
        attributions=attributions,
        new_work_total=new_work_total,
        new_work_attributed=new_work_attributed,
        basis=basis,
        score=min(score, TEMPORAL_ONLY_CEILING) if basis == "temporal_only" else score,
        statement=statement,
    )
