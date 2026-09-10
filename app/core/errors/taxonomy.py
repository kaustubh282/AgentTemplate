"""Standard internal error taxonomy (master prompt §44).

Errors are classified once, mapped to a safe HTTP representation, and never leak
stack traces, SQL, prompts, secrets or raw upstream payloads to the caller.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Application-wide error categories."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    FLOW_STATE_CONFLICT = "FLOW_STATE_CONFLICT"
    KNOWLEDGE_INSUFFICIENT = "KNOWLEDGE_INSUFFICIENT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_INVALID_RESPONSE = "UPSTREAM_INVALID_RESPONSE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    CAPABILITY_NOT_FOUND = "CAPABILITY_NOT_FOUND"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Whether a caller may safely retry the same request unchanged.
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
    }
)

HTTP_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.FLOW_STATE_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.KNOWLEDGE_INSUFFICIENT: 200,
    ErrorCode.GUARDRAIL_BLOCKED: 422,
    ErrorCode.CAPABILITY_NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.UPSTREAM_UNAVAILABLE: 503,
    ErrorCode.UPSTREAM_INVALID_RESPONSE: 502,
    ErrorCode.MODEL_TIMEOUT: 504,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.CONFIGURATION_ERROR: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: Safe, user-facing text. Never derived from exception messages.
SAFE_MESSAGE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "The request could not be processed because some details are invalid.",
    ErrorCode.AUTHENTICATION_REQUIRED: "Please sign in to continue.",
    ErrorCode.FORBIDDEN: "You do not have access to this resource.",
    ErrorCode.RESOURCE_NOT_FOUND: "The requested item could not be found.",
    ErrorCode.FLOW_STATE_CONFLICT: "That step is not available at this point in the journey.",
    ErrorCode.KNOWLEDGE_INSUFFICIENT: (
        "I could not verify this from the approved information currently available."
    ),
    ErrorCode.UPSTREAM_TIMEOUT: "The service took too long to respond. Please try again shortly.",
    ErrorCode.UPSTREAM_UNAVAILABLE: "This service is temporarily unavailable. Please try again shortly.",
    ErrorCode.UPSTREAM_INVALID_RESPONSE: "We received an unexpected response and cannot continue safely.",
    ErrorCode.MODEL_TIMEOUT: "The assistant took too long to respond. Please try again.",
    ErrorCode.MODEL_UNAVAILABLE: "The assistant is temporarily unavailable.",
    ErrorCode.GUARDRAIL_BLOCKED: "This request cannot be handled here.",
    ErrorCode.RATE_LIMITED: "Too many requests. Please wait a moment and try again.",
    ErrorCode.CAPABILITY_NOT_FOUND: "That action is not supported.",
    ErrorCode.CONFIGURATION_ERROR: "The service is not configured correctly.",
    ErrorCode.IDEMPOTENCY_CONFLICT: "This request was already submitted.",
    ErrorCode.BUDGET_EXCEEDED: "This conversation has reached its processing limit.",
    ErrorCode.INTERNAL_ERROR: "Something went wrong. Please try again.",
}


class AppError(Exception):
    """Base application error carrying a taxonomy code and safe presentation."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        #: Internal machine-readable reason. Logged, never returned verbatim to users.
        self.reason = reason or code.value
        self.details = details or {}
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None else retryable
        self.http_status = http_status or HTTP_STATUS_BY_CODE.get(code, 500)
        super().__init__(f"{code.value}: {self.reason}")

    @property
    def safe_message(self) -> str:
        return SAFE_MESSAGE_BY_CODE.get(self.code, SAFE_MESSAGE_BY_CODE[ErrorCode.INTERNAL_ERROR])

    def to_payload(self, request_id: str, correlation_id: str) -> dict[str, Any]:
        """Render the wire-safe error body (§29)."""
        return {
            "error": {
                "code": self.code.value,
                "message": self.safe_message,
                "retryable": self.retryable,
                "requestId": request_id,
                "correlationId": correlation_id,
            }
        }


class ValidationError(AppError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, reason=reason, details=details)


class AuthenticationRequiredError(AppError):
    def __init__(self, reason: str = "missing_or_invalid_token") -> None:
        super().__init__(ErrorCode.AUTHENTICATION_REQUIRED, reason=reason)


class ForbiddenError(AppError):
    def __init__(self, reason: str = "authorization_denied", details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FORBIDDEN, reason=reason, details=details)


class ResourceNotFoundError(AppError):
    def __init__(self, reason: str = "resource_not_found") -> None:
        super().__init__(ErrorCode.RESOURCE_NOT_FOUND, reason=reason)


class FlowStateConflictError(AppError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.FLOW_STATE_CONFLICT, reason=reason, details=details)


class KnowledgeInsufficientError(AppError):
    def __init__(self, reason: str = "insufficient_evidence") -> None:
        super().__init__(ErrorCode.KNOWLEDGE_INSUFFICIENT, reason=reason)


class UpstreamTimeoutError(AppError):
    def __init__(self, reason: str = "upstream_timeout", details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_TIMEOUT, reason=reason, details=details)


class UpstreamUnavailableError(AppError):
    def __init__(self, reason: str = "upstream_unavailable", details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.UPSTREAM_UNAVAILABLE, reason=reason, details=details)


class UpstreamInvalidResponseError(AppError):
    def __init__(self, reason: str = "upstream_invalid_response") -> None:
        super().__init__(ErrorCode.UPSTREAM_INVALID_RESPONSE, reason=reason)


class ModelTimeoutError(AppError):
    def __init__(self, reason: str = "model_timeout") -> None:
        super().__init__(ErrorCode.MODEL_TIMEOUT, reason=reason)


class ModelUnavailableError(AppError):
    def __init__(self, reason: str = "model_unavailable") -> None:
        super().__init__(ErrorCode.MODEL_UNAVAILABLE, reason=reason)


class GuardrailBlockedError(AppError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.GUARDRAIL_BLOCKED, reason=reason, details=details)


class RateLimitedError(AppError):
    def __init__(self, reason: str = "rate_limited", retry_after_seconds: int = 30) -> None:
        super().__init__(ErrorCode.RATE_LIMITED, reason=reason, details={"retryAfter": retry_after_seconds})


class CapabilityNotFoundError(AppError):
    def __init__(self, capability_id: str) -> None:
        super().__init__(ErrorCode.CAPABILITY_NOT_FOUND, reason=f"unknown_capability:{capability_id}")


class ConfigurationError(AppError):
    def __init__(self, reason: str) -> None:
        super().__init__(ErrorCode.CONFIGURATION_ERROR, reason=reason)


class IdempotencyConflictError(AppError):
    def __init__(self, reason: str = "duplicate_request") -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_CONFLICT, reason=reason)


class BudgetExceededError(AppError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.BUDGET_EXCEEDED, reason=reason, details=details)
