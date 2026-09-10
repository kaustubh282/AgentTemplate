"""JWT authentication boundary tests (master prompt §13.1 - all 14 required cases)."""

from __future__ import annotations

import base64
import io
import json
import logging

import jwt
import pytest

from app.core.auth.auth_context import ActorType, Role
from app.core.auth.jwt_service import DevSecretKeyResolver, JwtValidationService
from app.core.errors.taxonomy import AuthenticationRequiredError
from app.core.logging.structured import RedactingJsonFormatter
from tests.conftest import AUDIENCE, DEV_SECRET, ISSUER, auth_headers, make_settings, make_token

pytestmark = pytest.mark.security


def service(settings=None) -> JwtValidationService:
    resolved = settings or make_settings()
    return JwtValidationService(resolved, DevSecretKeyResolver(DEV_SECRET))


# 1. missing bearer token on a protected route
def test_missing_bearer_token_is_rejected(client):
    response = client.post(
        "/api/v1/actions",
        json={
            "conversation_id": "conv_1",
            "capability_id": "motor.workflow.action",
            "action": "BEGIN",
            "payload": {},
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# 2. malformed JWT
@pytest.mark.parametrize("bad", ["not-a-token", "a.b", "....", ""])
async def test_malformed_jwt_is_rejected(bad):
    with pytest.raises(AuthenticationRequiredError):
        await service().validate(bad)


# 3. invalid signature
async def test_invalid_signature_is_rejected():
    token = make_token(secret="a-completely-different-signing-secret")
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(token)
    assert exc.value.reason in ("invalid_signature", "invalid_token")


# 4. expired token
async def test_expired_token_is_rejected():
    token = make_token(expires_in=-3_600, issued_at_offset=-7_200)
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(token)
    assert exc.value.reason == "token_expired"


# 5. wrong issuer
async def test_wrong_issuer_is_rejected():
    token = make_token(issuer="https://evil.example/")
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(token)
    assert exc.value.reason == "invalid_issuer"


# 6. wrong audience
async def test_wrong_audience_is_rejected():
    token = make_token(audience="some-other-api")
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(token)
    assert exc.value.reason == "invalid_audience"


# 7. unsupported algorithm (including alg=none)
async def test_unsupported_algorithm_is_rejected():
    settings = make_settings(JWT_ALLOWED_ALGORITHMS="RS256")
    token = make_token(algorithm="HS256")
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service(settings).validate(token)
    assert exc.value.reason == "algorithm_not_allowed"


async def test_alg_none_token_is_rejected():
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "CUST-1001", "iss": ISSUER, "aud": AUDIENCE, "exp": 9_999_999_999}).encode()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(f"{header}.{payload}.")
    assert exc.value.reason == "algorithm_not_allowed"


def test_configuration_refuses_alg_none_in_the_allow_list():
    with pytest.raises(ValueError, match="alg=none"):
        make_settings(JWT_ALLOWED_ALGORITHMS="none")


# 8. tampered role / claim
async def test_tampered_claim_is_rejected():
    token = make_token(roles=["CUSTOMER"])
    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
    decoded["roles"] = ["AGENT_SUPERVISOR"]
    tampered_payload = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(AuthenticationRequiredError):
        await service().validate(f"{header}.{tampered_payload}.{signature}")


async def test_unknown_roles_in_a_valid_token_are_dropped_not_trusted():
    token = make_token(roles=["CUSTOMER", "SUPER_ADMIN", "root"])
    auth = await service().validate(token)
    assert auth.roles == (Role.CUSTOMER,)


async def test_refresh_token_cannot_authorize_an_api_call():
    token = make_token(token_use="refresh")
    with pytest.raises(AuthenticationRequiredError) as exc:
        await service().validate(token)
    assert exc.value.reason == "unexpected_token_use"


# 9. Customer attempting an Agent-only capability, and vice versa
def test_customer_cannot_use_agent_scoped_lookup(client, customer_token):
    response = client.get(
        "/api/v1/policies/POL-MTR-0002",
        headers=auth_headers(customer_token),
        params={"customer_id": "CUST-2002"},
    )
    assert response.status_code == 403


# 10. Agent attempting an unauthorized customer resource
def test_agent_without_assignment_is_forbidden(client, unassigned_agent_token):
    response = client.get(
        "/api/v1/policies/POL-MTR-0001",
        headers=auth_headers(unassigned_agent_token),
        params={"customer_id": "CUST-1001"},
    )
    assert response.status_code == 403


# 11. valid request creates a trusted AuthContext
async def test_valid_token_creates_trusted_auth_context():
    token = make_token(subject="CUST-1001", actor_type="CUSTOMER", roles=["CUSTOMER"])
    auth = await service().validate(token)
    assert auth.subject_id == "CUST-1001"
    assert auth.actor_type is ActorType.CUSTOMER
    assert auth.is_authenticated
    assert auth.tenant_id == "TENANT-IN"
    # The context carries no credential material at all.
    dumped = auth.model_dump()
    assert not any("token" in k for k in dumped if k != "token_id")
    assert "authorization" not in dumped


async def test_agent_assignment_claim_is_ignored_for_non_agents():
    token = make_token(actor_type="CUSTOMER", roles=["CUSTOMER"], assigned_customer_ids=["CUST-2002"])
    auth = await service().validate(token)
    assert auth.assigned_customer_ids == ()


# 12. the Authorization header is never written to logs or traces
def test_authorization_header_never_reaches_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter("svc", "test", "0.1.0"))
    logger = logging.getLogger("jwt_leak_probe")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    token = make_token()
    logger.info(
        "inbound", extra={"authorization": f"Bearer {token}", "headers": {"Authorization": f"Bearer {token}"}}
    )

    written = stream.getvalue()
    assert token not in written
    assert "[REDACTED]" in written


# 13. the JWT is never included in model context
async def test_jwt_cannot_cross_the_model_boundary(container):
    from app.core.errors.taxonomy import GuardrailBlockedError

    token = make_token()
    with pytest.raises(GuardrailBlockedError):
        container.sanitizer.assert_no_secrets(f"user said: Bearer {token}")

    cleaned, report = container.sanitizer.sanitize_payload(
        {"authorization": f"Bearer {token}", "question": "what is a deductible"}
    )
    assert "authorization" not in cleaned
    assert "authorization" in report.removed_fields


# 14. refresh tokens are never exposed to the AI runtime
async def test_refresh_token_field_is_stripped_before_model_context(container):
    cleaned, report = container.sanitizer.sanitize_payload({"refresh_token": "rt_abc123", "product": "Motor"})
    assert "refresh_token" not in cleaned
    assert cleaned["product"] == "Motor"
    assert "refresh_token" in report.removed_fields


# ---- transport-level checks -------------------------------------------------
@pytest.mark.parametrize("header", ["Token abc", "bearer", "Basic dXNlcjpwYXNz", "Bearer    "])
def test_non_bearer_authorization_headers_are_rejected(client, header):
    response = client.post(
        "/api/v1/actions",
        headers={"Authorization": header},
        json={
            "conversation_id": "c",
            "capability_id": "motor.workflow.action",
            "action": "BEGIN",
            "payload": {},
        },
    )
    assert response.status_code == 401


def test_401_response_carries_www_authenticate_and_no_internals(client):
    response = client.post(
        "/api/v1/actions",
        json={"conversation_id": "c", "capability_id": "x", "action": "y", "payload": {}},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    body = response.text.lower()
    for leak in ("traceback", 'file "', "sqlalchemy", "secret", "jwt_dev"):
        assert leak not in body


def test_public_faq_route_works_without_a_token(client):
    created = client.post("/api/v1/conversations", json={})
    assert created.status_code == 201
    response = client.post(
        "/api/v1/chat",
        json={"conversation_id": created.json()["conversation_id"], "message": "What is a deductible?"},
    )
    assert response.status_code == 200


def test_jwks_configuration_is_preferred_over_dev_secret():
    """Production key material must be asymmetric; dev secret is local-only."""
    settings = make_settings(JWT_JWKS_URI="https://idp.example/.well-known/jwks.json")
    from app.core.auth.jwt_service import JwksKeyResolver, build_key_resolver

    assert isinstance(build_key_resolver(settings), JwksKeyResolver)


def test_dev_secret_is_refused_outside_local_dev_test():
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError, match="local/dev/test"):
        make_settings(APP_ENV="preprod")


def test_encoded_token_round_trip_is_a_real_jwt():
    """Sanity: the fixture really produces a signed JWT (not a stub)."""
    token = make_token()
    decoded = jwt.decode(token, DEV_SECRET, algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE)
    assert decoded["sub"]
