"""FAQ / Knowledge agent (master prompt §5.2, §6.3).

Deliberately thin. It owns *only*:

* which knowledge filters apply to this caller and domain
* the FAQ prompt
* the shape of the answer

Authorization, PII, injection policy, budgets, schema validation, output scanning,
audit and telemetry all belong to the Harness and are not re-implemented here (§5.7.3).

Cost profile: one model call, zero agent handoffs.
"""

from __future__ import annotations

from dataclasses import replace

from app.ai.agents.schemas import FaqAnswer
from app.ai.harness.decisions import HarnessOutcome, HarnessResult, ReasonCode
from app.ai.harness.service import AgentExecutionContext
from app.ai.models.provider import approx_tokens, user_message
from app.ai.prompts.registry import PromptRegistry
from app.core.logging.structured import get_logger
from app.core.observability.metrics import ABSTENTION_TOTAL, metrics
from app.core.observability.tracing import tracer
from app.core.security.cache import FaqCacheKey, TtlLruCache, is_cacheable
from app.core.security.guardrails import GUARDRAIL_POLICY_VERSION
from app.rag.governance.documents import Audience
from app.rag.retrieval.grounding import (
    ABSTENTION_MESSAGE,
    CONFLICT_MESSAGE,
    GroundingDecision,
    Verification,
)
from app.rag.retrieval.retriever import (
    HybridRetriever,
    RetrievalFilter,
    RetrievalResult,
    normalize_query,
)

logger = get_logger(__name__)

AGENT_ID = "faq_agent"

#: The model returns this exact token when the evidence does not support an answer.
INSUFFICIENT_MARKER = "INSUFFICIENT_EVIDENCE"


class FaqAgent:
    """Grounded FAQ answering with abstention."""

    def __init__(
        self,
        retriever: HybridRetriever,
        prompts: PromptRegistry,
        *,
        cache: TtlLruCache[dict] | None = None,
        cache_enabled: bool = True,
    ) -> None:
        self._retriever = retriever
        self._prompts = prompts
        self._cache = cache
        self._cache_enabled = cache_enabled

    async def handle(self, agent_ctx: AgentExecutionContext) -> HarnessResult:
        agent_ctx.agent_id = AGENT_ID
        question = agent_ctx.request.user_message
        domain = agent_ctx.request.payload.get("domain")
        product = agent_ctx.request.payload.get("product")
        audience = self._audience_for(agent_ctx)

        # ---- retrieve -------------------------------------------------------
        with tracer.span("faq.retrieve", domain=domain or "any"):
            retrieval = self._retriever.retrieve(
                question,
                RetrievalFilter(domain=domain, product=product, audience=audience),
            )
        agent_ctx.evidence_document_ids = [c.chunk.document_id for c in retrieval.chunks]
        # The Harness grounds the final answer against this evidence itself (§6.3, H-3).
        agent_ctx.retrieval = retrieval

        # ---- indirect injection check on retrieved content -------------------
        # Runs *before* the conflict/evidence gates so a poisoned document is reported as
        # an injection, never mislabelled as a version conflict (§12).
        for scored in retrieval.chunks:
            doc_guard = agent_ctx.guardrails.check_retrieved_document(scored.chunk.text)
            if doc_guard.blocked:
                logger.warning(
                    "retrieved_document_injection_blocked",
                    extra={"documentId": scored.chunk.document_id},
                )
                agent_ctx.result_category = "BLOCKED_INDIRECT_INJECTION"
                return HarnessResult(
                    HarnessOutcome.BLOCK,
                    ReasonCode.BLOCK_PROMPT_INJECTION,
                    ABSTENTION_MESSAGE,
                )

        # ---- validate evidence before spending a model call ------------------
        pre = agent_ctx.grounding.assess_evidence(retrieval)
        if pre is not None and pre.decision is GroundingDecision.ABSTAIN:
            agent_ctx.grounding_assessment = pre
            agent_ctx.result_category = "ABSTAINED"
            metrics.increment(ABSTENTION_TOTAL, labels={"stage": "pre_answer"})
            return self._abstain(retrieval, Verification.UNSUPPORTED, ABSTENTION_MESSAGE)
        if pre is not None and pre.decision is GroundingDecision.SURFACE_CONFLICT:
            agent_ctx.grounding_assessment = pre
            agent_ctx.result_category = "CONFLICTING_SOURCES"
            return self._abstain(
                retrieval,
                Verification.CONFLICTING,
                CONFLICT_MESSAGE,
                reason=ReasonCode.ABSTAIN_CONFLICTING_SOURCES,
            )

        # ---- cached answer text: saves the model call; grounding is still central ---
        cache_key = self._cache_key(question, domain, audience)
        cached = self._read_cache(agent_ctx, cache_key, retrieval)
        if cached is not None:
            return cached

        # ---- build the minimum context and call the model once ---------------
        template = self._prompts.get("faq.answer")
        built = agent_ctx.builder.build(
            system_prompt="",
            question=question,
            ledger=agent_ctx.ledger,
            retrieval=retrieval,
            purpose_fields=agent_ctx.request.purpose_fields,
        )
        agent_ctx.built_context = built
        # Ground against what the model actually received (§6.3, H-8): a chunk the
        # builder dropped for budget reasons must not be able to certify the answer.
        delivered = self._delivered_evidence(retrieval, built.evidence_chunk_ids)
        agent_ctx.retrieval = delivered
        agent_ctx.evidence_document_ids = [c.chunk.document_id for c in delivered.chunks]
        if not delivered.chunks:
            agent_ctx.result_category = "ABSTAINED"
            metrics.increment(ABSTENTION_TOTAL, labels={"stage": "evidence_over_budget"})
            return self._abstain(delivered, Verification.UNSUPPORTED, ABSTENTION_MESSAGE)

        # The template owns the EVIDENCE and QUESTION slots, so it receives the parts
        # rather than the builder's composed content: passing `user_content` here would
        # render the evidence twice and double the input tokens (§58.2, §58.8).
        rendered = template.render(evidence=built.evidence_block, question=built.question)
        # Budget is checked against what is actually sent to the provider.
        agent_ctx.ledger.check_context(approx_tokens(rendered))

        # Budget check and usage accounting happen inside the invoker, bound to this
        # request's ledger by the Harness (H-2): the agent cannot skip either.
        result = await agent_ctx.invoker.generate(
            [user_message(rendered)],
            system_prompt=None,
            capability_id=agent_ctx.capability.id,
            allow_fallback=False,
        )

        answer_text = result.text.strip()
        if INSUFFICIENT_MARKER in answer_text.upper():
            agent_ctx.result_category = "ABSTAINED"
            metrics.increment(ABSTENTION_TOTAL, labels={"stage": "model_declared"})
            return self._abstain(retrieval, Verification.UNSUPPORTED, ABSTENTION_MESSAGE)

        # The post-answer grounding decision, the verification class and the
        # contributing citations are all set centrally by the Harness (§5.7.3, H-3).
        # The agent proposes an answer plus candidate citations; it decides nothing.
        self._write_cache(agent_ctx, cache_key, answer_text)
        return self._proposed_answer(answer_text, delivered)

    @staticmethod
    def _delivered_evidence(retrieval: RetrievalResult, delivered_chunk_ids: list[str]) -> RetrievalResult:
        """The subset of the retrieval that was actually rendered into the prompt."""
        wanted = set(delivered_chunk_ids)
        kept = [c for c in retrieval.chunks if c.chunk.chunk_id in wanted]
        return replace(retrieval, chunks=kept)

    @staticmethod
    def _proposed_answer(answer_text: str, retrieval: RetrievalResult) -> HarnessResult:
        payload = FaqAnswer(
            answer=answer_text,
            # Placeholder: the Harness replaces this with the assessed class, or abstains.
            verification=Verification.UNSUPPORTED.value,
            citations=[
                {
                    "document_id": s.chunk.document_id,
                    "document_name": s.chunk.metadata.document_name,
                    "version": s.chunk.metadata.version,
                    "section": s.chunk.section or "",
                    "effective_date": s.chunk.metadata.effective_date.isoformat(),
                }
                for s in retrieval.chunks
            ],
        )
        return HarnessResult(
            HarnessOutcome.ALLOW, ReasonCode.OK, answer_text, payload.model_dump(mode="json")
        )

    # ------------------------------------------------------------- helpers ---
    @staticmethod
    def _audience_for(agent_ctx: AgentExecutionContext) -> Audience:
        """Anonymous callers never see agent- or internal-only knowledge (§13.1)."""
        actor = agent_ctx.ctx.auth.actor_type.value
        if actor == "AGENT":
            return Audience.AGENT
        if actor == "CUSTOMER":
            return Audience.CUSTOMER
        return Audience.PUBLIC

    @staticmethod
    def _abstain(
        retrieval: object,
        verification: Verification,
        message: str,
        reason: ReasonCode = ReasonCode.ABSTAIN_INSUFFICIENT_EVIDENCE,
    ) -> HarnessResult:
        payload = FaqAnswer(answer=message, verification=verification.value, citations=[])
        return HarnessResult(HarnessOutcome.ABSTAIN, reason, message, payload.model_dump(mode="json"))

    def _cache_key(self, question: str, domain: str | None, audience: Audience) -> FaqCacheKey:
        return FaqCacheKey(
            normalized_query=normalize_query(question),
            domain=domain or "any",
            corpus_version=self._retriever._corpus.version,
            prompt_version=self._prompts.get("faq.answer").version,
            guardrail_policy_version=GUARDRAIL_POLICY_VERSION,
            language="en",
            audience=audience.value,
        )

    def _read_cache(
        self, agent_ctx: AgentExecutionContext, key: FaqCacheKey, retrieval: RetrievalResult
    ) -> HarnessResult | None:
        """Only public, non-customer-specific answers are ever served from cache (§18.1).

        The cache holds the *model's answer text* only. Evidence is retrieved afresh
        (cheap, local, version-keyed) and the Harness grounds the cached text against it
        exactly as it would a fresh answer, so caching never bypasses grounding.
        """
        if not (self._cache_enabled and self._cache is not None):
            return None
        if not is_cacheable(
            contains_customer_data=False,
            is_authenticated_scope=agent_ctx.ctx.auth.is_authenticated,
        ):
            return None
        entry = self._cache.get(key.render())
        if entry is None:
            return None
        agent_ctx.agent_id = AGENT_ID
        agent_ctx.result_category = "ANSWERED_CACHED"
        return self._proposed_answer(str(entry["answer"]), retrieval)

    def _write_cache(self, agent_ctx: AgentExecutionContext, key: FaqCacheKey, answer_text: str) -> None:
        if not (self._cache_enabled and self._cache is not None):
            return
        if agent_ctx.ctx.auth.is_authenticated:
            return
        self._cache.set(key.render(), {"answer": answer_text})
