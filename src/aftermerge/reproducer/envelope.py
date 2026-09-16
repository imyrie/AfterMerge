"""The request envelope: what gets replayed.

Defined before anything captures into it, on purpose. Replay is written against
this type from the start, so production capture (slice 2 part 2) becomes another
producer of an existing shape rather than a refactor of the replay path.

`replay_safe` is not advisory. A request that cannot be shown safe is not
replayed, and the flag is set by whoever sanitises the request -- never defaulted
to True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

#: Headers that must never survive into a replay. Credentials replayed into a
#: sandbox are still credentials sitting in a database and a log.
FORBIDDEN_HEADERS = frozenset(
    {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
)

#: Headers worth keeping, because they change how a request is handled.
SAFE_HEADERS = frozenset({"accept", "accept-encoding", "content-type", "user-agent"})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class UnsafeRequest(Exception):
    """Raised when an envelope cannot be constructed safely."""


@dataclass(frozen=True)
class RequestEnvelope:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    replay_safe: bool = False
    source_trace_id: str | None = None

    def __post_init__(self) -> None:
        leaked = FORBIDDEN_HEADERS & {h.lower() for h in self.headers}
        if leaked:
            raise UnsafeRequest(
                f"credential headers must be stripped before replay: {sorted(leaked)}"
            )
        if not self.path.startswith("/"):
            raise UnsafeRequest(f"path must be absolute, got {self.path!r}")

    @property
    def is_mutating(self) -> bool:
        return self.method.upper() not in SAFE_METHODS

    @property
    def target(self) -> str:
        return f"{self.path}?{urlencode(self.query)}" if self.query else self.path

    @classmethod
    def get(cls, path: str, **query: Any) -> RequestEnvelope:
        """A hand-written read-only request, for fixtures and tests."""
        return cls(
            method="GET",
            path=path,
            query={k: str(v) for k, v in query.items()},
            replay_safe=True,
        )

    @classmethod
    def sanitised(
        cls,
        *,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        source_trace_id: str | None = None,
    ) -> RequestEnvelope:
        """Build an envelope from raw request data, dropping anything unsafe.

        Only allowlisted headers survive. Denylisting is the wrong default: a
        header added next year would be replayed before anyone noticed.
        """
        kept = {
            name.lower(): value
            for name, value in (headers or {}).items()
            if name.lower() in SAFE_HEADERS
        }
        return cls(
            method=method.upper(),
            path=path,
            query=dict(query or {}),
            headers=kept,
            body=body,
            replay_safe=True,
            source_trace_id=source_trace_id,
        )
