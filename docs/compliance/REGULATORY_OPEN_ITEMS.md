# Regulatory open items

Every item requiring a determination from Legal, Compliance, InfoSec, Product or
Finance before this template can be used in production. Nothing here was decided by
the engineering build.

Ordered by what blocks production first.

---

## Blocking — must be resolved before a production release

| # | Item | Question that must be answered | Owner | Impact if unresolved |
|---|---|---|---|---|
| OI-01 | Regulatory applicability | Which IRDAI circulars, master circulars and cyber-security requirements apply to an AI assistant of this kind? | Compliance + Legal | The whole compliance register stays unverified |
| OI-02 | DPDP applicability and timeline | Which DPDP obligations are in force for this system, and by when? | Legal | Consent, notice, erasure and transfer controls cannot be finalised |
| OI-03 | Consent and notice architecture | How is consent captured, referenced and revoked, and what notice is shown? | Legal + Product | `PRV-03` remains a `GAP`; no consent capture exists |
| OI-04 | Retention periods | How long may conversation, workflow and audit data be retained? | Legal + Compliance | Current values (30 days / 1 hour / 7 years) are placeholders |
| OI-05 | Erasure vs audit interaction | Does an erasure request affect audit records that contain no conversation content? | Legal | Current behaviour (audit unaffected) is an assumption |
| OI-06 | Audit tamper-evidence | Which store satisfies the audit-integrity requirement? | InfoSec + Platform | The shipped sinks are not tamper-evident |
| OI-07 | Incident reporting timelines and contacts | What are the reporting obligations, and to whom? | Compliance + InfoSec | Deliberately not coded; the runbook carries placeholders |
| OI-08 | Penetration test | When, by whom, and against which environment? | InfoSec | No adversarial testing beyond the automated suite |
| OI-09 | Model provider approval | Which providers, models and regions are approved, and under what data terms? | Architecture + Legal | Cross-border transfer and cost assumptions unresolved |
| OI-10 | Judge model approval | May an LLM judge process evaluation data, and where? | AI governance + Legal | Judge eval metrics remain `NOT_EVIDENCED` |

---

## Required before integration certification

| # | Item | Question | Owner |
|---|---|---|---|
| OI-11 | InsureMO specification | What are the real endpoints, payloads, error bodies, auth scheme and idempotency semantics? | Integration + InsureMO |
| OI-12 | Authoritative rating | Which engine produces premium, and what are its inputs and error modes? | Actuarial + Product |
| OI-13 | Agent assignment authority | Where does agent-to-customer authorization come from — assignment, agency, branch, consent, delegation? | Business + InfoSec |
| OI-14 | Product appetite rules | Are the vehicle-age, IDV, trip-length and traveller-age limits in the template correct? | Underwriting |
| OI-15 | Payment handoff | Which payment provider, and what is the approved handoff and reconciliation flow? | Payments + Compliance |
| OI-16 | Identity provider | Which IdP, which claim shapes, what JWKS rotation cadence? | InfoSec + Platform |

---

## Required before the assistant makes customer-facing statements

| # | Item | Question | Owner |
|---|---|---|---|
| OI-17 | Knowledge approval process | Who approves a knowledge document, and what evidence is recorded? | Product governance + Compliance |
| OI-18 | Approved answer wording | Is the abstention wording acceptable, and is the human-handoff path correct? | Compliance + UX |
| OI-19 | Prohibited-claim completeness | Is the blocked-claim set complete for Indian general-insurance conduct rules? | Compliance |
| OI-20 | Grievance guidance | What exactly may the assistant say about grievance redressal and escalation? | Compliance |
| OI-21 | Accessibility standard | Which WCAG level applies, and who performs the audit? | UX + Compliance |
| OI-22 | Language coverage | Which languages must be supported, and how is knowledge governed per language? | Product |

---

## Required before scale

| # | Item | Question | Owner |
|---|---|---|---|
| OI-23 | Shared session, workflow and rate-limit stores | Which store, and what are its consistency guarantees? | Platform |
| OI-24 | BCP / DR objectives | What are the RTO and RPO, and when is failover tested? | Platform + Business continuity |
| OI-25 | Cost ceilings | What are the approved cost-per-1000 and cost-per-transaction limits? | Finance + Architecture |
| OI-26 | Observability backend | Which OTLP destination, what retention, what access model? | Platform |
| OI-27 | Supply-chain scanning | Which scanners, which thresholds, and where in CI? | Platform + InfoSec |

---

## Explicitly out of scope for this build

Recorded so the boundary is unambiguous:

- legal interpretation of any regulation
- regulatory reporting timelines or contacts hard-coded into application logic
- any assertion that the system is IRDAI, DPDP or CERT-In compliant
- selection of vendors, providers, regions or IdP
- infrastructure, network and container security implementation
- penetration testing and DR execution

---

## How to use this document

1. Assign each item an owner and a target date.
2. As each is resolved, update the corresponding row in
   [`COMPLIANCE_REGISTER.md`](COMPLIANCE_REGISTER.md) from `REQUIRES_VERIFICATION` to
   `VERIFIED`, with the decision reference.
3. Re-run `python scripts/run_evals.py all` and refresh
   [`PROJECT_READINESS.md`](../../PROJECT_READINESS.md).
4. Readiness may move to `READY_FOR_PREPROD_VALIDATION` only when every blocking item
   has an owner and a plan, and to production only when they are closed.
