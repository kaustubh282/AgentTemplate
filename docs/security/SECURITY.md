# Security

The security controls this template implements, where each one lives, and how to
verify it. Written so a reviewer can check a claim rather than take it on trust.

Every claim below names a runnable command. If a control is not implemented, this
document says so.

---

## 1. Security model in one paragraph

Untrusted inputs are the user's message, the retrieved documents **and the model's
output**. Trusted inputs are the validated JWT and the authoritative System of Record.
Every decision that matters — who you are, what you may see, what may execute, what
state may change — is made server-side from trusted inputs. Prompts are never used as
a security control.

---

## 2. Authentication

| Control | Where | Verify |
|---|---|---|
| Signature verification | `app/core/auth/jwt_service.py` | `pytest tests/security/test_jwt_boundary.py` |
| Algorithm allow-list; `alg=none` refused | same, plus config validator | `test_alg_none_token_is_rejected` |
| Issuer, audience, expiry, not-before, subject, issued-at | same | 6 dedicated tests |
| Token-use check (refresh/ID cannot authorize) | `_to_auth_context` | `test_refresh_token_cannot_authorize_an_api_call` |
| JWKS with `kid` selection, TTL cache, rotation refresh | `JwksKeyResolver` | `test_jwks_configuration_is_preferred_over_dev_secret` |
| Symmetric secrets refused outside local/dev/test | `Settings` validator | `test_dev_secret_is_refused_outside_local_dev_test` |
| Unknown role claims discarded, not trusted | `_to_auth_context` | `test_unknown_roles_in_a_valid_token_are_dropped_not_trusted` |
| Bearer header read in exactly one module | `app/api/deps.py` | `grep -rn "Authorization\|partition" app/api/deps.py` |
| Token decoded in exactly one module | `app/core/auth/jwt_service.py` | `grep -rln "jwt.decode" app/` → one file |
| Token never in logs, traces or model context | formatter + sanitizer | `test_authorization_header_never_reaches_logs`, `test_jwt_cannot_cross_the_model_boundary` |

No custom cryptography is implemented; PyJWT performs verification.

**Not implemented here:** the IdP itself, key generation and storage, revocation
distribution. `REQUIRES_VERIFICATION`.

---

## 3. Authorization

Layered, and every layer is server-side.

```text
1. channel        is this capability reachable from this channel?
2. authentication does this capability require a validated identity?
3. actor type     CUSTOMER / AGENT / SERVICE / ANONYMOUS
4. role           declared roles for the capability
5. permission     fine-grained (policy:read, purchase:submit, ...)
6. workflow state is this action legal in the current state?
7. confirmation   has the user explicitly confirmed a high-risk write?
                  (proven by a server-issued token bound to flow + action + state
                  version, not by a client boolean - see §2.1)
8. idempotency    does a sensitive write carry a key? (scoped to action + payload)
9. rate limit     tiered by cost class
10. resource scope does this caller own this specific resource - and this conversation?
```

Steps 1–9 are the `PolicyEnforcementPoint`; step 10 is the
`ResourceScopeInterceptor`.

### 2.1 Conversation ownership and confirmation evidence

Two controls were added after an external review found them missing or cooperative:

* **Conversation ownership (C-1).** A conversation is bound to the authenticated
  subject and tenant that created it. Every chat, action and flow-start resolves the
  conversation through `ConversationOrchestrator._open_conversation`, and the workflow
  engine's `get_active(conversation_id, ctx)` applies the same ownership rule to
  *reading* a journey that already applied to mutating it. Presenting another
  customer's conversation id returns `FORBIDDEN`, discloses nothing (not the state, not
  the allowed actions, not the vehicle or premium) and is audited as
  `conversation_owner_mismatch`. Probe: `pytest -k TestC1`.
* **Server-issued confirmation tokens (M-2).** Every flow response that offers a
  confirmation-requiring action carries `meta.confirmationTokens[action]`, an HMAC over
  `flow_id | action | state version` under `CONFIRMATION_TOKEN_SECRET`. `confirmed=true`
  is honoured only with that token, so a client cannot satisfy the gate by always
  sending `true`, cannot reuse a token for another action, and cannot confirm a review
  it was never shown. Idempotent replays of an already-confirmed action are exempt, so
  a retry after a timeout stays safe. Probe: `pytest -k TestM2`.
* **Idempotency keys are scoped (M-3)** to `action + payload hash`. Reusing a key for
  a different action or payload is `IDEMPOTENCY_CONFLICT` (409), never a silent no-op
  of a legitimate action.

### The ownership rules that matter

* A **customer** is pinned to their own subject. A `customer_id` in a payload or in a
  model-generated tool argument **cannot** widen scope.
* An **agent** must name a customer, and that customer must be explicitly assigned.
  The `AGENT` role by itself grants access to nothing.
* Authority is settled **before** the System of Record is consulted, so an
  unauthorized caller triggers no upstream call and learns nothing about the resource.
* A missing resource and a not-owned resource return the **same** reason code and the
  same user-facing message.

Verify: `pytest tests/security/test_resource_scope.py` (26 tests),
`python -m evals.native.run auth` (8/8).

---

## 4. Guardrails

| Layer | Blocks |
|---|---|
| Input | empty, oversized, unsafe markup, credential-shaped strings, abuse, prompt injection |
| Retrieved document | indirect injection before the content is used |
| Tool | any tool outside the capability's allow-list; budget exhaustion; unsanitised arguments |
| Output | secret-shaped content, markup, prohibited claims, PII patterns |
| Business | illegal transitions, missing prerequisites, unconfirmed high-risk writes, model-authored state changes |

### Prompt-injection detection

15 weighted patterns across 8 categories: instruction override, prompt extraction,
role escalation, tool manipulation, data exfiltration, cost abuse, embedded directives,
suspicious URLs. Combined with diminishing returns so one strong signal blocks and
several weak ones accumulate.

**This is a signal, not the boundary.** A successful injection still cannot authorize a
tool, widen resource scope or mutate state. Defence in depth is what makes the residual
risk acceptable.

### Prohibited claims

The assistant may never assert guaranteed approval or settlement, "IRDAI compliant",
definite coverage, definite eligibility, or a specific premium figure not sourced from
a provider. Verify: `pytest -k prohibited_claims`.

### Credential exposure is refused, not masked

If a user pastes a JWT, API key or `Authorization` value, the request is **blocked and
audited** rather than quietly masked — masking would leave the user believing the
secret was handled safely.

---

## 5. Data protection

Full detail in [PII_HANDLING.md](PII_HANDLING.md). Summary:

* six-level classification model; secrets are **removed**, not masked
* the log formatter redacts every message and every extra field — a caller cannot log
  raw PII by accident
* forbidden trace attributes (`prompt`, `messages`, `document_text`, `answer`, …)
* pseudonymous `subject_ref` / `conversation_ref` in logs and audit
* Provider → Domain → AI-facing DTO projection; the model sees masked, field-limited
  views only
* structural `assert_no_secrets` before any provider call
* output scanned twice (guardrail + scanner)

Verify: `pytest tests/unit/test_pii_boundaries.py` (57 tests),
`python -m evals.native.run pii` (17/17). Benchmark PII leakage incidents: **0**.

---

## 6. Abuse and cost control

* separate rate-limit tiers: AI, deterministic, public, high-risk write
* multi-dimensional limits: subject, IP hash, conversation, tenant
* per-capability budgets: model calls, tokens, agent steps, tool calls
* a blocked or abstained request costs **zero** tokens
* per-request cost telemetry and configurable cost ceilings that fail CI

**Enforced, not cooperative:** the model-call and token budget is checked and charged
inside `ModelInvoker` against the ledger the Harness binds for the request
(`app/ai/harness/context/budget.py::govern`). A handler that calls the invoker directly
is still budgeted, a call outside any Harness request is refused
(`BLOCK_UNGOVERNED_MODEL_CALL`), and the execution record's `model_calls` always equals
the calls that actually happened - including when the request was blocked mid-way.

**Production guard:** `SESSION_STORE_PROVIDER`, `WORKFLOW_STORE_PROVIDER` and
`RATE_LIMIT_STORE_PROVIDER=inmemory` are **refused in prod** because a process-local
store multiplies limits and loses journeys across instances. The `redis` adapters are
shipped; the rate limiter's shared store **fails closed** on outage (an unreachable
limiter denies rather than removes the control), and the state stores report a
controlled `UPSTREAM_UNAVAILABLE` (503, retryable) rather than fabricating state.

---

## 7. Transport, headers and network

Applied by the application:

```text
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'
Cache-Control: no-store
Strict-Transport-Security: (production only)
```

Plus a 64 KB body-size limit checked before validation, and explicit CORS origins
(wildcard refused in production).

**Platform responsibility, not implemented here:** TLS termination, WAF, private
networking, outbound allow-listing, least-privilege service roles, encryption at rest.

---

## 8. Error handling

A fixed safe message per taxonomy code. Never returned: stack traces, internal hosts,
SQL, prompts, secrets, raw upstream payloads. Unhandled exceptions become
`INTERNAL_ERROR`. Validation failures report a category and an error count, not field
internals. Every response carries `requestId` and `correlationId` so a user report can
be traced without the user seeing internals.

---

## 9. Secrets

* no secret in source control — enforced by a committed-credential scan test
* `.env.example` documents every variable with **empty** secret values
* `.gitignore` excludes `.env` while keeping `.env.example` tracked
* production requires externalised keys; symmetric JWT secrets are refused
* fixture secrets must be recognisably synthetic — a separate test asserts this

Verify: `pytest tests/unit/test_configuration.py`.

---

## 10. Supply chain

| Control | Status |
|---|---|
| Dependency management via `pyproject.toml` | implemented |
| Verified interoperable versions recorded | implemented |
| Committed-credential scanning | implemented (test) |
| Static analysis (ruff, mypy strict) | implemented, passing |
| Dependency vulnerability scanning | **scaffolded, not executed** |
| Container scanning | **scaffolded, not executed** |
| SBOM generation | **scaffolded, not executed** |
| License inventory | **not implemented** |

These gaps are recorded as blockers in `PROJECT_READINESS.md`. No major dependency was
upgraded speculatively during this build.

---

## 11. Verify everything

```bash
python scripts/run_evals.py security      # adversarial + authorization suites
python -m evals.native.run security auth pii
python -m pytest tests/security -q
python -m ruff check . && python -m mypy
```

---

## 12. Reporting a vulnerability

Follow the ProTec InfoSec disclosure process. Do not open a public issue containing
exploit detail, customer data or credentials. `REQUIRES_VERIFICATION` — the contact
address and SLA must be supplied by InfoSec and recorded here.

---

## 13. What has *not* been done

Stated plainly:

* **no penetration test** has been performed
* **no container or SBOM scan** has been executed; `pip-audit` **has** been run
  (3 findings, all in eval-only extras with no fix released; 0 reachable from `app/`)
* the audit sink is tamper-**evident** (HMAC hash chain, verifier shipped) but not
  tamper-**proof**: WORM / object-lock storage for the chained file is a platform item
* the shared (`redis`) stores are shipped and proven against an in-process emulator;
  confirmation against a managed Redis in preprod is still to be done
* only mock providers have been exercised end to end
* LLM-judge safety metrics (bias, toxicity) are `NOT_EVIDENCED`

None of these is a reason to hold the template; all are reasons it is not yet
production ready. See `PROJECT_READINESS.md`.
