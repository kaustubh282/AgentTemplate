# Focused external review — ProTec Insurance AI Template

**Reviewer role:** independent Principal Engineer / Security Reviewer
**Date:** 2026-09-08
**Method:** executable falsification. Existing self-scores ignored. 40 adversarial
probes written against the real composed container plus the standard toolchain.
**Project modified:** no (probes were written and executed outside the repository;
this report is the only file added).

---

## Verdict

| | |
|---|---|
| **Overall score** | **7.5 / 10** |
| **Status** | **`NEEDS_REMEDIATION`** |

The template's *design* is genuinely strong and much of it survived deliberate
falsification: the deterministic path really is zero-model, authoritative data really
cannot be overridden by chat, RAG safety really works, resilience really works, and log
redaction really holds against adversarial input.

It does **not** reach `READY_FOR_PREPROD_VALIDATION` because of one **Critical
cross-customer data disclosure** and five **High** findings that are *application
defects, not missing external infrastructure*. Three of them are cases where a control
exists, is documented, and is covered by a passing test — but the control is not
actually wired into the path it claims to protect. That pattern is the most important
outcome of this review.

Fixing C-1 and H-1…H-5 (all small, localised changes) would move this to
`READY_FOR_PREPROD_VALIDATION` on the strength of the rest.

---

## Scores

| Area | Score | Basis |
|---|---:|---|
| Architecture | **8.0** | Layering and boundary enforcement are real (113 mechanical checks, verified). Deductions: Strands is barely used, one whole module is dead code, and the Harness's advertised collaborator list overstates what it actually invokes. |
| Harness | **6.0** | Central and provably enforced for authorization, schema and output guardrails. **Budgets and grounding are cooperative, not enforced** — a handler that skips the ledger API bypasses both. |
| Workflow safety | **7.5** | Transitions, ownership on mutation, optimistic locking and basic idempotency all survived attack. Deductions: `confirmed` is a client-supplied boolean; idempotency keys collide across actions. |
| Authentication | **9.0** | Every falsification attempt failed. Token never reaches logs or model context; unknown roles discarded; refresh tokens rejected; `alg=none` refused at two layers. |
| Resource ownership | **5.5** | Policy-level ownership is excellent (7/7 probes refused correctly). But **cross-customer session access is unprotected** and **cross-tenant enforcement is dead code**. Claimed 9.6; not sustainable. |
| Security / privacy | **6.5** | Redaction is strong against adversarial input. Deductions: card redaction is shadowed by another rule; the session leak exposes financial data. |
| Guardrails | **8.5** | Verified centrally enforced: a rogue agent's prohibited claim was blocked by the Harness, not the agent. Injection blocked before any model call at zero token cost. |
| RAG | **8.5** | Untrusted wrapping present, indirect injection neutralised, lifecycle filtering correct, abstention at zero cost. Deduction: citation attribution is coarse. |
| Ragas | **8.0** | Genuinely the Ragas framework (verified: real `ragas.metrics.collections` classes, real `MetricResult` objects, real scores). Judge metrics honestly `NOT_EVIDENCED`. The retrieval-experiment matrix is real, useful evidence. |
| DeepEval | **7.5** | Genuinely DeepEval types (`BaseMetric` subclass verified, real `LLMTestCase`). Metrics are project-owned deterministic ones — clearly labelled as such, which is the honest choice, but it means DeepEval supplies the harness rather than the judgement. |
| Native evals | **8.5** | Genuinely execute against the real composed container with exact expected/actual recording. Not a stub. |
| Authoritative data tooling | **9.0** | All 4 probes passed, including the two that matter most: chat cannot override provider truth, and a provider outage yields no fabricated premium. |
| InsureMO replaceability | **8.0** | Verified: every probed adapter method fails closed with `REQUIRES_VERIFICATION`. Contract suite genuinely applies to both implementations. Not exercised against the real API — correctly treated as external. |
| Token efficiency | **7.5** | Real, measured improvement and honest cost reporting. Deduction: the budget is bypassable and telemetry under-reported actual usage by 6 calls under attack. |
| Resilience | **9.0** | Verified working, not asserted: retry retried (3 attempts), timeout fired (47 ms), breaker actually stopped the upstream (2 of 6 calls reached it), irreversible attempted exactly once. |
| Observability | **6.5** | Redaction and metric coverage are strong. **Readiness is decorative** — it reports `ready` with every provider failing and all knowledge revoked. |
| Test quality | **7.0** | Tests do exercise real runtime paths (18 real HTTP calls; 13/19 files build the real container; no filler assertions). Deductions: three tests prove a *helper* rather than the *path*; count inflated; one unreproduced failure. |
| Code quality | **8.5** | ruff + ruff-format + mypy clean across 133 files, small cohesive modules, no TODOs, no swallowed exceptions, no hardcoded secrets. One dead module. |
| Environment separation | **7.0** | The production guard is genuinely comprehensive (9 unsafe settings refused, verified). Deduction: a `Literal` loophole means prod silently runs in-memory critical state with no error. |
| Scalability readiness | **5.0** | The app is genuinely stateless and Protocol-backed, but there is **no configurable production store at all** — so this is not merely "not yet implemented", it is not reachable. |

---

## Critical findings

### C-1 — Cross-customer session access leaks another customer's PII and premium

**Severity: Critical.** Application defect. Not external infrastructure.

Any authenticated customer who supplies another customer's `conversation_id` receives
that customer's in-flight journey. Conversation ownership is never checked on the chat
path — `Conversation.owner_subject_id` exists in the model but is not consulted
anywhere, and `handle_message` does not even create a conversation record.

Probe (victim `CUST-1001` at `REVIEW`; attacker `CUST-2002` sends `"continue"` with the
victim's conversation id):

```json
{"title": "Review before you buy", "items": [
  {"label": "Vehicle",       "value": "VictimCar Deluxe (2022)"},
  {"label": "Registration",  "value": "********34"},
  {"label": "Fuel",          "value": "PETROL"},
  {"label": "IDV",           "value": "987,654"},
  {"label": "Add-ons",       "value": "Zero Depreciation"},
  {"label": "Total premium", "value": "INR 34,399.18"}]}
```

Also returned: `flowId`, `workflowId`, `state`, and the victim's `allowed_actions`.
A FAQ turn in the same conversation additionally returns
`flowStateUnchanged: "VALIDATE"`, confirming existence and position of another
customer's journey.

**Mitigating:** workflow *mutation* is correctly refused
(`ForbiddenError: workflow_owner_mismatch`), so this is disclosure, not takeover.
Registration is masked. **Aggravating:** IDV and total premium are financial data about
an identifiable person, and only a conversation id is needed.

**Why the test suite missed it:** the E2E interruption test uses one customer for both
turns, so it never crosses the boundary it appears to guard.

**Fix:** enforce `conversation.owner_subject_id == ctx.auth.subject_id` in
`ConversationOrchestrator.handle_message` / `handle_action`, and make
`get_active(conversation_id)` scope-aware. Small change; the field already exists.

---

## High findings

### H-1 — Cross-tenant isolation is dead code on the authoritative read path

`ResourceScopeInterceptor.authorize_resource` builds the reference it is about to check
using the **caller's** tenant:

```python
ref = ResourceRef(..., tenant_id=auth.tenant_id)  # caller's tenant
self._check_tenant(auth, ref)  # compares caller to caller
```

`_check_tenant` therefore can never fail. Probe: a caller asserting `TENANT-XX`
successfully read a `TENANT-IN` resource (`available=True`).

The unit test passes because it constructs a `ResourceRef` **by hand** with the
resource's real tenant — proving the guard function, not the path. Control `AZ-08`
("cross-tenant access denied") is **NOT CONFIRMED** end to end.

### H-2 — Model-call and token budgets are cooperative, not enforced

`ledger.check_model_call()` and `record_model_call()` are called *by agents*. Nothing in
the Harness or `ModelInvoker` consults the ledger. A handler that calls
`invoker.generate()` directly bypasses both the budget and the telemetry.

Probe: a handler made **6 real model calls** against a capability budget of **1**. The
Harness returned `ALLOW` and reported `model_calls=0` — under-reporting actual spend by
six calls. This defeats both the §58 token gate and the denial-of-wallet control for any
future domain agent, and silently corrupts the cost figures the readiness report relies
on.

**Fix:** enforce in `ModelInvoker` (inject the ledger, check and record there) rather
than trusting the caller.

### H-3 — Grounding is not centrally enforced

The Harness records `grounding_decision` but never acts on it. A handler returning
`ALLOW` with an unsupported claim and **zero citations** passed unchallenged
(`outcome=ALLOW`, `grounding_decision=None`).

Abstention lives entirely in `FaqAgent`. Claim `HRN-07` ("grounding failure causes
abstention centrally") is **NOT CONFIRMED** — the passing test exercises the FAQ agent,
which abstains for itself.

### H-4 — Readiness endpoint is decorative

```text
providers broken            -> {"status": "ready", "providers": "up"}
all knowledge revoked       -> {"status": "ready", "knowledge": "up"}
```

`model` and `providers` are hardcoded strings; the knowledge check counts documents
irrespective of lifecycle status. An orchestrator would route traffic to an instance
that cannot answer anything. This is an application defect, not a platform gap.

### H-5 — Production silently runs in-memory session and workflow state

```python
session_store_provider: Literal["inmemory"]
workflow_store_provider: Literal["inmemory"]
```

There is no other permissible value, so the otherwise-thorough production guard cannot
catch it and **no configuration error is raised**. Combined with the in-memory rate
limiter, running more than one instance silently multiplies rate limits and loses
in-flight journeys. The readiness report frames this as "not yet implemented" (B1); it
is more accurate to say the configuration surface does not admit a production store.

---

## Medium findings

| # | Finding |
|---|---|
| **M-1** | **Card redaction is shadowed.** The Aadhaar pattern runs before the card pattern and consumes the first 13 characters, so `4111 1111 1111 1111` → `********1111 1111` — the last **8** digits survive. 15-digit AMEX (`3782 822463 10005`) is not matched at all. The existing test passes because it only asserts the *full* raw string is absent. |
| **M-2** | **`confirmed` is a client-supplied boolean.** A client that always sends `confirmed=true` satisfies every confirmation gate. There is no server-side evidence the user was shown a confirmation directive and acted on it (no nonce, token or challenge). This meets the letter of "explicit user confirmation", not the intent. |
| **M-3** | **Idempotency keys collide across actions.** Keys are matched per-flow, not per action+payload. Reusing one key for `CONFIRM_PURCHASE` then `COMPLETE_PAYMENT` returned `replayed=True` and silently left the flow at `PAYMENT` — a *different* legitimate action was no-opped. |
| **M-4** | **Strands is not genuinely used on the AI runtime path.** There is no `strands.Agent(...)` construction anywhere in `app/`; Strands supplies the `Model` ABC and type imports only. Agents call `ModelInvoker.generate` directly, so the SDK's agent loop, tool executor and hook dispatch never run. `app/ai/hooks/harness_hooks.py` (a full module with 6 hook callbacks) is referenced **nowhere** outside itself — dead code. The minimal-agentic *design* is defensible and ADR 0003 argues it well, but the module is architecture theatre and the framing overstates SDK usage. |
| **M-5** | **Citation attribution is coarse.** Citations list every retrieved chunk, not the ones that produced the answer: a motor question cites `KB-TRAVEL-FAQ`, and an NCB question cites both motor and travel FAQs. Grounding is real; provenance is imprecise, which matters for a regulated answer. |
| **M-6** | **3 known vulnerabilities in eval extras** (`ragas 0.4.3` PYSEC-2026-3046, `nltk 3.10.3` PYSEC-2026-3740, `diskcache 5.6.3` PYSEC-2026-2447). Verified **not reachable from `app/`** (0 imports each), so the runtime surface is clean — but the readiness report claimed scanning was simply "not executed", and running it took one command. |
| **M-7** | **Telemetry does not distinguish provider-reported from estimated tokens.** `approx_tokens()` silently substitutes when a provider reports nothing, with no flag on `ModelUsage`. `provider="deterministic"` does at least make test-double usage identifiable. |

## Low findings

| # | Finding |
|---|---|
| **L-1** | **One unreproduced test failure.** My first full run gave `1 failed, 553 passed`; 20+ subsequent runs all gave 554 passed, and the failure did not recur under isolation of the timing-sensitive suites. Unresolved — possible order or timing sensitivity. A suite claimed as a release gate should not be non-deterministic. |
| **L-2** | **Test count is inflated.** 554 collected cases come from **288 unique test functions**; 113 cases (20% of the suite) are one parametrized boundary file. The headline "554 tests" oversells breadth. |
| **L-3** | Numeric business values under arbitrary keys (e.g. `idv: 987654` inside a logged body) are not redacted — only classified field names and text patterns are. |
| **L-4** | `build_provider_bundle` constructs the full mock bundle in **every** environment including production before selecting. Mocks are never *selected* in prod (config refuses it), but the objects and fixture data are instantiated. |

---

## Top 5 strengths (all independently verified)

1. **The deterministic path really is zero-model.** A full ENTRY→REVIEW journey
   executed with **0** model calls and **0** Harness execution records. This is the
   template's central claim and it holds under direct measurement.
2. **Authoritative data handling is genuinely sound.** Premium comes from the provider
   (18450.0); asserting a false premium in chat did not change it; the AI-facing
   projection leaked no internal field and no unmasked policy number; a provider outage
   returned `available=false, data=None` with no fabrication.
3. **Resilience is real, not asserted.** Retry retried (3 attempts to success), timeout
   fired at 47 ms, the circuit breaker actually prevented upstream calls (2 of 6
   reached the dependency), and an irreversible action was attempted exactly once.
4. **Log redaction survived adversarial input.** JWT, mobile, PAN, Aadhaar, password,
   refresh token, email and raw customer id were all clean in a deliberately hostile
   log line — including nested structures.
5. **Evaluation honesty.** Ragas and DeepEval are genuinely the real frameworks
   (verified by module paths and live scores), judge-dependent metrics are reported
   `NOT_EVIDENCED` rather than fabricated, InsureMO adapters fail closed on every
   probed method, and the retrieval-experiment matrix is real evidence that
   *contradicted* the more complex option — which is how such evidence should be used.

## Top 5 weaknesses

1. **Cross-customer session access (C-1)** — a Critical data disclosure reachable with
   any valid token and a conversation id.
2. **Three controls that exist, are documented, are tested, and are not actually wired**
   — cross-tenant checking (H-1), central budget enforcement (H-2), central grounding
   enforcement (H-3). Each has a passing test that proves a helper or an agent rather
   than the path. This is the systemic weakness, and it undermines confidence in
   *other* green claims by association.
3. **Cooperative rather than enforced Harness controls (H-2, H-3).** The Harness is
   described as the single control point; for budgets and grounding it is a bookkeeper
   that any handler can bypass — and which then reports incorrect usage figures.
4. **Decorative readiness plus unreachable production stores (H-4, H-5).** Both make
   the operational posture worse than documented, and neither is an external gap.
5. **Overstated framework and test signals (M-4, L-2).** A dead hook module and a
   parametrization-inflated test count invite more confidence than the substance
   supports.

---

## Previous readiness claims: CONFIRMED / PARTIAL / NOT CONFIRMED

| Claim | Verdict | Evidence |
|---|---|---|
| Deterministic actions use 0 model calls, 0 handoffs | **CONFIRMED** | 0 model calls across a full journey; 0 Harness records |
| FAQ ≤1 model call, 0 handoffs | **CONFIRMED** | measured |
| Abstention costs 0 model calls | **CONFIRMED** | `ABSTAIN`, `model_calls=0` |
| Hallucination rate 0.000 | **CONFIRMED** | reproduced via Ragas |
| Citation correctness 1.000 | **PARTIAL** | expected source *is* always cited, but non-contributing documents are cited too (M-5) |
| Chat history cannot override provider truth | **CONFIRMED** | false premium asserted in chat; tool returned 18450.0 |
| Provider failure produces no fabricated data | **CONFIRMED** | `available=false, data=None` |
| Raw provider DTO never reaches the AI-facing view | **CONFIRMED** | 0 leaked keys, no unmasked policy number |
| Customer cannot read another customer's policy | **CONFIRMED** | refused, `resource_scope_denied` |
| Forged `customer_id` cannot widen scope | **CONFIRMED** | pinned to token subject |
| Agent role alone grants nothing; assignment required | **CONFIRMED** | 4/4 probes correct |
| Denial does not disclose existence | **CONFIRMED** | identical reason for missing vs not-owned |
| No upstream call on an unauthorized read | **CONFIRMED** | provider call count unchanged |
| **Cross-tenant access denied (AZ-08)** | **NOT CONFIRMED** | dead code; `TENANT-XX` read a `TENANT-IN` resource (H-1) |
| **Cross-customer session/conversation isolation** | **NOT CONFIRMED** | Critical disclosure (C-1) |
| Wrong-user confirmation refused | **CONFIRMED** | `workflow_owner_mismatch` |
| Illegal transitions rejected without mutating state | **CONFIRMED** | reproduced |
| Optimistic locking rejects stale writes | **CONFIRMED** | `FLOW_STATE_CONFLICT` |
| Duplicate submit is safe | **PARTIAL** | same-action replay is safe; cross-action key reuse silently no-ops a different action (M-3) |
| High-risk writes require confirmation | **PARTIAL** | enforced server-side, but `confirmed` is client-asserted with no challenge (M-2) |
| **Harness: budgets enforced across agents (HRN-04)** | **NOT CONFIRMED** | 6 calls against a budget of 1 (H-2) |
| **Harness: grounding failure abstains centrally (HRN-07)** | **NOT CONFIRMED** | rogue ALLOW passed (H-3) |
| Harness: output guardrail enforced centrally | **CONFIRMED** | rogue prohibited claim → `BLOCK` |
| Harness: malformed output rejected centrally | **CONFIRMED** | schema violation → `BLOCK` |
| Harness: deterministic capabilities refused | **CONFIRMED** | `BLOCK_AUTHORIZATION` |
| Harness is in-process, delegates, <500 lines | **CONFIRMED** | verified |
| Retrieved text treated as untrusted data | **CONFIRMED** | `<<<UNTRUSTED_DOCUMENT` present in the rendered evidence |
| Indirect injection does not change behaviour | **CONFIRMED** | poisoned doc → `ABSTAIN`, no `PWNED` |
| Lifecycle/version filtering works | **CONFIRMED** | superseded and revoked both excluded |
| No PII/secrets in logs | **PARTIAL** | credentials and PII clean; card redaction shadowed (M-1); numeric business values unredacted (L-3) |
| JWT never in logs or model context | **CONFIRMED** | both paths clean |
| Retry / timeout / circuit breaker behaviour | **CONFIRMED** | all four behaviours measured |
| Irreversible actions never retried | **CONFIRMED** | attempted once |
| Production refuses 9 unsafe settings | **CONFIRMED** | reproduced |
| **Production does not use in-memory critical adapters** | **NOT CONFIRMED** | no other value is permitted (H-5) |
| Readiness reflects dependency health | **NOT CONFIRMED** | `ready` with everything broken (H-4) |
| Ragas genuinely used | **CONFIRMED** | real framework classes and scores |
| DeepEval genuinely used | **CONFIRMED** | real `BaseMetric` subclass, real `LLMTestCase` |
| Custom/offline substitutes clearly labelled | **CONFIRMED** | `NOT_EVIDENCED` used correctly and prominently |
| InsureMO adapters fail closed | **CONFIRMED** | 5/5 probed methods |
| Native evals genuinely execute | **CONFIRMED** | drive the real container, exact expected/actual |
| Static analysis clean | **CONFIRMED** | ruff, ruff-format, mypy all pass |
| "554 tests" | **PARTIAL** | 554 cases from 288 functions; 20% from one file; one unreproduced failure |
| Dependency scanning not executed | **CONFIRMED (and now closed)** | `pip-audit`: 3 vulns, all eval-only, runtime clean |
| Overall 8.9 / `READY_FOR_PREPROD_VALIDATION` | **NOT CONFIRMED** | 7.5 / `NEEDS_REMEDIATION` — one Critical, five High |

---

## Remediation to reach `READY_FOR_PREPROD_VALIDATION`

Ordered by risk. All are small, localised changes inside the application.

1. **C-1** — check conversation ownership in the orchestrator; make `get_active`
   scope-aware. *(the field already exists)*
2. **H-1** — pass the resource's real tenant into `ResourceRef`, and add an end-to-end
   test through the tool path, not the helper.
3. **H-2** — move budget checking and usage recording into `ModelInvoker` so it cannot
   be bypassed; re-verify that reported `model_calls` equals actual.
4. **H-3** — have the Harness override an `ALLOW` when grounding is absent or failed for
   a capability that requires grounding.
5. **H-4** — make readiness actually probe: a provider ping, `corpus.active_chunks()`
   rather than `len(corpus)`, and a model-configuration check.
6. **H-5** — widen the store `Literal`s and add a production guard that refuses
   `inmemory` for session, workflow and rate-limit stores.
7. **M-1** — order the card pattern before Aadhaar and add solid/spaced/AMEX cases.
8. **M-2/M-3** — issue a server-side confirmation token; scope idempotency keys to
   action + payload hash.
9. **M-4** — either wire `HarnessControlHooks` into a real Strands agent path or delete
   the module and adjust the framing.
10. **L-1** — reproduce and fix the flaky test before treating the suite as a gate.

---

## Note on scope fairness

Consistent with the brief, the following were **not** counted as application defects,
because the template fails closed and exposes clean interfaces for each: real InsureMO
certification, penetration testing, DR execution, tamper-evident platform storage, and
approved judge-model access. The InsureMO adapters, audit sink and eval judge gating
were each probed and all behave correctly in their unconfigured state.

What *was* counted: controls that are claimed and tested but not wired (H-1, H-2, H-3),
an endpoint that reports health it does not measure (H-4), a configuration surface that
cannot express a production choice (H-5), and a cross-customer data disclosure (C-1).
None of these requires external infrastructure to fix.
