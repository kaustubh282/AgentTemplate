"""Motor sales journey - the deterministic reference flow (§2.2).

ENTRY -> IDENTIFY_CUSTOMER -> COLLECT_DATA -> VALIDATE -> QUOTE -> REVIEW
      -> PAYMENT -> COMPLETE

Every transition is allow-listed, permission-checked, schema-validated and audited.
Business rules live in validators here, in the domain - never in a prompt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.auth.auth_context import Permission
from app.core.errors.taxonomy import ValidationError
from app.core.resilience.policies import SideEffectClass
from app.workflows.engine.definition import Transition, WorkflowDefinition, require_fields

WORKFLOW_ID = "motor_sales"
WORKFLOW_VERSION = "1.0.0"

STATE_ENTRY = "ENTRY"
STATE_IDENTIFY_CUSTOMER = "IDENTIFY_CUSTOMER"
STATE_COLLECT_DATA = "COLLECT_DATA"
STATE_VALIDATE = "VALIDATE"
STATE_QUOTE = "QUOTE"
STATE_REVIEW = "REVIEW"
STATE_PAYMENT = "PAYMENT"
STATE_COMPLETE = "COMPLETE"

STATES = (
    STATE_ENTRY,
    STATE_IDENTIFY_CUSTOMER,
    STATE_COLLECT_DATA,
    STATE_VALIDATE,
    STATE_QUOTE,
    STATE_REVIEW,
    STATE_PAYMENT,
    STATE_COMPLETE,
)

ACTION_BEGIN = "BEGIN"
ACTION_IDENTIFY = "IDENTIFY_CUSTOMER"
ACTION_SUBMIT_VEHICLE = "SUBMIT_VEHICLE_DETAILS"
ACTION_SELECT_ADDONS = "SELECT_ADDONS"
ACTION_REQUEST_QUOTE = "REQUEST_QUOTE"
ACTION_REVIEW = "REVIEW_QUOTE"
ACTION_EDIT = "EDIT_DETAILS"
ACTION_GO_BACK = "GO_BACK"
ACTION_CONFIRM_PURCHASE = "CONFIRM_PURCHASE"
ACTION_COMPLETE_PAYMENT = "COMPLETE_PAYMENT"
ACTION_RETRY_QUOTE = "RETRY_QUOTE"

#: Structured fields projected into AI context. Nothing else from the flow is sent.
AI_CONTEXT_FIELDS = ("product_code", "vehicle_make", "addons", "quote_reference")

_REGISTRATION = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$")
_ALLOWED_FUEL = frozenset({"PETROL", "DIESEL", "CNG", "EV"})
_ALLOWED_PRODUCTS = frozenset({"MTR-PVT-CAR", "MTR-TW"})
_ALLOWED_ADDONS = frozenset({"Zero Depreciation", "Roadside Assistance", "Engine Protect"})

MIN_IDV = 25_000.0
MAX_IDV = 50_000_000.0
MAX_VEHICLE_AGE_YEARS = 20


def validate_identity(payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    """Records which product the journey is for. Customer identity comes from the token."""
    require_fields(payload, ("product_code",))
    product_code = str(payload["product_code"]).strip().upper()
    if product_code not in _ALLOWED_PRODUCTS:
        raise ValidationError("unsupported_product", details={"productCode": product_code})
    return {"product_code": product_code}


def validate_vehicle(payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic business validation. No model is consulted (§2.4)."""
    require_fields(payload, ("registration_number", "vehicle_make", "manufacture_year", "fuel_type", "idv"))

    registration = str(payload["registration_number"]).strip().upper().replace(" ", "").replace("-", "")
    if not _REGISTRATION.match(registration):
        raise ValidationError("invalid_registration_number")

    fuel = str(payload["fuel_type"]).strip().upper()
    if fuel not in _ALLOWED_FUEL:
        raise ValidationError("unsupported_fuel_type", details={"allowed": sorted(_ALLOWED_FUEL)})

    try:
        year = int(payload["manufacture_year"])
        idv = float(payload["idv"])
    except (TypeError, ValueError):
        raise ValidationError("invalid_numeric_field") from None

    current_year = datetime.now(UTC).year
    if year > current_year or (current_year - year) > MAX_VEHICLE_AGE_YEARS:
        raise ValidationError("vehicle_age_out_of_appetite", details={"maxAgeYears": MAX_VEHICLE_AGE_YEARS})
    if not (MIN_IDV <= idv <= MAX_IDV):
        raise ValidationError("idv_out_of_range", details={"min": MIN_IDV, "max": MAX_IDV})

    return {
        "registration_number": registration,
        "vehicle_make": str(payload["vehicle_make"]).strip()[:60],
        "manufacture_year": year,
        "fuel_type": fuel,
        "idv": idv,
    }


def validate_addons(payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    addons = payload.get("addons") or []
    if not isinstance(addons, list):
        raise ValidationError("addons_must_be_a_list")
    unknown = [a for a in addons if a not in _ALLOWED_ADDONS]
    if unknown:
        raise ValidationError("unsupported_addons", details={"addons": unknown})
    return {"addons": list(dict.fromkeys(addons))}


def no_payload(_payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    """Navigation and service-driven transitions carry no user-written fields."""
    return {}


def build_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        domain="motor",
        description="Motor insurance purchase journey.",
        initial_state=STATE_ENTRY,
        states=STATES,
        terminal_states=frozenset({STATE_COMPLETE}),
        ai_context_fields=AI_CONTEXT_FIELDS,
        transitions=(
            Transition(
                action=ACTION_BEGIN,
                from_state=STATE_ENTRY,
                to_state=STATE_IDENTIFY_CUSTOMER,
                description="Start the motor purchase journey.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                validator=no_payload,
            ),
            Transition(
                action=ACTION_IDENTIFY,
                from_state=STATE_IDENTIFY_CUSTOMER,
                to_state=STATE_COLLECT_DATA,
                description="Confirm the product being purchased.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                fields_written=("product_code",),
                validator=validate_identity,
            ),
            Transition(
                action=ACTION_SUBMIT_VEHICLE,
                from_state=STATE_COLLECT_DATA,
                to_state=STATE_VALIDATE,
                description="Submit vehicle details for validation.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                fields_written=(
                    "registration_number",
                    "vehicle_make",
                    "manufacture_year",
                    "fuel_type",
                    "idv",
                ),
                requires_fields=("product_code",),
                validator=validate_vehicle,
            ),
            Transition(
                action=ACTION_SELECT_ADDONS,
                from_state=STATE_VALIDATE,
                to_state=STATE_VALIDATE,
                description="Choose optional add-ons.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                fields_written=("addons",),
                validator=validate_addons,
            ),
            Transition(
                action=ACTION_REQUEST_QUOTE,
                from_state=STATE_VALIDATE,
                to_state=STATE_QUOTE,
                description="Request an authoritative quote from the rating provider.",
                required_permissions=frozenset({Permission.QUOTE_CREATE}),
                fields_written=(),
                requires_fields=("product_code", "registration_number", "idv"),
                validator=no_payload,
                side_effect_class=SideEffectClass.LOW_RISK_WRITE,
                service_action="create_quote",
            ),
            Transition(
                action=ACTION_RETRY_QUOTE,
                from_state=STATE_VALIDATE,
                to_state=STATE_QUOTE,
                description="Retry quote generation after a provider failure.",
                required_permissions=frozenset({Permission.QUOTE_CREATE}),
                requires_fields=("product_code", "registration_number", "idv"),
                validator=no_payload,
                side_effect_class=SideEffectClass.LOW_RISK_WRITE,
                service_action="create_quote",
            ),
            Transition(
                action=ACTION_REVIEW,
                from_state=STATE_QUOTE,
                to_state=STATE_REVIEW,
                description="Review the quote before purchase.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                requires_fields=("quote_id",),
                validator=no_payload,
            ),
            Transition(
                action=ACTION_EDIT,
                from_state=STATE_REVIEW,
                to_state=STATE_COLLECT_DATA,
                description="Go back and edit the submitted details.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                validator=no_payload,
            ),
            Transition(
                action=ACTION_GO_BACK,
                from_state=STATE_QUOTE,
                to_state=STATE_VALIDATE,
                description="Return to add-on selection.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                validator=no_payload,
            ),
            Transition(
                action=ACTION_CONFIRM_PURCHASE,
                from_state=STATE_REVIEW,
                to_state=STATE_PAYMENT,
                description="Confirm purchase and initiate payment.",
                required_permissions=frozenset({Permission.PURCHASE_SUBMIT, Permission.PAYMENT_INITIATE}),
                requires_fields=("quote_id",),
                validator=no_payload,
                side_effect_class=SideEffectClass.HIGH_RISK_WRITE,
                requires_confirmation=True,
                requires_idempotency_key=True,
                service_action="initiate_payment",
            ),
            Transition(
                action=ACTION_COMPLETE_PAYMENT,
                from_state=STATE_PAYMENT,
                to_state=STATE_COMPLETE,
                description="Issue the policy after confirmed payment.",
                required_permissions=frozenset({Permission.PURCHASE_SUBMIT}),
                requires_fields=("payment_reference", "quote_id"),
                validator=no_payload,
                side_effect_class=SideEffectClass.IRREVERSIBLE,
                requires_confirmation=True,
                requires_idempotency_key=True,
                service_action="issue_policy",
                is_terminal=True,
            ),
        ),
    )
