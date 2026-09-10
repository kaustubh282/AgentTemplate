"""Redis connection factory (master prompt §19, §41, H-5 remediation).

One place decides how the process reaches its shared store. Production receives a
real ``redis://`` / ``rediss://`` URL from the secret manager. Tests, evals and the
recovery drill use ``fakeredis://<name>``: an in-process Redis emulator whose server
object is shared per name, so two application instances built in the same process
genuinely share state - which is what proves the multi-instance contract offline.

``fakeredis://`` is refused in production by ``Settings``; it is a test double, not a
deployment option.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import redis
import redis.asyncio as redis_async

from app.core.errors.taxonomy import ConfigurationError, UpstreamUnavailableError

FAKE_SCHEME = "fakeredis://"

_FAKE_SERVERS: dict[str, Any] = {}
_FAKE_LOCK = threading.Lock()


@dataclass(slots=True)
class RedisClients:
    """A synchronous client (for the rate limiter, which runs inside a sync PEP check)
    and an asyncio client (for the async session and workflow stores), both bound to
    the same server."""

    sync: redis.Redis
    asyncio: redis_async.Redis
    key_prefix: str

    def key(self, *parts: str) -> str:
        return ":".join((self.key_prefix, *parts))


def _fake_server(name: str) -> Any:
    try:
        import fakeredis
    except ImportError as exc:  # pragma: no cover - dev extra
        raise ConfigurationError("fakeredis_extra_not_installed") from exc
    with _FAKE_LOCK:
        server = _FAKE_SERVERS.get(name)
        if server is None:
            server = fakeredis.FakeServer()
            _FAKE_SERVERS[name] = server
        return server


def reset_fake_servers() -> None:
    """Drop every emulated server so tests start from an empty store."""
    with _FAKE_LOCK:
        _FAKE_SERVERS.clear()


def build_redis_clients(url: str, *, key_prefix: str) -> RedisClients:
    """Construct clients for ``url``. Never connects eagerly; failures surface per call."""
    if not url:
        raise ConfigurationError("REDIS_URL_required_for_redis_store_providers")

    if url.startswith(FAKE_SCHEME):
        import fakeredis
        import fakeredis.aioredis

        server = _fake_server(url[len(FAKE_SCHEME) :] or "default")
        return RedisClients(
            sync=fakeredis.FakeRedis(server=server, decode_responses=True),
            asyncio=fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
            key_prefix=key_prefix,
        )

    if not url.startswith(("redis://", "rediss://", "unix://")):
        raise ConfigurationError("unsupported_redis_url_scheme")

    return RedisClients(
        sync=redis.Redis.from_url(url, decode_responses=True, socket_timeout=2.0, socket_connect_timeout=2.0),
        asyncio=redis_async.Redis.from_url(
            url, decode_responses=True, socket_timeout=2.0, socket_connect_timeout=2.0
        ),
        key_prefix=key_prefix,
    )


def store_unavailable(operation: str, exc: Exception) -> UpstreamUnavailableError:
    """Map a Redis transport failure to the standard taxonomy (§44).

    The store is a dependency like any other: its outage is reported as a retryable
    upstream failure, never as an internal error and never by fabricating state.
    """
    return UpstreamUnavailableError("state_store_unavailable", details={"operation": operation})
