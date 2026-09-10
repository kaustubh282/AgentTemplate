"""Mock provider implementations (master prompt §9.1).

Mocks implement the *exact* production interfaces and can be driven into failure so
resilience paths are executable: latency, timeout, 4xx/5xx and malformed/partial
responses are all reproducible through :class:`FaultInjection`.

Mock data is always tagged ``source_system="MOCK"`` so it is distinguishable in
telemetry, and configuration refuses these providers in production.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    UpstreamInvalidResponseError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.integrations.contracts.dtos import (
    Claim,
    CreateQuoteRequest,
    CreateQuoteResponse,
    Customer,
    DocumentRef,
    InitiatePaymentRequest,
    InitiatePaymentResponse,
    IssuePolicyRequest,
    IssuePolicyResponse,
    Money,
    PaymentRecord,
    PaymentStatus,
    Policy,
    PolicyStatus,
    Product,
    Quote,
    QuoteLineItem,
    ReferenceItem,
)
from app.integrations.mock import fixtures


@dataclass
class FaultInjection:
    """Per-operation fault configuration used by tests and degraded-mode evals."""

    latency_ms: int = 0
    timeout_operations: set[str] = field(default_factory=set)
    unavailable_operations: set[str] = field(default_factory=set)
    invalid_response_operations: set[str] = field(default_factory=set)
    not_found_operations: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.latency_ms = 0
        self.timeout_operations.clear()
        self.unavailable_operations.clear()
        self.invalid_response_operations.clear()
        self.not_found_operations.clear()


class _MockBase:
    """Shared fault simulation and mutable fixture state."""

    def __init__(self, faults: FaultInjection | None = None) -> None:
        self.faults = faults or FaultInjection()
        self.calls: list[str] = []

    async def _simulate(self, operation: str) -> None:
        self.calls.append(operation)
        if self.faults.latency_ms:
            await asyncio.sleep(self.faults.latency_ms / 1000.0)
        if operation in self.faults.timeout_operations:
            # Sleep past any plausible caller timeout so the real timeout path runs.
            await asyncio.sleep(3600)
        if operation in self.faults.unavailable_operations:
            raise UpstreamUnavailableError("mock_upstream_unavailable", details={"op": operation})
        if operation in self.faults.invalid_response_operations:
            raise UpstreamInvalidResponseError("mock_malformed_upstream_payload")


class MockCustomerProvider(_MockBase):
    async def get_customer(self, customer_id: str, ctx: RequestContext) -> Customer | None:
        await self._simulate("get_customer")
        if "get_customer" in self.faults.not_found_operations:
            return None
        return fixtures.CUSTOMERS.get(customer_id)

    async def get_customer_tenant(self, customer_id: str) -> str | None:
        customer = fixtures.CUSTOMERS.get(customer_id)
        return customer.tenant_id if customer else None

    async def find_by_mobile(self, mobile: str, ctx: RequestContext) -> Customer | None:
        await self._simulate("find_by_mobile")
        for customer in fixtures.CUSTOMERS.values():
            if customer.mobile == mobile:
                return customer
        return None


class MockPolicyProvider(_MockBase):
    def __init__(self, faults: FaultInjection | None = None) -> None:
        super().__init__(faults)
        self._policies: dict[str, Policy] = {k: v.model_copy(deep=True) for k, v in fixtures.POLICIES.items()}

    async def list_policies(self, customer_id: str, ctx: RequestContext) -> list[Policy]:
        await self._simulate("list_policies")
        return [p.model_copy(deep=True) for p in self._policies.values() if p.customer_id == customer_id]

    async def get_policy(self, policy_id: str, ctx: RequestContext) -> Policy | None:
        await self._simulate("get_policy")
        if "get_policy" in self.faults.not_found_operations:
            return None
        policy = self._policies.get(policy_id)
        return policy.model_copy(deep=True) if policy else None

    async def get_policy_owner(self, policy_id: str) -> str | None:
        policy = self._policies.get(policy_id)
        return policy.customer_id if policy else None

    def persist(self, policy: Policy) -> None:
        """Used by the issuance provider so booked policies become authoritative (§8.1)."""
        self._policies[policy.policy_id] = policy.model_copy(deep=True)


class MockProductProvider(_MockBase):
    async def list_products(self, domain: str, ctx: RequestContext) -> list[Product]:
        await self._simulate("list_products")
        return [p for p in fixtures.PRODUCTS.values() if p.domain == domain]

    async def get_product(self, product_code: str, ctx: RequestContext) -> Product | None:
        await self._simulate("get_product")
        return fixtures.PRODUCTS.get(product_code)


class MockQuoteProvider(_MockBase):
    """Deterministic rating stub.

    The arithmetic here is a *placeholder* so flows are executable end to end; real
    premium must come from the authoritative rating engine. REQUIRES_VERIFICATION.
    """

    def __init__(self, faults: FaultInjection | None = None) -> None:
        super().__init__(faults)
        self._quotes: dict[str, Quote] = {}
        self._by_idempotency: dict[str, Quote] = {}

    async def create_quote(self, request: CreateQuoteRequest, ctx: RequestContext) -> CreateQuoteResponse:
        await self._simulate("create_quote")

        if request.idempotency_key and request.idempotency_key in self._by_idempotency:
            return CreateQuoteResponse(quote=self._by_idempotency[request.idempotency_key])

        product = fixtures.PRODUCTS.get(request.product_code)
        if product is None:
            raise ValidationError("unknown_product", details={"productCode": request.product_code})

        unknown_addons = [a for a in request.addons if a not in product.available_addons]
        if unknown_addons:
            raise ValidationError("unsupported_addons", details={"addons": unknown_addons})

        base = fixtures.MOCK_BASE_PREMIUM.get(request.product_code, 5000.0)
        idv = float(request.attributes.get("idv") or 0)
        base += idv * 0.012
        lines = [QuoteLineItem(label="Base premium", amount=Money(amount=round(base, 2)))]
        for addon in request.addons:
            lines.append(
                QuoteLineItem(label=addon, amount=Money(amount=fixtures.MOCK_ADDON_PREMIUM.get(addon, 500.0)))
            )
        subtotal = sum(line.amount.amount for line in lines)
        tax = round(subtotal * 0.18, 2)
        lines.append(QuoteLineItem(label="GST (18%)", amount=Money(amount=tax), note="Indicative"))

        quote = Quote(
            quote_id=f"QTE-{uuid.uuid4().hex[:10].upper()}",
            quote_reference=f"QREF{uuid.uuid4().hex[:8].upper()}",
            customer_id=request.customer_id,
            product=product.name,
            domain=request.domain,
            lines=lines,
            total=Money(amount=round(subtotal + tax, 2)),
            valid_until=date.today() + timedelta(days=15),
        )
        self._quotes[quote.quote_id] = quote
        if request.idempotency_key:
            self._by_idempotency[request.idempotency_key] = quote
        return CreateQuoteResponse(quote=quote)

    async def get_quote(self, quote_id: str, ctx: RequestContext) -> Quote | None:
        await self._simulate("get_quote")
        return self._quotes.get(quote_id)

    async def get_quote_owner(self, quote_id: str) -> str | None:
        quote = self._quotes.get(quote_id)
        return quote.customer_id if quote else None


class MockClaimsProvider(_MockBase):
    async def list_claims(self, customer_id: str, ctx: RequestContext) -> list[Claim]:
        await self._simulate("list_claims")
        return [c for c in fixtures.CLAIMS.values() if c.customer_id == customer_id]

    async def get_claim(self, claim_id: str, ctx: RequestContext) -> Claim | None:
        await self._simulate("get_claim")
        if "get_claim" in self.faults.not_found_operations:
            return None
        return fixtures.CLAIMS.get(claim_id)

    async def get_claim_owner(self, claim_id: str) -> str | None:
        claim = fixtures.CLAIMS.get(claim_id)
        return claim.customer_id if claim else None


class MockPaymentProvider(_MockBase):
    def __init__(self, faults: FaultInjection | None = None) -> None:
        super().__init__(faults)
        self._payments: dict[str, PaymentRecord] = {
            k: v.model_copy(deep=True) for k, v in fixtures.PAYMENTS.items()
        }
        self._by_idempotency: dict[str, InitiatePaymentResponse] = {}

    async def initiate_payment(
        self, request: InitiatePaymentRequest, ctx: RequestContext
    ) -> InitiatePaymentResponse:
        await self._simulate("initiate_payment")
        if request.idempotency_key in self._by_idempotency:
            return self._by_idempotency[request.idempotency_key]

        record = PaymentRecord(
            payment_id=f"PAY-{uuid.uuid4().hex[:10].upper()}",
            payment_reference=f"PAYREF{uuid.uuid4().hex[:8].upper()}",
            customer_id=request.customer_id,
            quote_id=request.quote_id,
            amount=request.amount,
            status=PaymentStatus.INITIATED,
        )
        self._payments[record.payment_id] = record
        response = InitiatePaymentResponse(
            payment=record,
            handoff_token=f"hst_{uuid.uuid4().hex}",
            expires_in_seconds=900,
        )
        self._by_idempotency[request.idempotency_key] = response
        return response

    async def get_payment(self, payment_id: str, ctx: RequestContext) -> PaymentRecord | None:
        await self._simulate("get_payment")
        return self._payments.get(payment_id)

    async def get_payment_owner(self, payment_id: str) -> str | None:
        payment = self._payments.get(payment_id)
        return payment.customer_id if payment else None

    async def confirm_payment(
        self, payment_id: str, gateway_reference: str, status: PaymentStatus, ctx: RequestContext
    ) -> PaymentRecord:
        """The gateway's callback. Only this path can move a payment to SUCCESS (§8)."""
        await self._simulate("confirm_payment")
        record = self._payments.get(payment_id)
        if record is None:
            raise ValidationError("unknown_payment", details={"paymentId": payment_id})
        if record.status in (PaymentStatus.SUCCESS, PaymentStatus.REFUNDED) and record.status is not status:
            raise ValidationError("payment_already_settled", details={"status": record.status.value})
        updated = record.model_copy(update={"status": status})
        self._payments[payment_id] = updated
        return updated.model_copy(deep=True)

    def mark_success(self, payment_id: str) -> None:
        """Test convenience equivalent to a successful gateway callback."""
        record = self._payments.get(payment_id)
        if record:
            self._payments[payment_id] = record.model_copy(update={"status": PaymentStatus.SUCCESS})


class MockPolicyIssuanceProvider(_MockBase):
    """Issues a policy and writes it to the authoritative (mock) policy store."""

    def __init__(
        self,
        policy_provider: MockPolicyProvider,
        quote_provider: MockQuoteProvider,
        faults: FaultInjection | None = None,
    ) -> None:
        super().__init__(faults)
        self._policies = policy_provider
        self._quotes = quote_provider
        self._by_idempotency: dict[str, IssuePolicyResponse] = {}

    async def issue_policy(self, request: IssuePolicyRequest, ctx: RequestContext) -> IssuePolicyResponse:
        await self._simulate("issue_policy")
        if request.idempotency_key in self._by_idempotency:
            return self._by_idempotency[request.idempotency_key]

        quote = await self._quotes.get_quote(request.quote_id, ctx)
        if quote is None:
            raise ValidationError("unknown_quote", details={"quoteId": request.quote_id})
        if quote.customer_id != request.customer_id:
            raise ValidationError("quote_customer_mismatch")

        policy = Policy(
            policy_id=f"POL-{uuid.uuid4().hex[:10].upper()}",
            policy_number=f"PTC{uuid.uuid4().int % 10**10:010d}",
            customer_id=request.customer_id,
            product=quote.product,
            domain=quote.domain,
            status=PolicyStatus.ACTIVE,
            annual_premium=quote.total,
            addons=[line.label for line in quote.lines if line.label not in ("Base premium", "GST (18%)")],
            inception_date=date.today(),
            renewal_due=date.today() + timedelta(days=365),
        )
        self._policies.persist(policy)
        response = IssuePolicyResponse(policy=policy)
        self._by_idempotency[request.idempotency_key] = response
        return response


class MockDocumentProvider(_MockBase):
    async def list_documents(self, policy_id: str, ctx: RequestContext) -> list[DocumentRef]:
        await self._simulate("list_documents")
        return [
            DocumentRef(
                document_id=f"DOC-{policy_id}",
                name="Policy schedule",
                content_type="application/pdf",
                size_bytes=184_320,
            )
        ]


class MockReferenceDataProvider(_MockBase):
    async def list_reference(self, group: str, ctx: RequestContext) -> list[ReferenceItem]:
        await self._simulate("list_reference")
        return list(fixtures.REFERENCE_DATA.get(group, []))


def build_mock_bundle(faults: FaultInjection | None = None) -> dict[str, Any]:
    """Construct a wired set of mocks sharing one fault-injection configuration."""
    shared = faults or FaultInjection()
    policy = MockPolicyProvider(shared)
    quote = MockQuoteProvider(shared)
    return {
        "customer": MockCustomerProvider(shared),
        "policy": policy,
        "quote": quote,
        "product": MockProductProvider(shared),
        "claims": MockClaimsProvider(shared),
        "payment": MockPaymentProvider(shared),
        "issuance": MockPolicyIssuanceProvider(policy, quote, shared),
        "document": MockDocumentProvider(shared),
        "reference": MockReferenceDataProvider(shared),
        "faults": shared,
    }
