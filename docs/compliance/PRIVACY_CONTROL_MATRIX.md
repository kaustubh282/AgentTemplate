# Privacy control matrix

Per master prompt §11. Each row records a control, its source, the interpretation this
template assumed, the technical evidence, and who must confirm it.

**No legal interpretation is hard-coded in the application.** Where an interpretation
was needed in order to write code, it is stated here as an assumption and marked
`REQUIRES_VERIFICATION`.

| ID | Requirement | Source | Interpretation assumed | Technical control | Evidence | Owner | Env | Status |
|---|---|---|---|---|---|---|---|---|
| PRV-01 | Collect only what the purpose needs | DPDP minimisation | A field at or above `PII` must not reach a model unless the capability declares it required for the current purpose | `ModelInputSanitizer.sanitize_payload` with `purpose_fields` | `pytest -k unnecessary_pii_is_removed` | Legal + Eng | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-02 | Purpose limitation | DPDP | Purpose is expressed per capability, not per session | `CapabilityRequest.purpose_fields` | `pytest -k pii_needed_for_the_purpose` | Legal | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-03 | Notice and consent reference | DPDP | Not implementable without the approved consent architecture | metadata hooks only | none | Legal | all | `GAP` |
| PRV-04 | Storage limitation | DPDP | Conversation data is convenience context and may be bounded and expired | TTL plus a 20-turn window; `CONVERSATION_RETENTION_DAYS` | session TTL tests | Legal | all | `REQUIRES_VERIFICATION` |
| PRV-05 | Erasure on request | DPDP | Conversation and workflow data are erasable; audit records hold no conversation content and are governed separately | `ConversationStore.delete`, `WorkflowStateStore.delete` | interface implemented | Legal + Eng | all | `REQUIRES_VERIFICATION` |
| PRV-06 | Access and portability | DPDP | Requires an export surface that does not exist yet | none | none | Legal + Eng | all | `GAP` |
| PRV-07 | Accuracy of personal data | DPDP | Authoritative data is read from the System of Record, never from conversation memory | typed authoritative tools | `pytest -k conversation_history_cannot_override` | Product | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-08 | Security safeguards | DPDP | Classification-driven masking and boundary enforcement | `app/core/privacy/` | 57 PII tests | InfoSec | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-09 | Processing record | DPDP | An audit trail with pseudonymous actors satisfies the record need without storing content | `AuditService` | audit tests | Compliance | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-10 | Breach notification | DPDP + CERT-In | Timelines and contacts must be configurable and compliance-owned, never coded | PII checklist in the runbook | runbook | Legal + InfoSec | all | `REQUIRES_VERIFICATION` |
| PRV-11 | Cross-border transfer | DPDP | Model and judge provider region is a deployment decision, not an application one | `MODEL_PROVIDER`, `MODEL_BASE_URL` | config tests | Legal | preprod, prod | `REQUIRES_VERIFICATION` |
| PRV-12 | Data of minors | DPDP | No age-gating exists; travel and health products may need it | none | none | Legal + Product | all | `GAP` |
| PRV-13 | No PII in logs | Internal privacy policy | Redaction must be structural, not left to caller discipline | `RedactingJsonFormatter` | `pytest -k no_pii_or_secret_reaches_the_log` | InfoSec | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-14 | No PII in traces | Internal | Free-text span attributes are refused outright | tracer forbidden-key filter | `pytest -k traces_carry_no_prompt` | InfoSec | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-15 | Pseudonymisation for operations | Internal | Operators need correlation, not identity | `subject_ref`, `conversation_ref`, IP hashing | `pytest -k log_fields_are_pseudonymous` | InfoSec | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-16 | Third-party personal data refused | DPDP + conduct | A request for another person's record is exfiltration, not a knowledge question | `third_party_personal_data` guardrail | `pytest -k third_party_data_request` | Legal + InfoSec | all | `IMPLEMENTED_PENDING_REVIEW` |
| PRV-17 | Sensitive personal data | DPDP | Health and financial identifiers are classified one level above ordinary PII | `SENSITIVE_PII` class | classification tests | Legal | all | `REQUIRES_VERIFICATION` |
| PRV-18 | Vendor and sub-processor disclosure | DPDP | Model and provider vendors must be disclosed; the template does not choose them | provider and model abstraction | contract tests | Legal + Vendor mgmt | all | `REQUIRES_VERIFICATION` |

---

## Assumptions made in order to be buildable

Each is a deliberate, reversible engineering decision recorded for review — **not** a
legal conclusion.

1. **Conversation transcripts are not the system of record** and may be bounded and
   expired. If Legal requires full transcript retention,
   `CONVERSATION_RETENTION_DAYS` and the turn window must change.
2. **Audit events are not erased** by a data-subject erasure request, because they
   contain no conversation content. This needs explicit confirmation.
3. **Pseudonymous references in logs are sufficient** for operational use. If logs must
   be fully identity-free, `subject_ref` must be removed as well.
4. **Masked identifiers (last four characters) are acceptable** in audit records. If
   not, `MaskingService` must move to full redaction for those fields.
5. **A four-turn history window is enough** for conversational quality. Increasing it
   increases both token cost and the personal data in model context.

---

## Verification

```bash
python -m pytest tests/unit/test_pii_boundaries.py -q   # 57 tests
python -m evals.native.run pii                          # 17/17
python scripts/benchmark.py | grep "PII leakage"        # expects 0
```
