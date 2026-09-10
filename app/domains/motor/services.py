"""Motor domain service actions (master prompt §2.1, §8).

These are the bindings a transition names. They call the *provider abstraction*, so
they work identically against the mock and against InsureMO. Premium, eligibility and
policy issuance come from the authoritative provider - never from a model.
"""

from __future__ import annotations

from typing import Any

from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import AppError, ErrorCode, ValidationError
from app.core.observability.metrics import PROVIDER_CALLS_TOTAL, metrics
from app.core.resilience.policies import ResiliencePolicy, SideEffectClass
from app.integrations.contracts.dtos import (
    CreateQuoteRequest,
    InitiatePaymentRequest,
    IssuePolicyRequest,
    Money,
    PaymentStatus,
)
from app.integrations.contracts.providers import ProviderBundle
from app.workflows.state.models import WorkflowState

DOMAIN = "motor"


class MotorSalesService:
    """Provider-backed actions for the motor sales journey."""

    def __init__(
        self,
        providers: ProviderBundle,
        audit: AuditService,
        *,
        policy: ResiliencePolicy,
    ) -> None:
        self._providers = providers
        self._audit = audit
        self._policy = policy

    async def create_quote(
        self, ctx: RequestContext, state: WorkflowState, _payload: dict[str, Any]
    ) -> dict[str, Any]:
        request = CreateQuoteRequest(
            customer_id=ctx.auth.subject_id,
            product_code=str(state.data["product_code"]),
            domain=DOMAIN,
            attributes={
                "idv": state.data.get("idv"),
                "manufacture_year": state.data.get("manufacture_year"),
                "fuel_type": state.data.get("fuel_type"),
            },
            addons=list(state.data.get("addons", [])),
            idempotency_key=ctx.idempotency_key,
        )
        response = await self._policy.execute(
            lambda: self._providers.quote.create_quote(request, ctx),
            side_effect=SideEffectClass.LOW_RISK_WRITE,
            idempotency_key=ctx.idempotency_key,
        )
        metrics.increment(PROVIDER_CALLS_TOTAL, labels={"operation": "create_quote"})
        quote = response.quote
        await self._audit.record(
            ctx,
            AuditAction.QUOTE_RECEIVED,
            AuditResult.SUCCESS,
            resource_type="QUOTE",
            resource_id=quote.quote_id,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code="quote_created",
            attributes={"product": quote.product, "domain": DOMAIN},
        )
        return {
            "quote_id": quote.quote_id,
            "quote_reference": quote.quote_reference,
            "quote_total": quote.total.amount,
            "quote_currency": quote.total.currency,
            "quote_valid_until": quote.valid_until.isoformat(),
            "quote_source_system": quote.source_system,
            "quote_lines": [
                {"label": line.label, "amount": line.amount.amount, "note": line.note} for line in quote.lines
            ],
        }

    async def initiate_payment(
        self, ctx: RequestContext, state: WorkflowState, _payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not ctx.idempotency_key:
            raise ValidationError("idempotency_key_required")
        request = InitiatePaymentRequest(
            customer_id=ctx.auth.subject_id,
            quote_id=str(state.data["quote_id"]),
            amount=Money(
                amount=float(state.data["quote_total"]),
                currency=str(state.data.get("quote_currency", "INR")),
            ),
            idempotency_key=ctx.idempotency_key,
        )
        response = await self._policy.execute(
            lambda: self._providers.payment.initiate_payment(request, ctx),
            side_effect=SideEffectClass.HIGH_RISK_WRITE,
            idempotency_key=ctx.idempotency_key,
        )
        await self._audit.record(
            ctx,
            AuditAction.PAYMENT_INITIATED,
            AuditResult.SUCCESS,
            resource_type="PAYMENT",
            resource_id=response.payment.payment_id,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code="payment_initiated",
        )
        return {
            "payment_id": response.payment.payment_id,
            "payment_reference": response.payment.payment_reference,
            "payment_handoff_token": response.handoff_token,
            "payment_expires_in_seconds": response.expires_in_seconds,
        }

    async def issue_policy(
        self, ctx: RequestContext, state: WorkflowState, _payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not ctx.idempotency_key:
            raise ValidationError("idempotency_key_required")
        # Payment success is a fact owned by the payment provider, never by the client
        # action that reaches this step (§2.1, §25 "no completion without upstream
        # confirmation"). Issuance is refused until the authoritative record says SUCCESS.
        payment_id = str(state.data["payment_id"])
        payment = await self._policy.execute(
            lambda: self._providers.payment.get_payment(payment_id, ctx),
            side_effect=SideEffectClass.READ_ONLY,
        )
        if payment is None or payment.status is not PaymentStatus.SUCCESS:
            await self._audit.record(
                ctx,
                AuditAction.POLICY_ACTION_SUBMITTED,
                AuditResult.DENIED,
                resource_type="PAYMENT",
                resource_id=payment_id,
                source_system="MOCK" if self._providers.is_mock else "INSUREMO",
                reason_code="payment_not_confirmed",
                attributes={"paymentStatus": payment.status.value if payment else "UNKNOWN"},
            )
            raise AppError(
                ErrorCode.FLOW_STATE_CONFLICT,
                reason="payment_not_confirmed",
                details={"paymentStatus": payment.status.value if payment else "UNKNOWN"},
                retryable=True,
            )
        request = IssuePolicyRequest(
            customer_id=ctx.auth.subject_id,
            quote_id=str(state.data["quote_id"]),
            payment_reference=str(state.data["payment_reference"]),
            idempotency_key=ctx.idempotency_key,
        )
        # Irreversible: never retried automatically, even with an idempotency key.
        response = await self._policy.execute(
            lambda: self._providers.issuance.issue_policy(request, ctx),
            side_effect=SideEffectClass.IRREVERSIBLE,
        )
        policy = response.policy
        await self._audit.record(
            ctx,
            AuditAction.POLICY_ACTION_SUBMITTED,
            AuditResult.SUCCESS,
            resource_type="POLICY",
            resource_id=policy.policy_id,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code="policy_issued",
            attributes={"product": policy.product, "domain": DOMAIN},
        )
        return {
            "policy_id": policy.policy_id,
            "policy_number": policy.policy_number,
            "policy_status": policy.status.value,
        }
