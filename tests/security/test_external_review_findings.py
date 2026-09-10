"""Executable regression probes for the external review findings (reports/FOCUSED_EXTERNAL_REVIEW.md).

Each test reproduces the reviewer's falsification probe against the real composed
container or the real HTTP app and asserts the *remediated* behaviour. The finding id
is in the test name so the readiness report can point at executable evidence.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.ai.harness.decisions import HarnessOutcome, HarnessResult, ReasonCode
from app.ai.harness.service import AgentExecutionContext, CapabilityRequest
from app.ai.models.provider import user_message
from app.core.audit.events import AuditAction
from app.core.config.settings import Settings
from app.core.errors.taxonomy import ConfigurationError, ForbiddenError, IdempotencyConflictError
from app.core.privacy.masking import masking_service
from app.domains.motor import workflow as wf
from app.integrations.mock import fixtures
from app.orchestration.capabilities import CAPABILITY_FAQ, CAPABILITY_INTENT
from app.rag.governance.documents import DocumentStatus
from tests.conftest import auth_headers, customer_context, public_context
from tests.e2e.test_demo_scenarios import (
    VALID_VEHICLE,
    act,
    confirmation_token_for,
    drive_to_review,
    new_conversation,
    start_motor_flow,
)

pytestmark = pytest.mark.security

VICTIM_VEHICLE = {**VALID_VEHICLE, "vehicle_make": "VictimCar Deluxe", "idv": 987654}


def _drive_victim_to_review(client, token, conversation_id) -> dict:
    assert start_motor_flow(client, token, conversation_id).status_code == 200
    body: dict = {}
    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VICTIM_VEHICLE)),
        (wf.ACTION_SELECT_ADDONS, {"addons": ["Zero Depreciation"]}),
        (wf.ACTION_REQUEST_QUOTE, {}),
        (wf.ACTION_REVIEW, {}),
    ]:
        response = act(client, token, conversation_id, action, payload)
        assert response.status_code == 200, (action, response.text)
        body = response.json()
    assert body["meta"]["state"] == wf.STATE_REVIEW
    return body


# =============================================================== C-1 ==============
class TestC1CrossCustomerConversationAccess:
    """Any authenticated customer supplying another customer's conversation id."""

    def test_attacker_cannot_read_the_victims_review_via_continue(
        self, client, customer_token, other_customer_token
    ):
        conversation_id = new_conversation(client, customer_token)
        _drive_victim_to_review(client, customer_token, conversation_id)

        attack = client.post(
            "/api/v1/chat",
            headers=auth_headers(other_customer_token, conversation_id),
            json={"conversation_id": conversation_id, "message": "continue"},
        )
        assert attack.status_code == 403
        assert attack.json()["error"]["code"] == "FORBIDDEN"
        for leaked in ("VictimCar", "987", "Zero Depreciation", "flowId", "allowed_actions", wf.STATE_REVIEW):
            assert leaked not in attack.text, f"disclosed {leaked!r} to another customer"

    def test_attacker_cannot_learn_flow_position_via_an_faq_turn(
        self, client, customer_token, other_customer_token
    ):
        conversation_id = new_conversation(client, customer_token)
        _drive_victim_to_review(client, customer_token, conversation_id)

        attack = client.post(
            "/api/v1/chat",
            headers=auth_headers(other_customer_token, conversation_id),
            json={"conversation_id": conversation_id, "message": "What is a deductible?"},
        )
        assert attack.status_code == 403
        assert "flowStateUnchanged" not in attack.text

    def test_attacker_cannot_act_or_start_in_the_victims_conversation(
        self, client, customer_token, other_customer_token
    ):
        conversation_id = new_conversation(client, customer_token)
        _drive_victim_to_review(client, customer_token, conversation_id)

        action = act(client, other_customer_token, conversation_id, wf.ACTION_REVIEW, {})
        assert action.status_code == 403
        started = start_motor_flow(client, other_customer_token, conversation_id)
        assert started.status_code == 403
        assert "VictimCar" not in started.text

    def test_the_owner_still_resumes_normally(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        before = _drive_victim_to_review(client, customer_token, conversation_id)
        resumed = client.post(
            "/api/v1/chat",
            headers=auth_headers(customer_token, conversation_id),
            json={"conversation_id": conversation_id, "message": "continue"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["meta"]["state"] == before["meta"]["state"]

    def test_denied_conversation_access_is_audited(self, client, customer_token, other_customer_token):
        conversation_id = new_conversation(client, customer_token)
        client.post(
            "/api/v1/chat",
            headers=auth_headers(other_customer_token, conversation_id),
            json={"conversation_id": conversation_id, "message": "continue"},
        )
        container = client.app.state.container
        denied = container.audit_sink.events(AuditAction.AUTHORIZATION_DENIED)
        assert any(e.reason_code == "conversation_owner_mismatch" for e in denied)

    async def test_engine_get_active_is_scope_aware(self, container):
        owner = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_c1")
        outcome = await container.workflow_engine.start(owner, wf.WORKFLOW_ID, conversation_id="conv_c1")
        assert outcome.state.owner_subject_id == fixtures.CUSTOMER_A

        attacker = customer_context(fixtures.CUSTOMER_B, conversation_id="conv_c1")
        with pytest.raises(ForbiddenError, match="workflow_owner_mismatch"):
            await container.workflow_engine.get_active("conv_c1", attacker)
        with pytest.raises(ForbiddenError, match="workflow_owner_mismatch"):
            await container.workflow_engine.start(attacker, wf.WORKFLOW_ID, conversation_id="conv_c1")


# =============================================================== H-1 ==============
class TestH1CrossTenantIsolationIsLive:
    async def test_other_tenant_cannot_read_a_tenant_in_resource(self, container):
        ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-XX")
        with pytest.raises(ForbiddenError, match="cross_tenant_access_denied"):
            await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)

    async def test_reference_tenant_comes_from_the_system_of_record(self, container):
        from app.core.resource_scope.scope import ResourceType

        ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-IN")
        ref = await container.scope.authorize_resource(ctx.auth, ResourceType.POLICY, fixtures.POLICY_A_MOTOR)
        assert ref.tenant_id == fixtures.CUSTOMERS[fixtures.CUSTOMER_A].tenant_id


# =============================================================== H-2 ==============
class TestH2BudgetsAreEnforcedNotCooperative:
    async def test_a_handler_bypassing_the_ledger_is_stopped_at_the_budget(self, container):
        """The reviewer's probe: 6 raw invoker calls against a budget of 1."""
        model = container.invoker.model
        calls_before = getattr(model, "call_count", 0)

        async def greedy(agent_ctx: AgentExecutionContext) -> HarnessResult:
            agent_ctx.agent_id = "greedy_agent"
            for _ in range(6):
                await agent_ctx.invoker.generate(
                    [user_message("hello")], capability_id=agent_ctx.capability.id
                )
            return HarnessResult(HarnessOutcome.ALLOW, ReasonCode.OK, "done", {})

        container.harness._handlers[CAPABILITY_INTENT] = greedy
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_INTENT, user_message="hello", payload={"message": "hello"}
            ),
        )
        assert result.outcome is HarnessOutcome.BLOCK
        assert result.reason_code is ReasonCode.BLOCK_TOKEN_BUDGET
        assert result.record is not None
        actual_calls = getattr(model, "call_count", 0) - calls_before
        assert actual_calls == 1, "exactly one call may reach the model under a budget of 1"
        assert result.record.model_calls == actual_calls, "telemetry must equal actual spend"

    async def test_a_model_call_outside_the_harness_is_refused(self, container):
        with pytest.raises(ForbiddenError, match="BLOCK_UNGOVERNED_MODEL_CALL"):
            await container.invoker.generate([user_message("hi")])

    async def test_recorded_calls_equal_actual_calls_on_the_normal_path(self, container):
        model = container.invoker.model
        before = getattr(model, "call_count", 0)
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
        assert result.record is not None
        assert result.record.model_calls == getattr(model, "call_count", 0) - before == 1


# =============================================================== H-3 ==============
class TestH3GroundingIsEnforcedCentrally:
    async def test_an_allow_with_no_evidence_is_abstained_by_the_harness(self, container):
        async def rogue(agent_ctx: AgentExecutionContext) -> HarnessResult:
            agent_ctx.agent_id = "rogue"
            return HarnessResult(
                HarnessOutcome.ALLOW,
                ReasonCode.OK,
                "Your deductible is waived for all claims under this policy.",
                {"answer": "x", "verification": "VERIFIED", "citations": []},
            )

        container.harness._handlers[CAPABILITY_FAQ] = rogue
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="Is my deductible waived?",
                payload={"question": "Is my deductible waived?"},
            ),
        )
        assert result.outcome is HarnessOutcome.ABSTAIN
        assert result.reason_code is ReasonCode.ABSTAIN_INSUFFICIENT_EVIDENCE
        assert result.record is not None
        assert result.record.grounding_decision == "ABSTAIN"

    async def test_a_fabricated_claim_over_real_evidence_is_abstained(self, container):
        from app.rag.governance.documents import Audience
        from app.rag.retrieval.retriever import RetrievalFilter

        async def fabricating(agent_ctx: AgentExecutionContext) -> HarnessResult:
            agent_ctx.agent_id = "fabricating"
            agent_ctx.retrieval = container.retriever.retrieve(
                agent_ctx.request.user_message, RetrievalFilter(audience=Audience.PUBLIC)
            )
            return HarnessResult(
                HarnessOutcome.ALLOW,
                ReasonCode.OK,
                "The premium is refunded twice yearly and every claim is guaranteed approval.",
                {"answer": "x", "verification": "VERIFIED", "citations": []},
            )

        container.harness._handlers[CAPABILITY_FAQ] = fabricating
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
        assert result.outcome is not HarnessOutcome.ALLOW

    def test_the_faq_agent_no_longer_owns_the_grounding_decision(self):
        from pathlib import Path

        source = Path("app/ai/agents/faq_agent.py").read_text(encoding="utf-8")
        assert "assess_answer(" not in source


# =============================================================== H-4 ==============
class TestH4ReadinessIsMeasured:
    def test_ready_when_everything_is_healthy(self, client):
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_not_ready_when_providers_are_failing(self, client):
        container = client.app.state.container
        container.faults.unavailable_operations.update({"get_policy", "create_quote", "list_policies"})
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert {d["name"]: d["status"] for d in body["dependencies"]}["providers"] == "degraded"

    def test_not_ready_when_all_knowledge_is_revoked(self, client):
        container = client.app.state.container
        for document in container.corpus.documents():
            container.corpus.set_status(document.document_id, DocumentStatus.REVOKED)
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert {d["name"]: d["status"] for d in response.json()["dependencies"]}["knowledge"] == "degraded"

    def test_not_ready_when_the_provider_breaker_is_open(self, client):
        container = client.app.state.container
        breaker = container.provider_policy.breaker
        for _ in range(50):
            breaker.on_failure()
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert {d["name"]: d["status"] for d in response.json()["dependencies"]}["providers"] == "down"

    def test_model_degrades_after_consecutive_failures(self, client):
        container = client.app.state.container
        container.invoker.consecutive_failures = container.invoker.DEGRADED_AFTER_FAILURES
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert {d["name"]: d["status"] for d in response.json()["dependencies"]}["model"] == "degraded"

    def test_readiness_exposes_no_topology(self, client):
        text = client.get("/api/v1/health/ready").text
        for forbidden in ("http", "redis", "localhost", "insuremo", "key"):
            assert forbidden not in text.lower()


# =============================================================== H-5 ==============
class TestH5ProductionRefusesInMemoryState:
    PROD_BASE = {
        "APP_ENV": "prod",
        "MODEL_PROVIDER": "openai",
        "MODEL_API_KEY": "placeholder-not-a-real-key",
        "JWT_JWKS_URI": "https://idp.example/jwks.json",
        "JWT_ALLOWED_ALGORITHMS": "RS256",
        "JWT_DEV_HS256_SECRET": "",
        "JWT_ISSUER": "https://idp.example/",
        "CUSTOMER_PROVIDER": "insuremo",
        "POLICY_PROVIDER": "insuremo",
        "QUOTE_PROVIDER": "insuremo",
        "PRODUCT_PROVIDER": "insuremo",
        "CLAIMS_PROVIDER": "insuremo",
        "PAYMENT_PROVIDER": "insuremo",
        "DOCUMENT_PROVIDER": "insuremo",
        "REFERENCE_DATA_PROVIDER": "insuremo",
        "INSUREMO_BASE_URL": "https://insuremo.example/api",
        "CORS_ALLOWED_ORIGINS": "https://app.protec.example",
        "CONFIRMATION_TOKEN_SECRET": "placeholder-not-a-real-secret-0123456789",
        "PSEUDONYM_SECRET": "placeholder-not-a-real-secret-0123456789",
        "SESSION_STORE_PROVIDER": "redis",
        "WORKFLOW_STORE_PROVIDER": "redis",
        "RATE_LIMIT_STORE_PROVIDER": "redis",
        "REDIS_URL": "rediss://redis.internal.example:6380/0",
        "AUDIT_STORE_PROVIDER": "chained_file",
        "AUDIT_CHAIN_SECRET": "placeholder-not-a-real-secret-0123456789",
    }

    @pytest.mark.parametrize(
        "store", ["SESSION_STORE_PROVIDER", "WORKFLOW_STORE_PROVIDER", "RATE_LIMIT_STORE_PROVIDER"]
    )
    def test_each_in_memory_store_is_refused_in_production(self, store):
        with pytest.raises(PydanticValidationError, match="in-memory critical state"):
            Settings(**{**self.PROD_BASE, store: "inmemory"})  # type: ignore[arg-type]

    def test_the_configuration_surface_admits_a_shared_store(self):
        settings = Settings(**self.PROD_BASE)  # type: ignore[arg-type]
        assert settings.configured_state_stores() == {
            "session_store_provider": "redis",
            "workflow_store_provider": "redis",
            "rate_limit_store_provider": "redis",
        }

    def test_the_shared_store_never_silently_falls_back(self):
        """A redis provider without a connection is a configuration error, not an
        in-memory fallback. The adapters themselves are proven in
        tests/integration/test_shared_stores.py."""
        from app.core.security.rate_limit import build_rate_limit_store
        from app.orchestration.session import build_conversation_store
        from app.workflows.state.store import build_workflow_store

        with pytest.raises(ConfigurationError, match="redis_clients_required"):
            build_workflow_store("redis")
        with pytest.raises(ConfigurationError, match="redis_clients_required"):
            build_conversation_store("redis", 60)
        with pytest.raises(ConfigurationError, match="redis_clients_required"):
            build_rate_limit_store("redis")


# =============================================================== M-1 ==============
class TestM1CardRedactionIsNotShadowed:
    @pytest.mark.parametrize(
        "card",
        [
            "4111 1111 1111 1111",
            "4111111111111111",
            "4111-1111-1111-1111",
            "3782 822463 10005",
            "378282246310005",
        ],
    )
    def test_no_card_digits_survive(self, card):
        masked = masking_service.mask_text(f"paid with card {card} yesterday")
        assert card not in masked
        digits = "".join(ch for ch in masked if ch.isdigit())
        assert digits == "", f"card digits leaked: {masked!r}"

    def test_aadhaar_is_still_masked_to_its_suffix(self):
        assert masking_service.mask_text("aadhaar 2345 6789 0123") == "aadhaar ********0123"


# =============================================================== M-2 ==============
class TestM2ConfirmationRequiresServerEvidence:
    def test_confirmed_true_without_a_token_is_refused(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        drive_to_review(client, customer_token, conversation_id)
        response = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            idem="m2-1",
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    def test_a_forged_token_is_refused(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        drive_to_review(client, customer_token, conversation_id)
        response = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            idem="m2-2",
            confirmation_token="v1." + "0" * 64,
        )
        assert response.status_code == 403

    def test_a_token_for_another_action_is_refused(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        review = drive_to_review(client, customer_token, conversation_id)
        confirm_token = confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE)
        at_payment = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            idem="m2-3a",
            confirmation_token=confirm_token,
        )
        assert at_payment.status_code == 200
        # Reusing the CONFIRM_PURCHASE token to confirm COMPLETE_PAYMENT must fail.
        response = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_COMPLETE_PAYMENT,
            {},
            confirmed=True,
            idem="m2-3b",
            confirmation_token=confirm_token,
        )
        assert response.status_code == 403

    def test_the_offered_token_confirms_exactly_what_was_shown(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        review = drive_to_review(client, customer_token, conversation_id)
        assert set(review["meta"]["confirmationTokens"]) == {wf.ACTION_CONFIRM_PURCHASE}
        response = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            idem="m2-4",
            confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
        )
        assert response.status_code == 200
        assert response.json()["meta"]["state"] == wf.STATE_PAYMENT

    def test_tokens_are_not_offered_for_actions_that_need_no_confirmation(self, client, customer_token):
        conversation_id = new_conversation(client, customer_token)
        assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
        body = act(client, customer_token, conversation_id, wf.ACTION_BEGIN, {}).json()
        assert "confirmationTokens" not in body["meta"]


# =============================================================== M-3 ==============
class TestM3IdempotencyKeysAreScoped:
    def test_reusing_a_key_for_a_different_action_is_a_conflict_not_a_silent_noop(
        self, client, customer_token
    ):
        conversation_id = new_conversation(client, customer_token)
        review = drive_to_review(client, customer_token, conversation_id)
        at_payment = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            idem="shared-key",
            confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
        )
        assert at_payment.status_code == 200
        response = act(
            client,
            customer_token,
            conversation_id,
            wf.ACTION_COMPLETE_PAYMENT,
            {},
            confirmed=True,
            idem="shared-key",
            confirmation_token=confirmation_token_for(at_payment.json(), wf.ACTION_COMPLETE_PAYMENT),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async def test_same_key_with_a_different_payload_is_a_conflict(self, container):
        base = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_m3")
        state = (await container.workflow_engine.start(base, wf.WORKFLOW_ID, conversation_id="conv_m3")).state
        state = (await container.workflow_service.execute(base, state, wf.ACTION_BEGIN, {})).outcome.state

        keyed = base.child(idempotency_key="k-1")
        state = (
            await container.workflow_service.execute(
                keyed, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
            )
        ).outcome.state
        # Same key, same action, *different* payload: never a silent replay.
        with pytest.raises(IdempotencyConflictError):
            await container.workflow_service.execute(
                keyed, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-TWO"}
            )
        # Same key reused for a different action: also a conflict.
        with pytest.raises(IdempotencyConflictError):
            await container.workflow_service.execute(
                keyed, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)
            )


# =============================================================== M-7 ==============
class TestM7EstimatedTokensAreFlagged:
    async def test_structured_calls_are_marked_estimated(self, container):
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_INTENT, user_message="hmm", payload={"message": "hmm"}
            ),
        )
        assert result.record is not None
        assert result.record.tokens_estimated is True

    async def test_provider_reported_usage_is_not_marked_estimated(self, container):
        result = await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
        assert result.record is not None
        assert result.record.tokens_estimated is False


# =============================================================== L-3 ==============
def test_l3_numeric_business_values_are_redacted_under_arbitrary_keys():
    redacted = masking_service.redact(
        {"vehicle": {"idv": 987654}, "quote": {"total_premium": 34399.18}, "n": 3}
    )
    assert redacted["vehicle"]["idv"] == "[REDACTED]"
    assert redacted["quote"]["total_premium"] == "[REDACTED]"
    assert redacted["n"] == 3
    assert masking_service.redact({"inputTokens": 1200})["inputTokens"] == 1200


# =============================================================== L-4 ==============
def test_l4_only_the_selected_provider_implementation_is_constructed(monkeypatch):
    from app.integrations import factory
    from tests.conftest import make_settings

    original_mock_bundle = factory.build_mock_bundle

    def explode(*_a, **_k):
        raise AssertionError("mock bundle must not be built when no provider is mock")

    monkeypatch.setattr(factory, "build_mock_bundle", explode)
    settings = make_settings(
        **dict.fromkeys(
            (
                "CUSTOMER_PROVIDER",
                "POLICY_PROVIDER",
                "QUOTE_PROVIDER",
                "PRODUCT_PROVIDER",
                "CLAIMS_PROVIDER",
                "PAYMENT_PROVIDER",
                "DOCUMENT_PROVIDER",
                "REFERENCE_DATA_PROVIDER",
            ),
            "insuremo",
        )
    )
    bundle = factory.build_provider_bundle(settings)
    assert bundle.is_mock is False

    def explode_client(*_a, **_k):
        raise AssertionError("InsureMO client must not be built when every provider is mock")

    monkeypatch.setattr(factory, "build_mock_bundle", original_mock_bundle)
    monkeypatch.setattr(factory, "InsureMoClient", explode_client)
    bundle = factory.build_provider_bundle(make_settings())
    assert bundle.is_mock is True
