# Data flow

Where data comes from, what it is allowed to touch, and what is stripped at each
boundary. Written so a privacy or security reviewer can trace any field end to end.

---

## 1. The four stores, kept separate

They are deliberately distinct (§41) so retention, encryption and access can differ:

| Store | Holds | Authoritative? | Retention | Code |
|---|---|---|---|---|
| Workflow state | typed, versioned journey state | yes, for the in-flight journey | `SESSION_TTL_SECONDS` | `app/workflows/state/store.py` |
| Conversation | bounded recent turns | **no** | `CONVERSATION_RETENTION_DAYS` | `app/orchestration/session.py` |
| Knowledge index | approved documents + provenance | yes, for knowledge | lifecycle-managed | `app/rag/governance/` |
| Audit events | append-only decision evidence | yes, for what happened | `AUDIT_RETENTION_DAYS` | `app/core/audit/` |

Conversation memory is **convenience context only**. After a transaction completes,
the booked policy lives in the System of Record; asking "what was my premium?" later
reads it back through a typed tool and never replays the chat.

---

## 2. Inbound: request to trusted context

```text
Authorization: Bearer <JWT>
        |
        v  app/api/deps.py  (the ONLY place a token is parsed)
JwtValidationService.validate
    signature | alg allow-list | iss | aud | exp | nbf | iat | sub | token_use
        |
        v
AuthContext  { subject_id, actor_type, roles, permissions, tenant_id,
               session_id, token_id, assigned_customer_ids }
        |
        |  no raw token, no unmapped claims, no refresh token
        v
RequestContext { request_id, correlation_id, conversation_id, channel,
                 auth, client_ip_hash, idempotency_key, environment }
```

Three things are dropped or transformed here on purpose:

* the **raw token** never leaves `deps.py`
* **unknown role strings** are discarded rather than trusted (`SUPER_ADMIN` → gone)
* the **client IP** is SHA-256 truncated before it reaches a log, metric or limiter
* an `assigned_customer_ids` claim on a non-agent token is **ignored**

---

## 3. Identity to authorization

```text
AuthContext
   |
   +-- effective_customer_id(auth, requested)
   |      CUSTOMER -> always auth.subject_id       (a supplied id cannot widen scope)
   |      AGENT    -> must name a customer, then be assigned to it
   |
   +-- authorize_customer_scope   <- checked BEFORE the SoR is touched
   |
   +-- resolve_owner (provider)   <- authoritative ownership, not caller-asserted
   |
   +-- owner != scope  ->  ForbiddenError("resource_scope_denied")
```

Order matters. Because the caller's authority is settled first, an unassigned agent is
refused **without any lookup** — so the denial cannot disclose whether the resource
exists, and an unauthorized caller never causes an upstream call.

A missing resource and a resource owned by someone else produce the *same* reason code
and the same user-facing message.

---

## 4. Outbound to the model

This is the most tightly controlled boundary in the system.

```text
user message + retrieved evidence + workflow state + tool results
        |
        v  ModelInputSanitizer
 (1) SECRET class            -> REMOVED (never masked; absence is the control)
 (2) >= PII and not a declared purpose field -> REMOVED
 (3) >= CONFIDENTIAL         -> MASKED  ("*******1234")
 (4) free text               -> pattern-masked (mobile, email, PAN, Aadhaar, card, JWT)
        |
        v  ContextBuilder  (priority order, each with a token budget)
 1. security / system instructions
 2. workflow state ............ only the fields the workflow declared
 3. retrieved evidence ........ bounded, wrapped as <<<UNTRUSTED_DOCUMENT>>>
 4. recent turns .............. bounded window, oldest dropped first
 5. tool results .............. AI-facing DTOs only, truncated to budget
        |
        v  assert_no_secrets(rendered)   <- structural final check
        v  ledger.check_context(tokens)  <- hard ceiling, else BLOCK_TOKEN_BUDGET
        |
        v
     provider
```

### Retrieved text is data, never instruction

```text
<<<UNTRUSTED_DOCUMENT source="Motor Insurance FAQ v2.0#What is IDV">>>
IDV stands for Insured Declared Value...
<<<END_UNTRUSTED_DOCUMENT>>>
```

Delimiters inside the payload are neutralised, so a poisoned document cannot close the
block and continue as instructions. Every retrieved chunk is additionally injection-
scanned before it is used.

### Evidence and question are passed as *parts*

The FAQ prompt template owns the `EVIDENCE` and `QUESTION` slots, so the agent passes
`built.evidence_block` and `built.question` — not the builder's composed content.
Passing the composed content would render the evidence twice. That mistake existed and
was caught by evaluation: fixing it cut median FAQ input from **721 to 438 tokens** and
cost per 1000 FAQ from 2.69 to 2.07.

---

## 5. Inbound from the model

```text
model output
   |
   +-- structured-output validation against the capability's output_schema
   +-- grounding check (answer terms traceable to retrieved evidence)
   +-- guardrail output scan
   |     secret-shaped content        -> BLOCK
   |     script / markup              -> BLOCK
   |     prohibited claim             -> BLOCK  (guaranteed approval, "IRDAI compliant",
   |                                            "definitely covered", premium promise)
   |     Aadhaar / PAN / card / JWT   -> SANITIZE
   +-- ModelOutputScanner (second pass)
   |
   v
DirectiveRegistry.build  -> registered type, valid payload, no markup,
                            accessibility present, else fail closed
```

The model cannot produce a UI component. It produces text or a structured payload; the
*server* selects the directive.

---

## 6. Provider calls

```text
business service
   |
   v  ResiliencePolicy
timeout -> retry (only when duplication is prevented) -> circuit breaker
   |
   v  provider (mock | InsureMO)
   |
   +-- 4xx / 5xx / malformed / timeout -> platform error taxonomy
   |                                      (never a raw upstream payload)
   v
Provider DTO -> Domain DTO -> AI-facing DTO
```

Retry safety is explicit:

| Side effect | No idempotency key | With key |
|---|---|---|
| `READ_ONLY` | retry | retry |
| `LOW_RISK_WRITE` | **no** | retry |
| `HIGH_RISK_WRITE` | **no** | retry |
| `IRREVERSIBLE` | **no** | **no** |

Policy issuance is `IRREVERSIBLE`: attempted exactly once, ever.

---

## 7. Telemetry, logs and audit

Three streams, three different redaction rules:

```text
              structured logs        traces                audit events
message text  pattern-masked         not included          not included
extras        recursively redacted   forbidden keys dropped small, redacted attributes
identifiers   pseudonymous refs      request/correlation id pseudonymous actor_ref
resources     masked                 not included          masked ("*******1234")
secrets       [REDACTED]             never                 never
stack traces  type + masked message  error.type only       never
```

`conversation_id` and `subject_id` never appear raw in a log line — `conversation_ref`
and `subject_ref` are stable pseudonyms, so an operator can correlate without reading
identity.

Audit events additionally carry the **version stamp** that made the decision
reproducible: prompt version, knowledge corpus version, guardrail policy version,
model id, workflow version.

---

## 8. Worked example: "What was my premium?"

```text
1. JWT validated                     -> AuthContext(subject=CUST-1001, CUSTOMER)
2. Router                            -> deterministic read capability, 0 model calls
3. PEP                               -> channel, role, permission policy:read, rate limit
4. effective_customer_id             -> CUST-1001 (a supplied id would be ignored)
5. authorize_customer_scope          -> own scope, allowed
6. PolicyProvider.get_policy_owner   -> CUST-1001, matches
7. ResiliencePolicy                  -> timeout + retry (read-only) + breaker
8. Policy (full record)              -> annual_premium 18450, policy_number PTC0000001234
9. AiPolicySummary projection        -> { policy_number_masked "*******1234",
                                          annual_premium 18450, currency INR }
10. Audit                            -> PROVIDER_CALL_OUTCOME, tool GetPolicyPremium,
                                         resource_ref "*******1234", no amount, no id
11. Response                         -> masked view only
```

Never involved: the conversation transcript, a model call, the unmasked policy number,
the customer id in any log.

If step 6–8 fails, the tool returns `available=false` with a reason code. It does not
return a remembered figure, and it does not guess.

---

## 9. Deletion and export

Privacy hooks exist for the operations a data-subject request needs (§11):

| Operation | Mechanism |
|---|---|
| Conversation deletion | `ConversationStore.delete(conversation_id)` |
| Workflow deletion | `WorkflowStateStore.delete(flow_id)` |
| Retention expiry | TTL on conversation and workflow state |
| Purpose limitation | `purpose_fields` gate on the model boundary |
| Processing trail | audit events with pseudonymous actor references |

Audit events are deliberately **not** deleted by a conversation deletion: they contain
no conversation content, and their retention is governed separately.
`REQUIRES_VERIFICATION` — the retention periods and the erasure/audit interaction need
Legal and Compliance sign-off.
