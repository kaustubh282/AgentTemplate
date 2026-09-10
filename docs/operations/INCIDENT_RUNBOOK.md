# Incident runbook

Severity taxonomy, AI-specific incident categories, and per-dependency response
procedures.

> **`REQUIRES_VERIFICATION`** — statutory reporting timelines, regulator contacts and
> escalation contacts are deliberately **not** in this document and **not** in the
> application. They belong in a compliance-owned checklist maintained from approved
> sources. Placeholders below marked `[COMPLIANCE]` must be completed by Compliance.

---

## 1. Severity taxonomy

| Sev | Definition | Response | Examples |
|---|---|---|---|
| **SEV1** | Customer harm, data breach, or the assistant is stating false regulated facts at scale | Immediate; page on-call; incident commander | PII leak; hallucinated premium reaching customers; cross-customer data exposure |
| **SEV2** | Major function unavailable or a security control failed | Within 30 min | FAQ down; auth bypass; audit sink failing; guardrail disabled |
| **SEV3** | Degraded quality or elevated cost | Within 4 h | abstention-rate spike; token regression; provider degraded |
| **SEV4** | Minor or cosmetic | Next business day | a single knowledge gap; one directive rendering oddly |

---

## 2. AI-specific incident categories

Conventional runbooks omit these.

| Category | Signal | Immediate action |
|---|---|---|
| **AI-01 Hallucination reaching customers** | grounding-failure spike; a customer complaint with a specific figure | Raise `GUARDRAIL_GROUNDING_MIN_SCORE`; if unresolved, disable the FAQ capability via feature flag |
| **AI-02 Prompt-injection breakthrough** | guardrail blocks spike then fall; unexpected output shape | Preserve the request id; raise `GUARDRAIL_INJECTION_BLOCK_SCORE`; add the pattern; add the case to `adversarial.jsonl` |
| **AI-03 Stale knowledge served** | `protec_stale_document_hits_total` above 0 | Revoke the document; verify the corpus version changed; caches invalidate automatically |
| **AI-04 Token / cost runaway** | median input tokens above baseline; cost ceiling breached | Compare with the baseline; check the zero-model-call rate; tighten `MAX_CONTEXT_TOKENS` |
| **AI-05 Model provider outage** | `protec_fallback_total` rising; `MODEL_UNAVAILABLE` errors | Confirm deterministic paths still work; do **not** enable fallback for high-risk capabilities |
| **AI-06 Denial of wallet** | rate-limit rejects plus cost spike from few subjects | Lower `RATE_LIMIT_AI_PER_MINUTE`; block the subject at the gateway |
| **AI-07 Corpus corruption** | zero-result rate spike; ingestion errors | Freeze ingestion; verify checksums; restore the previous corpus |
| **AI-08 Prompt regression** | quality drop after a prompt change | Roll back the prompt version; re-run the eval suite |

---

## 3. Dependency response procedures

### Model provider unavailable

**Symptoms** `MODEL_TIMEOUT` / `MODEL_UNAVAILABLE`; `protec_fallback_total` rising.

**What still works** every deterministic path — the whole transactional journey,
authoritative reads, all UI actions. Verified by
`test_deterministic_workflow_still_works_while_the_model_is_down`.

**What degrades** FAQ returns a controlled fallback message. **It does not invent an
answer**, and the payload is empty.

**Actions**
1. Confirm deterministic paths are healthy (`/health/ready`, flow metrics).
2. Check the provider status page and your quota.
3. If a secondary provider is approved, enable it — but **not** for high-risk
   capabilities; `allows_fallback()` refuses those by design.
4. Communicate that FAQ is degraded while purchase and service journeys work.

**Must not happen** answering from model memory; converting a timeout into a success.

### Retrieval / vector store unavailable

**What still works** every deterministic path.

**What degrades** FAQ returns controlled unavailability.

**Actions**
1. Check `protec_retrieval_zero_result_total` and ingestion logs.
2. Re-ingest from the approved source if the index is corrupt.
3. Verify the corpus version changed after re-ingestion.

**Must not happen** falling back to model memory for policy-specific facts. Verified by
`test_rag_failure_returns_controlled_unavailability_not_model_memory`.

### Provider / InsureMO unavailable

**What still works** FAQ, local validation, any capability not touching that provider.
Verified by `test_partial_dependency_outage_leaves_other_capabilities_working`.

**What degrades** quote, payment and issuance. **State is preserved** and the user gets
a retry directive.

**Actions**
1. Check `protec_provider_failures_total` and `protec_circuit_open_total`.
2. Confirm the circuit opened (it should — that protects the upstream).
3. Confirm no in-flight journey advanced incorrectly: `quote_id` must be absent from
   states that failed.
4. After recovery, the breaker half-opens automatically; the same action succeeds.

**Must not happen** a fabricated quote; a timeout treated as success; state advancing
past a failed side effect.

### Cache failure

Bypass the cache and use the authoritative path within budget. Never serve a stale or
unsafe entry. Cache keys embed corpus, prompt and guardrail versions, so a version
change cannot produce a stale hit.

### Observability backend failure

Telemetry is fail-open by construction: span export runs on the OpenTelemetry batch
processor, which drops spans when the collector is unreachable and never raises into a
request. Requests continue, observability degrades. Acceptable. Metrics are scraped,
so a Prometheus outage loses samples, not requests.

### Audit sink failure

`AUDIT_FAIL_CLOSED=true`: requests **fail** with a retryable error rather than
proceeding unaudited. **This is intended.** If you are considering flipping the flag,
that is a decision for InfoSec and Compliance, not for the on-call engineer.

---

## 4. Security incident checklist

1. **Preserve evidence.** Capture request ids, correlation ids, actor refs, audit
   events. Do not delete logs.
2. **Contain.** Block the subject or IP at the gateway; tighten the relevant guardrail
   threshold; disable the affected capability by feature flag if needed.
3. **Assess.** Which control failed? Which layer should have caught it? Was any data
   actually disclosed, or was it only attempted?
4. **Notify.** `[COMPLIANCE]` — internal notification path and timing.
5. **Remediate.** Add the pattern; add a regression case to the eval dataset; re-run
   the security suite.
6. **Verify.** `python scripts/run_evals.py security` must pass, including the new case.
7. **Review.** Blameless post-incident review; update the threat model.

---

## 5. PII leakage response checklist

1. **Stop the leak.** Disable the affected capability by feature flag.
2. **Scope it.** Which field, which channel (log / trace / audit / response / model
   context), how many requests, over what window? Use `requestId` and `correlationId`.
3. **Determine whether data actually left the boundary.** A masked value in a log is
   not a disclosure; a raw value sent to a model provider is.
4. **Preserve evidence** without copying the leaked data into a new location.
5. **Notify.** `[COMPLIANCE]` — DPDP and CERT-In obligations, timelines and contacts.
   **Deliberately not coded.**
6. **Remediate.** Classify the field; add it to `FIELD_CLASSIFICATION`; add a boundary
   test.
7. **Verify.** `pytest tests/unit/test_pii_boundaries.py` and
   `python -m evals.native.run pii` must pass with the new case.
8. **Purge** where required and technically possible; record what could not be purged.

---

## 6. Rollback

Every one of these is independently reversible:

| Surface | How |
|---|---|
| Application version | redeploy the previous image |
| Prompt version | restore the previous prompt YAML; version appears in audit |
| Knowledge document | revoke via `POST /api/v1/admin/knowledge/lifecycle` |
| Knowledge corpus | re-ingest the previous set; corpus version changes |
| Model configuration | change `MODEL_*` and restart |
| Guardrail thresholds | change `GUARDRAIL_*` and restart |
| Feature flags | change `FEATURE_*` and restart |
| Workflow definition | redeploy; in-flight flows carry their own `workflow_version` |

After any rollback: `python scripts/run_evals.py all` and confirm the benchmark shows
no regression against `evals/regression/baseline.json`.

---

## 7. Degradation matrix (§20.1)

| Dependency down | Allowed behaviour | Forbidden behaviour | Test |
|---|---|---|---|
| Model | deterministic workflow and actions continue | inventing AI output or business facts | `test_deterministic_workflow_still_works_while_the_model_is_down` |
| RAG | deterministic actions continue; FAQ returns controlled unavailability | model-memory fallback for policy facts | `test_rag_failure_returns_controlled_unavailability_not_model_memory` |
| Provider / InsureMO | unaffected read-only and local capabilities continue | fabricated quote, policy or claim success | `test_provider_failure_preserves_transaction_state` |
| Cache | bypass and use the authoritative path | stale or unsafe cache serving | `test_cache_expiry_prevents_a_stale_hit` |
| Observability | follow the configured fail-open policy | silently dropping required audit events | `test_a_failing_audit_sink_fails_closed` |

Every row is executable. `python -m evals.native.run reliability`

---

## 8. On-call quick reference

```bash
# health
curl -s $BASE/api/v1/health/ready | python -m json.tool

# metrics snapshot (needs finops:read)
curl -s $BASE/api/v1/internal/metrics -H "Authorization: Bearer $TOKEN"

# cost and token report
curl -s $BASE/api/v1/admin/finops/report -H "Authorization: Bearer $TOKEN"

# knowledge state
curl -s $BASE/api/v1/admin/knowledge -H "Authorization: Bearer $TOKEN"

# revoke a document immediately
curl -X POST $BASE/api/v1/admin/knowledge/lifecycle \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"document_id":"KB-MOTOR-FAQ","status":"REVOKED"}'
```

---

## 9. Contacts

`[COMPLIANCE]` and `[INFOSEC]` — to be completed and maintained by the accountable
teams:

| Role | Contact | Escalation window |
|---|---|---|
| On-call engineer | | |
| Incident commander | | |
| InfoSec | | |
| Data protection officer | | |
| Compliance | | |
| Regulator notification owner | | |
| InsureMO counterpart | | |
| Model provider support | | |

Left blank deliberately. Inventing a contact or a statutory timeline would be worse
than an obvious gap.
