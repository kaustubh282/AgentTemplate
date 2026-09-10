"""Authoritative policy/claim/payment read endpoints (master prompt §8.1).

These are deterministic capabilities: **zero model calls**. They exist so that
"what was my premium?" is answered from the System of Record rather than by replaying
a conversation, and so ownership is enforced before any authoritative read.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_container, protected_request_context, require_permissions
from app.bootstrap import Container
from app.core.auth.auth_context import Permission
from app.core.context.request_context import RequestContext, set_current_context
from app.domains.motor.module import (
    CAPABILITY_MOTOR_POLICY_DETAILS,
    CAPABILITY_MOTOR_POLICY_PREMIUM,
    PolicyLookupOutput,
)

router = APIRouter(prefix="/api/v1/policies", tags=["policies"])


@router.get("", response_model=PolicyLookupOutput)
async def list_policies(
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    scope_customer = container.scope.effective_customer_id(ctx.auth, customer_id)
    result = await container.tools.list_policy_summaries(ctx, scope_customer)
    return PolicyLookupOutput(**result.model_dump())


@router.get("/{policy_id}", response_model=PolicyLookupOutput)
async def get_policy(
    policy_id: str,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    await container.pep.authorize(
        ctx,
        CAPABILITY_MOTOR_POLICY_DETAILS,
        {"policy_id": policy_id, "customer_id": customer_id},
        requester="api",
    )
    result = await container.tools.get_policy_details(ctx, policy_id, requested_customer_id=customer_id)
    return PolicyLookupOutput(**result.model_dump())


@router.get("/{policy_id}/premium", response_model=PolicyLookupOutput)
async def get_policy_premium(
    policy_id: str,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    await container.pep.authorize(
        ctx,
        CAPABILITY_MOTOR_POLICY_PREMIUM,
        {"policy_id": policy_id, "customer_id": customer_id},
        requester="api",
    )
    result = await container.tools.get_policy_premium(ctx, policy_id, requested_customer_id=customer_id)
    return PolicyLookupOutput(**result.model_dump())


@router.get("/{policy_id}/addons", response_model=PolicyLookupOutput)
async def get_policy_addons(
    policy_id: str,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    await container.pep.authorize(
        ctx,
        CAPABILITY_MOTOR_POLICY_DETAILS,
        {"policy_id": policy_id, "customer_id": customer_id},
        requester="api",
    )
    result = await container.tools.get_policy_addons(ctx, policy_id, requested_customer_id=customer_id)
    return PolicyLookupOutput(**result.model_dump())


claims_router = APIRouter(prefix="/api/v1/claims", tags=["claims"])


@claims_router.get("/{claim_id}", response_model=PolicyLookupOutput)
async def get_claim_status(
    claim_id: str,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    result = await container.tools.get_claim_status(ctx, claim_id, requested_customer_id=customer_id)
    return PolicyLookupOutput(**result.model_dump())


payments_router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


class PaymentConfirmationRequest(BaseModel):
    """Gateway callback body. Authenticated as a SERVICE identity with ``payment:confirm``."""

    model_config = ConfigDict(extra="forbid")

    gateway_reference: str = Field(min_length=1, max_length=128)
    status: Literal["SUCCESS", "FAILED", "PENDING", "REFUNDED"]


@payments_router.post("/{payment_id}/confirmations", response_model=PolicyLookupOutput)
async def confirm_payment(
    payment_id: str,
    body: PaymentConfirmationRequest,
    ctx: Annotated[RequestContext, Depends(require_permissions(Permission.PAYMENT_CONFIRM))],
    container: Annotated[Container, Depends(get_container)],
) -> PolicyLookupOutput:
    """Record the payment gateway's outcome (§8). Issuance requires SUCCESS here first."""
    set_current_context(ctx)
    result = await container.tools.confirm_payment(ctx, payment_id, body.gateway_reference, body.status)
    return PolicyLookupOutput(**result.model_dump())


@payments_router.get("/{payment_id}", response_model=PolicyLookupOutput)
async def get_payment_status(
    payment_id: str,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
    customer_id: Annotated[str | None, Query(max_length=64)] = None,
) -> PolicyLookupOutput:
    set_current_context(ctx)
    result = await container.tools.get_payment_status(ctx, payment_id, requested_customer_id=customer_id)
    return PolicyLookupOutput(**result.model_dump())
