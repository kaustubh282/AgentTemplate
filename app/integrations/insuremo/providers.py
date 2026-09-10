"""InsureMO provider adapters.

REQUIRES_VERIFICATION - these adapters implement the *same* Protocols the mocks
implement, so switching is a configuration change. The request/response *mapping* is
intentionally absent: no InsureMO endpoint path, field name or payload shape has been
supplied, and inventing one would be a fabricated integration (§54.3).

Each method therefore raises :class:`InsureMoContractNotConfigured` until a mapping is
supplied through :class:`InsureMoMapping`. The contract tests in
``tests/contract/test_provider_contracts.py`` run against both implementations, so as
soon as a mapping lands the same behavioural suite applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
from app.integrations.insuremo.client import InsureMoClient, InsureMoContractNotConfigured


@dataclass(frozen=True, slots=True)
class InsureMoMapping:
    """Endpoint paths and field mappings, supplied from the approved specification.

    Every entry is empty by default. See
    ``docs/integrations/INSUREMO_MAPPING_TEMPLATE.md`` for the template to complete.
    """

    endpoints: dict[str, str] = field(default_factory=dict)
    field_map: dict[str, dict[str, str]] = field(default_factory=dict)

    def endpoint(self, operation: str) -> str:
        path = self.endpoints.get(operation)
        if not path:
            raise InsureMoContractNotConfigured(f"endpoint:{operation}")
        return path

    def map_response(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        mapping = self.field_map.get(operation)
        if not mapping:
            raise InsureMoContractNotConfigured(f"field_map:{operation}")
        return {target: payload.get(source) for target, source in mapping.items()}


class _InsureMoProviderBase:
    def __init__(self, client: InsureMoClient, mapping: InsureMoMapping | None = None) -> None:
        self._client = client
        self._mapping = mapping or InsureMoMapping()


class InsureMoCustomerProvider(_InsureMoProviderBase):
    async def get_customer(self, customer_id: str, ctx: RequestContext) -> Customer | None:
        path = self._mapping.endpoint("get_customer")
        payload = await self._client.request("GET", path.format(customer_id=customer_id), ctx)
        return Customer.model_validate(self._mapping.map_response("get_customer", payload))

    async def get_customer_tenant(self, customer_id: str) -> str | None:
        # REQUIRES_VERIFICATION: the tenant attribute of an InsureMO customer record.
        # Fails closed - the tenant check then denies - until the mapping is supplied.
        raise InsureMoContractNotConfigured("tenant_lookup:get_customer_tenant")

    async def find_by_mobile(self, mobile: str, ctx: RequestContext) -> Customer | None:
        path = self._mapping.endpoint("find_customer_by_mobile")
        payload = await self._client.request("GET", path, ctx, params={"mobile": mobile})
        return Customer.model_validate(self._mapping.map_response("find_customer_by_mobile", payload))


class InsureMoPolicyProvider(_InsureMoProviderBase):
    async def list_policies(self, customer_id: str, ctx: RequestContext) -> list[Policy]:
        path = self._mapping.endpoint("list_policies")
        payload = await self._client.request("GET", path, ctx, params={"customerId": customer_id})
        items = payload.get("items", [])
        return [Policy.model_validate(self._mapping.map_response("policy", item)) for item in items]

    async def get_policy(self, policy_id: str, ctx: RequestContext) -> Policy | None:
        path = self._mapping.endpoint("get_policy")
        payload = await self._client.request("GET", path.format(policy_id=policy_id), ctx)
        return Policy.model_validate(self._mapping.map_response("policy", payload))

    async def get_policy_owner(self, policy_id: str) -> str | None:
        raise InsureMoContractNotConfigured("ownership_lookup:get_policy_owner")


class InsureMoQuoteProvider(_InsureMoProviderBase):
    async def create_quote(self, request: CreateQuoteRequest, ctx: RequestContext) -> CreateQuoteResponse:
        path = self._mapping.endpoint("create_quote")
        payload = await self._client.request(
            "POST",
            path,
            ctx,
            json_body=self._mapping.map_response("create_quote_request", request.model_dump()),
            idempotency_key=request.idempotency_key,
        )
        return CreateQuoteResponse(quote=Quote.model_validate(self._mapping.map_response("quote", payload)))

    async def get_quote(self, quote_id: str, ctx: RequestContext) -> Quote | None:
        path = self._mapping.endpoint("get_quote")
        payload = await self._client.request("GET", path.format(quote_id=quote_id), ctx)
        return Quote.model_validate(self._mapping.map_response("quote", payload))

    async def get_quote_owner(self, quote_id: str) -> str | None:
        raise InsureMoContractNotConfigured("ownership_lookup:get_quote_owner")


class InsureMoProductProvider(_InsureMoProviderBase):
    async def list_products(self, domain: str, ctx: RequestContext) -> list[Product]:
        path = self._mapping.endpoint("list_products")
        payload = await self._client.request("GET", path, ctx, params={"domain": domain})
        return [
            Product.model_validate(self._mapping.map_response("product", item))
            for item in payload.get("items", [])
        ]

    async def get_product(self, product_code: str, ctx: RequestContext) -> Product | None:
        path = self._mapping.endpoint("get_product")
        payload = await self._client.request("GET", path.format(product_code=product_code), ctx)
        return Product.model_validate(self._mapping.map_response("product", payload))


class InsureMoClaimsProvider(_InsureMoProviderBase):
    async def list_claims(self, customer_id: str, ctx: RequestContext) -> list[Claim]:
        path = self._mapping.endpoint("list_claims")
        payload = await self._client.request("GET", path, ctx, params={"customerId": customer_id})
        return [
            Claim.model_validate(self._mapping.map_response("claim", item))
            for item in payload.get("items", [])
        ]

    async def get_claim(self, claim_id: str, ctx: RequestContext) -> Claim | None:
        path = self._mapping.endpoint("get_claim")
        payload = await self._client.request("GET", path.format(claim_id=claim_id), ctx)
        return Claim.model_validate(self._mapping.map_response("claim", payload))

    async def get_claim_owner(self, claim_id: str) -> str | None:
        raise InsureMoContractNotConfigured("ownership_lookup:get_claim_owner")


class InsureMoPaymentProvider(_InsureMoProviderBase):
    async def initiate_payment(
        self, request: InitiatePaymentRequest, ctx: RequestContext
    ) -> InitiatePaymentResponse:
        path = self._mapping.endpoint("initiate_payment")
        payload = await self._client.request(
            "POST",
            path,
            ctx,
            json_body=self._mapping.map_response("initiate_payment_request", request.model_dump()),
            idempotency_key=request.idempotency_key,
        )
        return InitiatePaymentResponse.model_validate(
            self._mapping.map_response("initiate_payment_response", payload)
        )

    async def get_payment(self, payment_id: str, ctx: RequestContext) -> PaymentRecord | None:
        path = self._mapping.endpoint("get_payment")
        payload = await self._client.request("GET", path.format(payment_id=payment_id), ctx)
        return PaymentRecord.model_validate(self._mapping.map_response("payment", payload))

    async def get_payment_owner(self, payment_id: str) -> str | None:
        raise InsureMoContractNotConfigured("ownership_lookup:get_payment_owner")

    async def confirm_payment(
        self, payment_id: str, gateway_reference: str, status: PaymentStatus, ctx: RequestContext
    ) -> PaymentRecord:
        path = self._mapping.endpoint("confirm_payment")
        payload = await self._client.request(
            "POST",
            path.format(payment_id=payment_id),
            ctx,
            json_body=self._mapping.map_response(
                "confirm_payment_request",
                {"payment_id": payment_id, "gateway_reference": gateway_reference, "status": status.value},
            ),
            idempotency_key=gateway_reference,
        )
        return PaymentRecord.model_validate(self._mapping.map_response("payment", payload))


class InsureMoPolicyIssuanceProvider(_InsureMoProviderBase):
    async def issue_policy(self, request: IssuePolicyRequest, ctx: RequestContext) -> IssuePolicyResponse:
        path = self._mapping.endpoint("issue_policy")
        payload = await self._client.request(
            "POST",
            path,
            ctx,
            json_body=self._mapping.map_response("issue_policy_request", request.model_dump()),
            idempotency_key=request.idempotency_key,
        )
        return IssuePolicyResponse(
            policy=Policy.model_validate(self._mapping.map_response("policy", payload))
        )


class InsureMoDocumentProvider(_InsureMoProviderBase):
    async def list_documents(self, policy_id: str, ctx: RequestContext) -> list[DocumentRef]:
        path = self._mapping.endpoint("list_documents")
        payload = await self._client.request("GET", path.format(policy_id=policy_id), ctx)
        return [
            DocumentRef.model_validate(self._mapping.map_response("document", item))
            for item in payload.get("items", [])
        ]


class InsureMoReferenceDataProvider(_InsureMoProviderBase):
    async def list_reference(self, group: str, ctx: RequestContext) -> list[ReferenceItem]:
        path = self._mapping.endpoint("list_reference")
        payload = await self._client.request("GET", path, ctx, params={"group": group})
        return [
            ReferenceItem.model_validate(self._mapping.map_response("reference", item))
            for item in payload.get("items", [])
        ]
