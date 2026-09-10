"""FastAPI application entry point (master prompt §29, §30, §42).

Responsibilities kept here and nowhere else: application lifespan, middleware order,
CORS/secure-header policy, exception -> safe-error mapping, and OpenAPI metadata.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.middleware import (
    BodySizeLimitMiddleware,
    CorrelationMiddleware,
    RequestTooLargeError,
    request_too_large_error,
    route_template,
)
from app.api.routers import admin, channels, conversations, health, policies
from app.bootstrap import Container, build_container, validate_startup
from app.core.auth.jwt_service import JwtValidationService
from app.core.config.settings import Settings, get_settings
from app.core.errors.taxonomy import SAFE_MESSAGE_BY_CODE, AppError, ErrorCode
from app.core.logging.structured import get_logger
from app.core.observability.metrics import REQUEST_ERRORS_TOTAL, metrics

logger = get_logger(__name__)

API_DESCRIPTION = """
ProTec Insurance AI Assistant platform.

Deterministic UI-directive orchestration with a grounded FAQ assistant. Business
truth comes from authoritative providers; the model never owns transactional state.
""".strip()


def _error_response(request: Request, error: AppError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    metrics.increment(
        REQUEST_ERRORS_TOTAL,
        labels={"route": route_template(request), "code": error.code.value},
    )
    headers: dict[str, str] = {}
    if error.code is ErrorCode.RATE_LIMITED:
        headers["Retry-After"] = str(error.details.get("retryAfter", 30))
    if error.code is ErrorCode.AUTHENTICATION_REQUIRED:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_payload(request_id, correlation_id),
        headers=headers,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the container once, fail fast on invalid configuration (§14)."""
    settings: Settings = app.state.settings
    validate_startup(settings)
    container = build_container(settings, responder=getattr(app.state, "responder", None))
    app.state.container = container
    app.state.jwt_service = JwtValidationService(settings)
    logger.info(
        "application_started",
        extra={
            "environment": settings.app_env.value,
            "version": settings.app_version,
            "capabilities": len(container.capability_registry),
        },
    )
    try:
        yield
    finally:
        logger.info("application_stopping", extra={"environment": settings.app_env.value})


def create_app(settings: Settings | None = None, *, responder: Any = None) -> FastAPI:
    resolved = settings or get_settings()

    app = FastAPI(
        title="ProTec Insurance AI Template",
        version=resolved.app_version,
        description=API_DESCRIPTION,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs" if not resolved.is_production else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.responder = responder

    # Middleware runs bottom-up: size limit is checked before correlation logging.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=64 * 1024)
    app.add_middleware(CorrelationMiddleware, hsts_enabled=resolved.is_production)
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Conversation-Id", "Idempotency-Key"],
            max_age=600,
        )

    app.include_router(health.router)
    app.include_router(conversations.router)
    app.include_router(policies.router)
    app.include_router(policies.claims_router)
    app.include_router(policies.payments_router)
    app.include_router(admin.router)
    app.include_router(channels.router)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "request_failed",
            extra={"errorCode": exc.code.value, "reason": exc.reason, "route": request.url.path},
        )
        return _error_response(request, exc)

    @app.exception_handler(RequestTooLargeError)
    async def handle_request_too_large(request: Request, exc: RequestTooLargeError) -> JSONResponse:
        """The body-size middleware cut the stream; answer 413 in the standard error shape."""
        logger.info("request_too_large", extra={"route": route_template(request), "maxBytes": exc.max_bytes})
        return _error_response(request, request_too_large_error())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Schema failures return a category, never field-level internals (§44)."""
        logger.info(
            "request_validation_failed",
            extra={"route": request.url.path, "errorCount": len(exc.errors())},
        )
        return _error_response(request, AppError(ErrorCode.VALIDATION_ERROR, reason="schema_invalid"))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a stack trace, host, SQL, prompt or upstream payload (§44)."""
        logger.error(
            "unhandled_exception",
            extra={"route": request.url.path, "errorType": type(exc).__name__},
        )
        return _error_response(request, AppError(ErrorCode.INTERNAL_ERROR, reason="unhandled"))

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        schema["components"] = schema.get("components", {})
        schema["components"]["securitySchemes"] = {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        }
        schema["security"] = [{"bearerAuth": []}]
        schema["components"]["schemas"]["ErrorResponse"] = {
            "type": "object",
            "properties": {
                "error": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "enum": [c.value for c in ErrorCode]},
                        "message": {"type": "string"},
                        "retryable": {"type": "boolean"},
                        "requestId": {"type": "string"},
                        "correlationId": {"type": "string"},
                    },
                    "required": ["code", "message", "retryable", "requestId", "correlationId"],
                }
            },
            "required": ["error"],
        }
        error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.setdefault("responses", {})
                for status, code in (
                    ("401", ErrorCode.AUTHENTICATION_REQUIRED),
                    ("403", ErrorCode.FORBIDDEN),
                    ("429", ErrorCode.RATE_LIMITED),
                    ("500", ErrorCode.INTERNAL_ERROR),
                ):
                    responses.setdefault(
                        status,
                        {
                            "description": SAFE_MESSAGE_BY_CODE[code],
                            "content": {"application/json": {"schema": error_ref}},
                        },
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def get_container_from_app(app: FastAPI) -> Container:
    container: Container = app.state.container
    return container


app = create_app()
