"""Sending captured requests at a sandbox and measuring what happens.

`replay` accepts a `Sandbox`, never a URL. That is the whole safety model: a
captured production request cannot be aimed at production, because there is no
argument through which to express it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from aftermerge.reproducer.envelope import RequestEnvelope
from aftermerge.reproducer.sandbox import Sandbox
from aftermerge.telemetry import client


class ReplayRefused(Exception):
    """Raised when an envelope must not be replayed."""


@dataclass(frozen=True)
class ReplayMeasurement:
    sha: str
    requests_sent: int
    failures: int
    db_spans_per_request: float
    code_site: str | None
    trace_database: str

    @property
    def clean(self) -> bool:
        return self.failures == 0


def replay(
    sandbox: Sandbox,
    envelope: RequestEnvelope,
    *,
    repeat: int = 20,
    timeout: float = 120.0,
) -> ReplayMeasurement:
    """Send one request shape `repeat` times and measure the work it caused."""
    if not envelope.replay_safe:
        raise ReplayRefused(
            f"{envelope.method} {envelope.path} is not marked replay-safe; "
            "capture flagged it, and replay does not second-guess that"
        )

    # Health polling during startup also creates root traces, so measure the
    # delta rather than an absolute count.
    traces_before = sandbox.trace_count()

    failures = 0
    with httpx.Client(base_url=sandbox.base_url, timeout=timeout) as http:
        for _ in range(repeat):
            try:
                response = http.request(
                    envelope.method,
                    envelope.path,
                    params=envelope.query or None,
                    headers=envelope.headers or None,
                    content=envelope.body,
                )
                if response.status_code >= 500:
                    failures += 1
            except httpx.HTTPError:
                failures += 1

    # Wait for every request we sent to be accounted for, then for export to go
    # quiet. Measuring while spans are still arriving understates the per-request
    # count, and does so silently.
    delivered = repeat - failures
    sandbox.wait_for_traces(traces_before + delivered)
    sandbox.wait_until_quiet(minimum=delivered)

    # The same named query production uses, pointed at this sandbox's own
    # database. Identical SQL on both sides is what makes the comparison mean
    # something.
    ch = client.get_client(database=sandbox.trace_database)
    result = client.run("span_count_per_trace", client=ch, service="orders", lookback_minutes=60)

    total = 0.0
    top_site: str | None = None
    top_value = 0.0
    if "spans_per_request" in result.columns:
        site_idx = result.columns.index("code_site")
        per_idx = result.columns.index("spans_per_request")
        for row in result.rows:
            value = float(row[per_idx])
            total += value
            if value > top_value:
                top_value, top_site = value, str(row[site_idx])

    return ReplayMeasurement(
        sha=sandbox.sha,
        requests_sent=repeat,
        failures=failures,
        db_spans_per_request=total,
        code_site=top_site,
        trace_database=sandbox.trace_database,
    )
