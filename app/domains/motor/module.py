"""Motor domain module (master prompt §45).

Registers workflows and capabilities. It reuses core auth, workflow, guardrail,
observability, audit, UI-directive and provider infrastructure without copying any of
it, and it does not modify a single core module.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.auth.auth_context import ActorType, Permission, Role
from app.core.context.request_context import Channel
from app.core.registry.capability import (
    AuditPolicy,
    BudgetSpec,
    Capability,
    ExecutionMode,
    RiskLevel,
)
from app.core.resilience.policies import SideEffectClass
from app.core.security.rate_limit import RateLimitClass
from app.domains.motor import workflow as wf
from app.workflows.engine.definition import WorkflowDefinition

DOMAIN = "motor"

CUSTOMER_CHANNELS = frozenset({Channel.WEB_CUSTOMER, Channel.MOBILE, Channel.WHATSAPP})
ALL_AUTHENTICATED_CHANNELS = CUSTOMER_CHANNELS | {Channel.WEB_AGENT}


class WorkflowActionInput(BaseModel):
    """Input contract for any deterministic workflow action."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    flow_id: str | None = None


class WorkflowActionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    allowed_actions: list[str] = Field(default_factory=list)
    replayed: bool = False


class PolicyLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(min_length=1, max_length=64)
    #: Agents must name the customer; the value is checked against assignment policy.
    customer_id: str | None = None


class PolicyLookupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    category: str
    data: dict | None = None
    reason_code: str | None = None


CAPABILITY_MOTOR_WORKFLOW_ACTION = "motor.workflow.action"
CAPABILITY_MOTOR_START = "motor.workflow.start"
CAPABILITY_MOTOR_POLICY_DETAILS = "motor.policy.details"
CAPABILITY_MOTOR_POLICY_PREMIUM = "motor.policy.premium"


def _workflow_capability(capability_id: str, description: str, states: frozenset[str] | None) -> Capability:
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
        service_binding="workflows.motor_sales",
        budgets=BudgetSpec(timeout_ms=4_000, model_call_budget=0, token_budget=0),
        rate_limit_class=RateLimitClass.DETERMINISTIC,
        audit_policy=AuditPolicy.FULL,
        allowed_channels=ALL_AUTHENTICATED_CHANNELS,
    )


def capabilities() -> tuple[Capability, ...]:
    return (
        _workflow_capability(
            CAPABILITY_MOTOR_START,
            "Start the motor purchase journey (deterministic, zero model calls).",
            None,
        ),
        _workflow_capability(
            CAPABILITY_MOTOR_WORKFLOW_ACTION,
            "Advance the motor purchase journey (deterministic, zero model calls).",
            frozenset(wf.STATES),
        ),
        Capability(
            id=CAPABILITY_MOTOR_POLICY_DETAILS,
            domain=DOMAIN,
            description="Read an owned motor policy from the authoritative source.",
            input_schema=PolicyLookupInput,
            output_schema=PolicyLookupOutput,
            execution_mode=ExecutionMode.DETERMINISTIC,
            model_required=False,
            risk_level=RiskLevel.MEDIUM,
            required_auth=True,
            allowed_actor_types=frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
            allowed_roles=frozenset({Role.CUSTOMER, Role.AGENT}),
            required_permissions=frozenset({Permission.POLICY_READ}),
            allowed_workflow_states=None,
            side_effect_class=SideEffectClass.READ_ONLY,
            service_binding="tools.GetPolicyDetails",
            budgets=BudgetSpec(timeout_ms=4_000),
            rate_limit_class=RateLimitClass.DETERMINISTIC,
            audit_policy=AuditPolicy.SUMMARY,
            allowed_channels=ALL_AUTHENTICATED_CHANNELS,
            allowed_tools=frozenset({"GetPolicyDetails"}),
        ),
        Capability(
            id=CAPABILITY_MOTOR_POLICY_PREMIUM,
            domain=DOMAIN,
            description="Read the premium of an owned policy from the System of Record.",
            input_schema=PolicyLookupInput,
            output_schema=PolicyLookupOutput,
            execution_mode=ExecutionMode.DETERMINISTIC,
            model_required=False,
            risk_level=RiskLevel.MEDIUM,
            required_auth=True,
            allowed_actor_types=frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
            allowed_roles=frozenset({Role.CUSTOMER, Role.AGENT}),
            required_permissions=frozenset({Permission.POLICY_READ}),
            allowed_workflow_states=None,
            side_effect_class=SideEffectClass.READ_ONLY,
            service_binding="tools.GetPolicyPremium",
            budgets=BudgetSpec(timeout_ms=4_000),
            rate_limit_class=RateLimitClass.DETERMINISTIC,
            audit_policy=AuditPolicy.SUMMARY,
            allowed_channels=ALL_AUTHENTICATED_CHANNELS,
            allowed_tools=frozenset({"GetPolicyPremium"}),
        ),
    )


class MotorDomainModule:
    """The motor domain's contribution to the platform."""

    domain = DOMAIN

    def workflows(self) -> tuple[WorkflowDefinition, ...]:
        return (wf.build_workflow(),)

    def capabilities(self) -> tuple[Capability, ...]:
        return capabilities()
