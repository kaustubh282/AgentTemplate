# Release readiness checklist

Per master prompt §37. Complete for every release. Anything unchecked is either a
blocker or a recorded, approved exception — never a silent omission.

---

## 1. Code and review

- [ ] Code review completed by someone who did not write the change
- [ ] Architecture review if a **protected core contract** changed (`app/core/**`,
      `app/workflows/**`, `app/ui_directives/**`, `app/integrations/contracts/**`)
- [ ] An ADR added for any significant decision
- [ ] Backward-compatibility analysis if the directive schema, API contract or workflow
      definition changed
- [ ] No new core file added merely to support a domain

## 2. Automated gates

```bash
python scripts/run_evals.py all      # must exit 0
```

- [ ] `ruff check` passes
- [ ] `ruff format --check` passes
- [ ] `mypy` passes
- [ ] full test suite passes (unit, contract, integration, e2e, security, performance)
- [ ] native deterministic evals pass (**no regression allowed**)
- [ ] Ragas evaluation passes against configured thresholds
- [ ] DeepEval suites pass (faq, agents, conversation, safety)
- [ ] benchmark shows **no material regression** against
      `evals/regression/baseline.json`
- [ ] load profiles pass

## 3. Determinism and workflow safety

- [ ] every legal transition passes; every prohibited transition is rejected
- [ ] duplicate submit is proven safe
- [ ] FAQ interruption does not mutate transaction state
- [ ] deterministic scenarios prove **zero** model calls
- [ ] high-risk writes require confirmation **and** an idempotency key

## 4. Security

- [ ] adversarial suite passes (injection, extraction, escalation, exfiltration,
      cost abuse)
- [ ] cross-customer and cross-resource authorization tests pass
- [ ] PII-to-log and PII-to-model-boundary tests pass
- [ ] secret-leakage tests pass
- [ ] production-with-mock startup rejection verified
- [ ] no Critical or High finding unresolved **within the implemented scan scope**
- [ ] **dependency, container and SBOM scans executed** — currently a known gap (B3);
      if still open, record the exception explicitly
- [ ] any new attack pattern added to `evals/datasets/adversarial.jsonl`

## 5. Privacy

- [ ] any new field classified in `FIELD_CLASSIFICATION`
- [ ] any new PII-bearing field has a model-boundary test
- [ ] no new raw identifier in logs, traces or audit records
- [ ] retention implications reviewed if a store changed

## 6. Configuration and secrets

- [ ] `.env.example` updated for any new setting (a test enforces this)
- [ ] no secret committed (a test enforces this)
- [ ] configuration validated for the target environment
- [ ] production secrets present in the secret manager
- [ ] feature flags set intentionally for the rollout

## 7. Prompt, model and knowledge changes

If any of these changed, the regression evals are **mandatory**, not optional:

- [ ] prompt version bumped and its metadata updated
- [ ] regression evals re-run and compared with the baseline
- [ ] knowledge documents carry complete governance metadata and an approval reference
- [ ] superseded documents actually marked `SUPERSEDED`, not left `ACTIVE`
- [ ] corpus version changed (proves cache invalidation will occur)
- [ ] model configuration change reviewed for cost and latency impact
- [ ] guardrail threshold change justified by evidence, not intuition

## 8. Performance and cost

- [ ] deterministic p95 within SLO (< 300 ms)
- [ ] FAQ p95 within SLO (< 3000 ms)
- [ ] median and p95 input tokens within budget
- [ ] model calls per request <= 1; **agent handoffs = 0**
- [ ] cost per 1000 within the configured ceiling
- [ ] zero-model-call rate not lower than the baseline

## 9. Observability

- [ ] new failure modes are observable (metric or audit event)
- [ ] dashboards updated for any new metric
- [ ] alerts configured for any new failure mode
- [ ] audit events added for any new significant action

## 10. Documentation

- [ ] `README.md` status table refreshed with real numbers
- [ ] `PROJECT_READINESS.md` regenerated from actual runs
- [ ] relevant `docs/` pages updated
- [ ] `EXTENDING_THE_TEMPLATE.md` updated if the extension model changed
- [ ] compliance register updated if a control changed

## 11. Rollout and rollback

- [ ] deployment plan agreed
- [ ] canary or staged rollout defined
- [ ] **rollback plan confirmed** for: application version, prompt version, knowledge
      corpus, model configuration, guardrail configuration, feature flags
- [ ] monitoring in place for the rollout window
- [ ] on-call briefed on what changed and what to watch

## 12. Approvals

| Role | Required when | Name | Date |
|---|---|---|---|
| Engineering lead | always | | |
| Architecture | protected core contract changed | | |
| InfoSec | security control or auth changed | | |
| Privacy / DPO | data handling or retention changed | | |
| Compliance | customer-facing wording or claims changed | | |
| Product | business rules or appetite changed | | |
| Actuarial | rating inputs or premium handling changed | | |
| Finance | cost ceilings changed | | |

---

## Recorded exceptions

Any unchecked item must appear here with an owner and an expiry. An exception without
an expiry is a blocker wearing a disguise.

| Item | Reason | Risk accepted by | Expires |
|---|---|---|---|
| | | | |

---

## First-release note

For the initial release of this template the following are **known open blockers**, not
exceptions to be waved through. See `PROJECT_READINESS.md`:

* B1 shared session, workflow and rate-limit stores
* B2 tamper-evident audit store
* B3 dependency, container and SBOM scanning
* B4 penetration test
* B5 InsureMO integration certification
* B6 LLM-judge evaluation metrics
* B7 DR test execution

Production readiness cannot be claimed while any of these is open.
