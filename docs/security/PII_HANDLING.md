# PII handling

How personal data is classified, minimised, masked and kept out of logs, traces, audit
records and model context.

The two controls that carry most of the weight:

1. **Secrets are removed, not masked.** Absence is a stronger guarantee than
   obfuscation.
2. **Data minimisation is default-deny at the model boundary.** A field at or above
   `PII` is dropped unless the capability explicitly declared it as needed for the
   current purpose.

---

## 1. Classification

`app/core/privacy/classification.py`

| Class | Examples | Model context | Logs |
|---|---|---|---|
| `PUBLIC` | product names, published FAQ text | allowed | allowed |
| `INTERNAL` | capability ids, states, outcomes | allowed | allowed |
| `CONFIDENTIAL` | policy number, claim number, quote id, payment reference | masked | masked |
| `PII` | name, mobile, email, address, DOB, PAN, vehicle/registration, customer id | **removed unless purpose-declared** | masked |
| `SENSITIVE_PII` | Aadhaar, health condition, diagnosis, bank account, IFSC | **removed unless purpose-declared** | masked |
| `SECRET` | password, OTP, PIN, access/refresh/ID token, `Authorization`, API key, private key, CVV, card number | **never** | **never** (`[REDACTED]`) |

Unknown field names default to `INTERNAL`, and substring matching catches variants
(`customer_mobile_number` → `PII`).

### Never logged, under any condition

```text
password  passwd  otp  pin  access_token  refresh_token  id_token
authorization  api_key  secret  client_secret  private_key
encryption_key  cvv  card_number  bearer  jwt  session_secret
```

---

## 2. The masking service

`app/core/privacy/masking.py` — one implementation, injected everywhere data leaves
the core. It works two ways, because free text carries personal data too.

**Structural** (by field name):

| Field | Raw | Masked |
|---|---|---|
| `mobile` | `9876543210` | `******3210` |
| `email` | `asha.verma@example.com` | `a***@example.com` |
| `pan` | `ABCDE1234F` | `********4F` |
| `aadhaar` | `234567890123` | `********0123` |
| `policy_number` | `PTC0000001234` | `*******1234` |
| `password` | anything | `[REDACTED]` |

**Content** (by pattern, applied to any free text): Indian mobile numbers, email
addresses, PAN, Aadhaar, payment card numbers, vehicle registrations, JWTs and
`Authorization` header values.

Redaction is recursive through nested dicts and lists, with a depth limit, and
`bytes` are always redacted.

---

## 3. Logs

`app/core/logging/structured.py` installs a formatter that routes **every** log message
and **every** extra field through the masking service. This is deliberate: a developer
cannot leak raw PII by writing a careless log line.

```json
{
  "timestamp": "2026-09-08T14:22:11.482Z",
  "severity": "INFO",
  "service": "protec-insurance-ai",
  "environment": "test",
  "requestId": "req_8f2c...",
  "correlationId": "corr_a91d...",
  "conversationRef": "conv_004821337265",
  "actorRef": "sub_918273645500",
  "channel": "WEB_CUSTOMER",
  "message": "authoritative_read_unavailable",
  "tool": "GetPolicyPremium"
}
```

Note what is absent: no `customer_id`, no raw `conversation_id`, no premium, no policy
number, no stack trace. Exceptions log a type and a masked message only.

`actorRef` and `conversationRef` are stable pseudonyms, so an operator can correlate a
session end to end without learning who the customer is.

Verify: `pytest tests/unit/test_pii_boundaries.py -k log`.

---

## 4. Traces

Spans carry identifiers, categories and measurements. A forbidden-key filter drops
`prompt`, `system_prompt`, `messages`, `document_text`, `chunk_text`, `answer` and
`user_message` if any code ever attempts to attach them.

Verify: `test_traces_carry_no_prompt_or_document_text`.

---

## 5. The model boundary

The most tightly controlled boundary in the platform.
`app/core/privacy/model_boundary.py`

```text
payload
  |
  +-- never-loggable field or SECRET class ......... REMOVED (recorded in the report)
  +-- >= PII and not in purpose_fields ............. REMOVED
  +-- >= CONFIDENTIAL .............................. MASKED
  +-- free-text string ............................. pattern-masked
  |
  v
assert_no_secrets(rendered_context)  -> GuardrailBlockedError("BLOCK_PII_POLICY")
```

`purpose_fields` implements purpose limitation concretely: a capability that genuinely
needs a mobile number to proceed declares it, and even then the value is minimised
rather than passed raw.

### AI-facing DTOs

Provider and domain objects never reach a model. Each AI-facing DTO declares
`ai_fields`, and the projection drops everything else:

```python
class AiPolicySummary(BaseModel):
    ai_fields: ClassVar[tuple[str, ...]] = (
        "policy_number_masked",
        "product",
        "status",
        "annual_premium",
        "addons",
        "renewal_due",
    )
```

`customer_id`, `policy_id`, `source_system`, `sum_insured` and `inception_date` are
**absent**, not masked. Verify:
`test_raw_provider_dto_never_reaches_the_ai_facing_view`.

### Output

Model output is scanned twice — by the guardrail (blocks secret-shaped content and
markup, sanitises Aadhaar/PAN/card/JWT patterns) and by `ModelOutputScanner`.

---

## 6. Audit records

Audit events are evidence about decisions, not a copy of the conversation. They carry:

* pseudonymous `actor_ref`
* **masked** `resource_ref` (`*******1234`)
* action, result, reason code
* the version stamp that made the decision reproducible
* small, redacted attributes

They never carry raw prompts, model output, chain-of-thought, amounts or identifiers.

Verify: `test_audit_records_contain_no_unredacted_pii`,
`test_authoritative_read_is_audited_without_sensitive_payload`.

---

## 7. Storage and retention

| Store | Content | Default retention | Config |
|---|---|---|---|
| Conversation | bounded recent turns (20 max) | 30 days | `CONVERSATION_RETENTION_DAYS` |
| Workflow state | typed journey state | session TTL (1 h) | `SESSION_TTL_SECONDS` |
| Audit | decision evidence, no content | 7 years | `AUDIT_RETENTION_DAYS` |
| Knowledge index | approved documents | lifecycle-managed | — |

Conversation history is bounded rather than retained indefinitely: old turns are
dropped, not archived. `REQUIRES_VERIFICATION` — all three retention periods need
Legal and Compliance confirmation, as does whether an erasure request should affect
audit records (it currently does not, because they contain no conversation content).

---

## 8. Data-subject request hooks

| Request | Mechanism | Status |
|---|---|---|
| Erasure | `ConversationStore.delete`, `WorkflowStateStore.delete` | interface implemented |
| Access / export | provider reads + masked projections | partial — no export endpoint |
| Purpose limitation | `purpose_fields` at the model boundary | implemented |
| Processing trail | audit events | implemented |
| Consent reference | metadata field | **not implemented** |

No legal interpretation is hard-coded. See
[`PRIVACY_CONTROL_MATRIX.md`](../compliance/PRIVACY_CONTROL_MATRIX.md).

---

## 9. Developer rules

**Do**

* pass a `RequestContext`; derive identity from `ctx.auth`
* project to an AI-facing DTO before anything reaches a model
* declare `purpose_fields` when a capability genuinely needs a PII field
* log identifiers and categories, not values

**Do not**

* put a raw provider or domain object into model context
* accept `customer_id` from a request payload when the token can determine it
* add a new PII-bearing field without classifying it
* log a full request body
* mask a secret instead of removing it

---

## 10. Verify

```bash
python -m pytest tests/unit/test_pii_boundaries.py -q     # 57 tests
python -m evals.native.run pii                            # 17/17
python scripts/benchmark.py | grep "PII leakage"           # expects 0
```

Current results: 57/57 tests pass, native PII suite 17/17, benchmark PII leakage
incidents **0**.
