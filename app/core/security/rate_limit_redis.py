"""Shared rate-limit store on Redis (master prompt §31, §19).

Implements :class:`~app.core.security.rate_limit.RateLimitStore` so that limits are
enforced *across* instances rather than multiplied by the instance count. It is a
fixed-window counter keyed by the window start, matching the in-memory store's
semantics exactly, so the limiter's callers and tests are unchanged.

``INCR`` + ``EXPIRE`` run in one transaction: the first hit in a window sets the TTL,
every hit is counted atomically, and the key disappears with the window.

The limiter runs inside the synchronous Policy Enforcement Point, so this store uses
the synchronous client. A transport failure **fails closed**: an unreachable limiter
denies the request rather than removing the abuse control (§31, §20.1).
"""

from __future__ import annotations

import time

from redis.exceptions import RedisError

from app.core.errors.taxonomy import RateLimitedError
from app.core.logging.structured import get_logger
from app.core.storage.redis_client import RedisClients

logger = get_logger(__name__)


class RedisRateLimitStore:
    """Multi-instance fixed-window counter."""

    def __init__(self, clients: RedisClients) -> None:
        self._clients = clients
        self._r = clients.sync

    def hit(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds
        redis_key = self._clients.key("rl", key, str(window_start))
        retry_after = max(int(window_start + window_seconds - now) + 1, 1)
        try:
            pipe = self._r.pipeline(transaction=True)
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds + 1)
            count, _ = pipe.execute()
        except RedisError as exc:
            # Fail closed: losing the limiter must not silently remove the control.
            logger.error("rate_limit_store_unavailable", extra={"errorType": type(exc).__name__})
            raise RateLimitedError("BLOCK_RATE_LIMIT_STORE_UNAVAILABLE", retry_after) from exc
        count = int(count)
        remaining = max(limit - count, 0)
        return count <= limit, remaining, retry_after

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except RedisError:
            return False
