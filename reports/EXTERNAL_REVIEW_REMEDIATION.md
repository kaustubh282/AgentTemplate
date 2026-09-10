# Remediation of the focused external review

**Responds to:** [`FOCUSED_EXTERNAL_REVIEW.md`](FOCUSED_EXTERNAL_REVIEW.md) (2026-09-08, verdict 7.5 / `NEEDS_REMEDIATION`)
**Date:** 2026-09-08
**Method:** every finding was reproduced as an executable probe first
(`tests/security/test_external_review_findings.py`, one test class per finding id),
then fixed in the application, then re-verified by the full suite, `ruff`, `mypy`,
the three eval layers and the benchmark. Nothing below is asserted without a command.

The review's most important observation was systemic: *three controls existed, were
documented and were covered by a passing test, but were not wired into the path they
claimed to protect*. Each of those is now enforced structurally, and the test that
proved the helper has been replaced by one that proves the path.

---

## Verdict on each finding

| # | Finding | Status | Where | Probe |
|---|---|---|---|---|
| **C-1** | Cross-customer session access leaked another customer's PII and premium | **FIXED** | `ConversationOrchestrator._open_conversation`; `WorkflowEngine.get_active(conversation_id, ctx)` and `start()` now assert ownership | `pytest -k TestC1` (6 tests, incl. the reviewer's `continue` probe) |
| **H-1** | Cross-tenant isolation was dead code (compared caller with caller) | **FIXED** | `ResourceScopeInterceptor.authorize_resource` builds `ResourceRef` from the System-of-Record tenant via `OwnershipResolver -> ResourceOwnership`; `_check_tenant` fails closed on an unresolved tenant | `pytest -k "cross_tenant or TestH1"` — through the tool path, not the helper |
| **H-2** | Model-call and token budgets were cooperative | **FIXED** | Budget is checked and charged inside `ModelInvoker`, bound per request by `budget.govern()` (a `ContextVar`); a call outside any Harness request is refused (`BLOCK_UNGOVERNED_MODEL_CALL`); ledger counters are copied to the record on *every* path, including blocks | `pytest -k TestH2` — the 6-call probe now stops after 1 and reports `model_calls == 1` |
| **H-3** | Grounding was not centrally enforced | **FIXED** | `GroundingPolicy` (`app/ai/harness/policies/grounding_policy.py`) runs inside the Harness for every capability with `requires_grounding=True`; the FAQ agent no longer calls `assess_answer` at all | `pytest -k "TestH3 or grounding_failure or inherits_every_control"` |
| **H-4** | Readiness was decorative | **FIXED** | Readiness counts *retrievable* chunks, probes the configured providers, reads the provider circuit breaker and the model's failure streak, and returns **503** when degraded | `pytest -k TestH4` (6 tests, incl. providers-broken and all-knowledge-revoked) |
| **H-5** | Production silently ran in-memory critical state | **FIXED** | Store `Literal`s widened to `inmemory \| redis`; `RATE_LIMIT_STORE_PROVIDER` added; production **refuses** `inmemory` for all three; **Redis adapters shipped** for session, workflow (WATCH/MULTI optimistic locking) and rate-limit stores, proven multi-instance against an in-process emulator | `pytest -k "TestH5 or test_shared_stores"` |
| M-1 | Card redaction shadowed by the Aadhaar pattern; AMEX unmatched | **FIXED** | Card pattern runs first; covers 16-digit solid/spaced/dashed and 15-digit AMEX | `pytest -k TestM1` (5 card shapes, zero digits survive) |
| M-2 | `confirmed` was a client-supplied boolean | **FIXED** | `ConfirmationService` issues HMAC tokens bound to `flow_id \| action \| state version` in `meta.confirmationTokens`; `confirmed=true` is honoured only with the matching token; `CONFIRMATION_TOKEN_SECRET` is required in prod | `pytest -k TestM2` (no token, forged token, wrong action, happy path) |
| M-3 | Idempotency keys collided across actions | **FIXED** | Keys are recorded as `IdempotencyRecord(key, action, payload_hash)`; reuse for a different action or payload is `IDEMPOTENCY_CONFLICT` (409) | `pytest -k TestM3` |
| M-4 | Strands hook bridge was dead code; framing overstated SDK usage | **FIXED** | `app/ai/hooks/` deleted; ADR 0003 and README reworded to say exactly what Strands supplies today; a mechanical dead-module test now fails on any unreferenced module | `pytest -k every_module_is_referenced` |
| M-5 | Citation attribution was coarse | **FIXED** | `GroundingService.contributing_document_ids` cites only chunks that cover the answer (best-covering chunk always kept); the Harness stamps the filtered citations | Ragas `citation_correctness` re-run (see readiness report) |
| M-6 | 3 known vulnerabilities in eval extras | **SCANNED / ACCEPTED** | `pip-audit` executed: `ragas 0.4.3`, `nltk 3.10.3`, `diskcache 5.6.3` — **no fix version released** for any; 0 imports from `app/`; eval-only extras | `python -m pip_audit` |
| M-7 | Estimated vs provider-reported tokens indistinguishable | **FIXED** | `ModelUsage.tokens_estimated`, `HarnessExecutionRecord.tokens_estimated`, metric `protec_model_tokens_estimated_total` | `pytest -k TestM7` |
| L-1 | One unreproduced test failure | **NOT REPRODUCED** | 7 consecutive full runs (599 tests) plus one reversed-collection-order run: all green. Left open as a watch item; no timing-sensitive test was changed to hide it | `pytest` ×7, `-p reverse_plugin` ×1 |
| L-2 | Test count inflated by parametrisation | **REPORTED HONESTLY** | Readiness now states both figures: 599 collected cases from 327 unique test functions | `pytest --collect-only` |
| L-3 | Numeric business values under arbitrary keys unredacted | **FIXED** | `BUSINESS_VALUE_KEYS` (idv, premium, sum insured, amount, …) redacted in logs regardless of key nesting; the model boundary is deliberately unaffected | `pytest -k l3_numeric` |
| L-4 | Mock bundle constructed in every environment | **FIXED** | Only the selected implementation is instantiated; production never builds mocks or fixture data | `pytest -k l4_only_the_selected` |

---

## What changed structurally

1. **Budgets moved from the agents into the invoker.** Three agents lost their
   `check_model_call` / `record_model_call` calls; the static test that forbids
   duplicated controls in agents now lists those names as forbidden. The Harness binds
   the ledger with `with govern(ledger):` around the handler.
2. **Grounding moved from the FAQ agent into a Harness policy object.** The agent
   proposes an answer and candidate citations and sets `agent_ctx.retrieval`; the
   `GroundingPolicy` decides. A cached answer is re-grounded against fresh retrieval, so
   the cache saves the model call but never bypasses grounding. Output guardrails run
   *before* grounding so a prohibited claim is `BLOCK`, not merely `ABSTAIN`.
   `service.py` stays under 500 lines (490) because the policy lives in
   `app/ai/harness/policies/`.
3. **Ownership now guards reads as well as writes.** The orchestrator opens every
   conversation through one gate; the engine's `get_active` takes the caller's context.
4. **Client input no longer proves confirmation.** A server-minted, action- and
   version-bound token does.
5. **Configuration can express a production store choice** and refuses the local one.
   The Redis adapters are shipped; a `redis` provider without a connection is a
   configuration error, never an in-memory fallback.

---

## Re-verification

| Gate | Result | Command |
|---|---|---|
| Tests | **655 passed, 0 failed** (357 unique functions) | `python -m pytest -q` |
| Repeat runs | repeated full runs green; reversed order green | see L-1 |
| Lint | PASS | `python -m ruff check .` |
| Format | PASS | `python -m ruff format --check .` |
| Types | PASS (strict, 140 files) | `python -m mypy` |
| Dependency scan | executed; 3 eval-only findings, no fix released, 0 reachable from `app/` | `python -m pip_audit` |
| Native / Ragas / DeepEval / benchmark | see `PROJECT_READINESS.md` (re-run after remediation) | `python scripts/run_evals.py all`, `python scripts/benchmark.py` |

---

## Claims the review marked NOT CONFIRMED — now

| Claim | Review | Now | Evidence |
|---|---|---|---|
| Cross-tenant access denied (AZ-08) | NOT CONFIRMED | **CONFIRMED** through the tool path | `TestH1`, `test_cross_tenant_access_is_denied` |
| Cross-customer session isolation | NOT CONFIRMED (Critical) | **CONFIRMED** | `TestC1` |
| Budgets enforced across agents (HRN-04) | NOT CONFIRMED | **CONFIRMED** at the invoker | `TestH2` |
| Grounding failure abstains centrally (HRN-07) | NOT CONFIRMED | **CONFIRMED** in the Harness | `TestH3`, `test_a_new_agent_inherits_every_control_without_copying_code` |
| Production does not use in-memory critical adapters | NOT CONFIRMED | **CONFIRMED** (refused at startup) | `TestH5` |
| Readiness reflects dependency health | NOT CONFIRMED | **CONFIRMED** (503 when degraded) | `TestH4` |
| Duplicate submit safe | PARTIAL | **CONFIRMED** (cross-action reuse is a 409) | `TestM3` |
| High-risk writes require confirmation | PARTIAL | **CONFIRMED** (server-issued evidence) | `TestM2` |
| No PII/secrets in logs | PARTIAL | **CONFIRMED** within scope (card + numeric business values) | `TestM1`, `l3` |

---

## Still open (unchanged by this remediation, all outside the application)

* confirming the shipped Redis adapters against a managed Redis in preprod
* WORM / object-lock storage for the (now tamper-evident) chained audit log
* penetration test · InsureMO certification · judge-model evaluation · platform-layer
  DR (region failover, backup restore) · container/SBOM scanning · WCAG audit
