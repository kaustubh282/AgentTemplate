"""Platform-level (domain-agnostic) capabilities.

These belong to the core platform rather than to a product domain: FAQ answering,
intent extraction and supervisor escalation are reused by every bot built on the
template.
"""

from __future__ import annotations

from app.ai.agents.schemas import ExtractedIntent, FaqAnswer, FaqRequest, IntentRequest, SupervisorDecision
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

CAPABILITY_FAQ = "platform.faq.answer"
CAPABILITY_INTENT = "platform.intent.extract"
CAPABILITY_SUPERVISOR = "platform.supervisor.route"

ALL_CHANNELS = frozenset(
    {Channel.WEB_CUSTOMER, Channel.WEB_AGENT, Channel.MOBILE, Channel.PUBLIC_WEB, Channel.WHATSAPP}
)
ALL_ACTORS = frozenset({ActorType.CUSTOMER, ActorType.AGENT, ActorType.ANONYMOUS, ActorType.SERVICE})
AUTHENTICATED_ACTORS = frozenset({ActorType.CUSTOMER, ActorType.AGENT, ActorType.SERVICE})
ALL_ROLES = frozenset({Role.CUSTOMER, Role.AGENT, Role.AGENT_SUPERVISOR, Role.ANONYMOUS, Role.SERVICE})


def platform_capabilities(*, public_faq: bool = True) -> tuple[Capability, ...]:
    """Platform capabilities. ``public_faq`` is FEATURE_PUBLIC_FAQ_ENABLED (§13.1): when
    off, every AI capability requires an authenticated caller."""
    actors = ALL_ACTORS if public_faq else AUTHENTICATED_ACTORS
    return (
        Capability(
            id=CAPABILITY_FAQ,
            domain="platform",
            description="Answer a general-insurance question from approved knowledge only.",
            input_schema=FaqRequest,
            output_schema=FaqAnswer,
            execution_mode=ExecutionMode.AI_ASSISTED,
            model_required=True,
            risk_level=RiskLevel.LOW,
            #: Public FAQ is explicitly unauthenticated (when the feature is on) but still
            #: rate-limited, guardrailed and grounded (§13.1).
            required_auth=not public_faq,
            allowed_actor_types=actors,
            allowed_roles=ALL_ROLES,
            required_permissions=frozenset({Permission.FAQ_ASK}),
            allowed_workflow_states=None,
            side_effect_class=SideEffectClass.READ_ONLY,
            service_binding="agents.faq",
            budgets=BudgetSpec(
                timeout_ms=12_000,
                model_call_budget=1,
                token_budget=2_500,
                agent_step_budget=1,
                tool_call_budget=0,
            ),
            rate_limit_class=RateLimitClass.AI,
            audit_policy=AuditPolicy.SUMMARY,
            allowed_channels=ALL_CHANNELS,
            #: Every FAQ answer is grounded by the Harness, not by the agent (§6.3, §7).
            requires_grounding=True,
        ),
        Capability(
            id=CAPABILITY_INTENT,
            domain="platform",
            description="Extract a typed intent and entities from one free-text message.",
            input_schema=IntentRequest,
            output_schema=ExtractedIntent,
            execution_mode=ExecutionMode.AI_ASSISTED,
            model_required=True,
            risk_level=RiskLevel.LOW,
            required_auth=not public_faq,
            allowed_actor_types=actors,
            allowed_roles=ALL_ROLES,
            required_permissions=frozenset({Permission.FAQ_ASK}),
            allowed_workflow_states=None,
            side_effect_class=SideEffectClass.READ_ONLY,
            service_binding="agents.intent",
            budgets=BudgetSpec(
                timeout_ms=6_000,
                model_call_budget=1,
                token_budget=1_000,
                agent_step_budget=1,
                tool_call_budget=0,
            ),
            rate_limit_class=RateLimitClass.AI,
            audit_policy=AuditPolicy.SUMMARY,
            allowed_channels=ALL_CHANNELS,
        ),
        Capability(
            id=CAPABILITY_SUPERVISOR,
            domain="platform",
            description="Escalation-only routing for language deterministic rules cannot classify.",
            input_schema=IntentRequest,
            output_schema=SupervisorDecision,
            execution_mode=ExecutionMode.AGENTIC,
            model_required=True,
            risk_level=RiskLevel.LOW,
            required_auth=not public_faq,
            allowed_actor_types=actors,
            allowed_roles=ALL_ROLES,
            required_permissions=frozenset({Permission.FAQ_ASK}),
            allowed_workflow_states=None,
            side_effect_class=SideEffectClass.READ_ONLY,
            service_binding="agents.supervisor",
            budgets=BudgetSpec(
                timeout_ms=6_000,
                model_call_budget=1,
                token_budget=1_200,
                agent_step_budget=2,
                tool_call_budget=0,
            ),
            rate_limit_class=RateLimitClass.AI,
            audit_policy=AuditPolicy.SUMMARY,
            allowed_channels=ALL_CHANNELS,
        ),
    )
