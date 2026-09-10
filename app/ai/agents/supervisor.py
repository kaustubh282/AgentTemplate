"""Supervisor / orchestrator agent (master prompt §5.1, §5.6).

This is an **escalation component, not the default entry path**. The deterministic
capability router resolves obvious workflow and UI actions long before this agent is
considered, and the FAQ path reaches its agent directly. The supervisor exists only
for language the deterministic router could not safely classify.

Complexity budget justification (§5.6):

* distinct responsibility - choosing between *unlike* capabilities when intent is
  genuinely ambiguous, which neither the FAQ agent nor the intent extractor owns
* why deterministic routing is insufficient - the deterministic router refuses to
  guess; without this component an ambiguous message would dead-end
* cost - one structured model call, no handoff to a second model
* latency impact - a single classification call, ~<1.5s target (§18)
* security boundary - it selects from a server-supplied allow-list and executes
  nothing; the PEP still authorizes whatever it names
* evaluation ownership - ``evals/datasets/ambiguous-intent.jsonl``

It performs **no** business action and states **no** insurance fact.
"""

from __future__ import annotations

from app.ai.agents.schemas import SupervisorDecision
from app.ai.harness.decisions import HarnessOutcome, HarnessResult, ReasonCode
from app.ai.harness.service import AgentExecutionContext
from app.ai.models.provider import user_message
from app.ai.prompts.registry import PromptRegistry
from app.core.logging.structured import get_logger

logger = get_logger(__name__)

AGENT_ID = "supervisor_agent"

UNSUPPORTED = "UNSUPPORTED"


class SupervisorAgent:
    """Chooses one approved capability. Grants itself nothing."""

    def __init__(self, prompts: PromptRegistry, routable_capabilities: frozenset[str]) -> None:
        self._prompts = prompts
        self._routable = routable_capabilities

    async def handle(self, agent_ctx: AgentExecutionContext) -> HarnessResult:
        agent_ctx.agent_id = AGENT_ID
        template = self._prompts.get("supervisor.route")

        built = agent_ctx.builder.build(
            system_prompt="",
            question=agent_ctx.request.user_message,
            ledger=agent_ctx.ledger,
            workflow_state=agent_ctx.request.workflow_state,
            workflow_context_fields=("state",) if agent_ctx.request.workflow_state else (),
            history=agent_ctx.request.history,
            purpose_fields=agent_ctx.request.purpose_fields,
        )
        agent_ctx.built_context = built

        allowed = [*sorted(self._routable), UNSUPPORTED]
        rendered = template.render(
            allowed_capabilities="\n".join(f"- {c}" for c in allowed),
            message=built.user_content,
        )

        # Budget and usage accounting are enforced by the governed invoker (H-2).
        decision, _usage = await agent_ctx.invoker.generate_structured(
            SupervisorDecision, [user_message(rendered)], capability_id=agent_ctx.capability.id
        )

        # A capability the server did not offer is rejected, never executed.
        if decision.capability not in self._routable:
            if decision.capability != UNSUPPORTED:
                logger.warning(
                    "supervisor_proposed_unknown_capability",
                    extra={"proposed": decision.capability},
                )
            agent_ctx.result_category = "ROUTE_UNSUPPORTED"
            return HarnessResult(
                HarnessOutcome.ESCALATE,
                ReasonCode.ESCALATE_UNSUPPORTED_SENSITIVE_CASE,
                "I am not able to help with that here. I can connect you with our support team.",
                SupervisorDecision(capability=UNSUPPORTED, reason_category="not_routable").model_dump(
                    mode="json"
                ),
            )

        if decision.needs_clarification:
            agent_ctx.result_category = "ROUTE_NEEDS_CLARIFICATION"
            return HarnessResult(
                HarnessOutcome.ESCALATE,
                ReasonCode.ESCALATE_NEEDS_CLARIFICATION,
                decision.clarifying_question or "Could you tell me a little more?",
                decision.model_dump(mode="json"),
            )

        agent_ctx.result_category = f"ROUTE_{decision.capability}"
        return HarnessResult(HarnessOutcome.ALLOW, ReasonCode.OK, "", decision.model_dump(mode="json"))
