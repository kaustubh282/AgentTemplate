# Compliance register

**This is a verification register, not a compliance claim.**

This template implements *compliance-enabling technical controls* and generates
*evidence*. It does **not** establish that the system complies with any regulation.
Applicability and interpretation must be determined by ProTec Legal, Compliance and
InfoSec.

No regulatory requirement has been invented here, and no compliance with any
regulation is asserted. Every row that needs a legal or organisational determination is
marked `REQUIRES_VERIFICATION`.

## Status vocabulary

| Status | Meaning |
|---|---|
| `VERIFIED` | Confirmed by the accountable owner against the approved source |
| `IMPLEMENTED_PENDING_REVIEW` | Technical control exists and is tested; interpretation not yet confirmed |
| `REQUIRES_VERIFICATION` | Needs a legal, compliance or organisational determination |
| `NOT_APPLICABLE_PENDING_APPROVAL` | Believed out of scope; needs sign-off |
| `GAP` | No control exists |

---

## A. IRDAI information and cyber security

| ID | Area | Technical control in this template | Evidence | Status | Owner |
|---|---|---|---|---|---|
| IRDAI-IS-01 | Access control | Role- and permission-based authorization; resource ownership enforcement; least privilege; customer/agent separation | `tests/security/test_resource_scope.py`, native `auth` 8/8 | `IMPLEMENTED_PENDING_REVIEW` | InfoSec |
| IRDAI-IS-02 | Authentication | JWT validation (signature, alg allow-list, iss, aud, exp, nbf, sub, token-use); asymmetric keys in production | `tests/security/test_jwt_boundary.py` 31 tests | `IMPLEMENTED_PENDING_REVIEW` | InfoSec |
| IRDAI-IS-03 | Audit logging | Separate append-only audit stream, fail-closed, version-stamped, no unredacted PII | `test_a_failing_audit_sink_fails_closed` plus audit assertions across suites | `IMPLEMENTED_PENDING_REVIEW` | InfoSec |
| IRDAI-IS-04 | Audit log integrity | `chained_file` sink: HMAC hash chain over every record (edit, deletion, reorder and forgery detectable) plus a signed head file that makes tail truncation detectable; per-instance writer attribution; `scripts/verify_audit_chain.py` verifies both. **Tamper-evident, not tamper-proof**: WORM / object-lock storage for the log and head, and key custody separate from the writer, remain platform responsibilities; plain `file` and `inmemory` sinks are not tamper-evident and are refused for production only by configuration review | `tests/unit/test_audit_chain.py`; `app/core/audit/chain.py` | `IMPLEMENTED_PENDING_REVIEW` | Platform |
| IRDAI-IS-05 | Encryption in transit | HSTS in production; TLS terminated at the platform edge | Middleware header test | `REQUIRES_VERIFICATION` | Platform |
| IRDAI-IS-06 | Encryption at rest | Not implemented in the application layer; expected from platform services | none | `REQUIRES_VERIFICATION` | Platform |
| IRDAI-IS-07 | Vulnerability management | Static analysis passing; CI `supply-chain` job runs `pip-audit --strict` (release blocking) on the pinned runtime lock, a licence inventory and a CycloneDX SBOM. Container image scanning **not yet executed** | ruff and mypy clean; `.github/workflows/ci.yml`; `requirements.lock` | `IMPLEMENTED_PENDING_REVIEW` | Platform |
| IRDAI-IS-08 | Incident management | Runbooks and AI-specific incident categories provided; statutory timelines deliberately not hard-coded | `docs/operations/INCIDENT_RUNBOOK.md` | `REQUIRES_VERIFICATION` | InfoSec and Compliance |
| IRDAI-IS-09 | Change management | Release checklist; versioned prompts, workflows, directives and datasets; rollback surfaces | `docs/operations/RELEASE_CHECKLIST.md` | `IMPLEMENTED_PENDING_REVIEW` | Engineering |
| IRDAI-IS-10 | BCP and DR | Stateless services, Protocol-backed stores, degradation matrix; **no DR test performed** | `docs/operations/BCP_DR_READINESS.md` | `GAP` | Platform |
| IRDAI-IS-11 | Penetration testing | **Not performed** | none | `GAP` | InfoSec |
| IRDAI-IS-12 | Outsourcing and cloud | Provider abstraction isolates third-party integration | Contract tests | `REQUIRES_VERIFICATION` | Vendor management |

---

## B. IRDAI policyholder protection and conduct

| ID | Area | Technical control | Evidence | Status | Owner |
|---|---|---|---|---|---|
| IRDAI-PP-01 | No misleading statements | Prohibited-claim guardrail blocks guaranteed approval, definite coverage, definite eligibility, unsourced premium and the phrase "IRDAI compliant" | `test_prohibited_claims_are_blocked_in_output` | `IMPLEMENTED_PENDING_REVIEW` | Compliance |
| IRDAI-PP-02 | Accurate product information | Answers grounded in approved, ACTIVE, versioned documents with citations; abstains otherwise | 22 FAQ cases; hallucination rate 0.000; citation correctness 1.000 | `IMPLEMENTED_PENDING_REVIEW` | Product and Compliance |
| IRDAI-PP-03 | Premium accuracy | Premium comes only from the authoritative provider; a provider failure returns unavailability, never a figure | `test_provider_timeout_returns_unavailable_not_invented_data` | `IMPLEMENTED_PENDING_REVIEW` | Product and Actuarial |
| IRDAI-PP-04 | Grievance information | Grievance guidance answered only from approved knowledge; a human handoff path exists | Case `faq-006`; `SHOW_HUMAN_HANDOFF` directive | `REQUIRES_VERIFICATION` | Compliance |
| IRDAI-PP-05 | Explicit consent for purchase | High-risk transitions require explicit confirmation and an idempotency key | `test_e2e_high_risk_action_requires_confirmation` | `IMPLEMENTED_PENDING_REVIEW` | Compliance |
| IRDAI-PP-06 | Record of the interaction | Audit trail plus explainability decision records, version-stamped | Audit tests | `IMPLEMENTED_PENDING_REVIEW` | Compliance |
| IRDAI-PP-07 | Accessibility of the journey | Semantic directive contracts, mandatory ARIA labels, non-colour status text, live regions, accessible validation messages | 22 native `ui_directives` checks | `IMPLEMENTED_PENDING_REVIEW` | Product and UX |
| IRDAI-PP-08 | Mis-selling prevention | Business rules deterministic and domain-owned; the model cannot decide eligibility or appetite | Determinism suite, 33 tests | `IMPLEMENTED_PENDING_REVIEW` | Product and Compliance |

---

## C. Indian data protection (DPDP Act and notified Rules)

Applicability, notice, consent architecture and effective timelines are **legal
determinations**. The rows below record only the technical hooks.

| ID | Area | Technical control | Evidence | Status | Owner |
|---|---|---|---|---|---|
| DPDP-01 | Data minimisation | Default-deny at the model boundary; PII removed unless purpose-declared; AI-facing DTO projection | 57 PII tests; native `pii` 17/17 | `IMPLEMENTED_PENDING_REVIEW` | Legal and Engineering |
| DPDP-02 | Purpose limitation | `purpose_fields` gate per capability | `test_pii_needed_for_the_purpose_is_kept_but_masked` | `IMPLEMENTED_PENDING_REVIEW` | Legal |
| DPDP-03 | Storage limitation | TTLs, bounded conversation window, configurable retention | Session and workflow TTL tests | `REQUIRES_VERIFICATION` | Legal |
| DPDP-04 | Erasure | `delete` on the conversation and workflow stores | Interface implemented | `REQUIRES_VERIFICATION` | Legal and Engineering |
| DPDP-05 | Access and portability | Provider reads and masked projections; **no export endpoint** | none | `GAP` | Legal and Engineering |
| DPDP-06 | Notice and consent | Metadata hooks only; **no consent capture implemented** | none | `GAP` | Legal |
| DPDP-07 | Security safeguards | Classification, masking, redaction, boundaries, authorization | Security suites | `IMPLEMENTED_PENDING_REVIEW` | InfoSec |
| DPDP-08 | Breach notification | Runbook exists; timelines and contacts deliberately configurable rather than coded | `INCIDENT_RUNBOOK.md` | `REQUIRES_VERIFICATION` | Legal and InfoSec |
| DPDP-09 | Processing record | Audit trail with pseudonymous actors | Audit tests | `IMPLEMENTED_PENDING_REVIEW` | Compliance |
| DPDP-10 | Cross-border transfer | Model and judge provider region is a deployment decision | none | `REQUIRES_VERIFICATION` | Legal |
| DPDP-11 | Data of minors | **No age-gating implemented** | none | `GAP` | Legal and Product |

---

## D. CERT-In directions

| ID | Area | Technical control | Evidence | Status | Owner |
|---|---|---|---|---|---|
| CERTIN-01 | Log retention | Configurable audit retention, default 7 years | `AUDIT_RETENTION_DAYS` | `REQUIRES_VERIFICATION` | InfoSec |
| CERTIN-02 | Time synchronisation | UTC timestamps throughout; NTP is a platform concern | Audit event model | `REQUIRES_VERIFICATION` | Platform |
| CERTIN-03 | Incident reporting timelines | Deliberately **not** hard-coded; owned by a compliance-maintained checklist | `INCIDENT_RUNBOOK.md` | `REQUIRES_VERIFICATION` | Compliance |
| CERTIN-04 | Log accessibility | Structured JSON logs and audit events, queryable | Logging tests | `IMPLEMENTED_PENDING_REVIEW` | Platform |

---

## E. Internal standards

| ID | Area | Status | Owner |
|---|---|---|---|
| INT-01 | Alignment with the ProTec InfoSec standard | `REQUIRES_VERIFICATION` - the standard was not supplied to this build | InfoSec |
| INT-02 | Alignment with the ProTec privacy policy | `REQUIRES_VERIFICATION` - not supplied | Privacy |
| INT-03 | Approved model providers and regions | `REQUIRES_VERIFICATION` | Architecture |
| INT-04 | Approved knowledge-approval workflow | `REQUIRES_VERIFICATION` - the lifecycle mechanism is implemented; the process is undefined | Product governance |
| INT-05 | Secure SDLC alignment | `IMPLEMENTED_PENDING_REVIEW` - tests, evals, static analysis, review checklist | Engineering |
| INT-06 | Approved cost ceilings | `REQUIRES_VERIFICATION` - configurable; current values are placeholders | Finance and Architecture |

---

## Summary

| Status | Count |
|---|---|
| `VERIFIED` | **0** |
| `IMPLEMENTED_PENDING_REVIEW` | 22 |
| `REQUIRES_VERIFICATION` | 19 |
| `GAP` | 5 |

Zero rows are `VERIFIED`, and that is the correct state for a template the accountable
owners have not yet reviewed. Nothing in this register should be read as a compliance
assertion.
