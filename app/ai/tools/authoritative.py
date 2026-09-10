"""Typed authoritative-data tools (master prompt §8.1).

The AI never touches a database, InsureMO or any System of Record directly. It may
name an approved read capability; these tools then:

1. accept a trusted ``RequestContext`` (never a caller- or model-supplied identity)
2. enforce ResourceScope / ownership before any authoritative read
3. call the business service / provider abstraction
4. return a minimal, masked AI-facing DTO - never the raw provider payload
5. map provider errors to the standard taxonomy
6. emit audit and trace evidence

If the authoritative source is unavailable or has no verified record, the tool
returns controlled unavailability. It never answers from conversation memory (§8.1
zero-hallucination rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.auth.auth_context import ActorType
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    AppError,
    ErrorCode,
    ForbiddenError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.logging.structured import get_logger
from app.core.observability.metrics import (
    PROVIDER_CALLS_TOTAL,
    PROVIDER_FAILURES_TOTAL,
    PROVIDER_LATENCY_MS,
    metrics,
)
from app.core.observability.tracing import tracer
from app.core.resilience.policies import ResiliencePolicy, SideEffectClass
from app.core.resource_scope.scope import ResourceOwnership, ResourceScopeInterceptor, ResourceType
from app.integrations.contracts.dtos import (
    AiClaimStatus,
    AiPaymentStatus,
    AiPolicySummary,
)
from app.integrations.contracts.providers import ProviderBundle

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Every declaration §8 requires for a tool."""

    name: str
    purpose: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    required_permission: str
    data_classification: str
    side_effect_class: SideEffectClass
    idempotent: bool
    timeout_ms: int
    max_retries: int
    rate_limit_class: str
    audit_required: bool
    allowed_environments: frozenset[str]


class ToolResult(BaseModel):
    """Uniform tool envelope. ``available=False`` means "we do not know", never a guess."""

    available: bool
    category: str
    data: dict[str, Any] | None = None
    reason_code: str | None = None


class AuthoritativeDataTools:
    """The small set of typed, business-oriented read capabilities (§8.1)."""

    def __init__(
        self,
        providers: ProviderBundle,
        scope: ResourceScopeInterceptor,
        audit: AuditService,
        *,
        policy: ResiliencePolicy,
    ) -> None:
        self._providers = providers
        self._scope = scope
        self._audit = audit
        self._policy = policy

    # ------------------------------------------------------------ policies ---
    async def get_policy_summary(
        self, ctx: RequestContext, policy_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        """GetPolicySummary - masked overview of one owned policy."""
        return await self._policy_view(
            ctx, policy_id, requested_customer_id, include_premium=False, tool="GetPolicySummary"
        )

    async def get_policy_details(
        self, ctx: RequestContext, policy_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        """GetPolicyDetails - the same minimal view plus premium and add-ons."""
        return await self._policy_view(
            ctx, policy_id, requested_customer_id, include_premium=True, tool="GetPolicyDetails"
        )

    async def get_policy_premium(
        self, ctx: RequestContext, policy_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        """GetPolicyPremium - answers "what was my premium?" from the SoR, not memory."""
        result = await self._policy_view(
            ctx, policy_id, requested_customer_id, include_premium=True, tool="GetPolicyPremium"
        )
        if result.available and result.data:
            result = result.model_copy(
                update={
                    "data": {
                        "policy_number_masked": result.data["policy_number_masked"],
                        "annual_premium": result.data["annual_premium"],
                        "currency": result.data["currency"],
                    }
                }
            )
        return result

    async def get_policy_addons(
        self, ctx: RequestContext, policy_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        """GetPolicyAddons - the add-ons actually recorded on the booked policy."""
        result = await self._policy_view(
            ctx, policy_id, requested_customer_id, include_premium=False, tool="GetPolicyAddons"
        )
        if result.available and result.data:
            result = result.model_copy(
                update={
                    "data": {
                        "policy_number_masked": result.data["policy_number_masked"],
                        "addons": result.data["addons"],
                    }
                }
            )
        return result

    async def list_policy_summaries(self, ctx: RequestContext, customer_id: str) -> ToolResult:
        try:
            await self._scope.authorize_customer_scope(ctx.auth, customer_id)
        except ForbiddenError as exc:
            await self._audit_denied(ctx, "ListPolicies", "CUSTOMER", exc.reason)
            raise
        try:
            policies = await self._call(
                "list_policies", lambda: self._providers.policy.list_policies(customer_id, ctx)
            )
        except AppError as exc:
            return await self._unavailable(ctx, "ListPolicies", exc)
        return ToolResult(
            available=True,
            category="POLICY_LIST",
            data={
                "policies": [
                    AiPolicySummary.from_policy(p, include_premium=False).model_dump(mode="json")
                    for p in policies
                ]
            },
        )

    # -------------------------------------------------------------- claims ---
    async def get_claim_status(
        self, ctx: RequestContext, claim_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        with tracer.span("tool.GetClaimStatus"):
            await self._authorize(ctx, "GetClaimStatus", ResourceType.CLAIM, claim_id, requested_customer_id)
            try:
                claim = await self._call("get_claim", lambda: self._providers.claims.get_claim(claim_id, ctx))
            except AppError as exc:
                return await self._unavailable(ctx, "GetClaimStatus", exc)
            if claim is None:
                return await self._unavailable(
                    ctx, "GetClaimStatus", ResourceNotFoundError("claim_not_found")
                )
            await self._audit_read(ctx, "GetClaimStatus", "CLAIM", claim_id)
            return ToolResult(
                available=True,
                category="CLAIM_STATUS",
                data=AiClaimStatus.from_claim(claim).model_dump(mode="json"),
            )

    # ------------------------------------------------------------ payments ---
    async def get_payment_status(
        self, ctx: RequestContext, payment_id: str, *, requested_customer_id: str | None = None
    ) -> ToolResult:
        with tracer.span("tool.GetPaymentStatus"):
            await self._authorize(
                ctx, "GetPaymentStatus", ResourceType.PAYMENT, payment_id, requested_customer_id
            )
            try:
                payment = await self._call(
                    "get_payment", lambda: self._providers.payment.get_payment(payment_id, ctx)
                )
            except AppError as exc:
                return await self._unavailable(ctx, "GetPaymentStatus", exc)
            if payment is None:
                return await self._unavailable(
                    ctx, "GetPaymentStatus", ResourceNotFoundError("payment_not_found")
                )
            await self._audit_read(ctx, "GetPaymentStatus", "PAYMENT", payment_id)
            return ToolResult(
                available=True,
                category="PAYMENT_STATUS",
                data=AiPaymentStatus.from_payment(payment).model_dump(mode="json"),
            )

    async def confirm_payment(
        self, ctx: RequestContext, payment_id: str, gateway_reference: str, status: str
    ) -> ToolResult:
        """Record the payment gateway's outcome - the only path to ``SUCCESS`` (§8).

        Callable by a *service* identity only (the gateway callback), never by a
        customer, agent or model. The workflow's issuance step reads this record.
        """
        from app.integrations.contracts.dtos import PaymentStatus

        if ctx.auth.actor_type is not ActorType.SERVICE:
            await self._audit_denied(ctx, "ConfirmPayment", "PAYMENT", "BLOCK_ACTOR_TYPE_NOT_PERMITTED")
            raise ForbiddenError("BLOCK_ACTOR_TYPE_NOT_PERMITTED")
        try:
            outcome = PaymentStatus(status)
        except ValueError:
            raise ValidationError("unknown_payment_status", details={"status": status}) from None
        with tracer.span("tool.ConfirmPayment"):
            try:
                record = await self._policy.execute(
                    lambda: self._providers.payment.confirm_payment(
                        payment_id, gateway_reference, outcome, ctx
                    ),
                    side_effect=SideEffectClass.HIGH_RISK_WRITE,
                    idempotency_key=gateway_reference,
                )
            except AppError as exc:
                return await self._unavailable(ctx, "ConfirmPayment", exc)
            await self._audit.record(
                ctx,
                AuditAction.PAYMENT_CONFIRMED,
                AuditResult.SUCCESS,
                resource_type="PAYMENT",
                resource_id=payment_id,
                source_system="MOCK" if self._providers.is_mock else "INSUREMO",
                reason_code=record.status.value,
                attributes={"tool": "ConfirmPayment"},
            )
            return ToolResult(
                available=True,
                category="PAYMENT_STATUS",
                data=AiPaymentStatus.from_payment(record).model_dump(mode="json"),
            )

    # ------------------------------------------------------------ internals ---
    async def _audit_denied(self, ctx: RequestContext, tool: str, resource_type: str, reason: str) -> None:
        """Every ownership/scope denial on a direct authoritative read is evidence (§22)."""
        await self._audit.record(
            ctx,
            AuditAction.RESOURCE_SCOPE_DENIED,
            AuditResult.DENIED,
            resource_type=resource_type,
            reason_code=reason,
            attributes={"tool": tool},
        )

    async def _authorize(
        self,
        ctx: RequestContext,
        tool: str,
        resource_type: ResourceType,
        resource_id: str,
        requested_customer_id: str | None,
    ) -> None:
        try:
            await self._scope.authorize_resource(
                ctx.auth, resource_type, resource_id, requested_customer_id=requested_customer_id
            )
        except ForbiddenError as exc:
            await self._audit_denied(ctx, tool, resource_type.value, exc.reason)
            raise

    async def _policy_view(
        self,
        ctx: RequestContext,
        policy_id: str,
        requested_customer_id: str | None,
        *,
        include_premium: bool,
        tool: str,
    ) -> ToolResult:
        with tracer.span(f"tool.{tool}"):
            await self._authorize(ctx, tool, ResourceType.POLICY, policy_id, requested_customer_id)
            try:
                policy = await self._call(
                    "get_policy", lambda: self._providers.policy.get_policy(policy_id, ctx)
                )
            except AppError as exc:
                return await self._unavailable(ctx, tool, exc)
            if policy is None:
                return await self._unavailable(ctx, tool, ResourceNotFoundError("policy_not_found"))
            await self._audit_read(ctx, tool, "POLICY", policy_id)
            return ToolResult(
                available=True,
                category="POLICY_VIEW",
                data=AiPolicySummary.from_policy(policy, include_premium=include_premium).model_dump(
                    mode="json"
                ),
            )

    async def _call(self, operation: str, fn: Any) -> Any:
        import time

        started = time.perf_counter()
        try:
            result = await self._policy.execute(fn, side_effect=SideEffectClass.READ_ONLY)
        except AppError:
            metrics.increment(PROVIDER_FAILURES_TOTAL, labels={"operation": operation})
            raise
        finally:
            metrics.observe(
                PROVIDER_LATENCY_MS,
                (time.perf_counter() - started) * 1000,
                labels={"operation": operation},
            )
            metrics.increment(PROVIDER_CALLS_TOTAL, labels={"operation": operation})
        return result

    async def _unavailable(self, ctx: RequestContext, tool: str, error: AppError) -> ToolResult:
        """Controlled unavailability. Never a fabricated value (§8.1, §20)."""
        await self._audit.record(
            ctx,
            AuditAction.PROVIDER_CALL_OUTCOME,
            AuditResult.TIMEOUT if error.code is ErrorCode.UPSTREAM_TIMEOUT else AuditResult.FAILURE,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code=error.code.value,
            attributes={"tool": tool},
        )
        logger.warning("authoritative_read_unavailable", extra={"tool": tool, "code": error.code.value})
        return ToolResult(
            available=False,
            category="UNAVAILABLE",
            data=None,
            reason_code=error.code.value,
        )

    async def _audit_read(self, ctx: RequestContext, tool: str, resource_type: str, resource_id: str) -> None:
        await self._audit.record(
            ctx,
            AuditAction.PROVIDER_CALL_OUTCOME,
            AuditResult.SUCCESS,
            resource_type=resource_type,
            resource_id=resource_id,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code=tool,
            attributes={"tool": tool},
        )


class OwnershipResolverAdapter:
    """Resolves authoritative ownership from providers (§13.2)."""

    def __init__(self, providers: ProviderBundle) -> None:
        self._providers = providers

    async def resolve_owner(self, resource_type: ResourceType, resource_id: str) -> ResourceOwnership | None:
        owner: str | None = None
        if resource_type is ResourceType.POLICY:
            owner = await self._providers.policy.get_policy_owner(resource_id)
        elif resource_type is ResourceType.CLAIM:
            owner = await self._providers.claims.get_claim_owner(resource_id)
        elif resource_type is ResourceType.QUOTE:
            owner = await self._providers.quote.get_quote_owner(resource_id)
        elif resource_type is ResourceType.PAYMENT:
            owner = await self._providers.payment.get_payment_owner(resource_id)
        if owner is None:
            return None
        # The tenant is the owning customer's tenant as recorded by the System of
        # Record (H-1). It is never taken from the caller.
        tenant = await self._providers.customer.get_customer_tenant(owner)
        return ResourceOwnership(customer_id=owner, tenant_id=tenant)


class ToolCatalog(Protocol):
    def specs(self) -> tuple[ToolSpec, ...]: ...
