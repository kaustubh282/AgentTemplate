"""End-to-end demo scenarios over HTTP (master prompt §50 Demos A-F, §28 E2E list).

Every assertion goes through the real FastAPI app: middleware, JWT dependency,
deterministic router, PEP, workflow engine, Harness, providers and directive
registry. Nothing is stubbed except the model (scripted) and the providers (mocks).
"""

from __future__ import annotations

import pytest

from app.domains.motor import workflow as wf
from app.ui_directives.schemas.directives import DirectiveType
from tests.conftest import auth_headers

pytestmark = pytest.mark.e2e

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}


# ------------------------------------------------------------------ helpers ---
def new_conversation(client, token: str | None = None) -> str:
    headers = auth_headers(token) if token else {}
    response = client.post("/api/v1/conversations", json={}, headers=headers)
    assert response.status_code == 201
    return response.json()["conversation_id"]


def act(
    client,
    token,
    conversation_id,
    action,
    payload=None,
    *,
    confirmed=False,
    idem=None,
    confirmation_token=None,
):
    headers = auth_headers(token, conversation_id)
    if idem:
        headers["Idempotency-Key"] = idem
    body = {
        "conversation_id": conversation_id,
        "capability_id": "motor.workflow.action",
        "action": action,
        "payload": payload or {},
        "confirmed": confirmed,
    }
    if confirmation_token:
        body["confirmation_token"] = confirmation_token
    return client.post("/api/v1/actions", headers=headers, json=body)


def confirmation_token_for(flow_response_body: dict, action: str) -> str:
    """The server-issued token offered with the flow response the user was shown (§8)."""
    return flow_response_body["meta"]["confirmationTokens"][action]


def confirm_payment_via_gateway(client, conversation_id: str, status: str = "SUCCESS") -> dict:
    """Simulate the payment gateway callback through the real, service-authenticated route."""
    import asyncio

    from tests.conftest import make_token

    container = client.app.state.container
    state = asyncio.run(container.workflow_store.get_active_for_conversation(conversation_id))
    assert state is not None and state.data.get("payment_id")
    gateway = make_token(
        subject="svc-payment-gateway",
        actor_type="SERVICE",
        roles=["SERVICE"],
        permissions=["payment:confirm"],
    )
    response = client.post(
        f"/api/v1/payments/{state.data['payment_id']}/confirmations",
        headers=auth_headers(gateway),
        json={"gateway_reference": f"gw-{conversation_id}", "status": status},
    )
    assert response.status_code == 200, response.text
    return response.json()


def start_motor_flow(client, token, conversation_id):
    return client.post(
        "/api/v1/flows/start",
        headers=auth_headers(token, conversation_id),
        json={"conversation_id": conversation_id, "capability_id": "motor.workflow.start"},
    )


def drive_to_review(client, token, conversation_id) -> dict:
    """Deterministically walk ENTRY -> REVIEW, asserting zero model calls throughout."""
    assert start_motor_flow(client, token, conversation_id).status_code == 200
    steps = [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
        (wf.ACTION_SELECT_ADDONS, {"addons": ["Zero Depreciation"]}),
        (wf.ACTION_REQUEST_QUOTE, {}),
        (wf.ACTION_REVIEW, {}),
    ]
    body: dict = {}
    for action, payload in steps:
        response = act(client, token, conversation_id, action, payload)
        assert response.status_code == 200, (action, response.text)
        body = response.json()
        assert body["meta"]["modelCalls"] == 0, f"{action} must not call a model"
    return body


# ============================== Demo A - FAQ ==============================
def test_demo_a_faq_success(client):
    """API -> router -> FAQ agent -> retrieval -> grounding -> response."""
    conversation_id = new_conversation(client)
    response = client.post(
        "/api/v1/chat",
        json={"conversation_id": conversation_id, "message": "What is a deductible?"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["response_type"] == "UI_DIRECTIVE"
    assert body["directive"]["type"] == DirectiveType.SHOW_FAQ_ANSWER.value
    assert "deductible" in body["message"].lower()

    payload = body["directive"]["payload"]
    assert payload["verification"] == "VERIFIED"
    assert payload["citations"], "a grounded answer must carry provenance"
    citation = payload["citations"][0]
    assert citation["document_id"] == "KB-COMMON-GLOSSARY"
    assert citation["version"] == "1.2"

    # Efficiency evidence (§59.2).
    assert body["meta"]["modelCalls"] == 1
    assert body["meta"]["agentHandoffs"] == 0
    assert body["meta"]["routePath"] == "FAQ_RAG"


def test_demo_a_faq_answers_from_the_domain_corpus(client):
    conversation_id = new_conversation(client)
    response = client.post(
        "/api/v1/chat",
        json={"conversation_id": conversation_id, "message": "What is zero depreciation cover?"},
    )
    body = response.json()
    assert body["directive"]["payload"]["verification"] in ("VERIFIED", "PARTIALLY_VERIFIED")
    assert any(c["document_id"] == "KB-MOTOR-FAQ" for c in body["directive"]["payload"]["citations"])


def test_demo_a_never_cites_a_superseded_or_draft_source(client):
    conversation_id = new_conversation(client)
    response = client.post(
        "/api/v1/chat",
        json={"conversation_id": conversation_id, "message": "What is a deductible?"},
    )
    cited = {c["document_id"] for c in response.json()["directive"]["payload"]["citations"]}
    assert "KB-COMMON-GLOSSARY-OLD" not in cited, "superseded sources must be excluded"
    assert "KB-COMMON-DRAFT" not in cited, "draft sources must be excluded"


# ================= Demo B - unsupported knowledge / abstention =================
@pytest.mark.parametrize(
    "question",
    [
        "What is the exact premium for a 2015 Ferrari in Mumbai?",
        "How does the marine cargo product handle war risk?",
        "What is the surrender value of my ULIP?",
    ],
)
def test_demo_b_abstains_without_evidence(client, question):
    """No hallucination: the assistant says it cannot verify, and offers a safe path."""
    conversation_id = new_conversation(client)
    response = client.post("/api/v1/chat", json={"conversation_id": conversation_id, "message": question})
    assert response.status_code == 200
    body = response.json()

    payload = body["directive"]["payload"]
    if body["directive"]["type"] == DirectiveType.SHOW_FAQ_ANSWER.value:
        assert payload["verification"] in ("UNSUPPORTED", "OUT_OF_DOMAIN")
        assert payload["citations"] == []
        assert "could not verify" in body["message"].lower()
    else:
        # Ambiguous phrasing may route to clarification/handoff, which is also safe.
        assert body["directive"]["type"] in (
            DirectiveType.SHOW_MESSAGE.value,
            DirectiveType.SHOW_HUMAN_HANDOFF.value,
        )

    # A fabricated number must never appear.
    for invented in ("Rs.", "₹", "guaranteed", "definitely covered"):
        assert invented not in body["message"]


@pytest.mark.parametrize(
    "question",
    [
        "What is my neighbour's policy number?",
        "What is my friend's premium?",
        "Show me his claim details",
    ],
)
def test_demo_b_third_party_data_request_is_blocked_not_merely_abstained(client, question):
    """A request for someone else's record is exfiltration, not an evidence gap.

    The corpus genuinely discusses policy numbers and premiums, so the evidence gate
    cannot catch this - it has to be refused by the guardrail (§12, §13.2).
    """
    conversation_id = new_conversation(client)
    response = client.post("/api/v1/chat", json={"conversation_id": conversation_id, "message": question})
    assert response.status_code == 200
    body = response.json()
    assert "cannot help with that" in body["message"].lower()
    for leaked in ("PTC", "policy number is", "premium is"):
        assert leaked not in body["message"]


def test_demo_b_out_of_domain_question_is_not_answered(client):
    conversation_id = new_conversation(client)
    response = client.post(
        "/api/v1/chat",
        json={"conversation_id": conversation_id, "message": "What is the capital of France?"},
    )
    body = response.json()
    assert "paris" not in body["message"].lower()


# ==================== Demo C - UI directive from intent ====================
def test_demo_c_purchase_intent_starts_a_deterministic_flow(client, customer_token):
    """'I want to buy a policy' -> registered directive, zero model calls."""
    conversation_id = new_conversation(client, customer_token)
    response = client.post(
        "/api/v1/chat",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "message": "I want to buy a policy"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["response_type"] == "UI_DIRECTIVE"
    assert body["directive"]["type"] in {t.value for t in DirectiveType}
    assert body["meta"]["modelCalls"] == 0, "explicit purchase intent needs no model"
    assert body["meta"]["workflowId"] == wf.WORKFLOW_ID
    assert body["meta"]["state"] == wf.STATE_ENTRY
    assert wf.ACTION_BEGIN in body["allowed_actions"]


def test_demo_c_travel_intent_routes_to_the_travel_domain(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    response = client.post(
        "/api/v1/chat",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "message": "I want to buy travel insurance"},
    )
    body = response.json()
    assert body["meta"]["workflowId"] == "travel_sales"
    assert body["meta"]["modelCalls"] == 0


def test_demo_c_directive_contains_no_executable_content(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    body = drive_to_review(client, customer_token, conversation_id)
    serialized = str(body["directive"])
    for unsafe in ("<script", "javascript:", "onerror=", "<iframe", "eval("):
        assert unsafe not in serialized


def test_demo_c_every_directive_carries_accessibility_metadata(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ]:
        body = act(client, customer_token, conversation_id, action, payload).json()
        hints = body["directive"]["payload"]["accessibility"]
        assert hints["aria_label"].strip()
        assert hints["role"]
        assert hints["live_region"] in ("off", "polite", "assertive")


# ============ Demo D - FAQ interruption does not mutate transaction ============
def test_demo_d_faq_interruption_preserves_transaction_state(client, customer_token):
    """The central requirement of §58.11: interrupt -> FAQ -> resume, no state change."""
    conversation_id = new_conversation(client, customer_token)
    before = drive_to_review(client, customer_token, conversation_id)
    state_before = before["meta"]["state"]
    version_before = before["meta"].get("flowVersion")

    # Interrupt with an FAQ while the transaction is active.
    faq = client.post(
        "/api/v1/chat",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "message": "What is a no claim bonus?"},
    )
    assert faq.status_code == 200
    faq_body = faq.json()
    assert faq_body["directive"]["type"] == DirectiveType.SHOW_FAQ_ANSWER.value
    assert faq_body["meta"]["modelCalls"] == 1

    # The Harness reports the flow untouched...
    assert faq_body["meta"]["flowStateUnchanged"] == state_before
    # ...and "continue" is offered so the user can resume.
    assert "CONTINUE" in faq_body["allowed_actions"]

    # Resume: state and version are identical, and no model call was needed.
    resumed = client.post(
        "/api/v1/chat",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "message": "continue"},
    )
    resumed_body = resumed.json()
    assert resumed_body["meta"]["state"] == state_before
    assert resumed_body["meta"]["modelCalls"] == 0
    if version_before is not None:
        assert resumed_body["meta"].get("flowVersion", version_before) == version_before

    # And the journey still completes normally afterwards, using the confirmation
    # token that was issued with the review the user actually saw.
    confirmed = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="idem-demo-d",
        confirmation_token=confirmation_token_for(resumed_body, wf.ACTION_CONFIRM_PURCHASE),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["meta"]["state"] == wf.STATE_PAYMENT


def test_demo_d_faq_during_a_flow_does_not_advance_the_state_machine(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    started = act(client, customer_token, conversation_id, wf.ACTION_BEGIN, {}).json()
    assert started["meta"]["state"] == wf.STATE_IDENTIFY_CUSTOMER

    client.post(
        "/api/v1/chat",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "message": "What is a deductible?"},
    )

    # The next legal action is still the one it was before the interruption.
    advanced = act(
        client, customer_token, conversation_id, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
    )
    assert advanced.status_code == 200
    assert advanced.json()["meta"]["state"] == wf.STATE_COLLECT_DATA


# ==================== Demo E - mock provider through a flow ====================
def test_demo_e_quote_comes_from_the_provider_not_the_model(client, customer_token, container=None):
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ]:
        act(client, customer_token, conversation_id, action, payload)

    quoted = act(client, customer_token, conversation_id, wf.ACTION_REQUEST_QUOTE, {})
    assert quoted.status_code == 200
    body = quoted.json()

    assert body["directive"]["type"] == DirectiveType.SHOW_QUOTE_SUMMARY.value
    payload = body["directive"]["payload"]
    # The directive names the authoritative source that computed the premium.
    assert payload["source_system"] == "MOCK"
    assert payload["total"]["amount"] > 0
    assert payload["lines"], "the quote must be itemised by the provider"
    assert body["meta"]["modelCalls"] == 0, "a quote must never involve a model"


def test_demo_e_flow_emits_audit_and_metrics(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    drive_to_review(client, customer_token, conversation_id)

    container = client.app.state.container
    from app.core.audit.events import AuditAction

    assert container.audit_sink.events(AuditAction.FLOW_STARTED)
    assert container.audit_sink.events(AuditAction.FLOW_STATE_CHANGED)
    assert container.audit_sink.events(AuditAction.QUOTE_RECEIVED)

    from app.core.observability.metrics import FLOW_STEP_COMPLETIONS_TOTAL, metrics

    snapshot = metrics.snapshot()
    names = {c["name"] for c in snapshot["counters"]}
    assert FLOW_STEP_COMPLETIONS_TOTAL in names


def test_demo_e_completed_purchase_is_readable_from_the_system_of_record(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    at_payment = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="idem-demo-e-1",
        confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
    )
    assert at_payment.status_code == 200, at_payment.text

    # Payment success is asserted by the gateway callback (a SERVICE identity), never by
    # the customer's next click: without it, issuance is refused and the state is kept.
    premature = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        idem="idem-demo-e-2",
        confirmation_token=confirmation_token_for(at_payment.json(), wf.ACTION_COMPLETE_PAYMENT),
    )
    assert premature.status_code == 200
    assert premature.json()["meta"]["state"] == wf.STATE_PAYMENT
    assert premature.json()["directive"]["type"] == DirectiveType.SHOW_RETRY.value

    confirmed_body = confirm_payment_via_gateway(client, conversation_id)
    assert confirmed_body["data"]["status"] == "SUCCESS"

    # The failed attempt bumped the state version; use the token from the latest response.
    completed = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        idem="idem-demo-e-3",
        confirmation_token=confirmation_token_for(premature.json(), wf.ACTION_COMPLETE_PAYMENT),
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["meta"]["state"] == wf.STATE_COMPLETE
    assert body["directive"]["type"] == DirectiveType.SHOW_COMPLETION.value
    # The completion reference is masked in the directive.
    assert body["directive"]["payload"]["reference_masked"].startswith("*")

    # The booked policy is now authoritative: it appears in the customer's list.
    listed = client.get("/api/v1/policies", headers=auth_headers(customer_token))
    assert listed.status_code == 200
    assert len(listed.json()["data"]["policies"]) >= 3


# ==================== Demo F - provider failure handling ====================
def test_demo_f_provider_timeout_produces_no_fake_quote(client, customer_token):
    """State preserved, controlled retry directive, no fabricated premium."""
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ]:
        act(client, customer_token, conversation_id, action, payload)

    container = client.app.state.container
    container.faults.timeout_operations.add("create_quote")
    try:
        failed = act(client, customer_token, conversation_id, wf.ACTION_REQUEST_QUOTE, {})
    finally:
        container.faults.timeout_operations.discard("create_quote")

    assert failed.status_code == 200
    body = failed.json()

    # A retry directive, not a quote.
    assert body["directive"]["type"] == DirectiveType.SHOW_RETRY.value
    assert body["meta"]["errorCode"] in ("UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE")
    assert body["meta"]["retryable"] is True
    # State is exactly where it was: not advanced to QUOTE.
    assert body["meta"]["state"] == wf.STATE_VALIDATE
    # Nothing numeric was invented.
    assert "total" not in str(body["directive"]["payload"])

    # Retrying after recovery succeeds and produces a real quote.
    retried = act(client, customer_token, conversation_id, wf.ACTION_RETRY_QUOTE, {})
    assert retried.status_code == 200
    assert retried.json()["directive"]["type"] == DirectiveType.SHOW_QUOTE_SUMMARY.value


def test_demo_f_provider_outage_is_audited(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ]:
        act(client, customer_token, conversation_id, action, payload)

    container = client.app.state.container
    container.faults.unavailable_operations.add("create_quote")
    try:
        act(client, customer_token, conversation_id, wf.ACTION_REQUEST_QUOTE, {})
    finally:
        container.faults.unavailable_operations.discard("create_quote")

    from app.core.audit.events import AuditAction, AuditResult

    outcomes = container.audit_sink.events(AuditAction.PROVIDER_CALL_OUTCOME)
    assert any(e.result in (AuditResult.FAILURE, AuditResult.TIMEOUT) for e in outcomes)


# ==================== §28 remaining base E2E scenarios ====================
def test_e2e_unauthorized_action_is_rejected(client, other_customer_token, customer_token):
    """Scenario 8: another customer cannot advance someone else's flow."""
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    act(client, customer_token, conversation_id, wf.ACTION_BEGIN, {})

    hijack = act(
        client,
        other_customer_token,
        conversation_id,
        wf.ACTION_IDENTIFY,
        {"product_code": "MTR-PVT-CAR"},
    )
    assert hijack.status_code == 403


def test_e2e_malformed_action_is_rejected(client, customer_token):
    """Scenario 9: an action that is not legal in the current state is refused."""
    conversation_id = new_conversation(client, customer_token)
    assert start_motor_flow(client, customer_token, conversation_id).status_code == 200
    response = act(client, customer_token, conversation_id, wf.ACTION_COMPLETE_PAYMENT, {}, confirmed=False)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FLOW_STATE_CONFLICT"


def test_e2e_duplicate_submit_is_safe(client, customer_token):
    """Scenario 10: the same idempotency key replays rather than double-charging."""
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    token = confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE)

    first = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="idem-dup-1",
        confirmation_token=token,
    )
    # The retry carries the same key, action and payload: a safe replay, even though
    # the token it was issued with is now bound to a superseded state version.
    second = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="idem-dup-1",
        confirmation_token=token,
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["meta"]["replayed"] is True
    assert first.json()["meta"]["state"] == second.json()["meta"]["state"] == wf.STATE_PAYMENT


def test_e2e_high_risk_action_requires_confirmation(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    drive_to_review(client, customer_token, conversation_id)
    response = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=False,
        idem="idem-noconfirm",
    )
    assert response.status_code == 403


def test_e2e_high_risk_action_requires_an_idempotency_key(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    response = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_e2e_travel_domain_completes_its_own_journey(client, customer_token):
    """§45.2 extension proof, exercised through the same API surface."""
    conversation_id = new_conversation(client, customer_token)
    started = client.post(
        "/api/v1/flows/start",
        headers=auth_headers(customer_token, conversation_id),
        json={"conversation_id": conversation_id, "capability_id": "travel.workflow.start"},
    )
    assert started.status_code == 200

    def travel_act(action, payload=None, *, confirmed=False, idem=None, confirmation_token=None):
        headers = auth_headers(customer_token, conversation_id)
        if idem:
            headers["Idempotency-Key"] = idem
        body = {
            "conversation_id": conversation_id,
            "capability_id": "travel.workflow.action",
            "action": action,
            "payload": payload or {},
            "confirmed": confirmed,
        }
        if confirmation_token:
            body["confirmation_token"] = confirmation_token
        return client.post("/api/v1/actions", headers=headers, json=body)

    assert travel_act("BEGIN").status_code == 200
    quoted = travel_act("SUBMIT_TRIP_DETAILS", {"region": "SCHENGEN", "trip_days": 14, "traveller_age": 34})
    assert quoted.status_code == 200
    assert quoted.json()["directive"]["type"] == DirectiveType.SHOW_QUOTE_SUMMARY.value
    assert quoted.json()["meta"]["modelCalls"] == 0

    accepted = travel_act(
        "ACCEPT_QUOTE",
        confirmed=True,
        idem="idem-travel-1",
        confirmation_token=confirmation_token_for(quoted.json(), "ACCEPT_QUOTE"),
    )
    assert accepted.status_code == 200
    assert accepted.json()["meta"]["state"] == "COMPLETE"


def test_e2e_response_always_matches_the_directive_contract(client, customer_token):
    """Every response validates against the published AssistantResponse schema."""
    from app.ui_directives.schemas.directives import AssistantResponse

    conversation_id = new_conversation(client, customer_token)
    responses = [
        client.post(
            "/api/v1/chat",
            headers=auth_headers(customer_token, conversation_id),
            json={"conversation_id": conversation_id, "message": "What is a deductible?"},
        ),
        start_motor_flow(client, customer_token, conversation_id),
        act(client, customer_token, conversation_id, wf.ACTION_BEGIN, {}),
    ]
    for response in responses:
        assert response.status_code == 200
        model = AssistantResponse.model_validate(response.json())
        assert model.schema_version == "1.0"
        assert model.request_id
        assert model.conversation_id == conversation_id
