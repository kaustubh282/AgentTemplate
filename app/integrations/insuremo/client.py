"""InsureMO HTTP client scaffolding.

REQUIRES_VERIFICATION - no InsureMO specification was supplied for this greenfield
build. Nothing in this module claims to know a real InsureMO endpoint, field name,
authentication scheme or error body. Every such value is read from configuration or
raises :class:`InsureMoContractNotConfigured`.

What *is* implemented here is the part that does not depend on the specification:
transport, timeout, correlation propagation, error normalisation to the platform
taxonomy, and refusal to guess.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    AppError,
    ConfigurationError,
    ErrorCode,
    ForbiddenError,
    ResourceNotFoundError,
    UpstreamInvalidResponseError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.core.logging.structured import get_logger

logger = get_logger(__name__)


class InsureMoContractNotConfigured(ConfigurationError):
    """Raised when a mapping that must come from the real specification is missing."""

    def __init__(self, what: str) -> None:
        super().__init__(f"REQUIRES_VERIFICATION:{what}")


class InsureMoClient:
    """Thin transport wrapper. Endpoint paths are injected, never invented."""

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        tenant: str | None,
        *,
        timeout_ms: int = 4_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._tenant = tenant
        self._timeout = timeout_ms / 1000.0
        self._client = client

    def _require_configuration(self) -> str:
        if not self._base_url:
            raise InsureMoContractNotConfigured("INSUREMO_BASE_URL")
        if not self._api_key:
            raise InsureMoContractNotConfigured("INSUREMO_API_KEY")
        return self._base_url

    def _headers(self, ctx: RequestContext) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Correlation propagation is specification-independent.
            "X-Correlation-Id": ctx.correlation_id,
            "X-Request-Id": ctx.request_id,
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._tenant:
            headers["X-Tenant-Id"] = self._tenant
        return headers

    async def request(
        self,
        method: str,
        path: str,
        ctx: RequestContext,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Perform one call and normalise the outcome to the platform error taxonomy."""
        base = self._require_configuration()
        headers = self._headers(ctx)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.request(
                method,
                f"{base.rstrip('/')}/{path.lstrip('/')}",
                headers=headers,
                json=json_body,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError("insuremo_timeout", details={"path": path}) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("insuremo_transport_error", details={"path": path}) from exc
        finally:
            if self._client is None:
                await client.aclose()

        return self._normalise(response, path)

    @staticmethod
    def _normalise(response: httpx.Response, path: str) -> dict[str, Any]:
        """Map upstream status codes onto platform errors without leaking payloads."""
        status = response.status_code
        if status == 401:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, reason="insuremo_unauthenticated")
        if status == 403:
            raise ForbiddenError("insuremo_forbidden")
        if status == 404:
            raise ResourceNotFoundError("insuremo_not_found")
        if status == 409:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, reason="insuremo_conflict")
        if status == 422 or status == 400:
            raise ValidationError("insuremo_rejected_request", details={"path": path})
        if status == 429:
            raise AppError(ErrorCode.RATE_LIMITED, reason="insuremo_rate_limited")
        if 500 <= status < 600:
            raise UpstreamUnavailableError("insuremo_server_error", details={"status": status})

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamInvalidResponseError("insuremo_non_json_response") from exc
        if not isinstance(payload, dict):
            raise UpstreamInvalidResponseError("insuremo_unexpected_payload_shape")
        return payload
