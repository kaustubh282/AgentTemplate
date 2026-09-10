"""Ragas RAG evaluation (master prompt §26.1, §26.5).

Two things happen here.

1. **Answer/context quality** on the golden datasets. Ragas metrics that need no
   judge model run offline and produce real numbers. Metrics that require an
   LLM judge run only when one is configured; otherwise they are reported as
   ``NOT_EVIDENCED`` with the exact reason, never fabricated (§26.12, §54.4).

2. **Retrieval experiment comparison** (§26.1). Each candidate configuration is
   measured for quality *and* for latency, retrieved tokens, context tokens and cost,
   because a configuration that scores marginally better while costing more latency
   and tokens is not automatically the right choice (§6.4).

The application never imports this module; the dependency points one way (§26.11).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

#: Ragas metrics that are computable without any model call.
OFFLINE_METRIC_NAMES = (
    "non_llm_string_similarity",
    "bleu_score",
    "rouge_score",
    "exact_match",
    "string_presence",
)

#: Ragas metrics that require a judge LLM (and, for some, an embedding model).
JUDGE_METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
    "factual_correctness",
    "semantic_similarity",
    "noise_sensitivity",
)


def judge_available() -> tuple[bool, str]:
    """Whether an LLM judge is configured. Returns (available, reason)."""
    if os.environ.get("RAGAS_JUDGE_DISABLED", "").lower() in ("1", "true", "yes"):
        return False, "RAGAS_JUDGE_DISABLED is set"
    key = os.environ.get("RAGAS_JUDGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return False, (
            "no judge credentials found (set RAGAS_JUDGE_API_KEY or OPENAI_API_KEY "
            "plus RAGAS_JUDGE_MODEL) - LLM-as-judge metrics are NOT_EVIDENCED"
        )
    if not os.environ.get("RAGAS_JUDGE_MODEL"):
        return False, "RAGAS_JUDGE_MODEL is not set - LLM-as-judge metrics are NOT_EVIDENCED"
    return True, "judge configured"


@dataclass
class MetricSummary:
    name: str
    status: str  # EVIDENCED | NOT_EVIDENCED
    mean: float | None = None
    median: float | None = None
    p95: float | None = None
    count: int = 0
    threshold: float | None = None
    passed: bool | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.name,
            "status": self.status,
            "mean": self.mean,
            "median": self.median,
            "p95": self.p95,
            "count": self.count,
            "threshold": self.threshold,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass
class RetrievalExperiment:
    """One candidate retrieval configuration and its full cost/quality profile."""

    name: str
    description: str
    recall_at_k: float = 0.0
    precision_at_k: float = 0.0
    mrr: float = 0.0
    abstention_correctness: float = 0.0
    zero_result_rate: float = 0.0
    stale_source_hits: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    retrieved_tokens_avg: float = 0.0
    context_tokens_avg: float = 0.0
    utilization_ratio_avg: float = 0.0
    estimated_input_tokens_avg: float = 0.0
    estimated_cost_per_1000_faq: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "configuration": self.name,
            "description": self.description,
            "quality": {
                "recallAtK": self.recall_at_k,
                "precisionAtK": self.precision_at_k,
                "mrr": self.mrr,
                "abstentionCorrectness": self.abstention_correctness,
                "zeroResultRate": self.zero_result_rate,
                "staleSourceHits": self.stale_source_hits,
            },
            "cost": {
                "latencyP50Ms": self.latency_p50_ms,
                "latencyP95Ms": self.latency_p95_ms,
                "latencyP99Ms": self.latency_p99_ms,
                "retrievedTokensAvg": self.retrieved_tokens_avg,
                "contextTokensAvg": self.context_tokens_avg,
                "retrievalUtilizationRatio": self.utilization_ratio_avg,
                "estimatedInputTokensAvg": self.estimated_input_tokens_avg,
                "estimatedCostPer1000Faq": self.estimated_cost_per_1000_faq,
            },
        }


@dataclass
class RagasReport:
    dataset_version: str
    framework_version: str
    judge_status: str
    judge_reason: str
    metrics: list[MetricSummary] = field(default_factory=list)
    experiments: list[RetrievalExperiment] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    environment: str = "test"
    model_id: str = ""
    prompt_version: str = ""
    corpus_version: str = ""
    command: str = "python -m evals.ragas.runners.rag_eval"

    @property
    def verdict(self) -> str:
        evidenced = [m for m in self.metrics if m.status == "EVIDENCED" and m.passed is not None]
        if not evidenced:
            return "NOT_EVIDENCED"
        return "PASS" if all(m.passed for m in evidenced) else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": "ragas",
            "frameworkVersion": self.framework_version,
            "generatedAt": self.generated_at,
            "command": self.command,
            "environment": self.environment,
            "datasetVersion": self.dataset_version,
            "modelId": self.model_id,
            "promptVersion": self.prompt_version,
            "corpusVersion": self.corpus_version,
            "judge": {"status": self.judge_status, "reason": self.judge_reason},
            "thresholds": self.thresholds,
            "metrics": [m.to_dict() for m in self.metrics],
            "retrievalExperiments": [e.to_dict() for e in self.experiments],
            "verdict": self.verdict,
        }


# ------------------------------------------------------------------ thresholds ---
#: Configurable release gates (§59.7): thresholds are configuration, not magic claims.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "non_llm_string_similarity": 0.45,
    "rouge_score": 0.25,
    "bleu_score": 0.05,
    "abstention_exact_match": 0.95,
    "retrieval_recall_at_k": 0.90,
    "citation_correctness": 0.95,
    "hallucination_rate_max": 0.05,
    # LLM-as-judge gates; only evaluated when a judge is configured (see judge_available).
    "faithfulness": 0.80,
    "answer_relevancy": 0.70,
    "context_precision": 0.70,
    "context_recall": 0.70,
}

#: The shipped default retrieval configuration; the recall@k gate applies to it.
DEFAULT_RETRIEVAL_VARIANT = "hybrid_rrf"

#: Judge metrics this runner actually wires when credentials are present.
WIRED_JUDGE_METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def thresholds_from_env() -> dict[str, float]:
    resolved = dict(DEFAULT_THRESHOLDS)
    for key in resolved:
        env_key = f"RAGAS_THRESHOLD_{key.upper()}"
        if env_key in os.environ:
            resolved[key] = float(os.environ[env_key])
    return resolved


def _summarize(
    name: str, values: list[float], threshold: float | None, *, higher_is_better: bool = True
) -> MetricSummary:
    if not values:
        return MetricSummary(
            name=name, status="NOT_EVIDENCED", reason="no cases produced a value for this metric"
        )
    ordered = sorted(values)
    rank = (len(ordered) - 1) * 0.95
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    p95 = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    mean = statistics.fmean(values)
    passed = None
    if threshold is not None:
        passed = mean >= threshold if higher_is_better else mean <= threshold
    return MetricSummary(
        name=name,
        status="EVIDENCED",
        mean=round(mean, 4),
        median=round(statistics.median(values), 4),
        p95=round(p95, 4),
        count=len(values),
        threshold=threshold,
        passed=passed,
    )


# =============================== answer quality ===============================
async def evaluate_answer_quality(container: Any, thresholds: dict[str, float]) -> list[MetricSummary]:
    """Run the offline Ragas metrics over the FAQ golden dataset."""
    from ragas.metrics.collections import (
        BleuScore,
        ExactMatch,
        NonLLMStringSimilarity,
        RougeScore,
    )

    from app.ai.harness.decisions import HarnessOutcome
    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from evals.datasets.schema import load_jsonl
    from evals.native.suites import context_for

    similarity = NonLLMStringSimilarity()
    bleu = BleuScore()
    rouge = RougeScore()
    exact = ExactMatch()

    dataset = load_jsonl("faq-golden.jsonl")

    grounded_similarity: list[float] = []
    grounded_bleu: list[float] = []
    grounded_rouge: list[float] = []
    abstention_match: list[float] = []
    citation_correct: list[float] = []
    hallucinations: list[float] = []

    for case in dataset.cases:
        result = await container.harness.execute(
            context_for(case.actor, conversation_id=f"conv_ragas_{case.case_id}"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message=case.input,
                payload={"question": case.input},
            ),
        )
        answer = result.message
        reference = case.reference_answer or ""

        is_abstention = "abstention" in case.tags
        if is_abstention:
            # Abstention is a *behavioural* property: the answer must decline.
            declined = result.outcome is HarnessOutcome.ABSTAIN
            abstention_match.append(1.0 if declined else 0.0)
            # A refusal must not smuggle in an unsupported claim.
            hallucinations.append(
                0.0
                if declined and not any(f.lower() in answer.lower() for f in case.forbidden_contains)
                else 1.0
            )
            continue

        if reference:
            grounded_similarity.append(float((await similarity.ascore(reference, answer)).value))
            grounded_bleu.append(float((await bleu.ascore(reference, answer)).value))
            grounded_rouge.append(float((await rouge.ascore(reference, answer)).value))

        cited = {c["document_id"] for c in (result.payload or {}).get("citations", [])}
        expected = set(case.expected_sources)
        citation_correct.append(1.0 if expected and expected <= cited else 0.0)
        hallucinations.append(
            1.0 if any(f.lower() in answer.lower() for f in case.forbidden_contains) else 0.0
        )

    # ExactMatch is used for the canonical abstention wording so a drift in the
    # user-facing refusal text is caught rather than silently accepted.
    from app.rag.retrieval.grounding import ABSTENTION_MESSAGE

    abstention_wording: list[float] = []
    for case in dataset.cases:
        if "abstention" not in case.tags:
            continue
        result = await container.harness.execute(
            context_for(case.actor, conversation_id=f"conv_ragas_word_{case.case_id}"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message=case.input,
                payload={"question": case.input},
            ),
        )
        abstention_wording.append(float((await exact.ascore(ABSTENTION_MESSAGE, result.message)).value))

    return [
        _summarize(
            "non_llm_string_similarity",
            grounded_similarity,
            thresholds.get("non_llm_string_similarity"),
        ),
        _summarize("rouge_score", grounded_rouge, thresholds.get("rouge_score")),
        _summarize("bleu_score", grounded_bleu, thresholds.get("bleu_score")),
        _summarize("abstention_correctness", abstention_match, thresholds.get("abstention_exact_match")),
        _summarize("abstention_wording_exact_match", abstention_wording, None),
        _summarize("citation_correctness", citation_correct, thresholds.get("citation_correctness")),
        _summarize(
            "hallucination_rate",
            hallucinations,
            thresholds.get("hallucination_rate_max"),
            higher_is_better=False,
        ),
    ]


def judge_metric_placeholders(reason: str) -> list[MetricSummary]:
    """Record judge-dependent metrics honestly rather than inventing a score."""
    return [MetricSummary(name=name, status="NOT_EVIDENCED", reason=reason) for name in JUDGE_METRIC_NAMES]


# ========================= retrieval experiment matrix =========================
def _build_retriever(corpus: Any, variant: str, settings: Any) -> Any:
    """Construct one candidate configuration (§26.1 comparison matrix)."""
    from app.rag.retrieval.retriever import HashingEmbedder, HybridRetriever, LexicalOverlapReranker

    common = {
        "top_k": settings.rag_top_k,
        "candidate_k": settings.rag_candidate_k,
        "min_score": settings.rag_min_score,
        "max_retrieval_tokens": settings.max_retrieval_tokens,
    }

    if variant == "bm25_only":
        retriever = HybridRetriever(corpus, **common)
        retriever._vector_search = lambda query, eligible: []
        return retriever
    if variant == "dense_only":
        retriever = HybridRetriever(corpus, **common)
        retriever._bm25 = lambda query, eligible: []
        return retriever
    if variant == "hybrid_rrf":
        return HybridRetriever(corpus, **common)
    if variant == "hybrid_rrf_reranker":
        return HybridRetriever(corpus, reranker=LexicalOverlapReranker(), **common)
    if variant == "hybrid_topk_8":
        return HybridRetriever(corpus, **{**common, "top_k": 8})
    if variant == "hybrid_wide_embedding":
        return HybridRetriever(corpus, embedder=HashingEmbedder(8192), **common)
    raise ValueError(f"unknown retrieval variant: {variant}")


VARIANTS = [
    ("bm25_only", "Lexical BM25 only."),
    ("dense_only", "Hashed dense vector only."),
    ("hybrid_rrf", "BM25 + dense fused with RRF (the shipped default)."),
    ("hybrid_rrf_reranker", "Hybrid + RRF + lexical-overlap reranker."),
    ("hybrid_topk_8", "Hybrid + RRF with top_k raised from 4 to 8."),
    ("hybrid_wide_embedding", "Hybrid + RRF with a 8192-dimension embedding."),
]


def run_retrieval_experiments(container: Any) -> list[RetrievalExperiment]:
    from app.ai.models.provider import approx_tokens
    from app.rag.governance.documents import Audience
    from app.rag.retrieval.grounding import GroundingDecision
    from app.rag.retrieval.retriever import RetrievalFilter
    from evals.datasets.schema import load_jsonl

    retrieval_cases = load_jsonl("retrieval-golden.jsonl").cases
    faq_cases = load_jsonl("faq-golden.jsonl").cases
    positives = [c for c in retrieval_cases if c.expected_sources]
    abstentions = [c for c in faq_cases if "abstention" in c.tags]

    settings = container.settings
    pricing_in = settings.model_input_cost_per_1k
    pricing_out = settings.model_output_cost_per_1k
    system_prompt_tokens = container.prompts.get("faq.answer").approx_tokens

    experiments: list[RetrievalExperiment] = []
    for variant, description in VARIANTS:
        retriever = _build_retriever(container.corpus, variant, settings)
        experiment = RetrievalExperiment(name=variant, description=description)

        latencies: list[float] = []
        recalls: list[float] = []
        precisions: list[float] = []
        reciprocal_ranks: list[float] = []
        retrieved_tokens: list[float] = []
        context_tokens: list[float] = []
        utilizations: list[float] = []
        zero_results = 0
        stale_hits = 0

        for case in positives:
            started = time.perf_counter()
            result = retriever.retrieve(case.input, RetrievalFilter(audience=Audience.PUBLIC))
            latencies.append((time.perf_counter() - started) * 1000)

            retrieved_docs = [c.chunk.document_id for c in result.chunks]
            expected = set(case.expected_sources)
            hit = bool(expected & set(retrieved_docs))
            recalls.append(1.0 if hit else 0.0)
            precisions.append(
                (sum(1 for d in retrieved_docs if d in expected) / len(retrieved_docs))
                if retrieved_docs
                else 0.0
            )
            rank = next((i for i, d in enumerate(retrieved_docs, start=1) if d in expected), 0)
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)

            retrieved_tokens.append(result.retrieved_tokens)
            context_tokens.append(result.context_tokens)
            utilizations.append(result.utilization_ratio)
            if result.zero_result:
                zero_results += 1
            stale_hits += sum(
                1
                for c in result.chunks
                if c.chunk.metadata.status.value in ("SUPERSEDED", "REVOKED", "DRAFT")
            )

        # Abstention correctness: an unsupported question must fail the evidence gate.
        correct_abstentions = 0
        for case in abstentions:
            result = retriever.retrieve(case.input, RetrievalFilter(audience=Audience.PUBLIC))
            assessment = container.grounding.assess_evidence(result)
            if assessment is not None and assessment.decision is GroundingDecision.ABSTAIN:
                correct_abstentions += 1
            if result.zero_result:
                zero_results += 1

        total_queries = len(positives) + len(abstentions)
        experiment.recall_at_k = round(statistics.fmean(recalls), 4) if recalls else 0.0
        experiment.precision_at_k = round(statistics.fmean(precisions), 4) if precisions else 0.0
        experiment.mrr = round(statistics.fmean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0
        experiment.abstention_correctness = (
            round(correct_abstentions / len(abstentions), 4) if abstentions else 0.0
        )
        experiment.zero_result_rate = round(zero_results / total_queries, 4) if total_queries else 0.0
        experiment.stale_source_hits = stale_hits

        ordered = sorted(latencies)
        experiment.latency_p50_ms = round(statistics.median(ordered), 3) if ordered else 0.0
        experiment.latency_p95_ms = (
            round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 3) if ordered else 0.0
        )
        experiment.latency_p99_ms = (
            round(ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)], 3) if ordered else 0.0
        )
        experiment.retrieved_tokens_avg = (
            round(statistics.fmean(retrieved_tokens), 1) if retrieved_tokens else 0.0
        )
        experiment.context_tokens_avg = round(statistics.fmean(context_tokens), 1) if context_tokens else 0.0
        experiment.utilization_ratio_avg = round(statistics.fmean(utilizations), 4) if utilizations else 0.0

        # Cost is what the *model* would be charged for this retrieval configuration.
        estimated_input = (
            system_prompt_tokens
            + experiment.context_tokens_avg
            + approx_tokens("QUESTION: a representative insurance question")
        )
        experiment.estimated_input_tokens_avg = round(estimated_input, 1)
        experiment.estimated_cost_per_1000_faq = round(
            (estimated_input / 1000.0) * pricing_in * 1000 + (250 / 1000.0) * pricing_out * 1000,
            4,
        )
        experiments.append(experiment)

    return experiments


def print_experiment_table(experiments: list[RetrievalExperiment]) -> None:
    print("\nRETRIEVAL EXPERIMENT COMPARISON (§26.1)")
    header = (
        f"{'configuration':<24}{'recall':>8}{'prec':>7}{'mrr':>7}{'abst':>7}"
        f"{'p95ms':>8}{'ctxTok':>8}{'util':>7}{'$/1k':>8}"
    )
    print(header)
    print("-" * len(header))
    for e in experiments:
        print(
            f"{e.name:<24}{e.recall_at_k:>8.3f}{e.precision_at_k:>7.3f}{e.mrr:>7.3f}"
            f"{e.abstention_correctness:>7.3f}{e.latency_p95_ms:>8.2f}"
            f"{e.context_tokens_avg:>8.0f}{e.utilization_ratio_avg:>7.3f}"
            f"{e.estimated_cost_per_1000_faq:>8.2f}"
        )
    print(
        "\nA more complex configuration is adopted only when the quality gain justifies\n"
        "its latency and token cost (§6.4). The shipped default is hybrid_rrf."
    )


# ==================================== main ====================================
async def run() -> RagasReport:
    import ragas

    from app.bootstrap import build_container
    from tests.conftest import default_responder, make_settings

    settings = make_settings()
    container = build_container(settings, responder=default_responder())

    thresholds = thresholds_from_env()
    available, reason = judge_available()

    report = RagasReport(
        dataset_version="1.0.0",
        framework_version=ragas.__version__,
        judge_status="AVAILABLE" if available else "UNAVAILABLE",
        judge_reason=reason,
        thresholds=thresholds,
        environment=settings.app_env.value,
        model_id=settings.model_id,
        prompt_version=container.prompts.combined_version(),
        corpus_version=container.corpus.version,
    )

    report.metrics.extend(await evaluate_answer_quality(container, thresholds))
    if available:
        report.metrics.extend(await _run_judge_metrics(container, thresholds))
    else:
        report.metrics.extend(judge_metric_placeholders(reason))

    report.experiments = run_retrieval_experiments(container)
    report.metrics.append(retrieval_recall_gate(report.experiments, thresholds))
    return report


def retrieval_recall_gate(
    experiments: list[RetrievalExperiment], thresholds: dict[str, float]
) -> MetricSummary:
    """Apply ``retrieval_recall_at_k`` to the shipped default configuration's recall.

    The experiment matrix is only informative unless something fails on it; this is
    what makes the recall threshold a release gate rather than a printed number.
    """
    default = next((e for e in experiments if e.name == DEFAULT_RETRIEVAL_VARIANT), None)
    threshold = thresholds.get("retrieval_recall_at_k")
    if default is None:
        return MetricSummary(
            name="retrieval_recall_at_k",
            status="NOT_EVIDENCED",
            threshold=threshold,
            reason=f"no experiment named {DEFAULT_RETRIEVAL_VARIANT} was run",
        )
    return MetricSummary(
        name="retrieval_recall_at_k",
        status="EVIDENCED",
        mean=default.recall_at_k,
        median=default.recall_at_k,
        p95=default.recall_at_k,
        count=1,
        threshold=threshold,
        passed=None if threshold is None else default.recall_at_k >= threshold,
        reason=f"recall@k of the shipped default ({DEFAULT_RETRIEVAL_VARIANT})",
    )


def _unwired_judge_placeholders() -> list[MetricSummary]:
    return [
        MetricSummary(
            name=name,
            status="NOT_EVIDENCED",
            reason="not wired in this runner; only faithfulness, answer_relevancy, context_precision "
            "and context_recall are evaluated by the configured judge",
        )
        for name in JUDGE_METRIC_NAMES
        if name not in WIRED_JUDGE_METRIC_NAMES
    ]


async def _run_judge_metrics(container: Any, thresholds: dict[str, float]) -> list[MetricSummary]:
    """Run the LLM-as-judge metrics with the configured judge (§26.5, §54.4).

    Configuration (environment):

    * ``RAGAS_JUDGE_API_KEY`` or ``OPENAI_API_KEY`` - credentials (required)
    * ``RAGAS_JUDGE_MODEL`` - judge model name (required)
    * ``RAGAS_JUDGE_PROVIDER`` - ragas provider name, default ``openai``
    * ``RAGAS_JUDGE_BASE_URL`` - OpenAI-compatible endpoint override (optional)
    * ``RAGAS_JUDGE_EMBEDDING_MODEL`` - embeddings for answer_relevancy,
      default ``text-embedding-3-small``
    * ``RAGAS_JUDGE_TIMEOUT_S`` - per-call timeout, default 90

    Every score reported here came back from the judge; a case the judge could not
    score is counted and explained, never filled in.
    """
    model = os.environ.get("RAGAS_JUDGE_MODEL", "")
    key = os.environ.get("RAGAS_JUDGE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    provider = os.environ.get("RAGAS_JUDGE_PROVIDER", "openai")
    base_url = os.environ.get("RAGAS_JUDGE_BASE_URL") or None
    embedding_model = os.environ.get("RAGAS_JUDGE_EMBEDDING_MODEL", "text-embedding-3-small")
    timeout_s = float(os.environ.get("RAGAS_JUDGE_TIMEOUT_S", "90"))

    def unavailable(reason: str) -> list[MetricSummary]:
        return [
            MetricSummary(name=name, status="NOT_EVIDENCED", reason=reason)
            for name in WIRED_JUDGE_METRIC_NAMES
        ] + _unwired_judge_placeholders()

    try:
        from openai import AsyncOpenAI
        from ragas.embeddings.base import BaseRagasEmbedding, embedding_factory
        from ragas.llms import llm_factory
        from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness
    except ImportError as exc:  # pragma: no cover - depends on the installed extras
        return unavailable(f"judge dependencies unavailable: {exc}")

    try:
        client = AsyncOpenAI(api_key=key, base_url=base_url)
        judge = llm_factory(model, provider=provider, client=client)
        embeddings = embedding_factory(provider, model=embedding_model, client=client)
        if not isinstance(embeddings, BaseRagasEmbedding):
            raise TypeError(f"embedding_factory returned a legacy interface ({type(embeddings).__name__})")
        faithfulness = Faithfulness(llm=judge)
        relevancy = AnswerRelevancy(llm=judge, embeddings=embeddings)
        precision = ContextPrecision(llm=judge)
        recall = ContextRecall(llm=judge)
    except Exception as exc:
        return unavailable(f"judge construction failed: {type(exc).__name__}: {exc}")

    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from app.rag.governance.documents import Audience
    from app.rag.retrieval.retriever import RetrievalFilter
    from evals.datasets.schema import load_jsonl
    from evals.native.suites import context_for

    values: dict[str, list[float]] = {name: [] for name in WIRED_JUDGE_METRIC_NAMES}
    failures: dict[str, list[str]] = {name: [] for name in WIRED_JUDGE_METRIC_NAMES}
    skipped_no_context = 0

    async def score(name: str, case_id: str, coroutine: Any) -> None:
        try:
            result = await asyncio.wait_for(coroutine, timeout=timeout_s)
            values[name].append(float(result.value))
        except Exception as exc:
            failures[name].append(f"{case_id}: {type(exc).__name__}: {exc}")

    for case in load_jsonl("faq-golden.jsonl").cases:
        if "abstention" in case.tags:
            continue  # abstention is measured behaviourally by the offline metrics
        result = await container.harness.execute(
            context_for(case.actor, conversation_id=f"conv_ragas_judge_{case.case_id}"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ, user_message=case.input, payload={"question": case.input}
            ),
        )
        retrieval = container.retriever.retrieve(case.input, RetrievalFilter(audience=Audience.PUBLIC))
        contexts = [scored.chunk.text for scored in retrieval.chunks]
        if not contexts:
            skipped_no_context += 1
            continue
        answer = result.message
        reference = case.reference_answer or ""

        await score(
            "faithfulness",
            case.case_id,
            faithfulness.ascore(user_input=case.input, response=answer, retrieved_contexts=contexts),
        )
        await score(
            "answer_relevancy", case.case_id, relevancy.ascore(user_input=case.input, response=answer)
        )
        if reference:
            await score(
                "context_precision",
                case.case_id,
                precision.ascore(user_input=case.input, reference=reference, retrieved_contexts=contexts),
            )
            await score(
                "context_recall",
                case.case_id,
                recall.ascore(user_input=case.input, retrieved_contexts=contexts, reference=reference),
            )

    summaries: list[MetricSummary] = []
    for name in WIRED_JUDGE_METRIC_NAMES:
        notes: list[str] = [f"judge={provider}/{model}"]
        if failures[name]:
            notes.append(f"{len(failures[name])} case(s) not scored: {'; '.join(failures[name][:3])}")
        if skipped_no_context:
            notes.append(f"{skipped_no_context} case(s) skipped with no retrieved context")
        if not values[name]:
            summaries.append(
                MetricSummary(
                    name=name,
                    status="NOT_EVIDENCED",
                    threshold=thresholds.get(name),
                    reason="the judge returned no score for any case; " + "; ".join(notes),
                )
            )
            continue
        summary = _summarize(name, values[name], thresholds.get(name))
        summary.reason = "; ".join(notes)
        summaries.append(summary)
    return summaries + _unwired_judge_placeholders()


def print_report(report: RagasReport) -> None:
    print("\n" + "=" * 78)
    print("RAGAS RAG EVALUATION (master prompt §26.1, §26.5)")
    print("=" * 78)
    print(f"ragas             : {report.framework_version}")
    print(f"dataset version   : {report.dataset_version}")
    print(f"environment       : {report.environment}")
    print(f"model             : {report.model_id}")
    print(f"prompt version    : {report.prompt_version}")
    print(f"corpus version    : {report.corpus_version}")
    print(f"judge             : {report.judge_status} - {report.judge_reason}")

    print(f"\n{'metric':<36}{'status':<15}{'mean':>8}{'thresh':>9}{'result':>9}")
    print("-" * 78)
    for metric in report.metrics:
        mean = f"{metric.mean:.3f}" if metric.mean is not None else "-"
        threshold = f"{metric.threshold:.3f}" if metric.threshold is not None else "-"
        verdict = "-" if metric.passed is None else ("PASS" if metric.passed else "FAIL")
        print(f"{metric.name:<36}{metric.status:<15}{mean:>8}{threshold:>9}{verdict:>9}")
        if metric.status == "NOT_EVIDENCED" and metric.reason:
            print(f"    reason: {metric.reason}")

    print_experiment_table(report.experiments)
    print(f"\nVERDICT: {report.verdict}")


def write_report(report: RagasReport, filename: str = "ragas-report.json") -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / filename
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path


def main() -> int:
    report = asyncio.run(run())
    print_report(report)
    path = write_report(report)
    print(f"report written: {path}")
    return 0 if report.verdict in ("PASS", "NOT_EVIDENCED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
