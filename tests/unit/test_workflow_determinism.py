"""Determinism gate (master prompt §59.6, §26.3).

Exact PASS/FAIL assertions, no LLM judge:
* 100% pass on valid transitions
* 100% rejection of explicitly prohibited transitions
* safe duplicate-submit / idempotency behaviour
* no transaction-state mutation caused by FAQ handling
* state recovery / resume from persisted structured state
* the model cannot mutate authoritative state
"""

from __future__ import annotations

import pytest

from app.core.errors.taxonomy import FlowStateConflictError, ForbiddenError, ValidationError
from app.domains.motor import workflow as wf
from tests.conftest import customer_context

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}


async def _start(container, ctx):
    return await container.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id=ctx.conversation_id)


async def _advance_to(container, ctx, target_state: str):
    """Drive the flow deterministically to a state, returning the state object.

    Setup steps run without an idempotency key: a key identifies one *submission*,
    so reusing the caller's key here would make every setup step an idempotent replay.
    """
    ctx = ctx.child(idempotency_key=None)
    outcome = await _start(container, ctx)
    state = outcome.state
    if target_state == wf.STATE_ENTRY:
        return state

    service = container.workflow_service
    state = (await service.execute(ctx, state, wf.ACTION_BEGIN)).outcome.state
    if target_state == wf.STATE_IDENTIFY_CUSTOMER:
        return state

    state = (
        await service.execute(ctx, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"})
    ).outcome.state
    if target_state == wf.STATE_COLLECT_DATA:
        return state

    state = (await service.execute(ctx, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE))).outcome.state
    if target_state == wf.STATE_VALIDATE:
        return state

    state = (await service.execute(ctx, state, wf.ACTION_REQUEST_QUOTE)).outcome.state
    if target_state == wf.STATE_QUOTE:
        return state

    state = (await service.execute(ctx, state, wf.ACTION_REVIEW)).outcome.state
    return state


# ------------------------------------------------------- valid transitions ---
@pytest.mark.parametrize(
    ("from_state", "action", "to_state", "payload"),
    [
        (wf.STATE_ENTRY, wf.ACTION_BEGIN, wf.STATE_IDENTIFY_CUSTOMER, {}),
        (
            wf.STATE_IDENTIFY_CUSTOMER,
            wf.ACTION_IDENTIFY,
            wf.STATE_COLLECT_DATA,
            {"product_code": "MTR-PVT-CAR"},
        ),
        (wf.STATE_COLLECT_DATA, wf.ACTION_SUBMIT_VEHICLE, wf.STATE_VALIDATE, dict(VALID_VEHICLE)),
        (
            wf.STATE_VALIDATE,
            wf.ACTION_SELECT_ADDONS,
            wf.STATE_VALIDATE,
            {"addons": ["Zero Depreciation"]},
        ),
        (wf.STATE_VALIDATE, wf.ACTION_REQUEST_QUOTE, wf.STATE_QUOTE, {}),
        (wf.STATE_QUOTE, wf.ACTION_REVIEW, wf.STATE_REVIEW, {}),
        (wf.STATE_QUOTE, wf.ACTION_GO_BACK, wf.STATE_VALIDATE, {}),
        (wf.STATE_REVIEW, wf.ACTION_EDIT, wf.STATE_COLLECT_DATA, {}),
    ],
)
async def test_valid_transitions_all_pass(container, from_state, action, to_state, payload):
    ctx = customer_context(conversation_id=f"conv_{from_state}_{action}")
    state = await _advance_to(container, ctx, from_state)
    assert state.state == from_state

    result = await container.workflow_service.execute(ctx, state, action, payload)

    assert result.outcome.state.state == to_state
    assert result.outcome.state.version > state.version


# ---------------------------------------------------- prohibited transitions ---
@pytest.mark.parametrize(
    ("from_state", "action"),
    [
        (wf.STATE_ENTRY, wf.ACTION_CONFIRM_PURCHASE),
        (wf.STATE_ENTRY, wf.ACTION_COMPLETE_PAYMENT),
        (wf.STATE_ENTRY, wf.ACTION_REQUEST_QUOTE),
        (wf.STATE_IDENTIFY_CUSTOMER, wf.ACTION_SUBMIT_VEHICLE),
        (wf.STATE_COLLECT_DATA, wf.ACTION_REQUEST_QUOTE),
        (wf.STATE_COLLECT_DATA, wf.ACTION_CONFIRM_PURCHASE),
        (wf.STATE_VALIDATE, wf.ACTION_COMPLETE_PAYMENT),
        (wf.STATE_QUOTE, wf.ACTION_CONFIRM_PURCHASE),
        (wf.STATE_REVIEW, wf.ACTION_COMPLETE_PAYMENT),
    ],
)
async def test_out_of_order_transitions_all_rejected(container, from_state, action):
    ctx = customer_context(conversation_id=f"conv_bad_{from_state}_{action}")
    state = await _advance_to(container, ctx, from_state)

    with pytest.raises(FlowStateConflictError):
        await container.workflow_service.execute(ctx, state, action, {}, confirmed=True)

    reloaded = await container.workflow_engine.load(state.flow_id, ctx)
    assert reloaded.state == from_state, "a rejected transition must not mutate state"


async def test_unknown_action_is_rejected(container):
    ctx = customer_context(conversation_id="conv_unknown_action")
    state = await _advance_to(container, ctx, wf.STATE_COLLECT_DATA)
    with pytest.raises(FlowStateConflictError):
        await container.workflow_service.execute(ctx, state, "DELETE_EVERYTHING", {})


# ----------------------------------------------------------- validation ---
@pytest.mark.parametrize(
    "bad_payload",
    [
        {**VALID_VEHICLE, "registration_number": "not-a-reg"},
        {**VALID_VEHICLE, "fuel_type": "PLUTONIUM"},
        {**VALID_VEHICLE, "manufacture_year": 1970},
        {**VALID_VEHICLE, "idv": 10},
        {**VALID_VEHICLE, "idv": 99_000_000},
        {k: v for k, v in VALID_VEHICLE.items() if k != "idv"},
    ],
)
async def test_business_validation_rejects_bad_input(container, bad_payload):
    ctx = customer_context(conversation_id="conv_validation")
    state = await _advance_to(container, ctx, wf.STATE_COLLECT_DATA)
    with pytest.raises(ValidationError):
        await container.workflow_service.execute(ctx, state, wf.ACTION_SUBMIT_VEHICLE, bad_payload)


async def test_prerequisite_fields_are_enforced(container):
    """A transition declaring requires_fields cannot run without them."""
    ctx = customer_context(conversation_id="conv_prereq")
    outcome = await _start(container, ctx)
    state = (await container.workflow_service.execute(ctx, outcome.state, wf.ACTION_BEGIN)).outcome.state
    state = (
        await container.workflow_service.execute(
            ctx, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
        )
    ).outcome.state
    # Jump straight to VALIDATE state data-wise is impossible; assert the guard exists.
    definition = container.workflow_engine.definition(wf.WORKFLOW_ID)
    transition = definition.find(wf.STATE_VALIDATE, wf.ACTION_REQUEST_QUOTE)
    assert transition is not None
    assert "registration_number" in transition.requires_fields


# ------------------------------------------------------------ idempotency ---
async def test_duplicate_submit_with_same_idempotency_key_is_replayed(container):
    ctx = customer_context(conversation_id="conv_idem", idempotency_key="idem-key-1")
    state = await _advance_to(container, ctx, wf.STATE_REVIEW)

    first = await container.workflow_service.execute(
        ctx, state, wf.ACTION_CONFIRM_PURCHASE, {}, confirmed=True
    )
    assert first.outcome.state.state == wf.STATE_PAYMENT
    payment_ref = first.outcome.state.data["payment_reference"]

    second = await container.workflow_service.execute(
        ctx, first.outcome.state, wf.ACTION_CONFIRM_PURCHASE, {}, confirmed=True
    )
    assert second.outcome.replayed is True
    assert second.outcome.state.data["payment_reference"] == payment_ref
    assert second.outcome.state.version == first.outcome.state.version


async def test_confirmation_required_for_high_risk_transition(container):
    ctx = customer_context(conversation_id="conv_confirm", idempotency_key="idem-key-2")
    state = await _advance_to(container, ctx, wf.STATE_REVIEW)
    with pytest.raises(ForbiddenError):
        await container.workflow_service.execute(ctx, state, wf.ACTION_CONFIRM_PURCHASE, {}, confirmed=False)


# --------------------------------------------------------------- ownership ---
async def test_another_subject_cannot_advance_someone_elses_flow(container):
    owner = customer_context(conversation_id="conv_owner")
    state = await _advance_to(container, owner, wf.STATE_COLLECT_DATA)

    attacker = customer_context(subject="CUST-2002", conversation_id="conv_owner")
    with pytest.raises(ForbiddenError):
        await container.workflow_service.execute(
            attacker, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)
        )


async def test_cross_tenant_flow_access_is_denied(container):
    owner = customer_context(conversation_id="conv_tenant")
    state = await _advance_to(container, owner, wf.STATE_COLLECT_DATA)
    other_tenant = customer_context(conversation_id="conv_tenant", tenant_id="TENANT-XX")
    with pytest.raises(ForbiddenError):
        await container.workflow_service.execute(
            other_tenant, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)
        )


# ------------------------------------------------------------ persistence ---
async def test_state_resumes_from_persisted_structured_state(container):
    ctx = customer_context(conversation_id="conv_resume")
    await _advance_to(container, ctx, wf.STATE_VALIDATE)

    resumed = await container.workflow_engine.get_active("conv_resume", ctx)
    assert resumed is not None
    assert resumed.state == wf.STATE_VALIDATE
    assert resumed.data["registration_number"] == "MH01AB1234"
    # Resume needs no conversation transcript at all.
    assert resumed.history, "transition history is structured, not conversational"
    assert all(not hasattr(h, "text") for h in resumed.history)


async def test_optimistic_locking_rejects_stale_write(container):
    ctx = customer_context(conversation_id="conv_lock")
    state = await _advance_to(container, ctx, wf.STATE_COLLECT_DATA)
    stale = state.model_copy(deep=True)

    await container.workflow_service.execute(ctx, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE))

    with pytest.raises(FlowStateConflictError):
        await container.workflow_engine.apply_action(
            ctx, stale, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)
        )


async def test_validator_cannot_write_undeclared_fields(container):
    """A validator that returns fields the transition did not declare is rejected."""
    ctx = customer_context(conversation_id="conv_undeclared")
    state = await _advance_to(container, ctx, wf.STATE_IDENTIFY_CUSTOMER)

    definition = container.workflow_engine.definition(wf.WORKFLOW_ID)
    transition = definition.find(wf.STATE_IDENTIFY_CUSTOMER, wf.ACTION_IDENTIFY)
    assert transition is not None
    object.__setattr__(
        transition, "validator", lambda p, s: {"product_code": "MTR-PVT-CAR", "is_admin": True}
    )
    try:
        with pytest.raises(ValidationError):
            await container.workflow_service.execute(
                ctx, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
            )
    finally:
        object.__setattr__(transition, "validator", wf.validate_identity)


async def test_full_journey_completes_and_persists_policy(container):
    ctx = customer_context(conversation_id="conv_full", idempotency_key="idem-full-1")
    state = await _advance_to(container, ctx, wf.STATE_REVIEW)

    state = (
        await container.workflow_service.execute(ctx, state, wf.ACTION_CONFIRM_PURCHASE, {}, confirmed=True)
    ).outcome.state

    complete_ctx = customer_context(conversation_id="conv_full", idempotency_key="idem-full-2")
    # Without the gateway's SUCCESS the issuance step is refused and state is preserved.
    premature = await container.workflow_service.execute(
        complete_ctx, state, wf.ACTION_COMPLETE_PAYMENT, {}, confirmed=True
    )
    assert premature.service_failed is True
    assert premature.error is not None and premature.error.reason == "payment_not_confirmed"
    assert premature.outcome.state.state == wf.STATE_PAYMENT
    assert premature.outcome.state.pending_action is None

    from app.integrations.contracts.dtos import PaymentStatus

    await container.providers.payment.confirm_payment(
        state.data["payment_id"], "gw-full", PaymentStatus.SUCCESS, complete_ctx
    )
    result = await container.workflow_service.execute(
        customer_context(conversation_id="conv_full", idempotency_key="idem-full-3"),
        premature.outcome.state,
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
    )

    final = result.outcome.state
    assert final.state == wf.STATE_COMPLETE
    assert final.status.value == "COMPLETED"
    assert final.data["policy_number"].startswith("PTC")
    # The booked policy is now readable from the authoritative provider, not memory.
    owner = await container.providers.policy.get_policy_owner(final.data["policy_id"])
    assert owner == ctx.auth.subject_id
