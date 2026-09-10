# Architecture

This document explains *why* the platform is shaped the way it is, and where each
guarantee is enforced. It is written for the engineer who has to extend it and the
reviewer who has to trust it.

---

## 1. The organising principle

> The model understands language. The application owns transactional truth.

Everything else follows. An LLM is excellent at interpreting "I want to insure my
car" and at explaining an approved document. It is unfit to be the system of record
for a premium, an eligibility decision or a policy issuance — not because it is
unreliable in general, but because those facts must be *reproducible, auditable and
attributable to an approved source*.

So the platform splits into two halves with a hard boundary between them:

| | Language half | Transactional half |
|---|---|---|
| Owns | intent, explanation, extraction | state, validation, business rules, money |
| Implemented by | Harness + agents + RAG | router + workflow engine + providers |
| Model calls | 0 or 1 | **always 0** |
| Failure mode | abstain | reject the transition |
| Source of truth | approved knowledge corpus | System of Record |

The boundary is not a convention. `app/ai/**` cannot import the workflow engine, and
[a test asserts it](../../tests/unit/test_architecture_boundaries.py).

---

## 2. Request lifecycle

```text
HTTP request
   |
   v
CorrelationMiddleware ........... request/correlation id, secure headers, metrics
   |
BodySizeLimitMiddleware ......... oversized payload refused before validation
   |
JWT auth dependency ............. signature, alg allow-list, iss, aud, exp, nbf, sub
   |                              -> trusted AuthContext (no token, no raw claims)
   v
RequestContext .................. pseudonymous refs, channel derived from identity
   |
CapabilityRouter ................ STRUCTURAL classification, zero model calls
   |
   +-- deterministic -----------------------------+
   |                                              |
   |   PolicyEnforcementPoint                     |   HarnessService.execute
   |     env / channel / authn / authz /          |     (the single AI control point)
   |     workflow state / confirmation /          |
   |     idempotency / rate limit / schema        |
   |          |                                   |
   |   WorkflowService                            |   guardrails -> budgets ->
   |     precheck -> service action -> commit     |   context -> model -> output
   |          |                                   |   validation -> grounding ->
   |   Provider (mock | InsureMO)                 |   leakage scan -> audit
   |          |                                   |          |
   +----------+------------------+----------------+----------+
                                 |
                        DirectiveRegistry ......... registered types only, fail closed
                                 |
                        AssistantResponse ......... schema-versioned contract
```

### Why the router comes first

A button click already carries its meaning. Sending it to a model would add latency,
cost and a probabilistic failure mode to a decision that has none. The router
classifies structurally into the eight paths of §2.3 and only escalates when language
understanding is genuinely required.

Measured effect (see `evals/reports/benchmark-report.json`):

* deterministic action p95: **0.6 ms**, 0 model calls
* zero-model-call request rate: **74%** of benchmark traffic
* FAQ median input: **438 tokens**, one model call

---

## 3. The centralized Harness

`app/ai/harness/service.py` is the only way AI-assisted work happens. It exists
because the alternative — each agent applying its own controls — guarantees drift: the
second agent forgets the PII check, the third forgets the budget.

The Harness **orchestrates**; it does not implement. It composes:

```text
HarnessService
  |-- PolicyEnforcementPoint ..... authorization, schema, workflow state, rate limit
  |-- ModelInputSanitizer ........ data minimisation, secret refusal, PII removal
  |-- GuardrailService ........... input / retrieved-document / output policy
  |-- TokenBudgetService ......... model calls, agent steps, tool calls, tokens
  |-- ContextBuilder ............. intentional assembly with full token accounting
  |-- GroundingService ........... evidence gate and abstention
  |-- ModelInvoker ............... timeouts, usage accounting, controlled fallback
  |-- ModelOutputScanner ......... post-response leakage
  |-- AuditService ............... audit events + explainability decision records
```

It stays under 500 lines, holds no regex of its own, makes no network call, and is an
**in-process module** rather than a service — there is no justification yet for
another network hop, another deployment and another failure domain. The abstraction
makes later extraction possible without requiring it now.

Ten proofs of centralization live in
[`tests/unit/test_harness_centralization.py`](../../tests/unit/test_harness_centralization.py),
including: two different agents get identical auth and PII treatment; a rogue agent's
malformed payload and prohibited claim are both caught by the Harness, not the agent;
a brand-new agent inherits every control without copying a line of it.

### Outcomes are explicit

Every execution ends in `ALLOW | BLOCK | ABSTAIN | FALLBACK | ESCALATE` with a
machine-readable reason code (`BLOCK_UNAUTHORIZED_TOOL`,
`ABSTAIN_INSUFFICIENT_EVIDENCE`, `FALLBACK_MODEL_UNAVAILABLE`, …) and a non-sensitive
`HarnessExecutionRecord`. No chain-of-thought is stored anywhere.

---

## 4. Determinism in the transactional half

A workflow is **data**, not code branches:

```python
Transition(
    action="CONFIRM_PURCHASE",
    from_state="REVIEW",
    to_state="PAYMENT",
    required_permissions={Permission.PURCHASE_SUBMIT, Permission.PAYMENT_INITIATE},
    requires_fields=("quote_id",),
    side_effect_class=SideEffectClass.HIGH_RISK_WRITE,
    requires_confirmation=True,
    requires_idempotency_key=True,
    service_action="initiate_payment",
)
```

Anything not declared is rejected. The engine checks ownership, permissions,
confirmation, idempotency and prerequisites, then commits with optimistic locking and
an audit event that records *which fields were written* but never their values.

### Ordering is a safety property

`WorkflowService.execute` runs **precheck → service action → commit**. If the provider
fails, nothing was committed: the state, its version and its collected data are exactly
as they were, and the user gets a retry directive rather than a fabricated quote. This
is Demo F, and it is
[tested end to end](../../tests/e2e/test_demo_scenarios.py).

---

## 5. Grounding, and why abstention is cheap

```text
question -> retrieve -> EVIDENCE GATE -> [abstain, 0 model calls]
                             |
                          answer -> GROUNDING CHECK -> [abstain]
                                          |
                                      verified answer + citations
```

The evidence gate runs **before** the model call, so refusing costs nothing. That
matters: a system where refusing is expensive is a system under pressure to answer.

### The relevance signal

Hybrid retrieval fuses BM25 and dense results with Reciprocal Rank Fusion. RRF is
excellent for *ranking* and useless as a *threshold* — its top result is always 1.0
however weak the match. The retriever therefore computes a separate absolute signal:
**IDF-weighted coverage of the query's terms by the chunk**. A question dominated by
terms the approved corpus has never seen scores low even when one common term matches.

Measured separation on the golden set: supported questions 0.81–1.00, unsupported
0.00–0.26, with the gate at 0.35.

### What the gate cannot do

"What is my neighbour's policy number?" scores 0.41 — the corpus genuinely discusses
policy numbers. An evidence gate is the wrong layer for that request; it is an
**exfiltration attempt**, and it is refused by the guardrail. Choosing the right layer
for each refusal is a recurring theme of this design.

---

## 6. Data boundaries

Three DTO layers, and only the third reaches a model:

```text
Provider DTO  ->  Domain DTO  ->  AI-facing DTO
(upstream shape)  (our shape)     (masked, field-limited, purpose-scoped)
```

```json
{
  "policy_number_masked": "*******1234",
  "product": "Private Car Package",
  "status": "ACTIVE",
  "annual_premium": 18450.0
}
```

`customer_id`, `policy_id`, `source_system`, `sum_insured` and `inception_date` are
absent — not masked, absent. The model is given what the current question needs and
nothing else, and `ModelInputSanitizer` removes anything classified at or above `PII`
unless the capability declared it as a purpose field.

---

## 7. Integration replaceability

The application depends on nine Protocols in
`app/integrations/contracts/providers.py`. Selection happens once, in
`app/integrations/factory.py`, from configuration:

```bash
POLICY_PROVIDER=mock       # non-prod
POLICY_PROVIDER=insuremo   # real
```

One contract suite runs against both implementations: identical method sets,
signatures and return annotations; identical error taxonomy; idempotency; timeout,
outage and malformed-response behaviour. Where InsureMO's specification is unknown,
the adapter raises `InsureMoContractNotConfigured("REQUIRES_VERIFICATION:…")`. Nothing
is invented.

---

## 8. Extension model

Core publishes interfaces; domains consume them. Core never imports a domain, and 113
mechanical checks enforce the direction. A domain contributes:

```text
domains/<domain>/
  workflow.py    states, transitions, validators (business rules live here)
  module.py      capabilities + directive builder + service actions
```

and reuses auth, resource scope, PEP, guardrails, PII, observability, audit,
directives, resilience and provider contracts. The travel domain is the proof: two
files, zero new infrastructure, its own tests, and a full journey through the same API.

---

## 9. Deliberate omissions

Recorded so a reviewer does not mistake them for oversights:

* **No agent-to-agent handoffs.** Every measured path uses 0. Three agents exist, each
  with a documented distinct responsibility; the supervisor carries a written
  complexity-budget justification (§5.6) that a test verifies is present.
* **Reranking is off by default.** The Ragas experiment matrix shows it matching the
  hybrid baseline on recall/precision/MRR at higher latency — so it stays off until
  evidence justifies it.
* **The Harness is in-process.** No microservice, no extra hop, no distributed
  transaction.
* **No semantic cache for customer data.** `is_cacheable()` structurally refuses
  authenticated or customer-specific responses.
* **In-memory stores.** Protocol-backed and swappable; the API layer is stateless.

---

## 10. Where to look

| Concern | Start here |
|---|---|
| Request handling | `app/api/`, `app/main.py` |
| Identity | `app/core/auth/`, `app/api/deps.py` |
| Ownership | `app/core/resource_scope/scope.py` |
| Authorization | `app/core/policy/enforcement.py` |
| Routing | `app/orchestration/router.py` |
| AI control plane | `app/ai/harness/service.py` |
| Grounding | `app/rag/retrieval/grounding.py` |
| Determinism | `app/workflows/engine/` |
| Client contract | `app/ui_directives/` |
| Integrations | `app/integrations/` |
| Evidence | `evals/`, `PROJECT_READINESS.md` |
