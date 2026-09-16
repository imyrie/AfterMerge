"""Shared OpenTelemetry wiring for the shopdemo services (slice 0, step B2)."""

from __future__ import annotations

import os

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from common.code_site import CodeSiteSpanProcessor

# Mounted at /app in the container; code.file.path is emitted relative to this.
SOURCE_ROOT = os.environ.get("SHOPDEMO_SOURCE_ROOT", "/app")


def configure(service_name: str) -> None:
    """Install the tracer provider. Call once, before the app handles traffic."""
    resource = Resource.create(
        {
            "service.name": service_name,
            # The deploy boundary. Slice 0 separates "before" from "after" with
            # GROUP BY on this attribute rather than by guessing from timestamps,
            # which keeps the comparison correct across rollbacks and overlaps.
            "service.version": os.environ.get("GIT_SHA", "dev"),
            "deployment.environment": os.environ.get("DEPLOY_ENV", "demo"),
        }
    )

    provider = TracerProvider(resource=resource)
    # Ordering matters: on_start must run before the batch processor sees the span.
    provider.add_span_processor(CodeSiteSpanProcessor(SOURCE_ROOT))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=os.environ.get(
                    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
                ),
                insecure=True,
            )
        )
    )
    trace.set_tracer_provider(provider)

    # asyncpg gives one span per query -- the signal the whole N+1 demo rests on.
    AsyncPGInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def instrument_app(app: FastAPI) -> None:
    FastAPIInstrumentor.instrument_app(app)
