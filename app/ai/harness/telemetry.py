"""Central AI evidence emission (master prompt §5.7.4, §5.7.8, §22, §23).

The Harness *orchestrates*; it does not implement every subsystem. Turning an execution
record into durable evidence is its own responsibility, so it lives here as a policy
object the Harness composes, alongside :class:`~app.ai.harness.policies.grounding_policy.GroundingPolicy`.

Two records are written for every AI-assisted request, whatever its outcome:

* ``AI_EXECUTION_COMPLETED`` - what ran, under which versions, at what measured cost
* ``AI_DECISION_RECORDED``  - the explainability record (§23): intent, the sources that
  were actually used, the guardrail and grounding outcomes, the final action category

Neither contains prompt text, retrieved document text, chain-of-thought or unredacted
PII: only identifiers, categories, versions and measurements.
"""

from __future__ import annotations

from app.ai.harness.decisions import HarnessExecutionRecord, HarnessOutcome
from app.core.audit.events import AuditAction, AuditResult, DecisionRecord, VersionStamp
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext


class EvidenceEmitter:
    """Writes the audit and explainability evidence for one Harness execution."""

    def __init__(self, audit: AuditService) -> None:
        self._audit = audit

    @staticmethod
    def _versions(record: HarnessExecutionRecord, *, with_capability: bool) -> VersionStamp:
        return VersionStamp(
            prompt_version=record.prompt_version,
            knowledge_corpus_version=record.knowledge_corpus_version,
            guardrail_policy_version=record.guardrail_policy_version,
            model_id=record.model_id,
            capability_version=record.capability_id if with_capability else None,
        )

    async def emit(self, ctx: RequestContext, record: HarnessExecutionRecord) -> None:
        await self._audit.record(
            ctx,
            AuditAction.AI_EXECUTION_COMPLETED,
            AuditResult.SUCCESS if record.outcome is HarnessOutcome.ALLOW else AuditResult.BLOCKED,
            reason_code=record.reason_code.value,
            versions=self._versions(record, with_capability=True),
            attributes={
                "executionMode": record.execution_mode,
                "agentId": record.agent_id,
                "modelCalls": record.model_calls,
                "agentSteps": record.agent_steps,
                "toolCalls": record.tool_calls,
                "inputTokens": record.input_tokens,
                "outputTokens": record.output_tokens,
                "tokensEstimated": record.tokens_estimated,
                "estimatedCost": record.estimated_cost,
                "latencyMs": round(record.latency_ms, 2),
                "resultCategory": record.result_category,
                "outcome": record.outcome.value,
                "groundingDecision": record.grounding_decision,
                "evidenceDocumentIds": list(record.evidence_document_ids),
                "errorType": record.error_type,
            },
        )
        await self._audit.record_decision(
            ctx,
            DecisionRecord(
                request_id=record.request_id,
                correlation_id=record.correlation_id,
                capability_id=record.capability_id,
                intent_selected=record.result_category,
                retrieved_source_ids=record.evidence_document_ids,
                tools_selected=list(record.tools_used),
                guardrail_result=record.guardrail_decision,
                grounding_result=record.grounding_decision,
                final_action_category=record.outcome.value,
                versions=self._versions(record, with_capability=False),
            ),
        )
