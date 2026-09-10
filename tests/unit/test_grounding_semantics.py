"""Grounding must reject restatements that keep the vocabulary but change the meaning
(master prompt §7): negation flips, invented quantities, foreign entities, universal
claims and non-answers. Verified at unit level and through the HTTP FAQ path."""

from __future__ import annotations

import re

import pytest

from app.rag.governance.documents import Audience
from app.rag.retrieval.grounding import GroundingDecision, GroundingService, Verification
from app.rag.retrieval.retriever import RetrievalFilter

QUESTION = "What is a deductible?"


def _retrieval(container):
    return container.retriever.retrieve(
        QUESTION, RetrievalFilter(domain=None, product=None, audience=Audience.PUBLIC)
    )


@pytest.mark.parametrize(
    "answer",
    [
        "A deductible is never paid by the policyholder; the insurer pays the deductible amount on every claim.",
        "A deductible is the amount you pay before the insurer pays. ProTec waives every deductible on all motor policies.",
        "A deductible is the amount you pay before the insurer pays; in India the deductible is always exactly 7 percent of the vehicle IDV by law.",
        "Under IRDAI rules a deductible cannot exceed Rs 500 on any private car policy and the insurer must refund it on renewal.",
        "Your deductible is Rs. 2,500 on every claim.",
        "Zero depreciation cover is included free with every deductible waiver and covers consumables at 100 percent.",
    ],
)
def test_meaning_changing_restatements_abstain(container, answer):
    assessment = GroundingService().assess_answer(answer, _retrieval(container))
    assert assessment.decision is GroundingDecision.ABSTAIN, (answer, assessment)
    assert assessment.verification is Verification.UNSUPPORTED


@pytest.mark.parametrize(
    "answer",
    [
        "I do not have enough information.",
        "Please contact support for more details about that.",
        "That is an interesting question.",
    ],
)
def test_non_answers_are_not_labelled_verified(container, answer):
    """Text with no regulated claim may stand, but never as VERIFIED with a citation."""
    assessment = GroundingService().assess_answer(answer, _retrieval(container))
    assert assessment.verification is Verification.UNSUPPORTED
    assert assessment.cited_document_ids == []
    assert assessment.reason_code == "NO_REGULATED_CLAIM"


def test_faithful_extractive_answer_is_verified_with_the_right_citation(container):
    retrieval = _retrieval(container)
    faithful = (
        "A deductible is the amount you agree to pay yourself towards a claim before the insurer pays the rest. "
        "If your deductible is Rs. 1,000 and the assessed claim amount is Rs. 10,000, you pay Rs. 1,000."
    )
    assessment = GroundingService().assess_answer(faithful, retrieval)
    assert assessment.decision is GroundingDecision.ANSWER
    assert assessment.verification is Verification.VERIFIED
    assert assessment.cited_document_ids == ["KB-COMMON-GLOSSARY"]


def test_partially_supported_answer_is_labelled_partial_not_verified(container):
    retrieval = _retrieval(container)
    mixed = (
        "A deductible is the amount you agree to pay yourself towards a claim before the insurer pays the rest. "
        "A compulsory deductible is fixed by the policy. "
        "A voluntary deductible is one you choose. "
        "Choosing a higher voluntary deductible usually reduces your premium. "
        "The deductible is chosen during the proposal stage together with your agent."
    )
    assessment = GroundingService().assess_answer(mixed, retrieval)
    assert assessment.decision is GroundingDecision.ANSWER
    assert assessment.verification is Verification.PARTIALLY_VERIFIED
    assert any("proposal stage" in s for s in assessment.unsupported_sentences)


def test_one_contradicting_sentence_sinks_an_otherwise_faithful_answer(container):
    """Hard failure: a refund/negation claim is never shipped as 'partially verified'."""
    retrieval = _retrieval(container)
    poisoned = (
        "A deductible is the amount you agree to pay yourself towards a claim before the insurer pays the rest. "
        "A compulsory deductible is fixed by the policy. "
        "A voluntary deductible is one you choose. "
        "Choosing a higher voluntary deductible usually reduces your premium. "
        "The deductible is refunded in full at renewal."
    )
    assessment = GroundingService().assess_answer(poisoned, retrieval)
    assert assessment.decision is GroundingDecision.ABSTAIN
    assert any("refunded" in s for s in assessment.unsupported_sentences)


@pytest.mark.parametrize(
    ("forced_answer", "expected"),
    [
        (
            "A deductible is never paid by the policyholder; the insurer pays the deductible on every claim.",
            "ABSTAIN",
        ),
        ("Deductibles in India are always exactly 7 percent of the vehicle price by law.", "ABSTAIN"),
        ("I do not have enough information.", "ALLOW"),
    ],
)
def test_http_faq_path_abstains_on_forced_hallucination(client, forced_answer, expected):
    """End to end: the model double is forced to hallucinate; the Harness must abstain,
    and a non-answer must not be presented as verified."""
    model = client.app.state.container.invoker.model
    model.responder._rules.insert(0, (re.compile(r"EVIDENCE:", re.I | re.S), lambda _p, _t=forced_answer: _t))
    try:
        conv = client.post("/api/v1/conversations", json={}).json()["conversation_id"]
        r = client.post("/api/v1/chat", json={"conversation_id": conv, "message": QUESTION})
    finally:
        model.responder._rules.pop(0)
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["outcome"] == expected
    assert body["directive"]["payload"]["citations"] == []
    assert body["directive"]["payload"]["verification"] == "UNSUPPORTED"
    assert "7 percent" not in body["message"] and "never paid" not in body["message"]


def test_grounding_uses_only_delivered_evidence(container):
    """A chunk dropped by the context budget cannot certify the answer (H-8)."""
    from app.ai.agents.faq_agent import FaqAgent
    from app.ai.harness.context.builder import BuiltContext

    retrieval = _retrieval(container)
    assert retrieval.chunks
    delivered = FaqAgent._delivered_evidence(retrieval, [])
    assert delivered.chunks == []
    only_first = FaqAgent._delivered_evidence(retrieval, [retrieval.chunks[0].chunk.chunk_id])
    assert [c.chunk.chunk_id for c in only_first.chunks] == [retrieval.chunks[0].chunk.chunk_id]
    assert BuiltContext.__dataclass_fields__["evidence_chunk_ids"]
