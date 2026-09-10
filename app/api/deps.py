"""FastAPI dependencies: authentication, context and authorization (§13.1, §53).

JWT parsing lives here and nowhere else. Route handlers receive a *trusted*
``AuthContext`` and never see a raw token. The Harness receives the context, never
the bearer header.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request

from app.bootstrap import Container
from app.core.auth.auth_context import AuthContext, Permission, Role, anonymous_context
from app.core.auth.jwt_service import JwtValidationService
from app.core.context.request_context import Channel, RequestContext, is_server_minted, new_id
from app.core.errors.taxonomy import AuthenticationRequiredError, ForbiddenError, ValidationError
from app.core.observability.metrics import AUTH_FAILURES_TOTAL, metrics


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_jwt_service(request: Request) -> JwtValidationService:
    service: JwtValidationService = request.app.state.jwt_service
    return service


def _hash_ip(value: str | None) -> str | None:
    """IP addresses are pseudonymised before they reach a log, metric or limiter."""
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


async def _resolve_auth(
    authorization: str | None, jwt_service: JwtValidationService, *, required: bool
) -> AuthContext:
    if not authorization:
        if required:
            metrics.increment(AUTH_FAILURES_TOTAL, labels={"reason": "missing_token"})
            raise AuthenticationRequiredError("missing_bearer_token")
        return anonymous_context()

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        metrics.increment(AUTH_FAILURES_TOTAL, labels={"reason": "malformed_header"})
        raise AuthenticationRequiredError("malformed_authorization_header")

    try:
        return await jwt_service.validate(token.strip())
    except AuthenticationRequiredError as exc:
        metrics.increment(AUTH_FAILURES_TOTAL, labels={"reason": exc.reason})
        raise


def _channel_for(auth: AuthContext, requested: str | None) -> Channel:
    """The channel is derived from the trusted identity, not simply believed."""
    if not auth.is_authenticated:
        return Channel.PUBLIC_WEB
    if auth.actor_type.value == "AGENT":
        return Channel.WEB_AGENT
    if auth.actor_type.value == "SERVICE":
        return Channel.INTERNAL
    if requested == Channel.MOBILE.value:
        return Channel.MOBILE
    return Channel.WEB_CUSTOMER


def _assert_server_minted(conversation_id: str | None) -> str | None:
    """A conversation id supplied by a client must be one the server minted (§16).

    The API never creates a conversation from an arbitrary client-chosen id. Because a
    minted id carries 128 bits of entropy, an attacker can neither reserve an id that a
    victim will later be issued nor use the accept/refuse difference to learn which
    conversations exist. Ownership is still enforced separately by the orchestrator.
    """
    if conversation_id is None:
        return None
    if not is_server_minted(conversation_id):
        raise ValidationError("conversation_id_not_server_minted")
    return conversation_id


def _build_context(
    request: Request,
    auth: AuthContext,
    container: Container,
    conversation_id: str | None,
    idempotency_key: str | None,
    channel_header: str | None,
) -> RequestContext:
    # An inbound correlation id is honoured so a trace spans the gateway and this
    # service; otherwise a fresh one is minted here. The middleware has already put
    # the same value on request.state, so log lines and the context agree.
    correlation_id = (
        request.headers.get("x-correlation-id")
        or getattr(request.state, "correlation_id", None)
        or new_id("corr")
    )
    request_id = getattr(request.state, "request_id", None) or new_id("req")

    return RequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        conversation_id=_assert_server_minted(conversation_id),
        session_id=auth.session_id,
        channel=_channel_for(auth, channel_header),
        auth=auth,
        client_ip_hash=_hash_ip(request.client.host if request.client else None),
        user_agent_family=(request.headers.get("user-agent") or "")[:40] or None,
        idempotency_key=idempotency_key,
        environment=container.settings.app_env.value,
        app_version=container.settings.app_version,
    )


async def optional_auth_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    jwt_service: JwtValidationService = Depends(get_jwt_service),
) -> AuthContext:
    """For explicitly public capabilities (public FAQ)."""
    return await _resolve_auth(authorization, jwt_service, required=False)


async def required_auth_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    jwt_service: JwtValidationService = Depends(get_jwt_service),
) -> AuthContext:
    """For protected routes."""
    return await _resolve_auth(authorization, jwt_service, required=True)


async def public_request_context(
    request: Request,
    auth: Annotated[AuthContext, Depends(optional_auth_context)],
    container: Annotated[Container, Depends(get_container)],
    x_conversation_id: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
    x_channel: Annotated[str | None, Header()] = None,
) -> RequestContext:
    return _build_context(request, auth, container, x_conversation_id, idempotency_key, x_channel)


async def protected_request_context(
    request: Request,
    auth: Annotated[AuthContext, Depends(required_auth_context)],
    container: Annotated[Container, Depends(get_container)],
    x_conversation_id: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
    x_channel: Annotated[str | None, Header()] = None,
) -> RequestContext:
    return _build_context(request, auth, container, x_conversation_id, idempotency_key, x_channel)


def require_roles(*roles: Role) -> Callable[[RequestContext], RequestContext]:
    """Reusable role gate. Resource-level authorization still applies downstream."""

    def dependency(
        ctx: Annotated[RequestContext, Depends(protected_request_context)],
    ) -> RequestContext:
        if not ctx.auth.has_role(*roles):
            raise ForbiddenError("BLOCK_ROLE_NOT_PERMITTED")
        return ctx

    return dependency


def require_permissions(*permissions: Permission) -> Callable[[RequestContext], RequestContext]:
    def dependency(
        ctx: Annotated[RequestContext, Depends(protected_request_context)],
    ) -> RequestContext:
        missing = [p for p in permissions if not ctx.auth.has_permission(p)]
        if missing:
            raise ForbiddenError("BLOCK_PERMISSION_MISSING")
        return ctx

    return dependency
