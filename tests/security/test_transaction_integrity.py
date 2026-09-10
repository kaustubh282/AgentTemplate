"""Transaction integrity under concurrency and client-asserted payment (master prompt §8,
§25 business guardrails, §2.1).

These tests exercise the *failing direction* of the two highest-impact remediation
items and do so through several surfaces: the orchestrator with real provider latency
(a true race), the HTTP boundary, and the multi-instance Redis-backed store.
"""

from __future__ import annotations

import asyncio

import pytest

from app.bootstrap import build_container
from app.core.audit.events import AuditAction
from app.core.errors.taxonomy import FlowStateConflictError
from app.domains.motor import workflow as wf
from app.integrations.contracts.dtos import PaymentStatus
from app.integrations.mock import fixtures
from tests.conftest import (
    auth_headers,
    customer_context,
    default_responder,
    make_settings,
    make_token,
)
from tests.e2e.test_demo_scenarios import (
    VALID_VEHICLE,
    act,
    confirmation_token_for,
    drive_to_review,
    new_conversation,
)

pytestmark = pytest.mark.security


async def _to_review(container, conversation_id: str):
    ctx = customer_context(fixtures.CUSTOMER_A, conversation_id=conversation_id)
    orch = container.orchestrator
    await orch.start_conversation(ctx)
    await orch.start_flow(ctx, conversation_id, "motor.workflow.start")
    for action, payload in (
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ):
        await orch.handle_action(ctx, "motor.workflow.action", action, payload)
    await orch.handle_action(
        ctx.child(idempotency_key=f"q-{conversation_id}"),
        "motor.workflow.action",
        wf.ACTION_REQUEST_QUOTE,
        {},
    )
    review = await orch.handle_action(ctx, "motor.workflow.action", wf.ACTION_REVIEW, {})
    return ctx, review.meta["confirmationTokens"][wf.ACTION_CONFIRM_PURCHASE]


async def _race_confirm(container, keys: list[str], latency_ms: int = 40):
    conversation_id = f"conv_race_{'_'.join(keys)}"
    ctx, token = await _to_review(container, conversation_id)
    payments = container.providers.payment
    before_calls = payments.calls.count("initiate_payment")
    before_records = len(payments._payments)
    container.faults.latency_ms = latency_ms
    try:
        results = await asyncio.gather(
            *(
                container.orchestrator.handle_action(
                    ctx.child(idempotency_key=key, request_id=f"req-{key}"),
                    "motor.workflow.action",
                    wf.ACTION_CONFIRM_PURCHASE,
                    {},
                    confirmed=True,
                    confirmation_token=token,
                )
                for key in keys
            ),
            return_exceptions=True,
        )
    finally:
        container.faults.latency_ms = 0
    final = await container.workflow_store.get_active_for_conversation(conversation_id)
    return (
        results,
        payments.calls.count("initiate_payment") - before_calls,
        len(payments._payments) - before_records,
        final,
    )


@pytest.mark.parametrize("keys", [["k-a", "k-b"], ["k-1", "k-2", "k-3"]])
async def test_concurrent_confirms_with_distinct_keys_initiate_exactly_one_payment(container, keys):
    """N simultaneous CONFIRM_PURCHASE submits with N idempotency keys -> 1 provider call."""
    results, provider_calls, new_payments, final = await _race_confirm(container, keys)

    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, FlowStateConflictError)]
    assert len(successes) == 1, results
    assert len(conflicts) == len(keys) - 1
    assert all(c.reason in ("transition_in_progress", "optimistic_lock_conflict") for c in conflicts)
    assert provider_calls == 1
    assert new_payments == 1
    assert final is not None and final.state == wf.STATE_PAYMENT
    assert final.pending_action is None


async def test_concurrent_confirms_with_same_key_are_one_payment_and_one_commit(container):
    results, provider_calls, new_payments, final = await _race_confirm(container, ["same", "same"])
    assert provider_calls == 1 and new_payments == 1
    assert sum(1 for r in results if not isinstance(r, Exception)) >= 1
    assert final is not None and final.state == wf.STATE_PAYMENT


async def test_failed_side_effect_releases_reservation_and_keeps_data(container):
    conversation_id = "conv_release"
    ctx, token = await _to_review(container, conversation_id)
    container.faults.unavailable_operations.add("initiate_payment")
    try:
        response = await container.orchestrator.handle_action(
            ctx.child(idempotency_key="k-fail"),
            "motor.workflow.action",
            wf.ACTION_CONFIRM_PURCHASE,
            {},
            confirmed=True,
            confirmation_token=token,
        )
    finally:
        container.faults.unavailable_operations.clear()
    assert response.directive is not None and response.directive.type.value == "SHOW_RETRY"
    state = await container.workflow_store.get_active_for_conversation(conversation_id)
    assert state is not None
    assert state.state == wf.STATE_REVIEW
    assert state.pending_action is None, "a failed side effect must release the reservation"
    assert state.data["quote_id"], "business data untouched"
    # Fresh tokens are issued for the new version so the user can retry immediately.
    fresh = response.meta["confirmationTokens"][wf.ACTION_CONFIRM_PURCHASE]
    retry = await container.orchestrator.handle_action(
        ctx.child(idempotency_key="k-retry"),
        "motor.workflow.action",
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        confirmation_token=fresh,
    )
    assert retry.meta["state"] == wf.STATE_PAYMENT


async def test_live_reservation_blocks_other_transitions_until_released(container):
    conversation_id = "conv_reserved"
    ctx, _ = await _to_review(container, conversation_id)
    state = await container.workflow_engine.get_active(conversation_id, ctx)
    transition = container.workflow_engine.definition(wf.WORKFLOW_ID).find(
        wf.STATE_REVIEW, wf.ACTION_CONFIRM_PURCHASE
    )
    reserved = await container.workflow_engine.reserve(ctx.child(request_id="holder"), state, transition)
    assert reserved.pending_action is not None and reserved.version == state.version + 1

    other = ctx.child(request_id="intruder")
    with pytest.raises(FlowStateConflictError, match="transition_in_progress"):
        await container.workflow_engine.precheck_action(other, reserved, wf.ACTION_EDIT, {})

    released = await container.workflow_engine.release(ctx.child(request_id="holder"), reserved)
    assert released.pending_action is None
    assert await container.workflow_engine.precheck_action(other, released, wf.ACTION_EDIT, {}) is not None


async def test_abandoned_reservation_expires(container):
    from datetime import UTC, datetime, timedelta

    from app.workflows.state.models import PendingReservation

    conversation_id = "conv_abandoned"
    ctx, _ = await _to_review(container, conversation_id)
    state = await container.workflow_engine.get_active(conversation_id, ctx)
    stale = state.model_copy(deep=True)
    stale.pending_action = PendingReservation(
        action=wf.ACTION_CONFIRM_PURCHASE,
        request_id="dead-instance",
        reserved_at=datetime.now(UTC) - timedelta(seconds=3_600),
    )
    await container.workflow_store.update(stale, expected_version=state.version)
    current = await container.workflow_engine.get_active(conversation_id, ctx)
    # A reservation older than the TTL is treated as abandoned and does not block.
    assert await container.workflow_engine.precheck_action(ctx, current, wf.ACTION_EDIT, {}) is not None


# ------------------------------------------------------------- HTTP boundary ---
def test_policy_is_not_issued_until_gateway_confirms_payment(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    at_payment = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="ti-1",
        confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
    )
    assert at_payment.status_code == 200

    premature = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        idem="ti-2",
        confirmation_token=confirmation_token_for(at_payment.json(), wf.ACTION_COMPLETE_PAYMENT),
    )
    assert premature.status_code == 200
    body = premature.json()
    assert body["meta"]["state"] == wf.STATE_PAYMENT
    assert body["directive"]["type"] == "SHOW_RETRY"
    assert body["meta"]["errorCode"] == "FLOW_STATE_CONFLICT"

    container = client.app.state.container
    policies_before = len(container.providers.policy._policies)
    denied = [
        e
        for e in container.audit_sink.events(AuditAction.POLICY_ACTION_SUBMITTED)
        if e.reason_code == "payment_not_confirmed"
    ]
    assert denied, "the refused issuance must be audited"
    assert len(container.providers.policy._policies) == policies_before


def test_payment_confirmation_requires_service_identity_with_permission(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    at_payment = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="ti-3",
        confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
    )
    state = asyncio.run(
        client.app.state.container.workflow_store.get_active_for_conversation(conversation_id)
    )
    payment_id = state.data["payment_id"]
    body = {"gateway_reference": "gw-x", "status": "SUCCESS"}

    # The customer who owns the payment cannot confirm it.
    r = client.post(
        f"/api/v1/payments/{payment_id}/confirmations", json=body, headers=auth_headers(customer_token)
    )
    assert r.status_code == 403
    # A service without the permission cannot either.
    plain_service = make_token(subject="svc-x", actor_type="SERVICE", roles=["SERVICE"])
    r = client.post(
        f"/api/v1/payments/{payment_id}/confirmations", json=body, headers=auth_headers(plain_service)
    )
    assert r.status_code == 403
    # A customer carrying the permission claim is still refused: actor type matters.
    forged = make_token(
        subject=fixtures.CUSTOMER_A,
        actor_type="CUSTOMER",
        roles=["CUSTOMER"],
        permissions=["payment:confirm"],
    )
    r = client.post(f"/api/v1/payments/{payment_id}/confirmations", json=body, headers=auth_headers(forged))
    assert r.status_code == 403
    # Anonymous is refused before any of this.
    r = client.post(f"/api/v1/payments/{payment_id}/confirmations", json=body)
    assert r.status_code == 401

    gateway = make_token(
        subject="svc-gateway", actor_type="SERVICE", roles=["SERVICE"], permissions=["payment:confirm"]
    )
    r = client.post(f"/api/v1/payments/{payment_id}/confirmations", json=body, headers=auth_headers(gateway))
    assert r.status_code == 200 and r.json()["data"]["status"] == "SUCCESS"
    events = client.app.state.container.audit_sink.events(AuditAction.PAYMENT_CONFIRMED)
    assert events and events[-1].actor_type == "SERVICE"

    completed = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        idem="ti-4",
        confirmation_token=confirmation_token_for(at_payment.json(), wf.ACTION_COMPLETE_PAYMENT),
    )
    assert completed.status_code == 200 and completed.json()["meta"]["state"] == wf.STATE_COMPLETE


def test_failed_payment_status_never_issues_a_policy(client, customer_token):
    conversation_id = new_conversation(client, customer_token)
    review = drive_to_review(client, customer_token, conversation_id)
    at_payment = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        idem="ti-5",
        confirmation_token=confirmation_token_for(review, wf.ACTION_CONFIRM_PURCHASE),
    )
    container = client.app.state.container
    state = asyncio.run(container.workflow_store.get_active_for_conversation(conversation_id))
    gateway = make_token(
        subject="svc-gateway", actor_type="SERVICE", roles=["SERVICE"], permissions=["payment:confirm"]
    )
    r = client.post(
        f"/api/v1/payments/{state.data['payment_id']}/confirmations",
        json={"gateway_reference": "gw-fail", "status": "FAILED"},
        headers=auth_headers(gateway),
    )
    assert r.status_code == 200 and r.json()["data"]["status"] == "FAILED"
    completed = act(
        client,
        customer_token,
        conversation_id,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        idem="ti-6",
        confirmation_token=confirmation_token_for(at_payment.json(), wf.ACTION_COMPLETE_PAYMENT),
    )
    assert completed.json()["meta"]["state"] == wf.STATE_PAYMENT
    assert completed.json()["directive"]["type"] == "SHOW_RETRY"


# --------------------------------------------------------- multi-instance ---
async def test_race_across_two_redis_backed_instances_initiates_one_payment():
    """Two application instances sharing Redis and one System of Record."""
    from app.core.storage.redis_client import reset_fake_servers

    reset_fake_servers()
    settings = make_settings(
        SESSION_STORE_PROVIDER="redis",
        WORKFLOW_STORE_PROVIDER="redis",
        RATE_LIMIT_STORE_PROVIDER="redis",
        REDIS_URL="fakeredis://race-integrity",
        CONFIRMATION_TOKEN_SECRET="shared-confirmation-secret-for-tests-0123456789",
    )
    a = build_container(settings, responder=default_responder())
    b = build_container(settings, responder=default_responder(), providers=a.providers)

    conversation_id = "conv_multi_race"
    ctx, token = await _to_review(a, conversation_id)
    payments = a.providers.payment
    before = payments.calls.count("initiate_payment")
    a.faults.latency_ms = 40
    try:
        results = await asyncio.gather(
            a.orchestrator.handle_action(
                ctx.child(idempotency_key="ma", request_id="ra"),
                "motor.workflow.action",
                wf.ACTION_CONFIRM_PURCHASE,
                {},
                confirmed=True,
                confirmation_token=token,
            ),
            b.orchestrator.handle_action(
                ctx.child(idempotency_key="mb", request_id="rb"),
                "motor.workflow.action",
                wf.ACTION_CONFIRM_PURCHASE,
                {},
                confirmed=True,
                confirmation_token=token,
            ),
            return_exceptions=True,
        )
    finally:
        a.faults.latency_ms = 0
    assert payments.calls.count("initiate_payment") - before == 1
    assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    assert sum(1 for r in results if isinstance(r, FlowStateConflictError)) == 1
    final = await b.workflow_store.get_active_for_conversation(conversation_id)
    assert final is not None and final.state == wf.STATE_PAYMENT and final.pending_action is None
    assert (await payments.get_payment(final.data["payment_id"], ctx)).status is PaymentStatus.INITIATED
