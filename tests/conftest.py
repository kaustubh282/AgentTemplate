"""Shared test fixtures.

Everything is deterministic: the model is a scripted test double, providers are mocks
with fault injection, and no test requires network access or credentials.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from app.ai.models.provider import EvidenceGroundedResponder, ScriptedResponder
from app.bootstrap import Container, build_container
from app.core.auth.auth_context import ActorType, AuthContext, Permission, Role, anonymous_context
from app.core.config.settings import Settings
from app.core.context.request_context import Channel, RequestContext
from app.core.observability.metrics import metrics
from app.core.observability.tracing import recorder
from app.integrations.mock import fixtures
from app.integrations.mock.providers import FaultInjection
from app.main import create_app

DEV_SECRET = "unit-test-signing-secret-not-a-real-key"
ISSUER = "https://auth.test.protec.example/"
AUDIENCE = "protec-insurance-ai"


# --------------------------------------------------------------- settings ---
def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "APP_ENV": "test",
        # Lifecycle overrides are persisted (§34); every test container gets its own file
        # so a revoke in one test can never leak into another run or into the repo.
        "RAG_LIFECYCLE_STATE_PATH": str(
            Path(tempfile.mkdtemp(prefix="protec-lifecycle-")) / "lifecycle.json"
        ),
        "JWT_ISSUER": ISSUER,
        "JWT_AUDIENCE": AUDIENCE,
        "JWT_DEV_HS256_SECRET": DEV_SECRET,
        "JWT_ALLOWED_ALGORITHMS": "HS256",
        "RATE_LIMIT_ENABLED": "false",
        "FAQ_CACHE_ENABLED": "false",
        # Short dependency budgets keep the fault-injection suite fast; the timeout
        # and retry *behaviour* under test is identical at production values.
        "PROVIDER_TIMEOUT_MS": "300",
        "PROVIDER_MAX_RETRIES": "1",
        "MODEL_TIMEOUT_MS": "2000",
    }
    base.update({k: str(v) if not isinstance(v, str) else v for k, v in overrides.items()})
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# ------------------------------------------------------------------ model ---
def default_responder() -> ScriptedResponder:
    """The shared model double for tests and evals.

    It answers *extractively from the supplied evidence* rather than from a
    rule-per-question script. That matters for evaluation honesty: a scripted double
    makes a metric measure the script's question coverage instead of the platform's
    retrieval and grounding. Structured-output calls still need explicit rules,
    because they must return valid JSON for a specific schema.
    """
    responder = EvidenceGroundedResponder()
    responder.add(r"ALLOWED_INTENTS", _extract_intent)
    responder.add(r"ALLOWED_CAPABILITIES", _route_capability)
    return responder


#: Words that make a message specific enough to classify without asking.
_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "START_PURCHASE": ("buy", "purchase", "new policy", "quote"),
    "GET_POLICY_PREMIUM": ("premium",),
    "GET_POLICY_DETAILS": ("policy details", "my policy", "renewal"),
    "GET_CLAIM_STATUS": ("claim status", "my claim"),
    "GET_PAYMENT_STATUS": ("payment status", "my payment"),
    "FAQ_QUESTION": (
        "what is",
        "what does",
        "what are",
        "how do",
        "how is",
        "explain",
        "define",
        "meaning",
        "cover",
        "deductible",
        "premium",
        "bonus",
    ),
}


def _message_from_prompt(prompt: str) -> str:
    """Pull the innermost MESSAGE/QUESTION block out of a rendered prompt."""
    for marker in ("MESSAGE:", "QUESTION:"):
        index = prompt.rfind(marker)
        if index != -1:
            return prompt[index + len(marker) :].strip()
    return prompt.strip()


def _extract_intent(prompt: str) -> str:
    """A plausible extractor: classify when the message is specific, else ask.

    A fixed response would make every ambiguity case look unambiguous, so the double
    decides from the message the same way a competent model would.
    """
    message = _message_from_prompt(prompt).lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(keyword in message for keyword in keywords):
            return json.dumps(
                {
                    "intent": intent,
                    "confidence": 0.85,
                    "entities": {},
                    "ambiguous": False,
                    "clarifying_question": "",
                }
            )
    return json.dumps(
        {
            "intent": "UNSUPPORTED",
            "confidence": 0.2,
            "entities": {},
            "ambiguous": True,
            "clarifying_question": "Could you tell me a little more about what you would like to do?",
        }
    )


def _route_capability(prompt: str) -> str:
    """Escalation routing: pick the FAQ capability for knowledge-shaped messages."""
    message = _message_from_prompt(prompt).lower()
    knowledge_shaped = any(keyword in message for keyword in _INTENT_KEYWORDS["FAQ_QUESTION"])
    if knowledge_shaped:
        return json.dumps(
            {
                "capability": "platform.faq.answer",
                "reason_category": "knowledge_question",
                "needs_clarification": False,
                "clarifying_question": "",
            }
        )
    return json.dumps(
        {
            "capability": "UNSUPPORTED",
            "reason_category": "not_routable",
            "needs_clarification": False,
            "clarifying_question": "",
        }
    )


@pytest.fixture
def responder() -> ScriptedResponder:
    return default_responder()


# -------------------------------------------------------------- container ---
@pytest.fixture
def faults() -> FaultInjection:
    return FaultInjection()


@pytest.fixture
def container(
    settings: Settings, responder: ScriptedResponder, faults: FaultInjection
) -> Iterator[Container]:
    metrics.reset()
    recorder.reset()
    built = build_container(settings, responder=responder, faults=faults)
    yield built
    metrics.reset()
    recorder.reset()


@pytest.fixture
def client(settings: Settings, responder: ScriptedResponder) -> Iterator[TestClient]:
    metrics.reset()
    app = create_app(settings, responder=responder)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    metrics.reset()


# ------------------------------------------------------------------ auth ----
def make_token(
    *,
    subject: str = fixtures.CUSTOMER_A,
    actor_type: str = "CUSTOMER",
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    algorithm: str = "HS256",
    secret: str = DEV_SECRET,
    expires_in: int = 900,
    issued_at_offset: int = 0,
    token_use: str = "access",
    assigned_customer_ids: list[str] | None = None,
    tenant_id: str | None = "TENANT-IN",
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = int(time.time()) + issued_at_offset
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in,
        "jti": f"jti_{subject}_{now}",
        "sid": f"sid_{subject}",
        "actor_type": actor_type,
        "roles": roles if roles is not None else [actor_type],
        "token_use": token_use,
        "tenant_id": tenant_id,
    }
    if permissions is not None:
        claims["permissions"] = permissions
    if assigned_customer_ids is not None:
        claims["assigned_customer_ids"] = assigned_customer_ids
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, secret, algorithm=algorithm)


@pytest.fixture
def customer_token() -> str:
    return make_token(subject=fixtures.CUSTOMER_A, actor_type="CUSTOMER", roles=["CUSTOMER"])


@pytest.fixture
def other_customer_token() -> str:
    return make_token(subject=fixtures.CUSTOMER_B, actor_type="CUSTOMER", roles=["CUSTOMER"])


@pytest.fixture
def agent_token() -> str:
    return make_token(
        subject="AGT-9001",
        actor_type="AGENT",
        roles=["AGENT"],
        assigned_customer_ids=[fixtures.AGENT_ASSIGNED_CUSTOMER],
    )


@pytest.fixture
def unassigned_agent_token() -> str:
    return make_token(subject="AGT-9002", actor_type="AGENT", roles=["AGENT"], assigned_customer_ids=[])


def auth_headers(token: str, conversation_id: str | None = None, **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if conversation_id:
        headers["X-Conversation-Id"] = conversation_id
    headers.update(extra)
    return headers


# ----------------------------------------------------------- context helpers ---
def customer_context(
    subject: str = fixtures.CUSTOMER_A,
    *,
    conversation_id: str = "conv_test",
    idempotency_key: str | None = None,
    tenant_id: str | None = "TENANT-IN",
) -> RequestContext:
    return RequestContext(
        conversation_id=conversation_id,
        channel=Channel.WEB_CUSTOMER,
        environment="test",
        idempotency_key=idempotency_key,
        auth=AuthContext(
            subject_id=subject,
            actor_type=ActorType.CUSTOMER,
            roles=(Role.CUSTOMER,),
            permissions=(),
            tenant_id=tenant_id,
            session_id="sid_test",
        ),
    )


def agent_context(
    subject: str = "AGT-9001",
    *,
    assigned: tuple[str, ...] = (fixtures.AGENT_ASSIGNED_CUSTOMER,),
    conversation_id: str = "conv_agent",
) -> RequestContext:
    return RequestContext(
        conversation_id=conversation_id,
        channel=Channel.WEB_AGENT,
        environment="test",
        auth=AuthContext(
            subject_id=subject,
            actor_type=ActorType.AGENT,
            roles=(Role.AGENT,),
            assigned_customer_ids=assigned,
            tenant_id="TENANT-IN",
        ),
    )


def public_context(conversation_id: str = "conv_public") -> RequestContext:
    return RequestContext(
        conversation_id=conversation_id,
        channel=Channel.PUBLIC_WEB,
        environment="test",
        auth=anonymous_context(),
    )


ALL_PERMISSIONS = [p.value for p in Permission]
