"""Timeouts, controlled retries, circuit breakers and fallback policy (§20, §18).

Retries are *never* blind: only idempotent, retryable failure classes are retried, and
side-effecting operations are excluded unless the caller supplies an idempotency key.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from app.core.errors.taxonomy import (
    AppError,
    ErrorCode,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.core.logging.structured import get_logger
from app.core.observability.metrics import CIRCUIT_OPEN_TOTAL, metrics

logger = get_logger(__name__)

T = TypeVar("T")


class SideEffectClass(StrEnum):
    """Side-effect classification driving retry safety (§8)."""

    READ_ONLY = "READ_ONLY"
    LOW_RISK_WRITE = "LOW_RISK_WRITE"
    HIGH_RISK_WRITE = "HIGH_RISK_WRITE"
    IRREVERSIBLE = "IRREVERSIBLE"


#: Failure classes that may be retried when the operation is safe to repeat.
RETRYABLE_ERROR_CODES = frozenset(
    {
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.MODEL_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_ms: int = 50
    max_delay_ms: int = 800
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay_ms * (2 ** (attempt - 1)), self.max_delay_ms)
        if self.jitter:
            raw = raw * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not cryptographic
        return raw / 1000.0


NO_RETRY = RetryPolicy(max_attempts=1)


def retry_allowed(side_effect: SideEffectClass, idempotency_key: str | None) -> bool:
    """Retrying a write is only safe when duplication is prevented by an idempotency key."""
    if side_effect is SideEffectClass.READ_ONLY:
        return True
    if side_effect is SideEffectClass.IRREVERSIBLE:
        return False
    return idempotency_key is not None


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Per-dependency breaker preventing repeated calls into a failing upstream."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_seconds: float = 15.0) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._state = CircuitState.CLOSED

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and (time.monotonic() - self._opened_at) >= self._reset_seconds
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        if self.state is CircuitState.OPEN:
            metrics.increment(CIRCUIT_OPEN_TOTAL, labels={"dependency": self.name})
            raise UpstreamUnavailableError("circuit_open", details={"dependency": self.name})

    def on_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning("circuit_opened", extra={"dependency": self.name})

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED


class ResiliencePolicy:
    """Composes timeout + retry + circuit breaker around a single dependency call."""

    def __init__(
        self,
        name: str,
        *,
        timeout_ms: int,
        retry: RetryPolicy = NO_RETRY,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.name = name
        self._timeout = timeout_ms / 1000.0
        self._retry = retry
        self._breaker = breaker or CircuitBreaker(name)

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        side_effect: SideEffectClass = SideEffectClass.READ_ONLY,
        idempotency_key: str | None = None,
    ) -> T:
        may_retry = retry_allowed(side_effect, idempotency_key)
        attempts = self._retry.max_attempts if may_retry else 1
        last_error: AppError | None = None

        for attempt in range(1, attempts + 1):
            self._breaker.before_call()
            try:
                result = await asyncio.wait_for(operation(), timeout=self._timeout)
            except TimeoutError:
                self._breaker.on_failure()
                last_error = UpstreamTimeoutError("dependency_timeout", details={"dependency": self.name})
            except AppError as exc:
                if exc.code not in RETRYABLE_ERROR_CODES:
                    self._breaker.on_failure()
                    raise
                self._breaker.on_failure()
                last_error = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._breaker.on_failure()
                logger.warning(
                    "dependency_unexpected_failure",
                    extra={"dependency": self.name, "errorType": type(exc).__name__},
                )
                raise UpstreamUnavailableError(
                    "dependency_failure", details={"dependency": self.name}
                ) from exc
            else:
                self._breaker.on_success()
                return result

            if attempt < attempts:
                await asyncio.sleep(self._retry.delay_for(attempt))

        assert last_error is not None
        raise last_error
