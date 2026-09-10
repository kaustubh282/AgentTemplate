"""Grounding and abstention policy (master prompt §6.3, §7).

Answers are classified rather than scored to users. The service decides, in code,
whether the retrieved evidence supports the answer that was produced, and it prefers
"I cannot verify this" over a plausible unsupported claim.

No uncalibrated numeric confidence is ever shown to a user; the numeric signals stay
internal for telemetry and evals (§7).
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.observability.metrics import (
    ABSTENTION_TOTAL,
    CONFLICTING_SOURCE_TOTAL,
    GROUNDING_FAILURE_TOTAL,
    metrics,
)
from app.rag.retrieval.retriever import RetrievalResult, tokenize


class Verification(StrEnum):
    """How well the answer is supported (§6.3)."""

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"
    CONFLICTING = "CONFLICTING"


class GroundingDecision(StrEnum):
    ANSWER = "ANSWER"
    ABSTAIN = "ABSTAIN"
    SURFACE_CONFLICT = "SURFACE_CONFLICT"


@dataclass(slots=True)
class GroundingAssessment:
    decision: GroundingDecision
    verification: Verification
    #: Internal-only support ratio. Never rendered as a user-facing confidence.
    support_ratio: float = 0.0
    evidence_score: float = 0.0
    cited_document_ids: list[str] = field(default_factory=list)
    unsupported_sentences: list[str] = field(default_factory=list)
    reason_code: str = "OK"


#: Sentences that state a regulated fact must be traceable to evidence.
_FACTUAL_MARKERS = re.compile(
    r"(?i)\b(cover(s|ed|age)?|exclu(de|des|ded|sion)|premium|deductible|claim|waiting\s+period"
    r"|no\s+claim\s+bonus|ncb|idv|sum\s+insured|policy|endorsement|renewal|grace\s+period)\b"
)

#: Polarity and absoluteness cues. A sentence that negates, waives or universalises a
#: fact is only supported when its best-matching evidence sentence carries the same cue;
#: lexical overlap alone would let "never paid by the policyholder" pass against
#: evidence that says the opposite (§7).
_POLARITY_CUES = re.compile(
    r"(?i)\b(not|never|no|none|cannot|can't|won't|without|waive[sd]?|free|refund(ed|s)?"
    r"|always|exactly|guaranteed?|all|every|100\s*%|mandatory|by\s+law|prohibited)\b"
)

#: Quantities stated in an answer must appear in the evidence: amounts, percentages,
#: durations and years are exactly the values an insurer is held to.
_NUMBER = re.compile(
    r"(?<![\w.])(?:rs\.?\s*|inr\s*|₹\s*)?\d[\d,]*(?:\.\d+)?\s*(?:%|percent|days?|months?|years?)?", re.I
)

#: Sentence boundary that does not split after common abbreviations ("Rs.", "No.").
_SENTENCE_SPLIT = re.compile(r"(?<!\bRs\.)(?<!\bNo\.)(?<!\bvs\.)(?<=[.!?])\s+")

#: Overlap an answer sentence needs with its best-matching evidence sentence.
SENTENCE_SUPPORT_THRESHOLD = 0.55
#: Overlap a marker-free answer needs with the evidence as a whole to count as an answer.
MARKER_FREE_ANSWER_THRESHOLD = 0.5

ABSTENTION_MESSAGE = (
    "I could not verify this from the approved information currently available. "
    "Please check with our support team so you get a confirmed answer."
)

OUT_OF_DOMAIN_MESSAGE = "I can only help with questions about our insurance products and services."

CONFLICT_MESSAGE = (
    "The approved documents I can see give different answers for this, so I do not want to "
    "guess. Our support team can confirm which one applies to your policy."
)


class GroundingService:
    """Validates that a produced answer is supported by retrieved evidence."""

    def __init__(
        self,
        *,
        min_evidence_score: float = 0.35,
        min_support_ratio: float = 0.5,
        partial_support_ratio: float = 0.8,
        min_citation_overlap: float = 0.25,
    ) -> None:
        self._min_evidence = min_evidence_score
        self._min_support = min_support_ratio
        self._partial_support = partial_support_ratio
        self._min_citation_overlap = min_citation_overlap

    def contributing_document_ids(self, answer: str, retrieval: RetrievalResult) -> list[str]:
        """Documents whose text actually supports the answer - not every retrieved chunk.

        A regulated answer must cite its provenance precisely (§6.3). A chunk is cited
        when it covers a meaningful share of the answer's terms; the single best-covering
        chunk is always cited so a grounded answer never ends up with no citation.
        """
        answer_terms = {t for t in tokenize(answer) if len(t) > 3}
        if not retrieval.chunks:
            return []
        if not answer_terms:
            return [retrieval.chunks[0].chunk.document_id]

        coverage: list[tuple[float, str]] = []
        for scored in retrieval.chunks:
            chunk_terms = set(tokenize(scored.chunk.text))
            overlap = len(answer_terms & chunk_terms) / len(answer_terms)
            coverage.append((overlap, scored.chunk.document_id))

        best = max(coverage, key=lambda item: item[0])
        cited: list[str] = []
        for overlap, document_id in coverage:
            if (overlap >= self._min_citation_overlap or document_id == best[1]) and document_id not in cited:
                cited.append(document_id)
        return cited

    def assess_evidence(self, retrieval: RetrievalResult) -> GroundingAssessment | None:
        """Pre-answer gate: is there enough evidence to attempt an answer at all?"""
        if retrieval.zero_result or not retrieval.chunks:
            metrics.increment(ABSTENTION_TOTAL, labels={"reason": "no_evidence"})
            return GroundingAssessment(
                GroundingDecision.ABSTAIN,
                Verification.UNSUPPORTED,
                reason_code="ABSTAIN_INSUFFICIENT_EVIDENCE",
            )
        if retrieval.top_score < self._min_evidence:
            metrics.increment(ABSTENTION_TOTAL, labels={"reason": "weak_evidence"})
            return GroundingAssessment(
                GroundingDecision.ABSTAIN,
                Verification.UNSUPPORTED,
                evidence_score=retrieval.top_score,
                reason_code="ABSTAIN_INSUFFICIENT_EVIDENCE",
            )
        if retrieval.conflicting_document_ids:
            metrics.increment(CONFLICTING_SOURCE_TOTAL)
            return GroundingAssessment(
                GroundingDecision.SURFACE_CONFLICT,
                Verification.CONFLICTING,
                evidence_score=retrieval.top_score,
                cited_document_ids=list(retrieval.conflicting_document_ids),
                reason_code="SURFACE_CONFLICTING_SOURCES",
            )
        return None

    def assess_answer(self, answer: str, retrieval: RetrievalResult) -> GroundingAssessment:
        """Post-answer gate: is every factual claim traceable to the evidence?"""
        pre = self.assess_evidence(retrieval)
        if pre is not None:
            return pre

        evidence_sentences = _sentences_of(retrieval)
        evidence_terms: set[str] = set()
        for sentence in evidence_sentences:
            evidence_terms |= set(tokenize(sentence))

        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(answer) if s.strip()]
        factual = [s for s in sentences if _FACTUAL_MARKERS.search(s) or _NUMBER.search(s)]

        # The quantity and entity indexes cost a full pass over the evidence, so they are
        # built only when the answer actually states a number or a name to check.
        answer_has_numbers = bool(_NUMBER.search(answer))
        answer_has_names = bool(_proper_nouns(answer))
        joined = " ".join(evidence_sentences) if (answer_has_numbers or answer_has_names) else ""
        evidence_numbers = (
            {_normalise_number(n) for n in _NUMBER.findall(joined)} if answer_has_numbers else frozenset()
        )
        evidence_names = _proper_nouns(joined) if answer_has_names else frozenset()
        cited = self.contributing_document_ids(answer, retrieval)

        if not factual:
            # No regulated claim was made (a clarifying question, a deflection, "I do not
            # have enough information"). Such text may stand, but it is only VERIFIED and
            # cited when it actually draws on the evidence; otherwise it is returned as
            # UNSUPPORTED with no citation, never dressed up as a grounded answer (§7).
            answer_terms = {t for t in tokenize(answer) if len(t) > 3}
            overlap = (len(answer_terms & evidence_terms) / len(answer_terms)) if answer_terms else 0.0
            if overlap >= MARKER_FREE_ANSWER_THRESHOLD:
                return GroundingAssessment(
                    GroundingDecision.ANSWER,
                    Verification.VERIFIED,
                    support_ratio=round(overlap, 4),
                    evidence_score=retrieval.top_score,
                    cited_document_ids=list(cited),
                )
            return GroundingAssessment(
                GroundingDecision.ANSWER,
                Verification.UNSUPPORTED,
                support_ratio=round(overlap, 4),
                evidence_score=retrieval.top_score,
                cited_document_ids=[],
                unsupported_sentences=sentences,
                reason_code="NO_REGULATED_CLAIM",
            )

        supported = 0
        unsupported: list[str] = []
        hard_failure = False
        for sentence in factual:
            verdict = self._sentence_support(sentence, evidence_sentences, evidence_numbers, evidence_names)
            if verdict is _Support.SUPPORTED:
                supported += 1
                continue
            unsupported.append(sentence)
            if verdict is _Support.CONTRADICTED:
                hard_failure = True

        ratio = supported / len(factual)
        if hard_failure:
            # A negated, universalised, renumbered or foreign-entity claim is the most
            # dangerous hallucination shape: it is never returned, whatever the ratio.
            metrics.increment(GROUNDING_FAILURE_TOTAL)
            metrics.increment(ABSTENTION_TOTAL, labels={"reason": "answer_contradicts_evidence"})
            return GroundingAssessment(
                GroundingDecision.ABSTAIN,
                Verification.UNSUPPORTED,
                support_ratio=round(ratio, 4),
                evidence_score=retrieval.top_score,
                unsupported_sentences=unsupported,
                reason_code="ABSTAIN_INSUFFICIENT_EVIDENCE",
            )
        if ratio >= 1.0:
            # VERIFIED means every regulated sentence is traceable - not most of them.
            return GroundingAssessment(
                GroundingDecision.ANSWER,
                Verification.VERIFIED,
                support_ratio=1.0,
                evidence_score=retrieval.top_score,
                cited_document_ids=list(cited),
            )
        if ratio >= self._min_support:
            return GroundingAssessment(
                GroundingDecision.ANSWER,
                Verification.PARTIALLY_VERIFIED,
                support_ratio=round(ratio, 4),
                evidence_score=retrieval.top_score,
                cited_document_ids=list(cited),
                unsupported_sentences=unsupported,
                reason_code="PARTIAL_SUPPORT",
            )

        metrics.increment(GROUNDING_FAILURE_TOTAL)
        metrics.increment(ABSTENTION_TOTAL, labels={"reason": "answer_not_grounded"})
        return GroundingAssessment(
            GroundingDecision.ABSTAIN,
            Verification.UNSUPPORTED,
            support_ratio=round(ratio, 4),
            evidence_score=retrieval.top_score,
            unsupported_sentences=unsupported,
            reason_code="ABSTAIN_INSUFFICIENT_EVIDENCE",
        )

    @staticmethod
    def _sentence_support(
        sentence: str,
        evidence_sentences: list[str],
        evidence_numbers: AbstractSet[str],
        evidence_names: AbstractSet[str],
    ) -> _Support:
        """Classify one factual sentence against the evidence (§7).

        SUPPORTED: a single evidence sentence covers it and agrees on polarity, quantities
        and named entities. CONTRADICTED: it introduces a quantity, a named entity or a
        polarity/absoluteness cue the evidence does not carry - the classic shape of a
        confident hallucination, which must abstain whatever the rest of the answer says.
        UNCOVERED: merely not traceable; downgrades the answer to PARTIALLY_VERIFIED.
        """
        terms = [t for t in tokenize(sentence) if len(t) > 3]
        if not terms:
            return _Support.SUPPORTED

        for raw in _NUMBER.findall(sentence):
            if _normalise_number(raw) not in evidence_numbers:
                return _Support.CONTRADICTED
        if _proper_nouns(sentence) - evidence_names:
            return _Support.CONTRADICTED

        best_overlap, best_sentence = 0.0, ""
        for candidate in evidence_sentences:
            candidate_terms = set(tokenize(candidate))
            overlap = sum(1 for t in terms if t in candidate_terms) / len(terms)
            if overlap > best_overlap:
                best_overlap, best_sentence = overlap, candidate

        answer_cues = _cues(sentence)
        if best_overlap < SENTENCE_SUPPORT_THRESHOLD:
            # Not traceable at all. With an absoluteness cue it is a fabricated rule.
            return _Support.CONTRADICTED if answer_cues else _Support.UNCOVERED
        if answer_cues <= _cues(best_sentence):
            return _Support.SUPPORTED
        return _Support.CONTRADICTED


class _Support(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNCOVERED = "UNCOVERED"
    CONTRADICTED = "CONTRADICTED"


def _cues(text: str) -> set[str]:
    return {m.group(0).lower() for m in _POLARITY_CUES.finditer(text)}


def _sentences_of(retrieval: RetrievalResult) -> list[str]:
    """Evidence split into sentences once per assessment."""
    return [
        s.strip()
        for scored in retrieval.chunks
        for s in _SENTENCE_SPLIT.split(scored.chunk.text)
        if s.strip()
    ]


def _normalise_number(raw: str) -> str:
    """'Rs. 1,000' / 'INR 1000' / '1000' compare equal; '7 percent' equals '7%'."""
    text = raw.lower().replace(",", "")
    text = re.sub(r"^(rs\.?|inr|₹)\s*", "", text)
    text = re.sub(r"\s*percent$", "%", text)
    text = re.sub(r"\s+", "", text)
    return text.strip(". ")


def _proper_nouns(text: str) -> set[str]:
    """Capitalised tokens that are not sentence-initial: product and organisation names."""
    names: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        words = sentence.split()
        for word in words[1:]:
            cleaned = word.strip(".,;:()\"'")
            if len(cleaned) > 2 and cleaned[0].isupper() and cleaned[1:].islower() and not cleaned.isupper():
                names.add(cleaned.lower())
    return names
