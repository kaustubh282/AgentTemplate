"""Safe response / retrieval caching (master prompt §18.1).

Only low-risk, read-only, version-stable workloads are cacheable. Cache keys embed
every version dimension that could make an answer stale, and customer-specific
transactional responses are structurally refused rather than merely discouraged.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.observability.metrics import (
    CACHE_HITS_TOTAL,
    CACHE_MISSES_TOTAL,
    CACHE_STALE_PREVENTED_TOTAL,
    metrics,
)


class Cache[T](Protocol):
    def get(self, key: str) -> T | None: ...
    def set(self, key: str, value: T) -> None: ...
    def invalidate_prefix(self, prefix: str) -> int: ...


@dataclass(slots=True)
class _Entry[T]:
    value: T
    expires_at: float


class TtlLruCache[T]:
    """Bounded TTL + LRU cache."""

    def __init__(self, max_entries: int = 512, ttl_seconds: int = 900) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds

    def get(self, key: str) -> T | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                metrics.increment(CACHE_MISSES_TOTAL)
                return None
            if entry.expires_at <= now:
                del self._entries[key]
                metrics.increment(CACHE_STALE_PREVENTED_TOTAL)
                metrics.increment(CACHE_MISSES_TOTAL)
                return None
            self._entries.move_to_end(key)
            metrics.increment(CACHE_HITS_TOTAL)
            return entry.value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._entries[key] = _Entry(value, time.monotonic() + self._ttl)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._entries if k.startswith(prefix)]
            for key in keys:
                del self._entries[key]
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class NoOpCache[T]:
    def get(self, key: str) -> T | None:
        metrics.increment(CACHE_MISSES_TOTAL)
        return None

    def set(self, key: str, value: T) -> None:
        return None

    def invalidate_prefix(self, prefix: str) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class FaqCacheKey:
    """Every dimension that can invalidate a cached FAQ answer (§18.1)."""

    normalized_query: str
    domain: str
    corpus_version: str
    prompt_version: str
    guardrail_policy_version: str
    language: str
    #: Public answers only. A non-anonymous audience makes the entry non-cacheable.
    audience: str = "PUBLIC"

    def render(self) -> str:
        raw = "|".join(
            [
                self.domain,
                self.corpus_version,
                self.prompt_version,
                self.guardrail_policy_version,
                self.language,
                self.audience,
                self.normalized_query,
            ]
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"faq:{self.domain}:{self.corpus_version}:{digest}"


def is_cacheable(*, contains_customer_data: bool, is_authenticated_scope: bool) -> bool:
    """Customer-specific transactional responses are never shared-cached (§18.1)."""
    return not contains_customer_data and not is_authenticated_scope


def build_cache(provider: str, max_entries: int, ttl_seconds: int) -> Any:
    if provider == "disabled":
        return NoOpCache()
    return TtlLruCache(max_entries=max_entries, ttl_seconds=ttl_seconds)
