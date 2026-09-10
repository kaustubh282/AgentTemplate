"""Provider contract tests (master prompt §9.2, §28).

The same behavioural suite is defined once and applied to every implementation of a
provider Protocol, so a future InsureMO adapter must satisfy exactly what the mock
satisfies. Where the InsureMO mapping is not yet supplied, the adapter must fail
loudly with ``REQUIRES_VERIFICATION`` rather than silently inventing a shape.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from app.core.errors.taxonomy import (
    AppError,
    UpstreamInvalidResponseError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.integrations.contracts.dtos import (
    CreateQuoteRequest,
    InitiatePaymentRequest,
    Money,
)
from app.integrations.contracts.providers import (
    ClaimsProvider,
    CustomerProvider,
    DocumentProvider,
    PaymentProvider,
    PolicyProvider,
    ProductProvider,
    QuoteProvider,
    ReferenceDataProvider,
)
from app.integrations.insuremo.client import InsureMoClient, InsureMoContractNotConfigured
from app.integrations.insuremo.providers import (
    InsureMoClaimsProvider,
    InsureMoCustomerProvider,
    InsureMoPaymentProvider,
    InsureMoPolicyProvider,
    InsureMoProductProvider,
    InsureMoQuoteProvider,
)
from app.integrations.mock import fixtures
from app.integrations.mock.providers import (
    FaultInjection,
    MockClaimsProvider,
    MockCustomerProvider,
    MockPaymentProvider,
    MockPolicyProvider,
    MockProductProvider,
    MockQuoteProvider,
)
from tests.conftest import customer_context

pytestmark = pytest.mark.contract


# ---------------------------------------------- structural interface parity ---
PROTOCOL_IMPLEMENTATIONS = [
    (CustomerProvider, MockCustomerProvider, InsureMoCustomerProvider),
    (PolicyProvider, MockPolicyProvider, InsureMoPolicyProvider),
    (QuoteProvider, MockQuoteProvider, InsureMoQuoteProvider),
    (ProductProvider, MockProductProvider, InsureMoProductProvider),
    (ClaimsProvider, MockClaimsProvider, InsureMoClaimsProvider),
    (PaymentProvider, MockPaymentProvider, InsureMoPaymentProvider),
]


@pytest.mark.parametrize(("protocol", "mock_cls", "real_cls"), PROTOCOL_IMPLEMENTATIONS)
def test_both_implementations_expose_the_same_methods(protocol, mock_cls, real_cls):
    """Substitution must be a configuration change, not a code change (§9)."""
    required = {
        name for name in dir(protocol) if not name.startswith("_") and callable(getattr(protocol, name, None))
    }
    for implementation in (mock_cls, real_cls):
        missing = [m for m in required if not hasattr(implementation, m)]
        assert not missing, f"{implementation.__name__} is missing {missing}"


@pytest.mark.parametrize(("protocol", "mock_cls", "real_cls"), PROTOCOL_IMPLEMENTATIONS)
def test_method_signatures_match_between_implementations(protocol, mock_cls, real_cls):
    for name in (n for n in dir(protocol) if not n.startswith("_")):
        proto_method = getattr(protocol, name, None)
        if not callable(proto_method):
            continue
        expected = list(inspect.signature(proto_method).parameters)
        for implementation in (mock_cls, real_cls):
            actual = list(inspect.signature(getattr(implementation, name)).parameters)
            assert actual == expected, f"{implementation.__name__}.{name} signature drift"


@pytest.mark.parametrize(("protocol", "mock_cls", "real_cls"), PROTOCOL_IMPLEMENTATIONS)
def test_return_annotations_match_between_implementations(protocol, mock_cls, real_cls):
    for name in (n for n in dir(protocol) if not n.startswith("_")):
        proto_method = getattr(protocol, name, None)
        if not callable(proto_method):
            continue
        expected = get_type_hints(proto_method).get("return")
        for implementation in (mock_cls, real_cls):
            actual = get_type_hints(getattr(implementation, name)).get("return")
            assert actual == expected, f"{implementation.__name__}.{name} return type drift"


def test_mock_bundle_satisfies_every_runtime_checkable_protocol(container):
    providers = container.providers
    assert isinstance(providers.customer, CustomerProvider)
    assert isinstance(providers.policy, PolicyProvider)
    assert isinstance(providers.quote, QuoteProvider)
    assert isinstance(providers.product, ProductProvider)
    assert isinstance(providers.claims, ClaimsProvider)
    assert isinstance(providers.payment, PaymentProvider)
    assert isinstance(providers.document, DocumentProvider)
    assert isinstance(providers.reference, ReferenceDataProvider)


# ----------------------------------------------------- behavioural contract ---
@pytest.fixture
def ctx():
    return customer_context(fixtures.CUSTOMER_A)


async def test_quote_provider_returns_a_valid_contract_response(ctx):
    provider = MockQuoteProvider()
    response = await provider.create_quote(
        CreateQuoteRequest(
            customer_id=fixtures.CUSTOMER_A,
            product_code="MTR-PVT-CAR",
            domain="motor",
            attributes={"idv": 500_000},
            addons=["Zero Depreciation"],
        ),
        ctx,
    )
    quote = response.quote
    assert quote.total.amount > 0
    assert quote.total.currency == "INR"
    assert quote.lines, "a quote must itemise its lines"
    assert quote.source_system == "MOCK", "mock data must be identifiable in telemetry"
    assert quote.customer_id == fixtures.CUSTOMER_A


async def test_quote_provider_rejects_an_unknown_product(ctx):
    provider = MockQuoteProvider()
    with pytest.raises(ValidationError):
        await provider.create_quote(
            CreateQuoteRequest(customer_id="C", product_code="NOPE", domain="motor"), ctx
        )


async def test_quote_provider_rejects_unsupported_addons(ctx):
    provider = MockQuoteProvider()
    with pytest.raises(ValidationError):
        await provider.create_quote(
            CreateQuoteRequest(
                customer_id="C", product_code="MTR-PVT-CAR", domain="motor", addons=["Time Travel"]
            ),
            ctx,
        )


async def test_quote_idempotency_returns_the_same_quote(ctx):
    provider = MockQuoteProvider()
    request = CreateQuoteRequest(
        customer_id=fixtures.CUSTOMER_A,
        product_code="MTR-PVT-CAR",
        domain="motor",
        idempotency_key="idem-quote-1",
    )
    first = await provider.create_quote(request, ctx)
    second = await provider.create_quote(request, ctx)
    assert first.quote.quote_id == second.quote.quote_id


async def test_payment_idempotency_prevents_duplicate_initiation(ctx):
    provider = MockPaymentProvider()
    request = InitiatePaymentRequest(
        customer_id=fixtures.CUSTOMER_A,
        quote_id="QTE-1",
        amount=Money(amount=100.0),
        idempotency_key="idem-pay-1",
    )
    first = await provider.initiate_payment(request, ctx)
    second = await provider.initiate_payment(request, ctx)
    assert first.payment.payment_id == second.payment.payment_id


async def test_ownership_lookups_return_the_authoritative_owner(ctx):
    policy = MockPolicyProvider()
    assert await policy.get_policy_owner(fixtures.POLICY_A_MOTOR) == fixtures.CUSTOMER_A
    assert await policy.get_policy_owner("POL-UNKNOWN") is None


# ------------------------------------------------------- failure behaviour ---
async def test_mock_simulates_upstream_unavailability(ctx):
    faults = FaultInjection(unavailable_operations={"get_policy"})
    provider = MockPolicyProvider(faults)
    with pytest.raises(UpstreamUnavailableError):
        await provider.get_policy(fixtures.POLICY_A_MOTOR, ctx)


async def test_mock_simulates_a_malformed_upstream_response(ctx):
    faults = FaultInjection(invalid_response_operations={"get_policy"})
    provider = MockPolicyProvider(faults)
    with pytest.raises(UpstreamInvalidResponseError):
        await provider.get_policy(fixtures.POLICY_A_MOTOR, ctx)


async def test_mock_simulates_a_missing_record_without_inventing_one(ctx):
    faults = FaultInjection(not_found_operations={"get_policy"})
    provider = MockPolicyProvider(faults)
    assert await provider.get_policy(fixtures.POLICY_A_MOTOR, ctx) is None


async def test_mock_simulates_latency(ctx):
    import time

    provider = MockPolicyProvider(FaultInjection(latency_ms=40))
    started = time.perf_counter()
    await provider.get_policy(fixtures.POLICY_A_MOTOR, ctx)
    assert (time.perf_counter() - started) >= 0.03


# --------------------------------------------- InsureMO error normalisation ---
class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "UPSTREAM_UNAVAILABLE"),
        (403, "FORBIDDEN"),
        (404, "RESOURCE_NOT_FOUND"),
        (409, "IDEMPOTENCY_CONFLICT"),
        (400, "VALIDATION_ERROR"),
        (422, "VALIDATION_ERROR"),
        (429, "RATE_LIMITED"),
        (500, "UPSTREAM_UNAVAILABLE"),
        (503, "UPSTREAM_UNAVAILABLE"),
    ],
)
def test_insuremo_status_codes_map_to_the_error_taxonomy(status, expected_code):
    with pytest.raises(AppError) as exc:
        InsureMoClient._normalise(_FakeResponse(status), "/quotes")
    assert exc.value.code.value == expected_code


def test_insuremo_non_json_response_is_an_invalid_response_error():
    with pytest.raises(UpstreamInvalidResponseError):
        InsureMoClient._normalise(_FakeResponse(200, None, "<html>"), "/quotes")


def test_insuremo_unexpected_payload_shape_is_rejected():
    with pytest.raises(UpstreamInvalidResponseError):
        InsureMoClient._normalise(_FakeResponse(200, ["not", "an", "object"]), "/quotes")


def test_insuremo_errors_do_not_leak_the_upstream_payload():
    payload = {"customerSecret": "abc", "internalHost": "db-prod-01"}
    with pytest.raises(AppError) as exc:
        InsureMoClient._normalise(_FakeResponse(500, payload), "/quotes")
    rendered = str(exc.value) + str(exc.value.details)
    assert "customerSecret" not in rendered
    assert "db-prod-01" not in rendered


# ----------------------------------------- REQUIRES_VERIFICATION behaviour ---
def test_insuremo_client_refuses_to_run_without_configuration():
    client = InsureMoClient(None, None, None)
    with pytest.raises(InsureMoContractNotConfigured, match="INSUREMO_BASE_URL"):
        client._require_configuration()


async def test_insuremo_adapter_fails_loudly_rather_than_inventing_a_mapping(ctx):
    """No endpoint or field mapping was supplied, so the adapter must refuse (§54.3)."""
    provider = InsureMoPolicyProvider(InsureMoClient("https://api.example", "k", "t"))
    with pytest.raises(InsureMoContractNotConfigured, match="endpoint:get_policy"):
        await provider.get_policy(fixtures.POLICY_A_MOTOR, ctx)


async def test_insuremo_ownership_lookup_is_explicitly_unimplemented():
    provider = InsureMoPolicyProvider(InsureMoClient("https://api.example", "k", "t"))
    with pytest.raises(InsureMoContractNotConfigured, match="ownership_lookup"):
        await provider.get_policy_owner("POL-1")


def test_insuremo_client_propagates_correlation_headers_but_never_logs_the_key():
    client = InsureMoClient("https://api.example", "super-secret-key", "tenant-1")
    headers = client._headers(customer_context())
    assert "X-Correlation-Id" in headers
    assert "X-Request-Id" in headers
    assert headers["Authorization"] == "Bearer super-secret-key"

    # The masking service refuses to let that header reach a log.
    from app.core.privacy.masking import masking_service

    assert "super-secret-key" not in str(masking_service.redact(headers))


# -------------------------------------------------------- factory selection ---
def test_provider_selection_is_configuration_driven():
    from app.core.config.settings import Settings
    from app.integrations.factory import build_provider_bundle

    mock_bundle = build_provider_bundle(Settings(APP_ENV="test"))
    assert isinstance(mock_bundle.policy, MockPolicyProvider)
    assert mock_bundle.is_mock is True

    real_bundle = build_provider_bundle(
        Settings(
            APP_ENV="test",
            POLICY_PROVIDER="insuremo",
            QUOTE_PROVIDER="insuremo",
            CUSTOMER_PROVIDER="insuremo",
            CLAIMS_PROVIDER="insuremo",
            PAYMENT_PROVIDER="insuremo",
            INSUREMO_BASE_URL="https://api.example",
            INSUREMO_API_KEY="k",
        )
    )
    assert isinstance(real_bundle.policy, InsureMoPolicyProvider)
    assert real_bundle.is_mock is False


def test_unsupported_provider_name_is_rejected_at_startup():
    from app.core.config.settings import Settings
    from app.core.errors.taxonomy import ConfigurationError
    from app.integrations.factory import build_provider_bundle

    with pytest.raises(ConfigurationError, match="unsupported_provider"):
        build_provider_bundle(Settings(APP_ENV="test", POLICY_PROVIDER="homemade"))
