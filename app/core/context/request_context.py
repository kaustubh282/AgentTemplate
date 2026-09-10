"""Typed request context threaded through every layer (master prompt §53).

The context is created once at the API boundary from *trusted* server-side values.
Nothing in it is derived from model output, and it is the only way downstream
services learn who the caller is.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.auth.auth_context import AuthContext


class Channel(StrEnum):
    """Delivery channel. Capabilities declare which channels may invoke them."""

    WEB_CUSTOMER = "WEB_CUSTOMER"
    WEB_AGENT = "WEB_AGENT"
    MOBILE = "MOBILE"
    PUBLIC_WEB = "PUBLIC_WEB"
    INTERNAL = "INTERNAL"
    #: Text-only messaging channel; rendered by a channel adapter (§45).
    WHATSAPP = "WHATSAPP"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


#: The shape :func:`new_id` produces. Client-supplied conversation ids are checked against
#: it at the API boundary (§16): a 128-bit server-minted id cannot be guessed, so an
#: attacker can neither squat an id a victim will later be given nor probe which ids
#: exist. Ids are still *authorised* by owner - this only stops forged shapes early.
SERVER_MINTED_ID = re.compile(r"^[a-z]{3,12}_[0-9a-f]{32}$")


def is_server_minted(value: str) -> bool:
    return bool(SERVER_MINTED_ID.match(value))


def utc_now() -> datetime:
    return datetime.now(UTC)


class RequestContext(BaseModel):
    """Immutable per-request context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(default_factory=lambda: new_id("req"))
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))
    conversation_id: str | None = None
    session_id: str | None = None
    channel: Channel = Channel.PUBLIC_WEB
    auth: AuthContext
    locale: str = "en-IN"
    client_ip_hash: str | None = None
    user_agent_family: str | None = None
    received_at: datetime = Field(default_factory=utc_now)
    idempotency_key: str | None = None
    #: Environment name captured at request start so evidence records are unambiguous.
    environment: str = "local"
    app_version: str = "0.0.0"

    def child(self, **overrides: Any) -> RequestContext:
        """Derive a related context (e.g. for a sub-capability) preserving correlation."""
        return self.model_copy(update=overrides)

    def log_fields(self) -> dict[str, Any]:
        """Non-sensitive identifiers safe for every log line."""
        return {
            "requestId": self.request_id,
            "correlationId": self.correlation_id,
            "conversationRef": self.conversation_ref,
            "channel": self.channel.value,
            "actorType": self.auth.actor_type.value,
            "actorRef": self.auth.subject_ref,
            "environment": self.environment,
            "appVersion": self.app_version,
        }

    @property
    def conversation_ref(self) -> str | None:
        """Pseudonymous conversation reference (never the raw id in logs)."""
        if self.conversation_id is None:
            return None
        from app.core.privacy.pseudonym import pseudonymize

        return pseudonymize("conv", self.conversation_id)


_request_context: ContextVar[RequestContext | None] = ContextVar("protec_request_context", default=None)


def set_current_context(ctx: RequestContext | None) -> None:
    _request_context.set(ctx)


def get_current_context() -> RequestContext | None:
    return _request_context.get()
