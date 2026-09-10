"""Rate limiting and abuse control (master prompt §31).

Expensive AI operations and cheap deterministic endpoints carry separate budgets so a
denial-of-wallet attempt cannot exhaust the model spend by hammering a free endpoint.
The store is an interface: the in-memory implementation is correct for a single
process, and a shared implementation (Redis etc.) plugs in without touching callers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from app.core.observability.metrics import RATE_LIMIT_REJECTS_TOTAL, metrics


class RateLimitClass(StrEnum):
    """Cost tiers. Capabilities declare which tier they belong to."""

    PUBLIC = "PUBLIC"
    DETERMINISTIC = "DETERMINISTIC"
    AI = "AI"
    HIGH_RISK_WRITE = "HIGH_RISK_WRITE"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    limit_class: RateLimitClass


class RateLimitStore(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]: ...


class InMemoryRateLimitStore:
    """Fixed-window counter. Single-process only; swap for a shared store when scaled."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        now = time.monotonic()
        with self._lock:
            window_start, count = self._buckets.get(key, (now, 0))
            if now - window_start >= window_seconds:
                window_start, count = now, 0
            count += 1
            self._buckets[key] = (window_start, count)
            remaining = max(limit - count, 0)
            retry_after = max(int(window_seconds - (now - window_start)) + 1, 1)
            return count <= limit, remaining, retry_after

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def build_rate_limit_store(provider: str, clients: Any | None = None) -> RateLimitStore:
    """Select the rate-limit store. ``redis`` enforces one limit across every instance (§31)."""
    from app.core.errors.taxonomy import ConfigurationError

    if provider == "inmemory":
        return InMemoryRateLimitStore()
    if provider == "redis":
        if clients is None:
            raise ConfigurationError("redis_clients_required_for_rate_limit_store")
        from app.core.security.rate_limit_redis import RedisRateLimitStore

        return RedisRateLimitStore(clients)
    raise ConfigurationError(f"unsupported_rate_limit_store_provider:{provider}")


class RateLimiter:
    """Applies per-subject, per-conversation, per-IP and per-tenant limits."""

    def __init__(
        self,
        store: RateLimitStore,
        *,
        enabled: bool = True,
        ai_per_minute: int = 20,
        deterministic_per_minute: int = 240,
        public_per_minute: int = 10,
        high_risk_per_minute: int = 5,
        burst_multiplier: float = 1.5,
    ) -> None:
        self._store = store
        self._enabled = enabled
        self._limits = {
            RateLimitClass.AI: ai_per_minute,
            RateLimitClass.DETERMINISTIC: deterministic_per_minute,
            RateLimitClass.PUBLIC: public_per_minute,
            RateLimitClass.HIGH_RISK_WRITE: high_risk_per_minute,
        }
        self._burst = burst_multiplier

    def check(
        self,
        limit_class: RateLimitClass,
        *,
        subject_ref: str,
        client_ip_hash: str | None = None,
        conversation_ref: str | None = None,
        tenant_id: str | None = None,
    ) -> RateLimitDecision:
        if not self._enabled:
            return RateLimitDecision(True, 10**6, 0, limit_class)

        limit = self._limits[limit_class]
        dimensions = [("sub", subject_ref)]
        if client_ip_hash:
            dimensions.append(("ip", client_ip_hash))
        if conversation_ref:
            dimensions.append(("conv", conversation_ref))
        if tenant_id:
            dimensions.append(("tenant", tenant_id))

        worst = RateLimitDecision(True, limit, 0, limit_class)
        for dimension, value in dimensions:
            # IP and tenant dimensions get a burst allowance for shared NAT egress.
            effective = limit if dimension in ("sub", "conv") else int(limit * self._burst)
            allowed, remaining, retry_after = self._store.hit(
                f"{limit_class.value}:{dimension}:{value}", effective, 60
            )
            if not allowed:
                metrics.increment(
                    RATE_LIMIT_REJECTS_TOTAL,
                    labels={"class": limit_class.value, "dimension": dimension},
                )
                return RateLimitDecision(False, 0, retry_after, limit_class)
            if remaining < worst.remaining:
                worst = RateLimitDecision(True, remaining, 0, limit_class)
        return worst
