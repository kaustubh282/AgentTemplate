"""Travel sales journey - the second-domain extension proof (§45.2, §59.8).

This domain exists to prove the extension model, not to add product scope. It is
deliberately small and it adds **no** core infrastructure: it reuses the workflow
engine, directive registry, PEP, resource scope, guardrails, observability, audit and
provider contracts exactly as the motor domain does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.auth.auth_context import Permission
from app.core.errors.taxonomy import ValidationError
from app.core.resilience.policies import SideEffectClass
from app.workflows.engine.definition import Transition, WorkflowDefinition, require_fields

WORKFLOW_ID = "travel_sales"
WORKFLOW_VERSION = "1.0.0"

STATE_ENTRY = "ENTRY"
STATE_COLLECT_TRIP = "COLLECT_TRIP"
STATE_QUOTE = "QUOTE"
STATE_COMPLETE = "COMPLETE"

STATES = (STATE_ENTRY, STATE_COLLECT_TRIP, STATE_QUOTE, STATE_COMPLETE)

ACTION_BEGIN = "BEGIN"
ACTION_SUBMIT_TRIP = "SUBMIT_TRIP_DETAILS"
ACTION_REQUEST_QUOTE = "REQUEST_QUOTE"
ACTION_ACCEPT_QUOTE = "ACCEPT_QUOTE"

AI_CONTEXT_FIELDS = ("region", "trip_days", "quote_reference")

_ALLOWED_REGIONS = frozenset({"ASIA", "SCHENGEN", "WORLDWIDE"})
MAX_TRIP_DAYS = 180


def validate_trip(payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    require_fields(payload, ("region", "trip_days", "traveller_age"))
    region = str(payload["region"]).strip().upper()
    if region not in _ALLOWED_REGIONS:
        raise ValidationError("unsupported_region", details={"allowed": sorted(_ALLOWED_REGIONS)})
    try:
        days = int(payload["trip_days"])
        age = int(payload["traveller_age"])
    except (TypeError, ValueError):
        raise ValidationError("invalid_numeric_field") from None
    if not (1 <= days <= MAX_TRIP_DAYS):
        raise ValidationError("trip_duration_out_of_appetite", details={"maxDays": MAX_TRIP_DAYS})
    if not (1 <= age <= 70):
        raise ValidationError("traveller_age_out_of_appetite", details={"minAge": 1, "maxAge": 70})
    return {"region": region, "trip_days": days, "traveller_age": age}


def no_payload(_payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    return {}


def build_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        domain="travel",
        description="Travel insurance purchase journey (extension proof).",
        initial_state=STATE_ENTRY,
        states=STATES,
        terminal_states=frozenset({STATE_COMPLETE}),
        ai_context_fields=AI_CONTEXT_FIELDS,
        transitions=(
            Transition(
                action=ACTION_BEGIN,
                from_state=STATE_ENTRY,
                to_state=STATE_COLLECT_TRIP,
                description="Start the travel purchase journey.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                validator=no_payload,
            ),
            Transition(
                action=ACTION_SUBMIT_TRIP,
                from_state=STATE_COLLECT_TRIP,
                to_state=STATE_QUOTE,
                description="Submit trip details and request a quote.",
                required_permissions=frozenset({Permission.QUOTE_CREATE}),
                fields_written=("region", "trip_days", "traveller_age"),
                validator=validate_trip,
                side_effect_class=SideEffectClass.LOW_RISK_WRITE,
                service_action="create_quote",
            ),
            Transition(
                action=ACTION_REQUEST_QUOTE,
                from_state=STATE_QUOTE,
                to_state=STATE_QUOTE,
                description="Regenerate the quote after a provider failure.",
                required_permissions=frozenset({Permission.QUOTE_CREATE}),
                requires_fields=("region", "trip_days"),
                validator=no_payload,
                side_effect_class=SideEffectClass.LOW_RISK_WRITE,
                service_action="create_quote",
            ),
            Transition(
                action=ACTION_ACCEPT_QUOTE,
                from_state=STATE_QUOTE,
                to_state=STATE_COMPLETE,
                description="Accept the travel quote.",
                required_permissions=frozenset({Permission.PURCHASE_SUBMIT}),
                requires_fields=("quote_id",),
                validator=no_payload,
                side_effect_class=SideEffectClass.HIGH_RISK_WRITE,
                requires_confirmation=True,
                requires_idempotency_key=True,
                is_terminal=True,
            ),
        ),
    )
