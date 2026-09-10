"""Resource scope / ownership tests (master prompt §13.2 - all 10 required cases)
plus the authoritative-data tool tests (§8.1 - all 10 required cases).
"""

from __future__ import annotations

import pytest

from app.core.audit.events import AuditAction
from app.core.errors.taxonomy import ForbiddenError
from app.core.resource_scope.scope import ResourceType
from app.integrations.mock import fixtures
from tests.conftest import agent_context, auth_headers, customer_context

pytestmark = pytest.mark.security


# =========================== §13.2 resource scope ===========================
# 1. Customer A requests own policy -> allowed
async def test_customer_can_read_own_policy(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is True
    assert result.data["policy_number_masked"].endswith("1234")


# 2. Customer A requests Customer B policy -> forbidden
async def test_customer_cannot_read_another_customers_policy(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    with pytest.raises(ForbiddenError):
        await container.tools.get_policy_details(ctx, fixtures.POLICY_B_MOTOR)


# 3. forged customerId in the request payload is ignored / rejected
async def test_forged_customer_id_in_payload_is_ignored_for_customers(container):
    """A customer is pinned to their own subject; a supplied id cannot widen scope."""
    ctx = customer_context(fixtures.CUSTOMER_A)
    resolved = container.scope.effective_customer_id(ctx.auth, fixtures.CUSTOMER_B)
    assert resolved == fixtures.CUSTOMER_A

    with pytest.raises(ForbiddenError):
        await container.tools.get_policy_details(
            ctx, fixtures.POLICY_B_MOTOR, requested_customer_id=fixtures.CUSTOMER_B
        )


def test_forged_customer_id_over_http_is_ignored(client, customer_token):
    response = client.get(
        f"/api/v1/policies/{fixtures.POLICY_B_MOTOR}",
        headers=auth_headers(customer_token),
        params={"customer_id": fixtures.CUSTOMER_B},
    )
    assert response.status_code == 403


# 4. a model-generated alternate customerId is blocked
async def test_model_generated_customer_id_cannot_widen_scope(container):
    """Simulates an agent proposing another customer's id as a tool argument."""
    ctx = customer_context(fixtures.CUSTOMER_A)
    model_supplied_arguments = {"policy_id": fixtures.POLICY_B_MOTOR, "customer_id": fixtures.CUSTOMER_B}
    with pytest.raises(ForbiddenError):
        await container.tools.get_policy_details(
            ctx,
            model_supplied_arguments["policy_id"],
            requested_customer_id=model_supplied_arguments["customer_id"],
        )


# 5. a valid Agent with approved access -> allowed
async def test_assigned_agent_can_read_assigned_customer_policy(container):
    ctx = agent_context(assigned=(fixtures.CUSTOMER_A,))
    result = await container.tools.get_policy_details(
        ctx, fixtures.POLICY_A_MOTOR, requested_customer_id=fixtures.CUSTOMER_A
    )
    assert result.available is True


# 6. an Agent without approved scope -> forbidden
async def test_unassigned_agent_is_forbidden(container):
    ctx = agent_context(assigned=())
    with pytest.raises(ForbiddenError):
        await container.tools.get_policy_details(
            ctx, fixtures.POLICY_A_MOTOR, requested_customer_id=fixtures.CUSTOMER_A
        )


async def test_agent_role_alone_grants_no_customer_access(container):
    """Presence of the AGENT role must not imply access to all customers (§13.2)."""
    ctx = agent_context(assigned=(fixtures.CUSTOMER_B,))
    with pytest.raises(ForbiddenError):
        await container.tools.get_policy_details(
            ctx, fixtures.POLICY_A_MOTOR, requested_customer_id=fixtures.CUSTOMER_A
        )


async def test_agent_must_name_a_customer_scope(container):
    ctx = agent_context()
    with pytest.raises(ForbiddenError):
        container.scope.effective_customer_id(ctx.auth, None)


# 7. a cross-tenant resource request -> forbidden
async def test_cross_tenant_access_is_denied(container):
    """A resource owned by TENANT-IN is refused to a caller asserting another tenant.

    Exercised through the *tool path*, not the guard helper: the interceptor must build
    the reference from the System of Record's tenant, not the caller's (AZ-08, H-1).
    """
    ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-OTHER")
    with pytest.raises(ForbiddenError, match="cross_tenant_access_denied"):
        await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)


async def test_cross_tenant_access_is_denied_through_the_scope_interceptor(container):
    ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-XX")
    with pytest.raises(ForbiddenError, match="cross_tenant_access_denied"):
        await container.scope.authorize_resource(ctx.auth, ResourceType.POLICY, fixtures.POLICY_A_MOTOR)


async def test_same_tenant_access_is_permitted(container):
    ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-IN")
    ref = await container.scope.authorize_resource(ctx.auth, ResourceType.POLICY, fixtures.POLICY_A_MOTOR)
    assert ref.tenant_id == "TENANT-IN", "the reference carries the resource's tenant, not the caller's"
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is True


async def test_tenant_check_fails_closed_when_the_resource_tenant_is_unknown(container):
    """A tenant-scoped caller is refused a resource whose tenant cannot be resolved."""
    from app.core.resource_scope.scope import ResourceOwnership, ResourceScopeInterceptor

    class UnknownTenantResolver:
        async def resolve_owner(self, resource_type, resource_id):
            return ResourceOwnership(customer_id=fixtures.CUSTOMER_A, tenant_id=None)

    interceptor = ResourceScopeInterceptor(UnknownTenantResolver())
    ctx = customer_context(fixtures.CUSTOMER_A, tenant_id="TENANT-IN")
    with pytest.raises(ForbiddenError, match="cross_tenant_access_denied"):
        await interceptor.authorize_resource(ctx.auth, ResourceType.POLICY, fixtures.POLICY_A_MOTOR)


# 8. an ownership failure does not leak protected metadata
async def test_ownership_failure_does_not_disclose_existence(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    with pytest.raises(ForbiddenError) as existing:
        await container.tools.get_policy_details(ctx, fixtures.POLICY_B_MOTOR)
    with pytest.raises(ForbiddenError) as missing:
        await container.tools.get_policy_details(ctx, "POL-DOES-NOT-EXIST")
    # Indistinguishable: a real policy and a missing one produce the same reason.
    assert existing.value.reason == missing.value.reason == "resource_scope_denied"


def test_forbidden_http_response_leaks_nothing(client, customer_token):
    response = client.get(f"/api/v1/policies/{fixtures.POLICY_B_MOTOR}", headers=auth_headers(customer_token))
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert fixtures.CUSTOMER_B not in response.text
    assert "PTC0000009999" not in response.text


# 9. an audit event is created for denied protected access
async def test_pep_denial_emits_audit_event(container):
    from app.core.context.request_context import Channel

    ctx = customer_context(fixtures.CUSTOMER_A).child(channel=Channel.PUBLIC_WEB)
    with pytest.raises(ForbiddenError):
        await container.pep.authorize(ctx, "motor.policy.details", {"policy_id": fixtures.POLICY_A_MOTOR})

    denied = container.audit_sink.events(AuditAction.AUTHORIZATION_DENIED)
    assert denied, "a denied protected access must be audited"
    assert denied[-1].result.value == "DENIED"
    assert denied[-1].actor_ref.startswith("sub_"), "audit uses a pseudonymous actor reference"


# 10. the Authorization header / JWT never enters model context - see test_jwt_boundary.py


# =========================== §8.1 authoritative tools ===========================
# 1. own booked policy premium fetched correctly
async def test_own_policy_premium_is_read_from_the_system_of_record(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_premium(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is True
    assert result.data["annual_premium"] == 18450.0
    assert set(result.data) == {"policy_number_masked", "annual_premium", "currency"}


# 2. own selected add-ons fetched correctly
async def test_own_policy_addons_are_read_from_the_system_of_record(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_addons(ctx, fixtures.POLICY_A_MOTOR)
    assert result.data["addons"] == ["Zero Depreciation", "Roadside Assistance"]


# 3. unauthorized policy id blocked - covered above


# 4. a provider timeout does not produce fabricated data
async def test_provider_timeout_returns_unavailable_not_invented_data(container, faults):
    faults.timeout_operations.add("get_policy")
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_premium(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is False
    assert result.data is None
    assert result.reason_code in ("UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE")


async def test_provider_outage_returns_unavailable(container, faults):
    faults.unavailable_operations.add("get_policy")
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is False
    assert result.data is None


# 5. old conversation history cannot override the current authoritative record
async def test_conversation_history_cannot_override_authoritative_record(container):
    """A stale premium asserted in conversation is not what the tool returns."""
    ctx = customer_context(fixtures.CUSTOMER_A)
    stale_history_claim = 999.0
    result = await container.tools.get_policy_premium(ctx, fixtures.POLICY_A_MOTOR)
    assert result.data["annual_premium"] != stale_history_claim
    assert result.data["annual_premium"] == 18450.0


# 6. the raw provider DTO is not passed to the model
async def test_raw_provider_dto_never_reaches_the_ai_facing_view(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    raw = await container.providers.policy.get_policy(fixtures.POLICY_A_MOTOR, ctx)
    assert raw is not None
    view = (await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)).data

    assert raw.policy_number not in str(view), "the unmasked policy number must not be exposed"
    for internal in ("policy_id", "customer_id", "source_system", "sum_insured", "inception_date"):
        assert internal not in view


# 7. the tool output schema is validated
async def test_tool_output_matches_the_declared_schema(container):
    from app.integrations.contracts.dtos import AiPolicySummary

    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    validated = AiPolicySummary.model_validate(result.data)
    assert validated.status == "ACTIVE"


# 8. PII fields are minimized / masked
async def test_ai_facing_view_masks_identifiers(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    masked = result.data["policy_number_masked"]
    assert masked.startswith("*")
    assert len(masked.replace("*", "")) <= 4


# 9. the audit event includes the capability/result without a sensitive payload
async def test_authoritative_read_is_audited_without_sensitive_payload(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)

    events = container.audit_sink.events(AuditAction.PROVIDER_CALL_OUTCOME)
    assert events
    event = events[-1]
    assert event.attributes["tool"] == "GetPolicyDetails"
    assert event.resource_ref is not None and event.resource_ref.startswith("*")
    serialized = event.model_dump_json()
    assert "18450" not in serialized
    assert fixtures.CUSTOMER_A not in serialized


# 10. a completed-policy lookup works without replaying the chat history
async def test_completed_policy_lookup_needs_no_conversation_history(container):
    """The tool takes only an id and a trusted context - no transcript is involved."""
    ctx = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_never_used")
    result = await container.tools.get_policy_premium(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is True

    conversation = await container.conversations.get("conv_never_used")
    assert conversation is None, "no conversation was needed to answer an authoritative question"


async def test_claim_and_payment_scope_is_enforced(container):
    ctx_a = customer_context(fixtures.CUSTOMER_A)
    allowed = await container.tools.get_claim_status(ctx_a, fixtures.CLAIM_A)
    assert allowed.available is True

    with pytest.raises(ForbiddenError):
        await container.tools.get_claim_status(ctx_a, fixtures.CLAIM_B)

    payment = await container.tools.get_payment_status(ctx_a, fixtures.PAYMENT_A)
    assert payment.available is True


async def test_policy_list_is_scoped_to_the_authenticated_customer(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.list_policy_summaries(ctx, fixtures.CUSTOMER_A)
    products = {p["product"] for p in result.data["policies"]}
    assert products == {"Private Car Package", "Overseas Travel Secure"}

    with pytest.raises(ForbiddenError):
        await container.tools.list_policy_summaries(ctx, fixtures.CUSTOMER_B)
