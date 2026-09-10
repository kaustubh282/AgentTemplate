"""Integration DTOs and the AI-facing projection boundary (§8.1, §10.4, §58.9).

Three distinct layers, deliberately:

``ProviderDto``   what an upstream returns (shape owned by the upstream)
``DomainDto``     what the application works with (shape owned by us)
``AiFacingDto``   the minimum a model may see (masked, field-limited)

Only the third ever reaches model context.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.privacy.masking import masking_service


class PolicyStatus(StrEnum):
    ACTIVE = "ACTIVE"
    LAPSED = "LAPSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    PENDING = "PENDING"


class ClaimStatus(StrEnum):
    REGISTERED = "REGISTERED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SETTLED = "SETTLED"


class PaymentStatus(StrEnum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    currency: str = "INR"


# ------------------------------------------------------------- domain DTOs ---
class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    full_name: str
    mobile: str
    email: str
    date_of_birth: date | None = None
    pincode: str | None = None
    tenant_id: str | None = None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str
    policy_number: str
    customer_id: str
    product: str
    domain: str
    status: PolicyStatus
    annual_premium: Money
    addons: list[str] = Field(default_factory=list)
    inception_date: date | None = None
    renewal_due: date | None = None
    sum_insured: Money | None = None
    source_system: str = "MOCK"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    claim_number: str
    policy_id: str
    customer_id: str
    status: ClaimStatus
    registered_on: date
    last_updated_on: date
    estimated_amount: Money | None = None
    source_system: str = "MOCK"


class PaymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: str
    payment_reference: str
    customer_id: str
    quote_id: str | None = None
    amount: Money
    status: PaymentStatus
    source_system: str = "MOCK"


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_code: str
    name: str
    domain: str
    available_addons: list[str] = Field(default_factory=list)
    min_age: int | None = None
    max_age: int | None = None


class QuoteLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    amount: Money
    note: str | None = None


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str
    quote_reference: str
    customer_id: str
    product: str
    domain: str
    lines: list[QuoteLineItem]
    total: Money
    valid_until: date
    #: The authoritative system that computed the premium (never the model, §2.1).
    source_system: str = "MOCK"


class CreateQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    product_code: str
    domain: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    addons: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class CreateQuoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: Quote


class InitiatePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    quote_id: str
    amount: Money
    idempotency_key: str


class InitiatePaymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment: PaymentRecord
    handoff_token: str
    expires_in_seconds: int = 900


class IssuePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    quote_id: str
    payment_reference: str
    idempotency_key: str


class IssuePolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Policy


class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    name: str
    content_type: str
    size_bytes: int


class ReferenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    group: str


# ---------------------------------------------------------- AI-facing DTOs ---
class AiPolicySummary(BaseModel):
    """Minimum policy view a model may see (§8.1). Masked, field-limited."""

    model_config = ConfigDict(extra="forbid")

    ai_fields: ClassVar[tuple[str, ...]] = (
        "policy_number_masked",
        "product",
        "status",
        "annual_premium",
        "addons",
        "renewal_due",
    )

    policy_number_masked: str
    product: str
    status: str
    annual_premium: float | None = None
    currency: str = "INR"
    addons: list[str] = Field(default_factory=list)
    renewal_due: str | None = None

    @classmethod
    def from_policy(cls, policy: Policy, *, include_premium: bool = True) -> AiPolicySummary:
        return cls(
            policy_number_masked=masking_service.mask_value("policy_number", policy.policy_number),
            product=policy.product,
            status=policy.status.value,
            annual_premium=policy.annual_premium.amount if include_premium else None,
            currency=policy.annual_premium.currency,
            addons=list(policy.addons),
            renewal_due=policy.renewal_due.isoformat() if policy.renewal_due else None,
        )


class AiClaimStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_fields: ClassVar[tuple[str, ...]] = ("claim_number_masked", "status", "last_updated_on")

    claim_number_masked: str
    status: str
    last_updated_on: str

    @classmethod
    def from_claim(cls, claim: Claim) -> AiClaimStatus:
        return cls(
            claim_number_masked=masking_service.mask_value("claim_number", claim.claim_number),
            status=claim.status.value,
            last_updated_on=claim.last_updated_on.isoformat(),
        )


class AiPaymentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_fields: ClassVar[tuple[str, ...]] = ("payment_reference_masked", "status", "amount")

    payment_reference_masked: str
    status: str
    amount: float
    currency: str = "INR"

    @classmethod
    def from_payment(cls, payment: PaymentRecord) -> AiPaymentStatus:
        return cls(
            payment_reference_masked=masking_service.mask_value(
                "payment_reference", payment.payment_reference
            ),
            status=payment.status.value,
            amount=payment.amount.amount,
            currency=payment.amount.currency,
        )


class AiQuoteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_fields: ClassVar[tuple[str, ...]] = ("quote_reference", "product", "total", "valid_until")

    quote_reference: str
    product: str
    total: float
    currency: str = "INR"
    valid_until: str

    @classmethod
    def from_quote(cls, quote: Quote) -> AiQuoteSummary:
        return cls(
            quote_reference=quote.quote_reference,
            product=quote.product,
            total=quote.total.amount,
            currency=quote.total.currency,
            valid_until=quote.valid_until.isoformat(),
        )
