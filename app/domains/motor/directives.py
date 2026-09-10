"""Motor domain UI directive builders (master prompt §2.3, §36).

State -> registered directive. The server decides what the client renders; the model
never produces markup, script or an unregistered component.
"""

from __future__ import annotations

from typing import Any

from app.core.privacy.masking import masking_service
from app.domains.motor import workflow as wf
from app.ui_directives.registry.registry import DirectiveRegistry, a11y
from app.ui_directives.schemas.directives import (
    Directive,
    DirectiveType,
    FieldType,
    FormField,
    MessageSeverity,
    MoneyAmount,
    QuoteLine,
    ReviewItem,
    SelectOption,
    ShowCompletionPayload,
    ShowConfirmationPayload,
    ShowFormPayload,
    ShowMessagePayload,
    ShowOptionsPayload,
    ShowPaymentHandoffPayload,
    ShowQuoteSummaryPayload,
    ShowRetryPayload,
    ShowReviewPayload,
)

FUEL_OPTIONS = [
    SelectOption(value="PETROL", label="Petrol"),
    SelectOption(value="DIESEL", label="Diesel"),
    SelectOption(value="CNG", label="CNG"),
    SelectOption(value="EV", label="Electric"),
]

ADDON_OPTIONS = [
    SelectOption(
        value="Zero Depreciation",
        label="Zero Depreciation",
        description="Depreciation is not deducted on replaced parts at claim time.",
    ),
    SelectOption(
        value="Roadside Assistance",
        label="Roadside Assistance",
        description="Towing, jump start, flat tyre and fuel delivery support.",
    ),
    SelectOption(
        value="Engine Protect",
        label="Engine Protect",
        description="Cover for specified engine damage not covered by the base policy.",
    ),
]

PRODUCT_OPTIONS = [
    SelectOption(value="MTR-PVT-CAR", label="Private car"),
    SelectOption(value="MTR-TW", label="Two wheeler"),
]


class MotorDirectiveBuilder:
    """Builds the directive for the current motor journey state."""

    def __init__(self, registry: DirectiveRegistry) -> None:
        self._registry = registry

    def for_state(self, state: str, data: dict[str, Any]) -> Directive:
        builder = {
            wf.STATE_ENTRY: self._entry,
            wf.STATE_IDENTIFY_CUSTOMER: self._identify,
            wf.STATE_COLLECT_DATA: self._collect,
            wf.STATE_VALIDATE: self._addons,
            wf.STATE_QUOTE: self._quote,
            wf.STATE_REVIEW: self._review,
            wf.STATE_PAYMENT: self._payment,
            wf.STATE_COMPLETE: self._complete,
        }.get(state)
        if builder is None:
            return self._registry.build(
                DirectiveType.SHOW_MESSAGE,
                ShowMessagePayload(
                    body="This step is not available.",
                    severity=MessageSeverity.WARNING,
                    accessibility=a11y("Journey status", role="status", status_text="Unavailable"),
                ),
            )
        return builder(data)

    # ------------------------------------------------------------- builders ---
    def _entry(self, _data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_MESSAGE,
            ShowMessagePayload(
                heading="Buy motor insurance",
                body="Let us start your motor insurance purchase.",
                accessibility=a11y("Motor purchase introduction", role="status"),
            ),
        )

    def _identify(self, _data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_OPTIONS,
            ShowOptionsPayload(
                group_id="motor_product",
                title="What would you like to insure?",
                options=PRODUCT_OPTIONS,
                accessibility=a11y("Choose the vehicle type to insure", role="radiogroup"),
            ),
        )

    def _collect(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_FORM,
            ShowFormPayload(
                form_id="motor_vehicle_details",
                title="Your vehicle details",
                submit_label="Continue",
                accessibility=a11y("Vehicle details form", role="form"),
                fields=[
                    FormField(
                        name="registration_number",
                        label="Registration number",
                        field_type=FieldType.REGISTRATION_NUMBER,
                        placeholder="MH01AB1234",
                        max_length=13,
                        validation_message="Enter the registration number as shown on the RC.",
                        accessibility=a11y("Vehicle registration number", role="textbox"),
                    ),
                    FormField(
                        name="vehicle_make",
                        label="Make and model",
                        field_type=FieldType.TEXT,
                        max_length=60,
                        validation_message="Enter the make and model of your vehicle.",
                        accessibility=a11y("Vehicle make and model", role="textbox"),
                    ),
                    FormField(
                        name="manufacture_year",
                        label="Year of manufacture",
                        field_type=FieldType.NUMBER,
                        validation_message=(
                            f"We can cover vehicles up to {wf.MAX_VEHICLE_AGE_YEARS} years old."
                        ),
                        accessibility=a11y("Year of manufacture", role="spinbutton"),
                    ),
                    FormField(
                        name="fuel_type",
                        label="Fuel type",
                        field_type=FieldType.SELECT,
                        options=FUEL_OPTIONS,
                        validation_message="Choose the fuel type shown on the RC.",
                        accessibility=a11y("Fuel type", role="combobox"),
                    ),
                    FormField(
                        name="idv",
                        label="Insured Declared Value (IDV)",
                        field_type=FieldType.NUMBER,
                        help_text="The maximum amount payable if the vehicle is a total loss.",
                        validation_message=(
                            f"IDV must be between {int(wf.MIN_IDV):,} and {int(wf.MAX_IDV):,}."
                        ),
                        accessibility=a11y("Insured declared value", role="spinbutton"),
                    ),
                ],
            ),
        )

    def _addons(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_OPTIONS,
            ShowOptionsPayload(
                group_id="motor_addons",
                title="Add optional cover",
                options=ADDON_OPTIONS,
                multi_select=True,
                accessibility=a11y("Optional add-on cover selection", role="group"),
            ),
        )

    def _quote(self, data: dict[str, Any]) -> Directive:
        lines = [
            QuoteLine(
                label=str(line["label"]),
                amount=MoneyAmount(
                    amount=float(line["amount"]), currency=str(data.get("quote_currency", "INR"))
                ),
                note=line.get("note"),
            )
            for line in data.get("quote_lines", [])
        ]
        return self._registry.build(
            DirectiveType.SHOW_QUOTE_SUMMARY,
            ShowQuoteSummaryPayload(
                quote_ref=str(data.get("quote_reference", "")),
                product=str(data.get("product_code", "")),
                lines=lines,
                total=MoneyAmount(
                    amount=float(data.get("quote_total", 0.0)),
                    currency=str(data.get("quote_currency", "INR")),
                ),
                valid_until=data.get("quote_valid_until"),
                source_system=str(data.get("quote_source_system", "UNKNOWN")),
                accessibility=a11y("Quote summary", role="region", status_text="Quote ready"),
            ),
        )

    def _review(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_REVIEW,
            ShowReviewPayload(
                title="Review before you buy",
                accessibility=a11y("Review your purchase details", role="region"),
                items=[
                    ReviewItem(
                        label="Vehicle",
                        value=f"{data.get('vehicle_make', '')} ({data.get('manufacture_year', '')})",
                        editable=True,
                        edit_action=wf.ACTION_EDIT,
                    ),
                    ReviewItem(
                        label="Registration",
                        value=masking_service.mask_value(
                            "registration_number", str(data.get("registration_number", ""))
                        ),
                    ),
                    ReviewItem(label="Fuel", value=str(data.get("fuel_type", ""))),
                    ReviewItem(label="IDV", value=f"{float(data.get('idv', 0)):,.0f}"),
                    ReviewItem(
                        label="Add-ons",
                        value=", ".join(data.get("addons", [])) or "None",
                        editable=True,
                        edit_action=wf.ACTION_EDIT,
                    ),
                    ReviewItem(
                        label="Total premium",
                        value=(
                            f"{data.get('quote_currency', 'INR')} {float(data.get('quote_total', 0)):,.2f}"
                        ),
                    ),
                ],
            ),
        )

    def _payment(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_PAYMENT_HANDOFF,
            ShowPaymentHandoffPayload(
                payment_ref=masking_service.mask_value(
                    "payment_reference", str(data.get("payment_reference", ""))
                ),
                amount=MoneyAmount(
                    amount=float(data.get("quote_total", 0.0)),
                    currency=str(data.get("quote_currency", "INR")),
                ),
                handoff_token=str(data.get("payment_handoff_token", "")),
                expires_in_seconds=int(data.get("payment_expires_in_seconds", 900)),
                accessibility=a11y("Secure payment handoff", role="region", status_text="Awaiting payment"),
            ),
        )

    def _complete(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_COMPLETION,
            ShowCompletionPayload(
                title="Your policy is active",
                reference_masked=masking_service.mask_value(
                    "policy_number", str(data.get("policy_number", ""))
                ),
                body="Your policy document will be available in your account shortly.",
                accessibility=a11y("Purchase complete", role="status", status_text="Completed"),
            ),
        )

    # ---------------------------------------------------------- exceptional ---
    def confirmation(self, data: dict[str, Any]) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_CONFIRMATION,
            ShowConfirmationPayload(
                title="Confirm your purchase",
                body=(
                    f"You are about to pay {data.get('quote_currency', 'INR')} "
                    f"{float(data.get('quote_total', 0)):,.2f} for this policy."
                ),
                confirm_action=wf.ACTION_CONFIRM_PURCHASE,
                accessibility=a11y("Confirm purchase", role="dialog"),
            ),
        )

    def retry(self, action: str, body: str) -> Directive:
        return self._registry.build(
            DirectiveType.SHOW_RETRY,
            ShowRetryPayload(
                body=body,
                retry_action=action,
                accessibility=a11y("Retry the previous step", role="alert", status_text="Action needed"),
            ),
        )
