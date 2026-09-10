"""Preprod mirrors production (master prompt §15.2/§15.3): every unsafe setting that
production refuses is refused in preprod too, and the additional guards added after the
acceptance audit fire (all providers critical, secret strength, TLS Redis, fail-closed
audit, guardrails on, OTEL endpoint when enabled, feature flags actually wired)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config.settings import Settings

SECRET = "0123456789abcdef0123456789abcdef-strong-enough"


def _hardened(env: str, **overrides: str) -> dict[str, str]:
    base = {
        "APP_ENV": env,
        "MODEL_PROVIDER": "openai",
        "MODEL_API_KEY": "sk-test-not-real-0123456789",
        "MODEL_ID": "gpt-test",
        "JWT_ISSUER": "https://auth.protec.example/",
        "JWT_JWKS_URI": "https://auth.protec.example/.well-known/jwks.json",
        "JWT_ALLOWED_ALGORITHMS": "RS256",
        "CUSTOMER_PROVIDER": "insuremo",
        "POLICY_PROVIDER": "insuremo",
        "QUOTE_PROVIDER": "insuremo",
        "PRODUCT_PROVIDER": "insuremo",
        "CLAIMS_PROVIDER": "insuremo",
        "PAYMENT_PROVIDER": "insuremo",
        "DOCUMENT_PROVIDER": "insuremo",
        "REFERENCE_DATA_PROVIDER": "insuremo",
        "INSUREMO_BASE_URL": "https://insuremo.example/api",
        "SESSION_STORE_PROVIDER": "redis",
        "WORKFLOW_STORE_PROVIDER": "redis",
        "RATE_LIMIT_STORE_PROVIDER": "redis",
        "REDIS_URL": "rediss://redis.internal:6380/0",
        "AUDIT_STORE_PROVIDER": "chained_file",
        "AUDIT_CHAIN_SECRET": SECRET,
        "CONFIRMATION_TOKEN_SECRET": SECRET,
        "PSEUDONYM_SECRET": SECRET,
        "RATE_LIMIT_ENABLED": "true",
        "OTEL_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.internal:4318",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("env", ["preprod", "prod"])
def test_valid_hardened_configuration_is_accepted(env):
    settings = Settings(**_hardened(env))  # type: ignore[arg-type]
    assert settings.is_hardened


@pytest.mark.parametrize("env", ["preprod", "prod"])
@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"PRODUCT_PROVIDER": "mock"}, "mock providers"),
        ({"DOCUMENT_PROVIDER": "mock"}, "mock providers"),
        ({"REFERENCE_DATA_PROVIDER": "mock"}, "mock providers"),
        ({"MODEL_PROVIDER": "deterministic"}, "test double"),
        ({"MODEL_FALLBACK_PROVIDER": "deterministic"}, "test double"),
        ({"SESSION_STORE_PROVIDER": "inmemory"}, "in-memory"),
        ({"REDIS_URL": "fakeredis://x"}, "fakeredis"),
        ({"AUDIT_STORE_PROVIDER": "file"}, "chained_file"),
        ({"AUDIT_FAIL_CLOSED": "false"}, "AUDIT_FAIL_CLOSED"),
        ({"CONFIRMATION_TOKEN_SECRET": "short"}, "at least 32"),
        ({"PSEUDONYM_SECRET": ""}, "PSEUDONYM_SECRET"),
        ({"AUDIT_CHAIN_SECRET": "tiny"}, "at least 32"),
        ({"JWT_ALLOWED_ALGORITHMS": "HS256"}, "symmetric"),
        ({"JWT_JWKS_URI": "http://auth.protec.example/jwks"}, "https"),
        ({"JWT_ISSUER": "https://auth.local.protec.example/"}, "local default"),
        ({"DEBUG_ENDPOINTS_ENABLED": "true"}, "DEBUG_ENDPOINTS_ENABLED"),
        ({"LOG_LEVEL": "DEBUG"}, "LOG_LEVEL"),
        ({"RATE_LIMIT_ENABLED": "false"}, "rate limiting"),
        ({"CORS_ALLOWED_ORIGINS": "https://a.example,*"}, "CORS"),
        ({"CORS_ALLOWED_ORIGINS": "null"}, "CORS"),
        ({"GUARDRAIL_INJECTION_ENABLED": "false"}, "guardrail"),
        ({"GUARDRAIL_OUTPUT_SCAN_ENABLED": "false"}, "guardrail"),
        ({"RAG_ALLOW_DRAFT_SOURCES": "true"}, "RAG_ALLOW_DRAFT_SOURCES"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}, "OTEL_EXPORTER_OTLP_ENDPOINT"),
    ],
)
def test_unsafe_setting_is_refused_in_hardened_environments(env, override, fragment):
    with pytest.raises(ValidationError, match=fragment):
        Settings(**_hardened(env, **override))  # type: ignore[arg-type]


def test_plaintext_redis_is_refused_in_prod_but_tolerated_in_preprod():
    Settings(**_hardened("preprod", REDIS_URL="redis://redis.internal:6379/0"))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="rediss"):
        Settings(**_hardened("prod", REDIS_URL="redis://redis.internal:6379/0"))  # type: ignore[arg-type]


def test_insuremo_without_base_url_is_refused_only_from_preprod_upwards():
    """Below preprod the adapter fails closed on use (§9.2); above it, at startup."""
    assert Settings(APP_ENV="dev", POLICY_PROVIDER="insuremo").uses_insuremo  # type: ignore[call-arg]
    for env in ("preprod", "prod"):
        with pytest.raises(ValidationError, match="INSUREMO_BASE_URL"):
            Settings(**_hardened(env, INSUREMO_BASE_URL=""))  # type: ignore[arg-type]


def test_dev_secret_and_deterministic_model_still_fine_below_preprod():
    settings = Settings(APP_ENV="dev", JWT_DEV_HS256_SECRET="dev-secret")  # type: ignore[call-arg]
    assert not settings.is_hardened and settings.model_provider == "deterministic"


# ------------------------------------------------------ feature flags wired ---
def test_public_faq_flag_off_requires_authentication_for_ai_capabilities():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import auth_headers, default_responder, make_settings, make_token

    app = create_app(make_settings(FEATURE_PUBLIC_FAQ_ENABLED="false"), responder=default_responder())
    with TestClient(app, raise_server_exceptions=False) as c:
        conv = c.post("/api/v1/conversations", json={}).json()["conversation_id"]
        anonymous = c.post("/api/v1/chat", json={"conversation_id": conv, "message": "What is a deductible?"})
        assert anonymous.status_code == 200
        assert anonymous.json()["meta"]["outcome"] == "BLOCK"
        tok = make_token()
        conv2 = c.post("/api/v1/conversations", json={}, headers=auth_headers(tok)).json()["conversation_id"]
        authed = c.post(
            "/api/v1/chat",
            json={"conversation_id": conv2, "message": "What is a deductible?"},
            headers=auth_headers(tok),
        )
        assert authed.json()["meta"]["outcome"] == "ALLOW"


def test_agentic_escalation_flag_off_routes_long_text_to_extraction():
    from app.bootstrap import build_container
    from tests.conftest import default_responder, make_settings

    container = build_container(
        make_settings(FEATURE_AGENTIC_ESCALATION_ENABLED="false"), responder=default_responder()
    )
    decision = container.router.route_message(" ".join(["please"] * 45))
    assert decision.capability_id == "platform.intent.extract"
    assert decision.reason == "escalation_disabled"
    default = build_container(make_settings(), responder=default_responder())
    assert (
        default.router.route_message(" ".join(["please"] * 45)).capability_id == "platform.supervisor.route"
    )
