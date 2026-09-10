# ADR 0012 - Three-layer evaluation: native, Ragas, DeepEval

## Status
Accepted.

## Context
A production AI template without executable evaluation is incomplete. But different
claims need different kinds of proof, and using one mechanism for all of them produces
either unfalsifiable scores or unusable rigidity.

"Expected model calls = 0, actual = 1" is a fact. "Is this answer relevant?" is a
judgement. Conflating them is how AI systems end up with opinion scores standing in for
correctness guarantees.

## Decision
Three complementary layers over one shared dataset format:

**Native deterministic harness** (project-owned) for facts that must not depend on a
judge: workflow transitions, directive validity, API contracts, authorization and
ownership, idempotency, prod-with-mock rejection, PII leakage, model-call and
agent-step counts, token budgets, latency SLOs, rate limits, timeout and circuit
behaviour, audit generation, health, fallback and provider substitution. Exact
PASS/FAIL with expected and actual values recorded verbatim. 128 checks across 8
suites.

**Ragas** for retrieval and grounded-answer quality, plus the retrieval experiment
comparison - each candidate configuration reporting quality *and* latency, tokens and
cost, so a marginally better score does not silently buy a worse system.

**DeepEval** as the agent regression framework, with project-owned deterministic
metrics so the suite runs in CI with no judge model.

Judge-dependent metrics are reported as `NOT_EVIDENCED` with the exact reason. They are
never fabricated, and a missing judge is never presented as a passing score.

The application never imports either framework; adapters live under `evals/`.

## Alternatives considered
1. **One framework for everything.** Rejected: an LLM judge cannot assert "zero model
   calls", and a deterministic harness cannot assess answer relevance.
2. **Library-default thresholds.** Rejected explicitly by §26.2, and rightly - a
   default threshold is not a production quality gate.
3. **Skip evaluation without a judge.** Rejected: it would leave the most important
   guarantees - determinism, security, budgets - unproven.
4. **Fabricate plausible judge scores.** Rejected absolutely. `NOT_EVIDENCED` is the
   honest report, and it is what appears in the readiness scorecard.

## Consequences
Positive: 128 native checks, real Ragas numbers (hallucination rate 0.000, citation
correctness 1.000, abstention correctness 1.000), four DeepEval suites passing, and a
benchmark with 14 gates and a saved baseline for regression comparison - all offline,
all reproducible, no credentials.

The evaluation layer also *found real defects*: the double-embedded evidence in the FAQ
prompt (median input tokens 721 to 438, cost down 23%), the unusable RRF threshold, the
false-positive conflict detection, and the missing idempotency guard. That is the
strongest argument for building it early.

Negative: answer relevance and faithfulness remain unmeasured without a judge, recorded
as blocker B6. Deterministic metrics can be over-fitted to the dataset, so the dataset
must grow with real traffic patterns.

## Security impact
Positive. The adversarial corpus is a regression suite: a newly discovered attack
becomes a permanent test. Security claims are commands a reviewer can run.

## Compliance impact
Provides the evidence base for the control-evidence matrix and the readiness report.
Version-stamped reports (dataset, framework, model, prompt, corpus versions) support
§26.12 evidence requirements. Production runtime does not run a judge synchronously
(§26.10); it emits safe telemetry for offline evaluation.

