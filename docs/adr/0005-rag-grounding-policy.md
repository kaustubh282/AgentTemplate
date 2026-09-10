# ADR 0005 - Grounding policy: absolute relevance gate and abstention

## Status
Accepted.

## Context
The FAQ assistant must never state an insurance fact that the approved knowledge does
not support. Two design questions follow: what counts as sufficient evidence, and what
happens when there is not enough.

The first attempt used the hybrid retriever's fused RRF score as the evidence
threshold. That was wrong in a way worth recording: RRF is a *rank* fusion, so its top
result is always 1.0 no matter how weak the match. The threshold was therefore
meaningless and every question looked well-evidenced.

## Decision
Keep RRF for ranking. Compute a separate **absolute** relevance signal for the gate:
IDF-weighted coverage of the query's terms by the chunk. A term the corpus has never
seen carries maximum weight, so a question dominated by unknown terms scores low even
when one common term matches.

Apply the gate **before** the model call, so refusing costs nothing. Apply a second
grounding check after the answer, comparing factual sentences against the retrieved
evidence. Prefer "I could not verify this from the approved information currently
available" over a plausible unsupported answer.

Classify the outcome rather than scoring it to the user: VERIFIED,
PARTIALLY_VERIFIED, UNSUPPORTED, OUT_OF_DOMAIN, CONFLICTING. Never show a numeric
confidence.

Surface a genuine conflict between two active document versions instead of letting the
model choose.

## Lifecycle, version selection, quarantine and conflict detection

Added after the external review (findings H-7, H-8, H-9, M-11). The rules below are
enforced in code (`app/rag/governance/documents.py`, `app/rag/ingestion/loader.py`,
`app/rag/retrieval/retriever.py`) and covered by `tests/integration/test_rag_pipeline.py`.

**Metadata is fail-closed.** `status` is a required front-matter field; a document
without one is refused at ingestion (`knowledge_document_missing_metadata`), it is never
assumed ACTIVE. An ACTIVE document with an empty `approved_by` is demoted to DRAFT and a
warning is logged, so unapproved material is never served.

**Version selection per lineage.** A lineage is `(document_name, domain, product,
document_type)`. Among ACTIVE documents of one lineage only the highest `version` stays
ACTIVE (components compared numerically where possible, so 1.10 > 1.9); the others are
marked SUPERSEDED at ingestion and the winner records the newest one in `supersedes`.
The same `document_id` arriving in two files resolves to the highest version, never to
file order. An explicit admin activation of an older version is honoured (it is an
audited human decision) and the two active versions are then surfaced as a conflict.

**Effective date.** A document whose `effective_date` is after today (UTC) is not
retrievable even when ACTIVE; the retriever's index key includes the date so the
document enters the index on the day it becomes effective without any other change.

**Lifecycle persistence.** `KnowledgeIngestionService(lifecycle_state_path=...)` writes
explicit lifecycle changes (activate, supersede, revoke) as `{document_id: status}` via
temp-file-and-replace, and re-applies them after every ingestion. A revoked document
therefore stays revoked across restarts and re-ingestion. An unreadable state file is a
startup error, not an empty override set. The admin lifecycle endpoint goes through the
ingestion service so its changes are persisted.

**Quarantine at ingestion.** Every chunk is scored by the prompt-injection detector at
ingestion. A document with any chunk scoring at or above 0.6 is stored as QUARANTINED,
with a warning naming the document and the categories detected. Quarantined material is
never retrievable (not even with `allow_draft`), never wins a lineage, and is never
auto-activated by persisted state. It remains listed in `KnowledgeCorpus.documents()`
and `KnowledgeCorpus.quarantined()` so an administrator can see and remove it.

**Retrieval budget.** No chunk can exceed `rag_max_chunk_tokens`: an oversized paragraph
is split at sentence boundaries (a run-on sentence by words). The retriever never admits
a chunk whose own estimate exceeds `max_retrieval_tokens`; such chunks are dropped and
counted in `RetrievalResult.dropped_oversized`, so grounding can no longer certify
against evidence the context builder would have cut. Exact-duplicate removal runs on the
whole normalised text and before the top-k slice, so duplicates do not consume slots.

**Content-aware conflict detection.** Two documents in the same scope (same domain or
one is `common`; same product or one is product-less; same document type) conflict when
they contain a pair of sentences about the same thing (Jaccard >= 0.6 over non-numeric
terms longer than three characters) whose numeric values differ ("30 days" vs "90 days")
or whose negation polarity differs ("covered" vs "not covered"). Two ACTIVE versions of
one lineage remain a conflict as a governance signal. Differing version strings alone no
longer make a conflict, so agreeing documents are no longer wrongly refused. The check is
lexical: paraphrased contradictions that share fewer than 60% of their terms are not
detected, which is why the post-answer grounding check remains mandatory.

## Alternatives considered
1. **RRF score as the threshold.** Tried, and it failed for the reason above.
2. **Embedding similarity threshold.** Rejected as the sole signal: it degrades with a
   hashed embedder and hides the reason for a refusal.
3. **Ask the model whether it has enough evidence.** Rejected: that is a security and
   quality control delegated to the thing being controlled, and it costs a model call
   to decline.
4. **Answer with a hedge instead of abstaining.** Rejected: a hedged wrong answer about
   a premium is still a wrong answer about a premium.
5. **Show a confidence percentage.** Rejected: an uncalibrated number presented as
   certainty is worse than no number.

## Consequences
Positive: measured separation on the golden set is 0.81-1.00 for supported questions
and 0.00-0.26 for unsupported, with the gate at 0.35; hallucination rate 0.000;
abstention correctness 1.000; refusing costs zero model calls.

Negative: the signal is lexical, so a well-evidenced question phrased with entirely
different vocabulary could under-score. Mitigated by the hybrid dense channel feeding
the ranking, and by treating the threshold as configuration.

Also learned: the gate is the wrong layer for some refusals. "What is my neighbour's
policy number?" scores 0.41 because the corpus genuinely discusses policy numbers - it
is an exfiltration attempt and belongs to the guardrail, not the evidence gate.

## Security impact
Positive. Abstention is the cheapest path, so there is no cost pressure to answer.
The conflict-surfacing rule prevents a silent choice between contradictory approved
sources.

## Compliance impact
Directly supports IRDAI-PP-02 and IRDAI-PP-03: answers are grounded in approved,
versioned, ACTIVE documents with citations, and the assistant declines rather than
guessing. Also supports the no-misleading-statement requirement.

