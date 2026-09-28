"""Consuming spans from Kafka and reaching a verdict during the rollout.

Offsets are committed only after a batch has been folded into the windows. At
least once is the right guarantee here: a replayed message re-counts a few spans
into a rolling window, which moves an average slightly, whereas losing messages
would silently understate the very amplification being watched for.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from aftermerge.detector.rules import SLO, Detection
from aftermerge.streaming.decode import decode
from aftermerge.streaming.windows import DEFAULT_MAX_SAMPLES, StreamState

DEFAULT_BROKERS = "localhost:29092"
DEFAULT_TOPIC = "otel-spans"
DEFAULT_GROUP = "aftermerge-streaming-detector"
BROKERS_ENV = "AFTERMERGE_KAFKA_BROKERS"


@dataclass(frozen=True)
class StreamVerdict:
    baseline: str
    candidate: str
    detection: Detection
    spans_seen: int


def brokers() -> str:
    return os.environ.get(BROKERS_ENV, DEFAULT_BROKERS)


def build_consumer(group: str = DEFAULT_GROUP, *, from_beginning: bool = False) -> Any:
    from confluent_kafka import Consumer

    return Consumer(
        {
            "bootstrap.servers": brokers(),
            "group.id": group,
            "auto.offset.reset": "earliest" if from_beginning else "latest",
            # Committed explicitly after folding a batch in, so a crash replays
            # the batch rather than skipping it.
            "enable.auto.commit": False,
        }
    )


def consume(
    state: StreamState,
    slo: SLO,
    *,
    consumer: Any,
    topic: str = DEFAULT_TOPIC,
    min_samples: int = 30,
    evaluate_every: int = 200,
    max_messages: int | None = None,
    poll_timeout: float = 1.0,
    idle_timeout: float | None = None,
    on_verdict: Callable[[StreamVerdict], None] | None = None,
) -> Iterator[StreamVerdict]:
    """Fold spans into rolling windows, yielding a verdict as it changes.

    `min_samples` is far below the batch detector's default. A streaming verdict
    is deliberately an early signal on thin evidence -- it says "this looks wrong
    already", and the batch path remains what confirms it.
    """
    consumer.subscribe([topic])
    spans_seen = 0
    since_evaluation = 0
    last: tuple[str, str, bool] | None = None
    # Without this a replay of a finite topic never returns: the poll simply
    # keeps timing out against a drained partition.
    idle_since: float | None = None

    try:
        while max_messages is None or spans_seen < max_messages:
            message = consumer.poll(poll_timeout)
            if message is None or message.error():
                if idle_timeout is not None:
                    now = time.monotonic()
                    idle_since = idle_since or now
                    if now - idle_since >= idle_timeout:
                        break
                continue
            idle_since = None

            for span in decode(message.value()):
                state.observe(span)
                spans_seen += 1
                since_evaluation += 1

            consumer.commit(message=message, asynchronous=False)

            if since_evaluation < evaluate_every:
                continue
            since_evaluation = 0

            outcome = state.compare(slo, min_samples=min_samples)
            if outcome is None:
                continue
            baseline, candidate, detection = outcome

            # Only emit when the verdict actually changes, or a rollout produces
            # one line per evaluation and the signal is lost in the noise.
            signature = (baseline, candidate, detection.triggered)
            if signature == last:
                continue
            last = signature

            verdict = StreamVerdict(
                baseline=baseline,
                candidate=candidate,
                detection=detection,
                spans_seen=spans_seen,
            )
            if on_verdict is not None:
                on_verdict(verdict)
            yield verdict
        outcome = state.compare(slo, min_samples=min_samples)
        if outcome is not None:
            baseline, candidate, detection = outcome
            if (baseline, candidate, detection.triggered) != last:
                verdict = StreamVerdict(
                    baseline=baseline,
                    candidate=candidate,
                    detection=detection,
                    spans_seen=spans_seen,
                )
                if on_verdict is not None:
                    on_verdict(verdict)
                yield verdict
    finally:
        consumer.close()


__all__ = [
    "DEFAULT_BROKERS",
    "DEFAULT_GROUP",
    "DEFAULT_MAX_SAMPLES",
    "DEFAULT_TOPIC",
    "StreamVerdict",
    "build_consumer",
    "consume",
]
