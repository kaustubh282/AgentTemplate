"""Intent / structured extraction agent (master prompt §5.3).

Converts free text into a typed intent plus entities. It never calls a business API,
never decides authorization, and reports ambiguity instead of guessing.

Cost profile: one structured model call, zero agent handoffs.
"""

from __future__ import annotations

from app.ai.agents.schemas import ExtractedIntent, Intent
from app.ai.harness.decisions import HarnessOutcome, HarnessResult, ReasonCode
from app.ai.harness.service import AgentExecutionContext
from app.ai.models.provider import user_message
from app.ai.prompts.registry import PromptRegistry
from app.core.logging.structured import get_logger

logger = get_logger(__name__)

AGENT_ID = "intent_agent"

#: Intents the extractor may return. Anything else is coerced to UNSUPPORTED.
ALLOWED_INTENTS: tuple[Intent, ...] = (
    Intent.FAQ_QUESTION,
    Intent.START_PURCHASE,
    Intent.CONTINUE_PURCHASE,
    Intent.GET_POLICY_DETAILS,
    Intent.GET_POLICY_PREMIUM,
    Intent.GET_CLAIM_STATUS,
    Intent.GET_PAYMENT_STATUS,
    Intent.HUMAN_HANDOFF,
    Intent.UNSUPPORTED,
)

#: Entities the extractor is permitted to emit. Others are dropped, not trusted.
ALLOWED_ENTITY_KEYS = frozenset(
    {"domain", "product_code", "policy_reference", "claim_reference", "addons", "registration_number"}
)


class IntentAgent:
    """Constrained extraction with an allow-listed output space."""

    def __init__(self, prompts: PromptRegistry) -> None:
        self._prompts = prompts

    async def handle(self, agent_ctx: AgentExecutionContext) -> HarnessResult:
        agent_ctx.agent_id = AGENT_ID
        template = self._prompts.get("intent.extract")

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

        rendered = template.render(
            allowed_intents="\n".join(f"- {i.value}" for i in ALLOWED_INTENTS),
            message=built.user_content,
        )

        # Budget and usage accounting are enforced by the governed invoker (H-2).
        extracted, _usage = await agent_ctx.invoker.generate_structured(
            ExtractedIntent,
            [user_message(rendered)],
            capability_id=agent_ctx.capability.id,
        )

        safe = self._constrain(extracted)
        agent_ctx.result_category = f"INTENT_{safe.intent.value}"

        if safe.ambiguous or safe.intent is Intent.UNSUPPORTED:
            message = safe.clarifying_question or (
                "Could you tell me a little more about what you would like to do?"
            )
            return HarnessResult(
                HarnessOutcome.ESCALATE,
                ReasonCode.ESCALATE_NEEDS_CLARIFICATION,
                message,
                safe.model_dump(mode="json"),
            )

        return HarnessResult(
            HarnessOutcome.ALLOW,
            ReasonCode.OK,
            safe.clarifying_question or "",
            safe.model_dump(mode="json"),
        )

    @staticmethod
    def _constrain(extracted: ExtractedIntent) -> ExtractedIntent:
        """Drop anything outside the allow-list rather than trusting model output."""
        intent = extracted.intent if extracted.intent in ALLOWED_INTENTS else Intent.UNSUPPORTED
        entities = {k: v for k, v in extracted.entities.items() if k in ALLOWED_ENTITY_KEYS}
        dropped = set(extracted.entities) - set(entities)
        if dropped:
            logger.info("intent_entities_dropped", extra={"droppedKeys": sorted(dropped)})
        return extracted.model_copy(update={"intent": intent, "entities": entities})
