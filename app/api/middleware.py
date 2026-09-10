"""Request middleware: correlation, secure headers, metrics, context propagation.

Applies to every request before routing, so a correlation id and safe response
headers exist even for a rejected request (§21, §42).
"""

from __future__ import annotations

import json
import re
import time
import uuid

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context.request_context import set_current_context
from app.core.errors.taxonomy import AppError, ErrorCode
from app.core.logging.structured import get_logger
from app.core.observability.metrics import (
    CONCURRENCY_GAUGE,
    REQUEST_LATENCY_MS,
    REQUESTS_TOTAL,
    metrics,
)

logger = get_logger(__name__)

#: Conservative defaults; a platform WAF/gateway may add more (§42).
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cache-Control": "no-store",
}

#: Label used when no route matched (404s, unknown paths). Keeps cardinality bounded.
UNMATCHED_ROUTE = "unmatched"

#: Client-supplied identifiers are echoed back; bound and restrict them so a header
#: cannot become a 5 KB log/response payload or carry control characters (L-8).
_MAX_ID_LENGTH = 128
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def route_template(request: Request) -> str:
    """The matched route's *template* (``/api/v1/policies/{policy_id}``), never the raw
    path, so identifiers do not leak into metric labels and cardinality stays bounded
    (M-16)."""
    route = request.scope.get("route")
    template = getattr(route, "path_format", None)
    return str(template) if template else UNMATCHED_ROUTE


def _client_id(value: str | None, prefix: str) -> str:
    if value and len(value) <= _MAX_ID_LENGTH and _ID_PATTERN.match(value):
        return value
    return f"{prefix}_{uuid.uuid4().hex}"


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Assigns request/correlation identifiers and records platform metrics."""

    def __init__(self, app: object, *, hsts_enabled: bool = False) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._hsts = hsts_enabled
        self._inflight = 0

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _client_id(request.headers.get("x-request-id"), "req")
        correlation_id = _client_id(request.headers.get("x-correlation-id"), "corr")
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        self._inflight += 1
        metrics.set_gauge(CONCURRENCY_GAUGE, self._inflight)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        finally:
            self._inflight -= 1
            metrics.set_gauge(CONCURRENCY_GAUGE, self._inflight)
            set_current_context(None)

        # Routing has happened by now, so the scope carries the matched route.
        route = route_template(request)
        latency_ms = (time.perf_counter() - started) * 1000
        metrics.observe(REQUEST_LATENCY_MS, latency_ms, labels={"route": route})
        metrics.increment(REQUESTS_TOTAL, labels={"route": route, "status": str(response.status_code)})

        response.headers["X-Request-Id"] = request_id
        response.headers["X-Correlation-Id"] = correlation_id
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class RequestTooLargeError(HTTPException):
    """Raised from the request body stream once the byte budget is exceeded.

    It is an ``HTTPException`` so FastAPI's body parser re-raises it unchanged instead of
    converting it into a generic 400; ``app.main`` maps it to the standard error body.
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(status_code=413, detail="request_too_large")
        self.max_bytes = max_bytes


def request_too_large_error() -> AppError:
    return AppError(ErrorCode.VALIDATION_ERROR, reason="request_too_large", http_status=413)


class BodySizeLimitMiddleware:
    """Rejects oversized payloads before they reach validation (§25, §31; M-15).

    Pure ASGI: a declared ``Content-Length`` above the limit is refused immediately, and
    the body stream itself is metered so a chunked or mis-declared body is cut off at the
    same limit rather than trusted.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = 64 * 1024) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max:
            await self._reject(scope, send)
            return

        received = 0
        response_started = False

        async def metered_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    raise RequestTooLargeError(self._max)
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, metered_receive, guarded_send)
        except RequestTooLargeError:
            # Reached only when nothing inside the stack handled the exception (for
            # example a body read outside a route). If a response already started
            # there is nothing safe to send; let the server close the connection.
            if response_started:
                raise
            await self._reject(scope, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        state = scope.get("state") or {}
        payload = request_too_large_error().to_payload(
            str(state.get("request_id", "unknown")), str(state.get("correlation_id", "unknown"))
        )
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
