"""Provider contracts: the InsureMO pick-and-drop boundary (master prompt §9).

The application depends only on these Protocols. Whether a call reaches a mock or
InsureMO is decided once, by configuration and dependency injection - never by a
conditional sprinkled through business code.

Every implementation must:
  * accept a trusted ``RequestContext``
  * raise only the standard error taxonomy
  * emit provider metrics/audit through :class:`ProviderCallRecorder`
  * never let a raw upstream payload escape into model context
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.core.context.request_context import RequestContext
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
    PaymentRecord,
    PaymentStatus,
    Policy,
    Product,
    Quote,
    ReferenceItem,
)


@runtime_checkable
class CustomerProvider(Protocol):
    async def get_customer(self, customer_id: str, ctx: RequestContext) -> Customer | None: ...
    async def find_by_mobile(self, mobile: str, ctx: RequestContext) -> Customer | None: ...
    #: Authoritative tenant of a customer, used by the ResourceScope tenant check (§13.2).
    async def get_customer_tenant(self, customer_id: str) -> str | None: ...


@runtime_checkable
class PolicyProvider(Protocol):
    async def list_policies(self, customer_id: str, ctx: RequestContext) -> list[Policy]: ...
    async def get_policy(self, policy_id: str, ctx: RequestContext) -> Policy | None: ...
    async def get_policy_owner(self, policy_id: str) -> str | None: ...


@runtime_checkable
class QuoteProvider(Protocol):
    async def create_quote(self, request: CreateQuoteRequest, ctx: RequestContext) -> CreateQuoteResponse: ...
    async def get_quote(self, quote_id: str, ctx: RequestContext) -> Quote | None: ...
    async def get_quote_owner(self, quote_id: str) -> str | None: ...


@runtime_checkable
class ProductProvider(Protocol):
    async def list_products(self, domain: str, ctx: RequestContext) -> list[Product]: ...
    async def get_product(self, product_code: str, ctx: RequestContext) -> Product | None: ...


@runtime_checkable
class ClaimsProvider(Protocol):
    async def list_claims(self, customer_id: str, ctx: RequestContext) -> list[Claim]: ...
    async def get_claim(self, claim_id: str, ctx: RequestContext) -> Claim | None: ...
    async def get_claim_owner(self, claim_id: str) -> str | None: ...


@runtime_checkable
class PaymentProvider(Protocol):
    async def initiate_payment(
        self, request: InitiatePaymentRequest, ctx: RequestContext
    ) -> InitiatePaymentResponse: ...
    async def get_payment(self, payment_id: str, ctx: RequestContext) -> PaymentRecord | None: ...
    async def get_payment_owner(self, payment_id: str) -> str | None: ...

    async def confirm_payment(
        self, payment_id: str, gateway_reference: str, status: PaymentStatus, ctx: RequestContext
    ) -> PaymentRecord:
        """Record the gateway's outcome for a payment (callback/webhook path, §8).

        Policy issuance requires ``PaymentStatus.SUCCESS`` from this authoritative record;
        a client action alone can never assert that a payment succeeded.
        """
        ...


@runtime_checkable
class PolicyIssuanceProvider(Protocol):
    async def issue_policy(self, request: IssuePolicyRequest, ctx: RequestContext) -> IssuePolicyResponse: ...


@runtime_checkable
class DocumentProvider(Protocol):
    async def list_documents(self, policy_id: str, ctx: RequestContext) -> list[DocumentRef]: ...


@runtime_checkable
class ReferenceDataProvider(Protocol):
    async def list_reference(self, group: str, ctx: RequestContext) -> list[ReferenceItem]: ...


class ProviderBundle:
    """All configured providers, resolved once at startup."""

    def __init__(
        self,
        *,
        customer: CustomerProvider,
        policy: PolicyProvider,
        quote: QuoteProvider,
        product: ProductProvider,
        claims: ClaimsProvider,
        payment: PaymentProvider,
        issuance: PolicyIssuanceProvider,
        document: DocumentProvider,
        reference: ReferenceDataProvider,
        is_mock: bool,
        health_probe: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self.customer = customer
        self.policy = policy
        self.quote = quote
        self.product = product
        self.claims = claims
        self.payment = payment
        self.issuance = issuance
        self.document = document
        self.reference = reference
        #: Surfaced in telemetry so mock data is always distinguishable (§9.1).
        self.is_mock = is_mock
        self._health_probe = health_probe

    async def health(self) -> str:
        """Coarse dependency status for readiness: ``up`` | ``degraded`` | ``down`` (§30).

        Installed by the factory for the selected implementation, so readiness reports
        what the configured providers can actually do rather than a hardcoded string.
        """
        if self._health_probe is None:
            return "down"
        return await self._health_probe()
