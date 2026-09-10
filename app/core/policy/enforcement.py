"""Policy Enforcement Point (master prompt §4.2).

Every capability and tool execution passes through this class before it runs, whether
the caller is a deterministic route handler, a workflow step or an agent's tool
request. The model may *request* a capability; only the PEP grants it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.auth.auth_context import ActorType
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    AuthenticationRequiredError,
    ForbiddenError,
    RateLimitedError,
    ValidationError,
)
from app.core.observability.metrics import AUTHZ_DENIALS_TOTAL, metrics
from app.core.registry.capability import Capability, CapabilityRegistry, RiskLevel
from app.core.resource_scope.scope import ResourceScopeInterceptor
from app.core.security.rate_limit import RateLimiter


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Outcome of a policy evaluation, with the validated input attached."""

    allowed: bool
    reason_code: str
    capability: Capability
    validated_input: BaseModel | None = None


class PolicyEnforcementPoint:
    """Server-side authority for 'may this execute, now, for this caller?'."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        rate_limiter: RateLimiter,
        scope: ResourceScopeInterceptor,
        audit: AuditService,
        *,
        environment: str = "local",
    ) -> None:
        self._registry = registry
        self._rate_limiter = rate_limiter
        self._scope = scope
        self._audit = audit
        self._environment = environment

    async def authorize(
        self,
        ctx: RequestContext,
        capability_id: str,
        raw_input: dict[str, Any] | BaseModel,
        *,
        workflow_state: str | None = None,
        confirmed: bool = False,
        requester: str = "application",
    ) -> PolicyDecision:
        """Run the full pre-execution check list (§4.2)."""
        capability = self._registry.get(capability_id)

        try:
            self._check_environment(capability)
            self._check_channel(ctx, capability)
            self._check_authentication(ctx, capability)
            self._check_authorization(ctx, capability)
            self._check_workflow_state(capability, workflow_state)
            self._check_confirmation(capability, confirmed=confirmed)
            self._check_idempotency(ctx, capability)
            self._check_rate_limit(ctx, capability)
            validated = self._validate_input(capability, raw_input)
        except (ForbiddenError, AuthenticationRequiredError) as exc:
            metrics.increment(
                AUTHZ_DENIALS_TOTAL,
                labels={"capability": capability.id, "reason": exc.reason},
            )
            await self._audit.record(
                ctx,
                AuditAction.AUTHORIZATION_DENIED,
                AuditResult.DENIED,
                reason_code=exc.reason,
                attributes={"capabilityId": capability.id, "requester": requester},
            )
            raise

        if capability.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            await self._audit.record(
                ctx,
                AuditAction.HIGH_RISK_TOOL_REQUESTED,
                AuditResult.SUCCESS,
                reason_code="policy_allowed",
                attributes={
                    "capabilityId": capability.id,
                    "riskLevel": capability.risk_level.value,
                    "requester": requester,
                    "sideEffect": capability.side_effect_class.value,
                },
            )

        return PolicyDecision(True, "ALLOW", capability, validated)

    def authorize_tool_request(self, capability: Capability, requested_tool: str) -> None:
        """Reject any tool the capability did not declare (§12, §8)."""
        if requested_tool not in capability.allowed_tools:
            metrics.increment(
                AUTHZ_DENIALS_TOTAL,
                labels={"capability": capability.id, "reason": "BLOCK_UNAUTHORIZED_TOOL"},
            )
            raise ForbiddenError("BLOCK_UNAUTHORIZED_TOOL", details={"capabilityId": capability.id})

    # ------------------------------------------------------------- checks ---
    def _check_environment(self, capability: Capability) -> None:
        if self._environment not in capability.allowed_environments:
            raise ForbiddenError("BLOCK_ENVIRONMENT_NOT_PERMITTED")

    @staticmethod
    def _check_channel(ctx: RequestContext, capability: Capability) -> None:
        if ctx.channel not in capability.allowed_channels:
            raise ForbiddenError("BLOCK_CHANNEL_NOT_PERMITTED")

    @staticmethod
    def _check_authentication(ctx: RequestContext, capability: Capability) -> None:
        if capability.required_auth and not ctx.auth.is_authenticated:
            raise AuthenticationRequiredError("BLOCK_AUTHENTICATION_REQUIRED")

    @staticmethod
    def _check_authorization(ctx: RequestContext, capability: Capability) -> None:
        auth = ctx.auth
        if auth.actor_type not in capability.allowed_actor_types:
            raise ForbiddenError("BLOCK_ACTOR_TYPE_NOT_PERMITTED")
        if capability.allowed_roles and not auth.has_role(*capability.allowed_roles):
            raise ForbiddenError("BLOCK_ROLE_NOT_PERMITTED")
        missing = [p for p in capability.required_permissions if not auth.has_permission(p)]
        if missing:
            raise ForbiddenError("BLOCK_PERMISSION_MISSING")

    @staticmethod
    def _check_workflow_state(capability: Capability, workflow_state: str | None) -> None:
        allowed = capability.allowed_workflow_states
        if allowed is None:
            return
        if workflow_state is None or workflow_state not in allowed:
            raise ForbiddenError("BLOCK_INVALID_WORKFLOW_STATE")

    @staticmethod
    def _check_confirmation(capability: Capability, *, confirmed: bool) -> None:
        if capability.requires_confirmation and not confirmed:
            raise ForbiddenError("BLOCK_CONFIRMATION_REQUIRED")

    @staticmethod
    def _check_idempotency(ctx: RequestContext, capability: Capability) -> None:
        if capability.requires_idempotency_key and not ctx.idempotency_key:
            raise ForbiddenError("BLOCK_IDEMPOTENCY_KEY_REQUIRED")

    def _check_rate_limit(self, ctx: RequestContext, capability: Capability) -> None:
        decision = self._rate_limiter.check(
            capability.rate_limit_class,
            subject_ref=ctx.auth.subject_ref,
            client_ip_hash=ctx.client_ip_hash,
            conversation_ref=ctx.conversation_ref,
            tenant_id=ctx.auth.tenant_id,
        )
        if not decision.allowed:
            raise RateLimitedError("BLOCK_RATE_LIMIT", decision.retry_after_seconds)

    @staticmethod
    def _validate_input(capability: Capability, raw_input: dict[str, Any] | BaseModel) -> BaseModel:
        if isinstance(raw_input, capability.input_schema):
            return raw_input
        payload = raw_input.model_dump() if isinstance(raw_input, BaseModel) else raw_input
        try:
            return capability.input_schema.model_validate(payload)
        except PydanticValidationError as exc:
            raise ValidationError(
                "BLOCK_INPUT_SCHEMA_INVALID",
                details={"capabilityId": capability.id, "errorCount": exc.error_count()},
            ) from None

    @staticmethod
    def validate_output(capability: Capability, payload: Any) -> BaseModel:
        """Central structured-output validation (§5.7.2 stage 12)."""
        if isinstance(payload, capability.output_schema):
            return payload
        data = payload.model_dump() if isinstance(payload, BaseModel) else payload
        try:
            return capability.output_schema.model_validate(data)
        except PydanticValidationError as exc:
            raise ValidationError(
                "BLOCK_OUTPUT_SCHEMA_INVALID",
                details={"capabilityId": capability.id, "errorCount": exc.error_count()},
            ) from None

    @property
    def scope(self) -> ResourceScopeInterceptor:
        return self._scope

    @staticmethod
    def actor_is_service(ctx: RequestContext) -> bool:
        return ctx.auth.actor_type is ActorType.SERVICE
