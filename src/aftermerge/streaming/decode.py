"""Turning OTLP JSON off the topic into flat span records.

The collector is configured to emit `otlp_json` rather than the default
protobuf, so this needs no generated stubs and a message can be read by eye
while debugging.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: OTLP span kind enum. The JSON encoding emits the integer, not the name.
SPAN_KINDS = {1: "Internal", 2: "Server", 3: "Client", 4: "Producer", 5: "Consumer"}

#: OTLP status code enum. 0 unset, 1 ok, 2 error.
STATUS_ERROR = 2


@dataclass(frozen=True)
class SpanRecord:
    service: str
    version: str
    name: str
    kind: str
    trace_id: str
    duration_ms: float
    is_error: bool
    code_site: str | None


def _attributes(items: list[dict[str, Any]] | None) -> dict[str, str]:
    """Flatten OTLP's typed key/value list into plain strings."""
    flat: dict[str, str] = {}
    for item in items or []:
        value = item.get("value") or {}
        for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if key in value:
                flat[item.get("key", "")] = str(value[key])
                break
    return flat


def decode(payload: bytes | str) -> list[SpanRecord]:
    """Decode one Kafka message into span records.

    A malformed message yields no spans rather than raising: one bad payload
    should not stop a consumer that is otherwise healthy, and the offset still
    advances.
    """
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    records: list[SpanRecord] = []
    for resource_span in document.get("resourceSpans", []) or []:
        resource = _attributes((resource_span.get("resource") or {}).get("attributes"))
        service = resource.get("service.name", "")
        version = resource.get("service.version", "")

        for scope_span in resource_span.get("scopeSpans", []) or []:
            for span in scope_span.get("spans", []) or []:
                try:
                    start = int(span.get("startTimeUnixNano", 0))
                    end = int(span.get("endTimeUnixNano", 0))
                except (TypeError, ValueError):
                    start = end = 0
                attributes = _attributes(span.get("attributes"))
                status = span.get("status") or {}
                records.append(
                    SpanRecord(
                        service=service,
                        version=version,
                        name=str(span.get("name", "")),
                        kind=SPAN_KINDS.get(int(span.get("kind", 0) or 0), "Unspecified"),
                        trace_id=str(span.get("traceId", "")),
                        duration_ms=max(0.0, (end - start) / 1e6),
                        is_error=int(status.get("code", 0) or 0) == STATUS_ERROR,
                        code_site=attributes.get("code.file.path") or None,
                    )
                )
    return records
