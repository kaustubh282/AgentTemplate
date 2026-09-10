"""Hybrid retrieval with metadata filtering and bounded context (§6.2, §6.4, §58.8).

The default pipeline is the simplest explainable candidate the master prompt asks to
benchmark first:

    query normalisation -> lifecycle/metadata filters -> BM25 + vector -> RRF
    -> bounded top-k -> optional reranker (off by default) -> token budget

Reranking, query decomposition, multi-query and agentic retrieval stay *off* until
Ragas plus latency/cost evidence justifies them. Each stage reports its incremental
cost so that comparison is possible.

The default embedding is a dependency-free hashed bag-of-words vector. It is a real
dense channel for the hybrid, and it is swappable: any object implementing
:class:`Embedder` can replace it without touching the retriever.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from app.core.observability.metrics import (
    RERANKER_LATENCY_MS,
    RETRIEVAL_DOCS,
    RETRIEVAL_LATENCY_MS,
    RETRIEVAL_TOKENS,
    RETRIEVAL_ZERO_RESULT_TOTAL,
    STALE_DOCUMENT_HITS_TOTAL,
    metrics,
)
from app.rag.governance.documents import Audience, Chunk, DocumentStatus, KnowledgeCorpus, today_utc

_TOKEN = re.compile(r"[a-z0-9]+")

#: Very small stop list; enough to stop common words dominating BM25 on short queries.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
    }
)


def normalize_query(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation noise (§6.2)."""
    return " ".join(_TOKEN.findall(text.lower()))


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


class Embedder(Protocol):
    def embed(self, text: str) -> dict[int, float]: ...
    @property
    def name(self) -> str: ...


class HashingEmbedder:
    """Deterministic hashed bag-of-words embedding with L2 normalisation.

    Dependency-free and offline-reproducible, which keeps evals deterministic. Swap
    for a real embedding service by implementing :class:`Embedder`.
    """

    def __init__(self, dimensions: int = 2048) -> None:
        self._dims = dimensions

    @property
    def name(self) -> str:
        return f"hashing-{self._dims}"

    def embed(self, text: str) -> dict[int, float]:
        counts = Counter(tokenize(text))
        if not counts:
            return {}
        vector: dict[int, float] = {}
        for token, count in counts.items():
            index = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self._dims  # noqa: S324
            vector[index] = vector.get(index, 0.0) + (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        return {k: v / norm for k, v in vector.items()}


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(value * b.get(index, 0.0) for index, value in a.items())


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """Metadata filters applied *before* scoring (§6.2, §26.1)."""

    domain: str | None = None
    product: str | None = None
    audience: Audience = Audience.PUBLIC
    language: str = "en"
    allow_draft: bool = False
    document_types: frozenset[str] = frozenset()


@dataclass(slots=True)
class ScoredChunk:
    chunk: Chunk
    #: Fused RRF score, used for *ranking*. Rank-based, so the best hit is always 1.0.
    score: float
    #: Absolute relevance in [0, 1]: IDF-weighted coverage of the query's terms by this
    #: chunk. Unlike ``score`` this is comparable against a threshold, so it - not the
    #: RRF score - is what the evidence/abstention gate reads (§6.3, §7).
    relevance: float = 0.0
    lexical_rank: int | None = None
    vector_rank: int | None = None


@dataclass(slots=True)
class RetrievalResult:
    """Retrieved evidence plus the measurements the RAG gates require."""

    chunks: list[ScoredChunk] = field(default_factory=list)
    latency_ms: float = 0.0
    reranker_latency_ms: float = 0.0
    candidates_considered: int = 0
    retrieved_tokens: int = 0
    context_tokens: int = 0
    zero_result: bool = False
    stale_filtered: int = 0
    corpus_version: str = "empty"
    embedder: str = ""
    #: Documents that disagree on the same question, surfaced not silently resolved.
    conflicting_document_ids: list[str] = field(default_factory=list)
    #: Candidates refused because one chunk alone exceeds the retrieval budget. Such a
    #: chunk would be cut by the context builder, so admitting it would let grounding
    #: certify against evidence the model never saw (H-8).
    dropped_oversized: int = 0
    #: Candidates removed as exact duplicates of a higher-ranked chunk.
    deduplicated: int = 0

    @property
    def utilization_ratio(self) -> float:
        """context_tokens / retrieved_tokens - how much retrieval was actually used."""
        return round(self.context_tokens / self.retrieved_tokens, 4) if self.retrieved_tokens else 0.0

    @property
    def top_score(self) -> float:
        """Best *absolute* relevance. This is the evidence signal, not the RRF rank."""
        return max((c.relevance for c in self.chunks), default=0.0)

    @property
    def top_rank_score(self) -> float:
        """Best fused RRF score, for ranking diagnostics only."""
        return self.chunks[0].score if self.chunks else 0.0


class Reranker(Protocol):
    def rerank(self, query: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]: ...


class LexicalOverlapReranker:
    """Optional, off by default. Scores exact term coverage of the query."""

    def rerank(self, query: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]:
        terms = set(tokenize(query))
        if not terms:
            return chunks
        for scored in chunks:
            coverage = len(terms & set(tokenize(scored.chunk.text))) / len(terms)
            scored.score = round(0.6 * scored.score + 0.4 * coverage, 6)
        return sorted(chunks, key=lambda s: s.score, reverse=True)


class HybridRetriever:
    """BM25 + dense retrieval fused with Reciprocal Rank Fusion."""

    def __init__(
        self,
        corpus: KnowledgeCorpus,
        *,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        top_k: int = 4,
        candidate_k: int = 12,
        min_score: float = 0.12,
        max_retrieval_tokens: int = 1_400,
        rrf_k: int = 60,
        bm25_k1: float = 1.5,
        bm25_b: float = 0.75,
    ) -> None:
        self._corpus = corpus
        self._embedder = embedder or HashingEmbedder()
        self._reranker = reranker
        self._top_k = top_k
        self._candidate_k = candidate_k
        self._min_score = min_score
        self._max_retrieval_tokens = max_retrieval_tokens
        self._rrf_k = rrf_k
        self._k1 = bm25_k1
        self._b = bm25_b
        self._index_version = ""
        self._chunks: list[Chunk] = []
        self._doc_tokens: list[list[str]] = []
        self._doc_freq: Counter[str] = Counter()
        self._avg_len = 0.0
        self._vectors: list[dict[int, float]] = []

    # --------------------------------------------------------------- index ---
    def _ensure_index(self, allow_draft: bool) -> None:
        # The date is part of the key so a document whose effective date arrives at
        # midnight enters the index without any other corpus change.
        version = f"{self._corpus.version}:{int(allow_draft)}:{today_utc().isoformat()}"
        if version == self._index_version:
            return
        self._chunks = self._corpus.active_chunks(allow_draft=allow_draft)
        self._doc_tokens = [tokenize(c.text) for c in self._chunks]
        self._doc_freq = Counter()
        for tokens in self._doc_tokens:
            self._doc_freq.update(set(tokens))
        self._avg_len = (
            sum(len(t) for t in self._doc_tokens) / len(self._doc_tokens) if self._doc_tokens else 0.0
        )
        self._vectors = [self._embedder.embed(c.text) for c in self._chunks]
        self._index_version = version

    # ------------------------------------------------------------- retrieve ---
    def retrieve(self, query: str, filters: RetrievalFilter | None = None) -> RetrievalResult:
        started = time.perf_counter()
        filters = filters or RetrievalFilter()
        self._ensure_index(filters.allow_draft)

        normalized = normalize_query(query)
        result = RetrievalResult(corpus_version=self._corpus.version, embedder=self._embedder.name)

        eligible = [i for i, chunk in enumerate(self._chunks) if self._passes(chunk, filters)]
        result.stale_filtered = self._count_stale()
        if result.stale_filtered:
            metrics.increment(STALE_DOCUMENT_HITS_TOTAL, value=result.stale_filtered)
        result.candidates_considered = len(eligible)

        if not eligible or not normalized:
            result.zero_result = True
            metrics.increment(RETRIEVAL_ZERO_RESULT_TOTAL)
            result.latency_ms = (time.perf_counter() - started) * 1000
            metrics.observe(RETRIEVAL_LATENCY_MS, result.latency_ms)
            return result

        lexical = self._bm25(normalized, eligible)
        vector = self._vector_search(normalized, eligible)
        fused = self._rrf(lexical, vector)

        candidates = [
            ScoredChunk(
                self._chunks[i],
                score,
                relevance=self._relevance(normalized, i),
                lexical_rank=lr,
                vector_rank=vr,
            )
            for i, score, lr, vr in fused[: self._candidate_k]
        ]

        if self._reranker is not None:
            rerank_start = time.perf_counter()
            candidates = self._reranker.rerank(query, candidates)
            result.reranker_latency_ms = (time.perf_counter() - rerank_start) * 1000
            metrics.observe(RERANKER_LATENCY_MS, result.reranker_latency_ms)

        # Dedup runs *before* the top-k slice so a duplicate never consumes a slot.
        admissible, result.deduplicated = self._dedupe([c for c in candidates if c.score >= self._min_score])
        selected, result.dropped_oversized = self._apply_budget(admissible, limit=self._top_k)
        result.chunks = selected
        result.retrieved_tokens = sum(c.chunk.token_estimate for c in candidates)
        result.context_tokens = sum(c.chunk.token_estimate for c in selected)
        result.conflicting_document_ids = self._detect_conflicts(selected)
        result.zero_result = not selected

        if result.zero_result:
            metrics.increment(RETRIEVAL_ZERO_RESULT_TOTAL)
        metrics.observe(RETRIEVAL_DOCS, len(selected))
        metrics.observe(RETRIEVAL_TOKENS, result.context_tokens)
        result.latency_ms = (time.perf_counter() - started) * 1000
        metrics.observe(RETRIEVAL_LATENCY_MS, result.latency_ms)
        return result

    # --------------------------------------------------------------- stages ---
    @staticmethod
    def _passes(chunk: Chunk, filters: RetrievalFilter) -> bool:
        meta = chunk.metadata
        if filters.domain and meta.domain != filters.domain and meta.domain != "common":
            return False
        if filters.product and meta.product and meta.product != filters.product:
            return False
        if meta.language != filters.language:
            return False
        if filters.document_types and meta.document_type.value not in filters.document_types:
            return False
        # An anonymous/public caller may never see agent- or internal-only material.
        if filters.audience is Audience.PUBLIC and meta.audience is not Audience.PUBLIC:
            return False
        if filters.audience is Audience.CUSTOMER and meta.audience in (
            Audience.AGENT,
            Audience.INTERNAL,
        ):
            return False
        return not (filters.audience is Audience.AGENT and meta.audience is Audience.INTERNAL)

    def _count_stale(self) -> int:
        return sum(
            1
            for doc in self._corpus.documents()
            if doc.status
            in (
                DocumentStatus.SUPERSEDED,
                DocumentStatus.REVOKED,
                DocumentStatus.DRAFT,
                DocumentStatus.QUARANTINED,
            )
            or not doc.is_effective
        )

    def _bm25(self, query: str, eligible: list[int]) -> list[tuple[int, float]]:
        terms = tokenize(query)
        total_docs = len(self._doc_tokens) or 1
        scores: list[tuple[int, float]] = []
        for index in eligible:
            tokens = self._doc_tokens[index]
            if not tokens:
                continue
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for term in terms:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                doc_freq = self._doc_freq.get(term, 0)
                idf = math.log(1 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                denom = freq + self._k1 * (1 - self._b + self._b * length / (self._avg_len or 1))
                score += idf * (freq * (self._k1 + 1)) / denom
            if score > 0:
                scores.append((index, score))
        return sorted(scores, key=lambda x: x[1], reverse=True)

    def _relevance(self, query: str, chunk_index: int) -> float:
        """IDF-weighted coverage of the query's terms by one chunk.

        Each query term is weighted by how discriminative it is across the corpus, so
        a question dominated by terms the approved knowledge has never seen ("Ferrari",
        "Mumbai", "neighbour") scores low even when a common term like "premium"
        happens to match. That is what lets the evidence gate abstain instead of
        answering from a loosely-related chunk (§6.3, §7).
        """
        terms = set(tokenize(query))
        if not terms:
            return 0.0

        total_docs = len(self._doc_tokens) or 1
        chunk_terms = set(self._doc_tokens[chunk_index])

        matched_weight = 0.0
        total_weight = 0.0
        for term in terms:
            doc_freq = self._doc_freq.get(term, 0)
            # An unseen term is maximally discriminative: it is exactly the signal
            # that the corpus cannot answer this question.
            weight = math.log(1 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            total_weight += weight
            if term in chunk_terms:
                matched_weight += weight

        return round(matched_weight / total_weight, 6) if total_weight else 0.0

    def _vector_search(self, query: str, eligible: list[int]) -> list[tuple[int, float]]:
        query_vector = self._embedder.embed(query)
        scores = [(i, cosine(query_vector, self._vectors[i])) for i in eligible]
        return sorted((s for s in scores if s[1] > 0), key=lambda x: x[1], reverse=True)

    def _rrf(
        self, lexical: list[tuple[int, float]], vector: list[tuple[int, float]]
    ) -> list[tuple[int, float, int | None, int | None]]:
        """Reciprocal Rank Fusion, normalised to [0, 1] for threshold comparability."""
        lexical_rank = {index: rank for rank, (index, _) in enumerate(lexical, start=1)}
        vector_rank = {index: rank for rank, (index, _) in enumerate(vector, start=1)}
        best_possible = 2.0 / (self._rrf_k + 1)

        fused: list[tuple[int, float, int | None, int | None]] = []
        for index in set(lexical_rank) | set(vector_rank):
            score = 0.0
            if index in lexical_rank:
                score += 1.0 / (self._rrf_k + lexical_rank[index])
            if index in vector_rank:
                score += 1.0 / (self._rrf_k + vector_rank[index])
            fused.append(
                (index, round(score / best_possible, 6), lexical_rank.get(index), vector_rank.get(index))
            )
        return sorted(fused, key=lambda x: x[1], reverse=True)

    @staticmethod
    def _dedupe(chunks: list[ScoredChunk]) -> tuple[list[ScoredChunk], int]:
        """Drop exact duplicates (whole normalised text) keeping the higher-ranked copy."""
        unique: list[ScoredChunk] = []
        seen_text: set[str] = set()
        removed = 0
        for scored in chunks:
            fingerprint = hashlib.sha256(normalize_query(scored.chunk.text).encode("utf-8")).hexdigest()
            if fingerprint in seen_text:
                removed += 1
                continue
            seen_text.add(fingerprint)
            unique.append(scored)
        return unique, removed

    def _apply_budget(self, chunks: list[ScoredChunk], *, limit: int) -> tuple[list[ScoredChunk], int]:
        """Take up to ``limit`` chunks within the retrieval token budget (§58.8).

        A chunk that alone exceeds the budget is never admitted, however well it ranks:
        the context builder would drop it and the model would answer from nothing while
        grounding certified against it (H-8). Such drops are counted, not hidden.
        """
        selected: list[ScoredChunk] = []
        dropped_oversized = 0
        used = 0
        for scored in chunks:
            tokens = scored.chunk.token_estimate
            if tokens > self._max_retrieval_tokens:
                dropped_oversized += 1
                continue
            if len(selected) >= limit:
                break
            if used + tokens > self._max_retrieval_tokens:
                break
            selected.append(scored)
            used += tokens
        return selected, dropped_oversized

    @staticmethod
    def _detect_conflicts(chunks: list[ScoredChunk]) -> list[str]:
        """Flag two authoritative documents that disagree about the *same* subject.

        Surfacing a conflict is the point (§6.3): the LLM must not silently pick one.

        Detection is content-aware (H-9). Two chunks from different documents in the
        same scope conflict when they contain a sentence pair about the same thing
        (high overlap of non-numeric terms) that differs in its numbers ("30 days" vs
        "90 days") or its polarity ("covered" vs "not covered"). Two ACTIVE versions
        of one lineage remain a conflict on their own, as a governance signal. Version
        strings alone never make a conflict: agreeing documents agree.
        """
        by_doc: dict[str, list[ScoredChunk]] = {}
        for scored in chunks:
            by_doc.setdefault(scored.chunk.document_id, []).append(scored)
        if len(by_doc) < 2:
            return []

        ordered = sorted(by_doc.values(), key=lambda group: group[0].score, reverse=True)
        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                if _documents_conflict(first, second):
                    return sorted({first[0].chunk.document_id, second[0].chunk.document_id})
        return []


# ------------------------------------------------------------ conflict rules ---
_NEGATION_TERMS = frozenset({"not", "never", "no", "cannot", "excluded", "excludes", "waived", "nor"})
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
#: Minimum Jaccard overlap of non-numeric content terms for two sentences to be
#: "about the same thing" and therefore comparable.
CONFLICT_TERM_OVERLAP = 0.6


@dataclass(frozen=True, slots=True)
class _SentenceFacts:
    terms: frozenset[str]
    numbers: frozenset[str]
    negated: bool


def _sentence_facts(text: str) -> list[_SentenceFacts]:
    facts: list[_SentenceFacts] = []
    for sentence in _SENTENCE_SPLIT.split(" ".join(text.split())):
        lowered = sentence.lower()
        raw_tokens = _TOKEN.findall(lowered)
        terms = frozenset(t for t in raw_tokens if len(t) > 3 and not t.isdigit())
        if not terms:
            continue
        numbers = frozenset(n.replace(",", "").rstrip(".") for n in _NUMBER.findall(lowered))
        negated = any(t in _NEGATION_TERMS for t in raw_tokens)
        facts.append(_SentenceFacts(terms, numbers, negated))
    return facts


def _same_scope(left: Chunk, right: Chunk) -> bool:
    a, b = left.metadata, right.metadata
    same_domain = a.domain == b.domain or "common" in (a.domain, b.domain)
    same_product = a.product == b.product or a.product is None or b.product is None
    return same_domain and same_product and a.document_type is b.document_type


def _same_lineage_different_version(left: Chunk, right: Chunk) -> bool:
    a, b = left.metadata, right.metadata
    return a.lineage_key == b.lineage_key and a.version != b.version


def _sentences_disagree(left: _SentenceFacts, right: _SentenceFacts) -> bool:
    overlap = len(left.terms & right.terms) / len(left.terms | right.terms)
    if overlap < CONFLICT_TERM_OVERLAP:
        return False
    if left.negated != right.negated:
        return True
    return bool(left.numbers and right.numbers and left.numbers != right.numbers)


def _documents_conflict(first: list[ScoredChunk], second: list[ScoredChunk]) -> bool:
    """Content-aware conflict between two documents' retrieved chunks."""
    left_chunk, right_chunk = first[0].chunk, second[0].chunk
    if not _same_scope(left_chunk, right_chunk):
        return False
    if _same_lineage_different_version(left_chunk, right_chunk):
        return True
    left_facts = [f for scored in first for f in _sentence_facts(scored.chunk.text)]
    right_facts = [f for scored in second for f in _sentence_facts(scored.chunk.text)]
    return any(_sentences_disagree(a, b) for a in left_facts for b in right_facts)
