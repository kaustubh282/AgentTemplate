"""Provider selection through configuration and DI (master prompt §9, §15.3).

The rest of the application never learns whether a call reached a mock or InsureMO.
Selection happens exactly once, here, and production refuses mocks outright.
"""

from __future__ import annotations

from typing import Any

from app.core.config.settings import MOCK_PROVIDER, Settings
from app.core.errors.taxonomy import ConfigurationError
from app.core.logging.structured import get_logger
from app.integrations.contracts.providers import ProviderBundle
from app.integrations.insuremo.client import InsureMoClient
from app.integrations.insuremo.providers import (
    InsureMoClaimsProvider,
    InsureMoCustomerProvider,
    InsureMoDocumentProvider,
    InsureMoMapping,
    InsureMoPaymentProvider,
    InsureMoPolicyIssuanceProvider,
    InsureMoPolicyProvider,
    InsureMoProductProvider,
    InsureMoQuoteProvider,
    InsureMoReferenceDataProvider,
)
from app.integrations.mock.providers import FaultInjection, build_mock_bundle

logger = get_logger(__name__)

SUPPORTED_PROVIDERS = frozenset({MOCK_PROVIDER, "insuremo"})


def _assert_supported(name: str, value: str) -> None:
    if value not in SUPPORTED_PROVIDERS:
        raise ConfigurationError(f"unsupported_provider:{name}={value}")


def assert_no_mocks_in_production(settings: Settings) -> None:
    """Startup guard: production must never run against a mock (§15.3)."""
    if not settings.is_hardened:
        return
    mocked = [n for n, v in settings.configured_providers().items() if v == MOCK_PROVIDER]
    if mocked:
        raise ConfigurationError("production_refuses_mock_providers:" + ",".join(sorted(mocked)))


def build_provider_bundle(
    settings: Settings,
    *,
    faults: FaultInjection | None = None,
    insuremo_mapping: InsureMoMapping | None = None,
    insuremo_client: InsureMoClient | None = None,
) -> ProviderBundle:
    """Resolve every provider from configuration."""
    for name, value in {
        "CUSTOMER_PROVIDER": settings.customer_provider,
        "POLICY_PROVIDER": settings.policy_provider,
        "QUOTE_PROVIDER": settings.quote_provider,
        "PRODUCT_PROVIDER": settings.product_provider,
        "CLAIMS_PROVIDER": settings.claims_provider,
        "PAYMENT_PROVIDER": settings.payment_provider,
        "DOCUMENT_PROVIDER": settings.document_provider,
        "REFERENCE_DATA_PROVIDER": settings.reference_data_provider,
    }.items():
        _assert_supported(name, value)

    assert_no_mocks_in_production(settings)

    configured = {
        "customer": settings.customer_provider,
        "policy": settings.policy_provider,
        "quote": settings.quote_provider,
        "product": settings.product_provider,
        "claims": settings.claims_provider,
        "payment": settings.payment_provider,
        "document": settings.document_provider,
        "reference": settings.reference_data_provider,
    }
    any_mock = MOCK_PROVIDER in configured.values()
    any_real = any(value != MOCK_PROVIDER for value in configured.values())

    # Only the selected implementation is instantiated (L-4): production never builds
    # mock objects or fixture data, and a fully mocked local run never builds a client.
    mocks: dict[str, Any] = build_mock_bundle(faults) if any_mock else {}
    client: InsureMoClient | None = None
    mapping = insuremo_mapping or InsureMoMapping()
    if any_real:
        client = insuremo_client or InsureMoClient(
            settings.insuremo_base_url,
            settings.insuremo_api_key,
            settings.insuremo_tenant,
            timeout_ms=settings.provider_timeout_ms,
        )

    def pick(mock_key: str, real: type[Any]) -> Any:
        if configured[mock_key] == MOCK_PROVIDER:
            return mocks[mock_key]
        assert client is not None  # any_real guarantees construction above
        return real(client, mapping)

    uses_mock = MOCK_PROVIDER in {
        settings.customer_provider,
        settings.policy_provider,
        settings.quote_provider,
        settings.claims_provider,
        settings.payment_provider,
    }
    if uses_mock:
        logger.info(
            "provider_selection",
            extra={"mockProvidersEnabled": True, "environment": settings.app_env.value},
        )

    active_faults = faults

    async def health_probe() -> str:
        """What the configured providers can actually do right now (§30, H-4)."""
        if uses_mock:
            if active_faults is not None and (
                active_faults.timeout_operations or active_faults.unavailable_operations
            ):
                return "degraded"
            return "up"
        # REQUIRES_VERIFICATION: a real InsureMO ping endpoint. Until one is supplied,
        # an unconfigured client is reported as down rather than assumed healthy.
        return "up" if settings.insuremo_base_url else "down"

    return ProviderBundle(
        customer=pick("customer", InsureMoCustomerProvider),
        policy=pick("policy", InsureMoPolicyProvider),
        quote=pick("quote", InsureMoQuoteProvider),
        product=pick("product", InsureMoProductProvider),
        claims=pick("claims", InsureMoClaimsProvider),
        payment=pick("payment", InsureMoPaymentProvider),
        issuance=(
            mocks["issuance"]
            if settings.policy_provider == MOCK_PROVIDER
            else pick("policy", InsureMoPolicyIssuanceProvider)
        ),
        document=pick("document", InsureMoDocumentProvider),
        reference=pick("reference", InsureMoReferenceDataProvider),
        is_mock=uses_mock,
        health_probe=health_probe,
    )
