# BCP / DR readiness

**Status: scaffolding only. No DR test has been performed.** This document records
what the application makes possible and what the platform must still provide and
verify. It is not a DR plan.

---

## 1. What the application contributes

| Property | Status | Why it matters for recovery |
|---|---|---|
| Stateless API process | **implemented** | any instance can serve any request; identity comes from the token |
| State behind Protocols | **implemented** | state can move to a replicated store without touching business code |
| Versioned workflow state | **implemented** | an in-flight journey carries its own `workflow_version`, so recovery onto a different app version does not corrupt it |
| Optimistic locking | **implemented** | concurrent recovery writes conflict loudly rather than silently overwriting |
| Idempotency keys on sensitive writes | **implemented** | a replayed submission after failover does not double-charge or double-issue |
| Side-effect ordering | **implemented** | a failure mid-transaction leaves nothing committed |
| Immutable, config-injected artifact | **implemented** | the same image can be redeployed anywhere |
| Fail-fast configuration validation | **implemented** | a misconfigured recovery environment refuses to start rather than serving unsafely |
| Graceful degradation matrix | **implemented and tested** | partial recovery is usable |
| Knowledge as re-ingestible data | **implemented** | the corpus can be rebuilt from the approved source |

The genuinely useful DR property here is the last one plus idempotency: after a
failover, a client can safely retry the exact submission it was making.

---

## 2. What the platform must provide

| Requirement | Status | Owner |
|---|---|---|
| Replicated session / conversation store | **not implemented** | Platform |
| Replicated workflow state store | **not implemented** | Platform |
| Replicated rate-limit store | **not implemented** | Platform |
| Tamper-evident, replicated audit store | **not implemented** | InfoSec + Platform |
| Knowledge index backup and restore | **not implemented** | Platform |
| Multi-AZ or multi-region deployment | **not implemented** | Platform |
| Backup schedule and retention | **not defined** | Platform |
| Restore procedure and its test evidence | **not defined** | Platform |
| RTO and RPO targets | **not defined** | Business continuity |
| Model provider failover plan | **partial** — a controlled fallback exists in code, refused for high-risk capabilities | Architecture |
| Provider / InsureMO failover plan | **not defined** | Integration |

Everything marked "not implemented" is a Protocol away from being implementable, but
none of it is done.

---

## 3. Recovery objectives

**`REQUIRES_VERIFICATION` — none of these has been agreed.**

| Scenario | RTO | RPO | Owner |
|---|---|---|---|
| Single instance loss | | | |
| Availability-zone loss | | | |
| Region loss | | | |
| Session store loss | | | |
| Workflow store loss | | | |
| Audit store loss | | | |
| Knowledge index loss | | | |
| Model provider outage | | | |
| InsureMO outage | | | |

### What is technically true today, whatever the targets turn out to be

* **Session store loss** — conversations are lost; workflows are lost. Users must
  restart a journey. No authoritative data is lost, because completed transactions
  live in the System of Record.
* **Workflow store loss** — in-flight journeys are lost; **completed policies are
  not**, because issuance writes to the SoR. This is the direct benefit of not letting
  the model or the session own transactional truth.
* **Knowledge index loss** — FAQ degrades to controlled unavailability and abstains. It
  does **not** answer from model memory. Recovery is re-ingestion from the approved
  source.
* **Audit store loss** — with `AUDIT_FAIL_CLOSED=true`, requests fail rather than
  proceeding unaudited. That is a deliberate availability-for-accountability trade.

---

## 4. Data classification for recovery

| Store | Loss impact | Reconstructible? |
|---|---|---|
| Workflow state | in-flight journeys lost; users restart | no |
| Conversation | recent context lost; no functional loss | no, and it is not needed |
| Knowledge index | FAQ degrades safely | **yes** — re-ingest from the approved source |
| Audit events | accountability gap; likely a reportable event | **no** — must be replicated |
| Authoritative policy/claim data | not held by this application | n/a — owned by the SoR |

The most important row is the last one: this application is **not** the system of
record for policies, claims or payments, which materially reduces its DR criticality.

---

## 5. Degraded-mode operation

Already implemented and tested — see the degradation matrix in
[`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md) §7 and
`python -m evals.native.run reliability`.

The practical consequence: during a model or knowledge outage the **transactional
journey keeps working**, and during a provider outage **FAQ keeps working**. A total
outage of both is required to make the service unusable.

---

## 6. DR test plan

The **application-layer** scenarios are executed by `scripts/recovery_drill.py` (also
run as `tests/integration/test_recovery_drill.py`, so they are part of the gate). The
drill builds several application instances against one shared store and one chained
audit log, kills one mid-journey and measures what the application can guarantee.
Results are written to `evals/reports/recovery-drill-report.json`.

- [x] **Instance loss** — a fresh instance resumes the journey at the same state and
      version; a confirmation token issued by the dead instance verifies on the
      survivor; the purchase completes. **App-layer RTO ≈ 12 ms** (replacement
      instance ready), **RPO = 0 committed transitions**
- [x] **Workflow store: concurrent writers** — two instances race on one flow; exactly
      one commits, the other receives `FLOW_STATE_CONFLICT`, and then reads the
      winner's state
- [x] **Duplicate after failover** — the same idempotency key on the survivor is a safe
      replay, never a second charge
- [x] **Store outage** — the shared store vanishes; the request fails with a
      controlled `UPSTREAM_UNAVAILABLE` (503, retryable) and nothing is fabricated
- [x] **Audit continuity** — the hash-chained log written by three instances verifies
      end to end after the crash
- [x] **Knowledge restore** — re-ingestion is idempotent; a lifecycle change changes
      the corpus version and therefore every cache key

The **platform-layer** scenarios below are *not* executed here and remain owned by
Platform:

- [ ] **Managed Redis failover** — the emulator proves semantics, not a real
      cluster's failover behaviour
- [ ] **Audit store failover** — WORM / object-lock storage behaviour under failover
- [ ] **Model provider failover** — confirm high-risk capabilities refuse fallback
- [ ] **Provider failover** — confirm state preservation and retry directives
- [ ] **Region failover** — full end-to-end with RTO/RPO measured
- [ ] **Restore from backup** — measured, with data-integrity verification

---

## 7. Honest assessment

**DR readiness score: 7.5** — application-layer recovery is executed and measured
(RTO ≈ 12 ms for a replacement instance, RPO = 0 transitions); platform-layer
recovery (region failover, backup restore, managed-store failover) is **not** evidenced
and caps the score until the platform executes its part of the plan.

The application is *architected* for recovery — stateless, Protocol-backed,
version-safe, idempotent, degradation-tested. But architecture is not evidence. With
in-memory stores and no executed failover test, this template cannot claim DR
readiness, and `PROJECT_READINESS.md` records it as blocker **B7**.

What would move it: implement the shared stores (B1), implement the tamper-evident
audit store (B2), agree RTO/RPO, then execute the test plan above.
