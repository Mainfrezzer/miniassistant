"""Session-scoped LRU cache for read_url results.

Shared between orchestrator and subagents within a single chat session.
Thread-safe — subagents run in parallel.

Limits: 200 entries, 100 MB total raw bytes. Eviction = LRU on either cap.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any


MAX_ENTRIES = 200
MAX_BYTES = 100 * 1024 * 1024  # 100 MB


@dataclass
class CacheEntry:
    raw: str
    tokens: int
    fetched_at: float


class UrlCache:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> None:
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._inflight: dict[str, Event] = {}
        self._lock = Lock()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dedup_waits = 0

    def get(self, url: str) -> CacheEntry | None:
        with self._lock:
            entry = self._store.get(url)
            if entry is None:
                self.misses += 1
                return None
            self._store.move_to_end(url)
            self.hits += 1
            return entry

    def put(self, url: str, raw: str, tokens: int) -> None:
        size = len(raw.encode("utf-8"))
        with self._lock:
            if url in self._store:
                old = self._store.pop(url)
                self._current_bytes -= len(old.raw.encode("utf-8"))
            # Single entry larger than cap: skip caching (don't wipe cache for one oversized item)
            if size > self._max_bytes:
                return
            self._store[url] = CacheEntry(raw=raw, tokens=tokens, fetched_at=time.time())
            self._current_bytes += size
            # Evict oldest while over cap, but never the just-inserted entry
            while len(self._store) > 1 and (
                len(self._store) > self._max_entries or self._current_bytes > self._max_bytes
            ):
                oldest_key = next(iter(self._store))
                if oldest_key == url:
                    break
                evicted = self._store.pop(oldest_key)
                self._current_bytes -= len(evicted.raw.encode("utf-8"))
                self.evictions += 1

    def begin_fetch(self, url: str) -> tuple[bool, Event]:
        """Claim leadership for a fetch. Returns (is_leader, event).
        Leader must call finish_fetch() after put() (or on failure).
        Follower should event.wait() then re-check cache."""
        with self._lock:
            existing = self._inflight.get(url)
            if existing is not None:
                self.dedup_waits += 1
                return False, existing
            evt = Event()
            self._inflight[url] = evt
            return True, evt

    def finish_fetch(self, url: str) -> None:
        """Signal waiters that a fetch has completed (success or failure)."""
        with self._lock:
            evt = self._inflight.pop(url, None)
        if evt is not None:
            evt.set()

    def urls(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._store),
                "bytes": self._current_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "dedup_waits": self.dedup_waits,
                "inflight": len(self._inflight),
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._current_bytes = 0
            pending = list(self._inflight.values())
            self._inflight.clear()
        for evt in pending:
            evt.set()


def get_cache(config: dict[str, Any]) -> UrlCache:
    """Fetch or create session-scoped cache in config['_chat_context']."""
    ctx = config.get("_chat_context")
    if ctx is None:
        ctx = {}
        config["_chat_context"] = ctx
    cache = ctx.get("url_cache")
    if not isinstance(cache, UrlCache):
        cache = UrlCache()
        ctx["url_cache"] = cache
    return cache
