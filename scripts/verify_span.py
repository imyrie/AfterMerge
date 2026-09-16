"""Phase A2: confirm emitted spans landed in ClickHouse.

Polls, because the collector batches for 5s before exporting -- an immediate
query legitimately returns nothing and that is not a failure.

    uv run python scripts/verify_span.py <trace_id>
    uv run python scripts/verify_span.py            # most recent spans
"""

from __future__ import annotations

import os
import sys
import time

import clickhouse_connect

HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "otel")
DEADLINE_SECONDS = 40

QUERY = """
SELECT
    SpanName,
    SpanKind,
    ServiceName,
    ResourceAttributes['service.version'] AS version,
    ParentSpanId,
    round(Duration / 1e6, 3)              AS duration_ms
FROM otel_traces
WHERE {predicate}
ORDER BY Timestamp ASC
LIMIT 20
"""


def main() -> int:
    trace_id = sys.argv[1] if len(sys.argv) > 1 else None
    client = clickhouse_connect.get_client(host=HOST, port=PORT, database=DATABASE)

    if trace_id:
        predicate, params = "TraceId = {tid:String}", {"tid": trace_id}
        target = f"trace {trace_id}"
    else:
        predicate, params = "Timestamp > now() - INTERVAL 10 MINUTE", {}
        target = "spans from the last 10 minutes"

    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        try:
            result = client.query(QUERY.format(predicate=predicate), parameters=params)
        except Exception as exc:  # table absent until the exporter's first write
            if "otel_traces" not in str(exc) or time.monotonic() > deadline:
                raise
            result = None

        if result and result.result_rows:
            print(f"{len(result.result_rows)} span(s) found for {target}\n")
            rows = [result.column_names, *result.result_rows]
            widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
            for row in rows:
                print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
            return 0

        if time.monotonic() > deadline:
            print(f"no spans found for {target} after {DEADLINE_SECONDS}s", file=sys.stderr)
            print("check: docker compose logs otel-collector", file=sys.stderr)
            return 1

        print("waiting for the collector to flush its batch...", file=sys.stderr)
        time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
