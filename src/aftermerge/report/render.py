"""Rendering an investigation as markdown.

The report is organised by trust level, and the levels are always all shown --
including the empty ones. A reader should be able to see at a glance which
claims are measured, which are inferred, and which have actually been verified
by something that ran.
"""

from __future__ import annotations

from aftermerge.investigator.service import Investigation

_LEVEL_BLURB = {
    1: "Measured directly from telemetry. Each row records the query and parameters "
    "that produced it, so any number here can be re-derived.",
    2: "Inferred from the facts above, ranked by how much of the observed change they "
    "explain. Never asserted as fact.",
    3: "Confirmed by something that actually ran. Nothing reaches this level until a "
    "process exits and its exit code is recorded.",
}


def _fmt(value: float | None, unit: str | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f} {unit}".strip() if unit else f"{float(value):,.2f}"


def render(investigation: Investigation) -> str:
    incident = investigation.incident
    out: list[str] = []

    out.append(f"# Incident: {incident.service} {incident.route}")
    out.append("")
    out.append(
        f"**{incident.severity.upper()}** &middot; detected "
        f"{incident.detected_at:%Y-%m-%d %H:%M} UTC"
    )
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| baseline | `{incident.baseline_version}` |")
    out.append(f"| candidate | `{incident.candidate_version}` |")
    out.append(f"| onset | {incident.onset_at:%Y-%m-%d %H:%M} UTC |")
    out.append("")
    out.append("## Why this fired")
    out.append("")
    for reason in incident.summary.split("; "):
        out.append(f"- {reason}")
    out.append("")

    # --- level 1 ---
    out.append("## Observed facts")
    out.append("")
    out.append(f"*{_LEVEL_BLURB[1]}*")
    out.append("")
    if investigation.facts:
        out.append("| fact | value | query |")
        out.append("|---|---|---|")
        for fact in investigation.facts:
            out.append(f"| {fact.kind} | {_fmt(fact.value, fact.unit)} | `{fact.query_name}` |")
    else:
        out.append("None recorded.")
    out.append("")

    # --- correlation detail ---
    correlation = investigation.correlation
    if correlation is not None and correlation.attributions:
        out.append("### Where the work comes from")
        out.append("")
        out.append("| code site | baseline | candidate | change | in this diff |")
        out.append("|---|---|---|---|---|")
        for a in correlation.attributions:
            if a.delta == 0 and not a.changed:
                continue
            out.append(
                f"| `{a.source_path}` | {a.baseline_per_request:.1f} | "
                f"{a.candidate_per_request:.1f} | {a.delta:+.1f} | "
                f"{'yes' if a.changed else 'no'} |"
            )
        out.append("")

    # --- level 2 ---
    out.append("## Hypotheses")
    out.append("")
    out.append(f"*{_LEVEL_BLURB[2]}*")
    out.append("")
    if investigation.hypotheses:
        for h in investigation.hypotheses:
            # The number is the fraction of new work the change accounts for, not
            # a subjective confidence. Labelling it "confidence" would overstate
            # what was actually computed.
            label = (
                f"explains {float(h.score):.0%} of the new work"
                if h.kind == "change_correlation"
                else f"score {float(h.score):.2f}"
            )
            out.append(f"**{label}** — {h.statement}")
            out.append("")
            out.append(f"<sub>basis: {h.kind}; cites {len(h.supporting_fact_ids)} fact(s)</sub>")
            out.append("")
    else:
        out.append("None proposed.")
        out.append("")

    if correlation is not None:
        out.append("### Files changed by this deploy")
        out.append("")
        for change in correlation.changed_files:
            marker = "" if change.source_path else " *(outside the deployed tree)*"
            out.append(f"- `{change.repo_path}` (+{change.insertions}/-{change.deletions}){marker}")
        out.append("")

    # --- level 3 ---
    out.append("## Verified conclusions")
    out.append("")
    out.append(f"*{_LEVEL_BLURB[3]}*")
    out.append("")
    verifications = [v for h in investigation.hypotheses for v in h.verifications]
    if verifications:
        for v in verifications:
            out.append(f"**{v.method}: {v.verdict}** (exit code {v.exit_code})")
            out.append("")
            summary = str(v.metrics.get("summary") or "")
            if summary:
                out.append(summary)
                out.append("")
            # The command is part of the evidence: a conclusion nobody else can
            # re-run is a claim, not a verification.
            out.append("Reproduce with:")
            out.append("")
            out.append("```bash")
            out.append(v.command)
            out.append("```")
            out.append("")
    else:
        out.append(
            "**None.** Nothing above has been reproduced or verified by execution. "
            "Reproduction and differential replay arrive in slice 2."
        )
    out.append("")

    if investigation.notes:
        out.append("## Limitations")
        out.append("")
        for note in investigation.notes:
            out.append(f"- {note}")
        out.append("")

    return "\n".join(out)
