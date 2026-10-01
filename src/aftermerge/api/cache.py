"""A small TTL cache for metric responses.

The TTL is not an arbitrary tuning knob. These metrics are read from daily
rollups that the Airflow DAG refreshes hourly (see `docs/airflow.md`), so a
cache entry living longer than that refresh interval serves numbers the
warehouse has already corrected, while one living much shorter spends a
warehouse scan to re-derive a value that provably cannot have changed. The
default is therefore tied to the pipeline's own cadence rather than guessed.

Single-flight matters more than the hit rate here. Without it, N concurrent
requests for a cold key issue N identical warehouse scans -- the exact moment
the cache is most needed is the moment it would do nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

#: Matches the hourly rollup refresh. An entry is never useful past it.
DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ENTRIES = 256


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    #: Requests that waited on another request's in-flight load rather than
    #: issuing their own. Counted separately from hits because they did not read
    #: a stored value -- they avoided a duplicate query, which is a different
    #: saving and worth seeing on its own.
    coalesced: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses + self.coalesced

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
            "evictions": self.evictions,
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass
class _Entry[T]:
    value: T
    expires_at: float


class TTLCache[T]:
    """Async-safe, bounded, single-flight."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.stats = CacheStats()
        self._entries: dict[str, _Entry[T]] = {}
        self._inflight: dict[str, asyncio.Future[T]] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.monotonic()

    async def get_or_load(self, key: str, load: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        """Return the cached value, or load it. The flag says whether it was a hit.

        Three outcomes, which is why the lock is held only around bookkeeping and
        never across the load itself: a live entry is returned immediately; an
        in-flight load for the same key is awaited rather than duplicated; and
        otherwise this caller takes responsibility for loading.
        """
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > self._now():
                self.stats.hits += 1
                return entry.value, True
            if entry is not None:
                del self._entries[key]

            inflight = self._inflight.get(key)
            if inflight is not None:
                self.stats.coalesced += 1
                waiter = inflight
            else:
                self.stats.misses += 1
                waiter = None
                future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
                self._inflight[key] = future

        if waiter is not None:
            # Shielded so a cancelled waiter cannot cancel the shared load and
            # strand every other caller waiting on it.
            return await asyncio.shield(waiter), False

        try:
            value = await load()
        except BaseException as exc:
            async with self._lock:
                pending = self._inflight.pop(key, None)
            if pending is not None and not pending.done():
                pending.set_exception(exc)
            raise
        else:
            async with self._lock:
                self._store(key, value)
                pending = self._inflight.pop(key, None)
            if pending is not None and not pending.done():
                pending.set_result(value)
            return value, False

    def _store(self, key: str, value: T) -> None:
        if len(self._entries) >= self.max_entries:
            # Oldest expiry first: a bound that evicts what was closest to
            # useless anyway, without the bookkeeping an LRU would need.
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]
            self.stats.evictions += 1
        self._entries[key] = _Entry(value=value, expires_at=self._now() + self.ttl_seconds)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)
