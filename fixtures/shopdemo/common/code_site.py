"""Attach source location to every span (slice 0, step B3).

Auto-instrumented spans -- especially asyncpg's one-span-per-query -- record what
happened but not *where in your code* it was triggered from. Without that, tying a
regression to a pull request means correlating on timestamps and hoping.

With it, correlation becomes a set intersection:

    changed_files(PR #42) INTERSECT distinct(SpanAttributes['code.file.path'])

which is an observed fact rather than an inference.

Paths are emitted **relative to the repository root** so they match `git diff --name-only`
output directly, with no normalisation step at query time.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

# Frames belonging to these paths are plumbing, never the answer we want.
_SKIP_MARKERS: Sequence[str] = ("site-packages", "dist-packages", "/usr/lib/python")


class CodeSiteSpanProcessor(SpanProcessor):
    """Record the first application stack frame responsible for each span."""

    def __init__(self, source_root: str, max_depth: int = 60) -> None:
        self._root = os.path.realpath(source_root)
        self._max_depth = max_depth

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        frame = sys._getframe(1)
        depth = 0

        while frame is not None and depth < self._max_depth:
            filename = frame.f_code.co_filename

            if filename.startswith(self._root) and not any(m in filename for m in _SKIP_MARKERS):
                span.set_attribute("code.file.path", os.path.relpath(filename, self._root))
                span.set_attribute("code.function.name", frame.f_code.co_name)
                span.set_attribute("code.line.number", frame.f_lineno)
                return

            frame = frame.f_back
            depth += 1

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
