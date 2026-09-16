"""Phase A2: prove the telemetry path before any application exists.

Emits one parent span with one child, straight to the collector over OTLP/gRPC.
If these spans reach ClickHouse, the whole pipe -- SDK, exporter, collector,
ClickHouse schema creation -- is known good, and any later failure is application
code rather than plumbing.

    uv run python scripts/emit_test_span.py
"""

from __future__ import annotations

import os
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")


def main() -> int:
    resource = Resource.create(
        {
            "service.name": "phase-a-probe",
            # Slice 0 leans on this attribute to separate deploys. Setting it here
            # confirms resource attributes survive the trip into ClickHouse.
            "service.version": "probe-0001",
            "deployment.environment": "local",
        }
    )

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("aftermerge.probe")

    with tracer.start_as_current_span("probe-parent") as parent:
        trace_id = format(parent.get_span_context().trace_id, "032x")
        parent.set_attribute("aftermerge.probe", True)
        with tracer.start_as_current_span("probe-child") as child:
            child.set_attribute("code.file.path", __file__)

    # BatchSpanProcessor does NOT flush on interpreter exit. Without this the
    # process ends, the spans are dropped, and the empty table looks like a
    # collector fault. This single line is the most common Phase A bug.
    flushed = provider.force_flush(timeout_millis=10_000)
    provider.shutdown()

    if not flushed:
        print(f"force_flush timed out -- collector unreachable at {ENDPOINT}?", file=sys.stderr)
        return 1

    print(f"endpoint  {ENDPOINT}")
    print(f"trace_id  {trace_id}")
    print("\nverify with:")
    print(f"  uv run python scripts/verify_span.py {trace_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
