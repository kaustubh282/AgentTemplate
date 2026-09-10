# Security control matrix

Mapping of implemented controls to common engineering references. These are
**engineering references, not compliance mappings**, and they do not substitute for the
ProTec InfoSec standard, which was not supplied to this build
(`REQUIRES_VERIFICATION`).

---

## OWASP Top 10 for LLM / GenAI applications

| Risk | Control in this template | Evidence | Status |
|---|---|---|---|
| **LLM01** Prompt injection | 15-pattern weighted detector across 8 categories; retrieved content wrapped as data with neutralised delimiters; blocked before any model call. Defence in depth: a successful injection still cannot authorize a tool, widen resource scope or mutate state | `tests/security/test_adversarial.py`; native `security` 22/22 | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM02** Insecure output handling | Structured-output validation against the capability schema; 13 registered directive types; markup, script and `javascript:` refused in any payload string; the model never selects a UI component | 22 native `ui_directives` checks | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM03** Training-data poisoning | Not applicable: no training or fine-tuning. *Knowledge* poisoning is covered by lifecycle governance, checksums and indirect-injection scanning | RAG suite, 51 tests | `NOT_APPLICABLE_PENDING_APPROVAL` |
| **LLM04** Model denial of service | Tiered rate limits; per-capability model-call, token, agent-step and tool-call budgets; cost-abuse patterns block on their own; blocked and abstained requests cost zero tokens | budget and rate-limit tests | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM05** Supply-chain vulnerabilities | `pyproject.toml` declares version ranges; the runtime closure is pinned exactly in `requirements.lock`, which the container image installs from. The CI `supply-chain` job runs `pip-audit --strict` on the lock (release blocking), a `pip-licenses` inventory and a CycloneDX SBOM, all uploaded as artifacts. Container image scanning and base-image digest pinning **not yet executed** | `requirements.lock`; `.github/workflows/ci.yml` `supply-chain`; `tests/unit/test_ops_hardening.py` | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM06** Sensitive information disclosure | Six-level classification; secrets removed rather than masked; default-deny PII at the model boundary; AI-facing DTO projection; output scanned twice | 57 PII tests; benchmark leakage 0 | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM07** Insecure plugin / tool design | Every tool declares schema, permission, data classification, side-effect class, idempotency, timeout, retry and audit policy; the PEP enforces a per-capability allow-list | `pytest -k unauthorized_tool` | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM08** Excessive agency | The model cannot reach the workflow engine (import-enforced); only allow-listed transitions write; high-risk writes need confirmation and an idempotency key; agent steps and handoffs are budgeted, and measured handoffs are 0 | determinism suite, 33 tests | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM09** Overreliance | Evidence gate plus post-answer grounding; abstention preferred over a plausible answer; citations returned; no user-facing confidence score | hallucination rate 0.000 | `IMPLEMENTED_PENDING_REVIEW` |
| **LLM10** Model theft | Not applicable: no self-hosted proprietary weights | none | `NOT_APPLICABLE_PENDING_APPROVAL` |

---

## OWASP API Security Top 10

| Risk | Control | Evidence | Status |
|---|---|---|---|
| **API1** Broken object-level authorization | `ResourceScopeInterceptor`; a customer is pinned to their own subject; authority settled before any SoR read; denial does not disclose existence | 26 scope tests | `IMPLEMENTED_PENDING_REVIEW` |
| **API2** Broken authentication | Full JWT validation; `alg=none` refused at config *and* runtime; asymmetric keys required in production | 31 JWT tests | `IMPLEMENTED_PENDING_REVIEW` |
| **API3** Broken object-property-level authorization | AI-facing DTO projection; `extra="forbid"` on every schema; unknown roles and claims discarded | `pytest -k projected_not_forwarded` | `IMPLEMENTED_PENDING_REVIEW` |
| **API4** Unrestricted resource consumption | Tiered rate limits; 64 KB body limit; token, step and tool budgets; configurable cost ceilings | rate-limit and budget tests | `IMPLEMENTED_PENDING_REVIEW` |
| **API5** Broken function-level authorization | The PEP checks channel, actor type, role, permission, workflow state, confirmation and idempotency per capability | `pytest -k workflow_state_restriction` | `IMPLEMENTED_PENDING_REVIEW` |
| **API6** Unrestricted access to sensitive business flows | High-risk transitions require explicit confirmation and an idempotency key; irreversible actions are never retried | `pytest -k requires_an_idempotency_key` | `IMPLEMENTED_PENDING_REVIEW` |
| **API7** Server-side request forgery | No user-controlled outbound URL; provider base URLs come from configuration only | config tests | `IMPLEMENTED_PENDING_REVIEW` |
| **API8** Security misconfiguration | Startup validation refuses nine unsafe production settings; secure headers on every response; explicit CORS | `pytest -k production_configuration_guard` | `IMPLEMENTED_PENDING_REVIEW` |
| **API9** Improper inventory management | Versioned API, directive schema, workflow, prompt and dataset versions; OpenAPI generated and validated | `python -m evals.native.run contract` | `IMPLEMENTED_PENDING_REVIEW` |
| **API10** Unsafe consumption of third-party APIs | Status-to-taxonomy mapping; non-JSON and unexpected shapes rejected; no raw payload in context or errors; circuit breaker | 9 status-code tests | `IMPLEMENTED_PENDING_REVIEW` |

---

## OWASP ASVS — selected chapters

| Area | Control | Status |
|---|---|---|
| V2 Authentication | JWT validation, algorithm allow-list, key rotation, no custom cryptography | `IMPLEMENTED_PENDING_REVIEW` |
| V3 Session management | Stateless bearer tokens; session id in the trusted context; TTLs | `IMPLEMENTED_PENDING_REVIEW` |
| V4 Access control | Server-side authorization at every layer; deny by default | `IMPLEMENTED_PENDING_REVIEW` |
| V5 Validation and encoding | Pydantic schemas with `extra="forbid"`; markup refused in directive payloads | `IMPLEMENTED_PENDING_REVIEW` |
| V7 Error handling and logging | Fixed safe messages; structured redacted logs; separate audit stream | `IMPLEMENTED_PENDING_REVIEW` |
| V8 Data protection | Classification, masking, minimisation, retention hooks | `IMPLEMENTED_PENDING_REVIEW` |
| V9 Communications | HSTS in production; TLS terminated at the platform edge | `REQUIRES_VERIFICATION` |
| V10 Malicious code | Static analysis; no dynamic code execution; no `eval` | `IMPLEMENTED_PENDING_REVIEW` |
| V11 Business logic | Deterministic state machine; allow-listed transitions; idempotency | `IMPLEMENTED_PENDING_REVIEW` |
| V12 Files and resources | No file upload implemented in this template | `NOT_APPLICABLE_PENDING_APPROVAL` |
| V13 API | Versioned endpoints; explicit error model; OpenAPI | `IMPLEMENTED_PENDING_REVIEW` |
| V14 Configuration | Startup validation; no secrets in source; environment separation | `IMPLEMENTED_PENDING_REVIEW` |

---

## NIST CSF functions

| Function | Coverage |
|---|---|
| **Identify** | Asset and data classification, threat model, capability registry with risk levels and side-effect classes |
| **Protect** | Authentication, authorization, resource scope, guardrails, masking, rate limits, secure headers, startup guards |
| **Detect** | Structured logs, metrics, traces, audit events, guardrail intervention counters, injection detection, stale-source counters |
| **Respond** | Error taxonomy, circuit breakers, controlled fallback, escalation to a human path, incident runbooks |
| **Recover** | Degradation matrix, state preservation on failure, rollback surfaces, BCP/DR scaffolding (**untested**) |

---

## Gaps

| Gap | Severity | Owner |
|---|---|---|
| Dependency, container and SBOM scanning not executed | High | Platform |
| Penetration test not performed | High | InfoSec |
| Audit sink not tamper-evident | High | InfoSec + Platform |
| In-memory rate limiter and stores prevent safe horizontal scaling | High | Platform |
| Encryption at rest not addressed in the application layer | Medium | Platform |
| Licence inventory not implemented | Medium | Platform |
| ProTec InfoSec standard not supplied, so no mapping to it exists | Medium | InfoSec |
