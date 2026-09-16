"""Recording a differential replay as level-3 evidence.

The comparison is run as a **subprocess**, and the resulting
`CompletedProcess` is what reaches `VerificationRepository`. That is not
ceremony: it means the stored evidence is an exit code and stdout that anyone
can reproduce by running the same command, and it keeps the invariant intact --
nothing can record a verification it merely believes to be true.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aftermerge.store.repositories import HypothesisRepository, VerificationRepository
from aftermerge.store.tables import Verification

TIMEOUT_SECONDS = 1800


def verify_differential(
    session: Session,
    incident: Any,
    *,
    repo_root: Path,
    repeat: int = 20,
    hypothesis_id: uuid.UUID | None = None,
) -> Verification:
    """Run the replay as a real process and record its outcome."""
    if hypothesis_id is None:
        hypotheses = HypothesisRepository(session).for_incident(incident.id)
        if not hypotheses:
            raise ValueError(
                "a verification attaches to a hypothesis; run `aftermerge investigate` first"
            )
        hypothesis_id = hypotheses[0].id

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "aftermerge.cli",
            "replay",
            "--repeat",
            str(repeat),
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )

    metrics: dict[str, Any] = {}
    try:
        metrics = json.loads(process.stdout)
    except json.JSONDecodeError:
        # A crashed replay has no metrics. The exit code still tells the truth,
        # and losing the numbers must not be mistaken for a passing result.
        metrics = {"parse_error": True, "stderr_tail": process.stderr[-800:]}

    return VerificationRepository(session).record(
        hypothesis_id=hypothesis_id,
        method="differential_replay",
        process=process,
        metrics=metrics,
        # `aftermerge replay` exits 2 when it cannot run at all (no incident, no
        # replayable capture). That is not evidence against the hypothesis.
        inconclusive_codes=frozenset({2}),
    )
