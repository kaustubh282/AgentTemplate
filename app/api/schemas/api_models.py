"""API request/response contracts (master prompt §29)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Conversation ids are minted by the server (POST /conversations) and carry 128 bits of
#: entropy. Accepting only this shape means a client cannot reserve an id a victim will
#: later be issued, and cannot use accept-vs-refuse to probe which conversations exist
#: (§16). Ownership is still enforced separately by the orchestrator.
SERVER_MINTED_ID_PATTERN = r"^[a-z]{3,12}_[0-9a-f]{32}$"


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str | None = None
    locale: str = "en-IN"


class CreateConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    created_at: str
    expires_at: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=SERVER_MINTED_ID_PATTERN)
    message: str = Field(min_length=1, max_length=4_000)


class ActionRequest(BaseModel):
    """A structured UI action. Resolved deterministically with zero model calls."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(pattern=SERVER_MINTED_ID_PATTERN)
    capability_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    #: Server-issued token from the previous flow response (``meta.confirmationTokens``).
    #: Required whenever ``confirmed`` is true for a high-risk action (§8).
    confirmation_token: str | None = Field(default=None, max_length=128)


class StartFlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(pattern=SERVER_MINTED_ID_PATTERN)
    capability_id: str = Field(min_length=1, max_length=128)


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool
    requestId: str
    correlationId: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str


class DependencyHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: str


class ReadinessResponse(BaseModel):
    """Readiness reflects critical dependency health without exposing topology (§30)."""

    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    dependencies: list[DependencyHealth]
