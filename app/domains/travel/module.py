"""Travel domain module - extension proof (§45.2).

Note what is *absent*: no auth code, no PII code, no guardrails, no logging setup, no
provider transport, no directive machinery. All of it is reused from core.
"""

from __future__ import annotations

from typing import Any

from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.auth.auth_context import ActorType, Permission, Role
from app.core.context.request_context import Channel, RequestContext
from app.core.registry.capability import (
    AuditPolicy,
    BudgetSpec,
    Capability,
    ExecutionMode,
    RiskLevel,
)
from app.core.resilience.policies import ResiliencePolicy, SideEffectClass
from app.core.security.rate_limit import RateLimitClass
from app.domains.motor.module import WorkflowActionInput, WorkflowActionOutput
from app.domains.travel import workflow as wf
from app.integrations.contracts.dtos import CreateQuoteRequest
from app.integrations.contracts.providers import ProviderBundle
from app.ui_directives.registry.registry import DirectiveRegistry, a11y
from app.ui_directives.schemas.directives import (
    Directive,
    DirectiveType,
    FieldType,
    FormField,
    MoneyAmount,
    QuoteLine,
    SelectOption,
    ShowCompletionPayload,
    ShowFormPayload,
    ShowMessagePayload,
    ShowQuoteSummaryPayload,
)
from app.workflows.engine.definition import WorkflowDefinition
from app.workflows.state.models import WorkflowState

DOMAIN = "travel"

CAPABILITY_TRAVEL_START = "travel.workflow.start"
CAPABILITY_TRAVEL_ACTION = "travel.workflow.action"

REGION_OPTIONS = [
    SelectOption(value="ASIA", label="Asia (excluding Japan)"),
    SelectOption(value="SCHENGEN", label="Schengen"),
    SelectOption(value="WORLDWIDE", label="Worldwide"),
]


class TravelSalesService:
    """One provider-backed action. Reuses the same provider contract as motor."""

    def __init__(self, providers: ProviderBundle, audit: AuditService, *, policy: ResiliencePolicy) -> None:
        self._providers = providers
        self._audit = audit
        self._policy = policy

    async def create_quote(
        self, ctx: RequestContext, state: WorkflowState, _payload: dict[str, Any]
    ) -> dict[str, Any]:
        request = CreateQuoteRequest(
            customer_id=ctx.auth.subject_id,
            product_code="TRV-OVERSEAS",
            domain=DOMAIN,
            attributes={
                "region": state.data.get("region"),
                "trip_days": state.data.get("trip_days"),
            },
            addons=list(state.data.get("addons", [])),
            idempotency_key=ctx.idempotency_key,
        )
        response = await self._policy.execute(
            lambda: self._providers.quote.create_quote(request, ctx),
            side_effect=SideEffectClass.LOW_RISK_WRITE,
            idempotency_key=ctx.idempotency_key,
        )
        quote = response.quote
        await self._audit.record(
            ctx,
            AuditAction.QUOTE_RECEIVED,
            AuditResult.SUCCESS,
            resource_type="QUOTE",
            resource_id=quote.quote_id,
            source_system="MOCK" if self._providers.is_mock else "INSUREMO",
            reason_code="quote_created",
            attributes={"domain": DOMAIN},
        )
        return {
            "quote_id": quote.quote_id,
            "quote_reference": quote.quote_reference,
            "quote_total": quote.total.amount,
            "quote_currency": quote.total.currency,
            "quote_valid_until": quote.valid_until.isoformat(),
            "quote_source_system": quote.source_system,
            "quote_lines": [
                {"label": line.label, "amount": line.amount.amount, "note": line.note} for line in quote.lines
            ],
        }


class TravelDirectiveBuilder:
    """Reuses the shared directive registry; adds no new component types."""

    def __init__(self, registry: DirectiveRegistry) -> None:
        self._registry = registry

    def for_state(self, state: str, data: dict[str, Any]) -> Directive:
        if state == wf.STATE_ENTRY:
            return self._registry.build(
                DirectiveType.SHOW_MESSAGE,
                ShowMessagePayload(
                    heading="Buy travel insurance",
                    body="Let us start your travel insurance purchase.",
                    accessibility=a11y("Travel purchase introduction", role="status"),
                ),
            )
        if state == wf.STATE_COLLECT_TRIP:
            return self._registry.build(
                DirectiveType.SHOW_FORM,
                ShowFormPayload(
                    form_id="travel_trip_details",
                    title="Your trip",
                    accessibility=a11y("Trip details form", role="form"),
                    fields=[
                        FormField(
                            name="region",
                            label="Where are you travelling?",
                            field_type=FieldType.SELECT,
                            options=REGION_OPTIONS,
                            validation_message="Choose the region for your trip.",
                            accessibility=a11y("Travel region", role="combobox"),
                        ),
                        FormField(
                            name="trip_days",
                            label="Trip length in days",
                            field_type=FieldType.NUMBER,
                            validation_message=f"Trips up to {wf.MAX_TRIP_DAYS} days can be covered.",
                            accessibility=a11y("Trip length in days", role="spinbutton"),
                        ),
                        FormField(
                            name="traveller_age",
                            label="Traveller age",
                            field_type=FieldType.NUMBER,
                            validation_message="Enter the traveller's age in years.",
                            accessibility=a11y("Traveller age", role="spinbutton"),
                        ),
                    ],
                ),
            )
        if state == wf.STATE_QUOTE:
            return self._registry.build(
                DirectiveType.SHOW_QUOTE_SUMMARY,
                ShowQuoteSummaryPayload(
                    quote_ref=str(data.get("quote_reference", "")),
                    product="TRV-OVERSEAS",
                    lines=[
                        QuoteLine(
                            label=str(line["label"]),
                            amount=MoneyAmount(
                                amount=float(line["amount"]),
                                currency=str(data.get("quote_currency", "INR")),
                            ),
                            note=line.get("note"),
                        )
                        for line in data.get("quote_lines", [])
                    ],
                    total=MoneyAmount(
                        amount=float(data.get("quote_total", 0.0)),
                        currency=str(data.get("quote_currency", "INR")),
                    ),
                    valid_until=data.get("quote_valid_until"),
                    source_system=str(data.get("quote_source_system", "UNKNOWN")),
                    accessibility=a11y("Travel quote summary", role="region", status_text="Quote ready"),
                ),
            )
        return self._registry.build(
            DirectiveType.SHOW_COMPLETION,
            ShowCompletionPayload(
                title="Quote accepted",
                reference_masked=str(data.get("quote_reference", "")),
                body="We have recorded your acceptance and will follow up with the next step.",
                accessibility=a11y("Travel journey complete", role="status", status_text="Completed"),
            ),
        )


def capabilities() -> tuple[Capability, ...]:
    channels = frozenset({Channel.WEB_CUSTOMER, Channel.MOBILE, Channel.WEB_AGENT})

    def workflow_capability(
        capability_id: str, description: str, states: frozenset[str] | None
    ) -> Capability:
        """One deterministic workflow capability, fully typed."""
        return Capability(
            id=capability_id,
            domain=DOMAIN,
            description=description,
            input_schema=WorkflowActionInput,
            output_schema=WorkflowActionOutput,
            execution_mode=ExecutionMode.DETERMINISTIC,
            model_required=False,
            risk_level=RiskLevel.MEDIUM,
            required_auth=True,
            allowed_actor_types=frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
            allowed_roles=frozenset({Role.CUSTOMER, Role.AGENT}),
            required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
            allowed_workflow_states=states,
            side_effect_class=SideEffectClass.LOW_RISK_WRITE,
            service_binding="workflows.travel_sales",
            budgets=BudgetSpec(timeout_ms=4_000),
            rate_limit_class=RateLimitClass.DETERMINISTIC,
            audit_policy=AuditPolicy.FULL,
            allowed_channels=channels,
        )

    return (
        workflow_capability(
            CAPABILITY_TRAVEL_START,
            "Start the travel purchase journey (deterministic).",
            None,
        ),
        workflow_capability(
            CAPABILITY_TRAVEL_ACTION,
            "Advance the travel purchase journey (deterministic).",
            frozenset(wf.STATES),
        ),
    )


class TravelDomainModule:
    domain = DOMAIN

    def workflows(self) -> tuple[WorkflowDefinition, ...]:
        return (wf.build_workflow(),)

    def capabilities(self) -> tuple[Capability, ...]:
        return capabilities()
