"""Central grounding policy applied by the Harness (master prompt §6.3, §7, §5.7.6 stage 13).

The Harness - not the agent - decides whether an answer may stand. An agent supplies
the evidence it retrieved and a proposed answer; this policy re-assesses the answer
against that evidence with the shared :class:`GroundingService` and either lets it
through with the *assessed* verification class and *contributing* citations, or
replaces it with a central abstention.

It is a policy object composed by the Harness (§5.7.4): it holds no retrieval or
scoring logic of its own, and no agent can skip it because the Harness runs it for
every capability that declares ``requires_grounding``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.harness.decisions import HarnessExecutionRecord, HarnessOutcome, HarnessResult, ReasonCode
from app.core.logging.structured import get_logger
from app.core.observability.metrics import GUARDRAIL_INTERVENTIONS_TOTAL, metrics
from app.rag.retrieval.grounding import (
    ABSTENTION_MESSAGE,
    CONFLICT_MESSAGE,
    GroundingDecision,
    GroundingService,
)

if TYPE_CHECKING:
    from app.ai.harness.service import AgentExecutionContext

logger = get_logger(__name__)


class GroundingPolicy:
    """Decides, centrally, whether an ALLOW is actually grounded."""

    def __init__(self, grounding: GroundingService) -> None:
        self._grounding = grounding

    def enforce(
        self,
        result: HarnessResult,
        agent_ctx: AgentExecutionContext,
        record: HarnessExecutionRecord,
    ) -> HarnessResult | None:
        """Return a replacement result when the answer must not stand, else ``None``."""
        retrieval = agent_ctx.retrieval
        if retrieval is None:
            # An ALLOW with no evidence at all is never a grounded answer.
            self._count("no_evidence")
            return self.abstain(
                record, agent_ctx, ReasonCode.ABSTAIN_INSUFFICIENT_EVIDENCE, "grounding_evidence_missing"
            )

        assessment = self._grounding.assess_answer(result.message, retrieval)
        agent_ctx.grounding_assessment = assessment
        record.grounding_decision = assessment.decision.value

        if assessment.decision is GroundingDecision.SURFACE_CONFLICT:
            agent_ctx.result_category = "CONFLICTING_SOURCES"
            return self.abstain(
                record,
                agent_ctx,
                ReasonCode.ABSTAIN_CONFLICTING_SOURCES,
                assessment.reason_code,
                message=CONFLICT_MESSAGE,
                verification=assessment.verification.value,
            )
        if assessment.decision is GroundingDecision.ABSTAIN:
            self._count("unsupported")
            return self.abstain(
                record, agent_ctx, ReasonCode.ABSTAIN_INSUFFICIENT_EVIDENCE, assessment.reason_code
            )

        # Grounded. The verification class and the *contributing* citations are set
        # here so provenance is precise and cannot be inflated by a handler (§6.3).
        self._stamp_payload(result.payload, assessment.verification.value, assessment)
        if not agent_ctx.result_category.startswith("ANSWERED_"):
            agent_ctx.result_category = f"ANSWERED_{assessment.verification.value}"
        return None

    @staticmethod
    def abstain(
        record: HarnessExecutionRecord,
        agent_ctx: AgentExecutionContext,
        reason: ReasonCode,
        detail: str,
        *,
        message: str = ABSTENTION_MESSAGE,
        verification: str = "UNSUPPORTED",
    ) -> HarnessResult:
        record.outcome = HarnessOutcome.ABSTAIN
        record.reason_code = reason
        if record.grounding_decision is None:
            record.grounding_decision = GroundingDecision.ABSTAIN.value
        if not agent_ctx.result_category.startswith(("ABSTAINED", "CONFLICTING")):
            agent_ctx.result_category = "ABSTAINED"
        logger.info("harness_abstained", extra={"reasonCode": reason.value, "detail": detail})
        return HarnessResult(
            HarnessOutcome.ABSTAIN,
            reason,
            message,
            {"verification": verification, "citations": []},
            record=record,
        )

    @staticmethod
    def _stamp_payload(payload: Any, verification: str, assessment: Any) -> None:
        if not isinstance(payload, dict):
            return
        if "verification" in payload:
            payload["verification"] = verification
        citations = payload.get("citations")
        if isinstance(citations, list):
            cited = set(assessment.cited_document_ids)
            payload["citations"] = [c for c in citations if c.get("document_id") in cited]
        if "evidence_score" in payload:
            payload["evidence_score"] = round(assessment.evidence_score, 4)

    @staticmethod
    def _count(reason: str) -> None:
        metrics.increment(
            GUARDRAIL_INTERVENTIONS_TOTAL, labels={"stage": "harness_grounding", "reason": reason}
        )
