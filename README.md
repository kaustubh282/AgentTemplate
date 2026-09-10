# ProTec Insurance AI Template

A production-oriented base template for Indian general-insurance AI assistants: a
grounded FAQ assistant plus a deterministic, UI-directive-driven transactional flow
framework, built so new bots and product domains plug into stable contracts.

**The central design rule:** the model understands language; the application owns
transactional truth. Premium, eligibility, coverage, claim status and policy issuance
come from authoritative providers, never from a model.

---

## Status

| Gate | Result | How to reproduce |
|---|---|---|
| Tests | **805 passed, 0 failed** (91% line coverage) | `python -m pytest -q` |
| Native deterministic evals | **128/128 PASS** | `python -m evals.native.run` |
| Ragas RAG evaluation | **PASS** (7 judge metrics `NOT_EVIDENCED`) | `python -m evals.ragas.runners.rag_eval` |
| DeepEval agent regression | **PASS** (4 suites) | `python -m evals.deepeval.run` |
| Benchmark gates | **14/14 PASS** | `python scripts/benchmark.py` |
| Load profiles | **4/4 PASS** | `python scripts/load_test.py {faq,mixed,burst,degraded}` |
| Lint / format | **PASS** | `python -m ruff check . && python -m ruff format --check .` |
| Types | **PASS** (135 files) | `python -m mypy` |

An independent verification and validation audit of this template
([`reports/MASTER_TEMPLATE_ACCEPTANCE_REPORT.md`](reports/MASTER_TEMPLATE_ACCEPTANCE_REPORT.md))
found 9 High findings. Every one is now closed and re-verified; the evidence is in
[`reports/ACCEPTANCE_REMEDIATION.md`](reports/ACCEPTANCE_REMEDIATION.md).

Read [`PROJECT_READINESS.md`](PROJECT_READINESS.md) for the evidence-based scorecard,
the blockers, and every `REQUIRES_VERIFICATION` item. **This template is not
production ready as-is** — it is ready for pre-production validation once the items in
that report are closed.

---

## Quick start

```bash
# 1. Install
python -m pip install -e ".[dev,evals]"

# 2. Configure (defaults run entirely offline: mock providers, scripted model)
cp .env.example .env

# 3. Run
python -m uvicorn app.main:app --reload
#   API docs:   http://127.0.0.1:8000/api/v1/docs
#   OpenAPI:    http://127.0.0.1:8000/api/v1/openapi.json
#   Liveness:   http://127.0.0.1:8000/api/v1/health/live

# 4. Verify everything
python scripts/run_evals.py all
```

No credentials are needed for any of the above. The default model is an offline
deterministic double, and the default providers are mocks — both are refused in
production by configuration validation.

### Try the demos

```bash
# Demo A - grounded FAQ (1 model call, cites its source)
curl -s localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"What is a deductible?"}' | python -m json.tool

# Demo B - abstention (no hallucination when evidence is missing)
curl -s localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"message":"What is the exact premium for a 2015 Ferrari in Mumbai?"}'

# Demo C - purchase intent starts a deterministic flow with ZERO model calls
#   Conversation ids are minted by the server; the API refuses a client-invented id.
CONV=$(curl -s localhost:8000/api/v1/conversations -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["conversation_id"])')
curl -s localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"conversation_id\":\"$CONV\",\"message\":\"I want to buy a policy\"}"

# Demo G - the same journey over a text-only channel, still zero model calls
curl -s localhost:8000/api/v1/channels/whatsapp/messages -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"conversation_id\":\"$CONV\",\"text\":\"I want to buy a policy\"}"
```

Demos D (FAQ interruption preserves transaction state), E (mock provider through a
flow) and F (provider timeout produces no fake quote) are exercised end to end in
[`tests/e2e/test_demo_scenarios.py`](tests/e2e/test_demo_scenarios.py).

---

## Architecture at a glance

```text
Channels / UI
     |
API Gateway / WAF / Auth boundary            <- platform responsibility
     |
FastAPI application  (app/main.py, app/api/)
     |
JWT auth guard -> trusted AuthContext        <- app/core/auth
     |
ResourceScope / ownership interceptor        <- app/core/resource_scope
     |
Deterministic capability router              <- app/orchestration/router.py
     |
     +---------------------+---------------------+
     |                     |                     |
Fast deterministic    FAQ / language        Agentic escalation
     |                     |                     |
Workflow engine       RAG + grounding       Supervisor agent
     |                     |                     |
     +----------> Policy Enforcement Point <-----+     <- app/core/policy
                          |
                  Business service layer
                          |
                 Integration abstraction          <- app/integrations/contracts
                          |
              +-----------+-----------+
        Mock provider           InsureMO provider
        (non-prod)              (REQUIRES_VERIFICATION)
                          |
                 Authoritative systems
```

Every AI-assisted request enters through **one** control point,
[`HarnessService.execute`](app/ai/harness/service.py). Deterministic traffic never
reaches it — a structural guard rejects a deterministic capability inside the Harness,
and a test proves deterministic actions produce no Harness execution record.

### Repository layout

```text
app/
  api/            FastAPI routes, dependencies, middleware  (thin handlers only)
  core/           auth, resource scope, policy, config, errors, logging,
                  observability, privacy, security, audit (hash-chained), resilience,
                  registry, storage (Redis backend)
  ai/             harness (control plane + policies), agents, prompts, models, tools
  rag/            knowledge governance, ingestion, hybrid retrieval, grounding
  workflows/      deterministic engine, definitions, versioned state, store
  ui_directives/  versioned directive schemas + fail-closed registry
  channels/       channel adapters: inbound normalisation + per-channel rendering
  integrations/   provider contracts, mocks with fault injection, InsureMO adapters
  domains/        motor (reference), travel (extension proof)
  orchestration/  deterministic router, conversation service, session
  finops/         token and cost reporting
tests/            unit, contract, integration, e2e, security, performance
evals/            native | ragas | deepeval + shared datasets, reports, baseline
docs/             architecture, security, compliance, integrations, operations, ADRs
scripts/          run_evals, benchmark, load_test, recovery_drill, verify_audit_chain,
                  build_datasets, cost_report
ops/              Grafana dashboards and Prometheus alert rules as code (drift-tested)
knowledge/        approved knowledge documents with governance front-matter
```

---

## The seven rules this template enforces in code

1. **The model never owns transactional truth.** Only `WorkflowEngine.apply_action`
   writes state, it is unreachable from `app/ai/**` (enforced by an import test), and
   premium/issuance come from providers.
2. **Deterministic before agentic.** A structural router resolves button clicks, form
   submits, navigation and explicit purchase intent with **zero model calls**.
3. **UI directives are contracts.** 13 registered types, schema-validated, markup
   refused, accessibility fields mandatory, unknown types fail closed.
4. **One control point for AI.** Authorization, PII, injection, budgets, output
   validation, grounding, audit and telemetry live in the Harness — agents stay thin,
   and a test asserts they do not re-implement any of it.
5. **Abstain rather than guess.** Retrieval → evidence gate → answer → grounding check.
   Insufficient evidence returns "I could not verify this", costing zero model calls.
6. **Ownership is server-side.** A customer is pinned to their own subject; an agent
   must be explicitly assigned; a denial is indistinguishable from a missing resource.
7. **Provider swap is configuration.** `POLICY_PROVIDER=mock|insuremo`, one contract
   suite applied to both implementations.

---

## Extending the template

Add a domain by implementing `DomainModule` — see
[`docs/architecture/EXTENDING_THE_TEMPLATE.md`](docs/architecture/EXTENDING_THE_TEMPLATE.md).
The travel domain is the proof: it is **two files** (`workflow.py`, `module.py`) and
adds no infrastructure. A test asserts that it stays that way and that it reuses core
auth, guardrails, observability, audit, directives and provider contracts.

---

## Documentation

| Area | Document |
|---|---|
| Architecture | [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md), [DATA_FLOW.md](docs/architecture/DATA_FLOW.md) |
| Extending | [EXTENDING_THE_TEMPLATE.md](docs/architecture/EXTENDING_THE_TEMPLATE.md) |
| Threat model | [THREAT_MODEL.md](docs/architecture/THREAT_MODEL.md) |
| Security | [SECURITY.md](docs/security/SECURITY.md), [PII_HANDLING.md](docs/security/PII_HANDLING.md) |
| Compliance | [COMPLIANCE_REGISTER.md](docs/compliance/COMPLIANCE_REGISTER.md), [CONTROL_EVIDENCE_MATRIX.md](docs/compliance/CONTROL_EVIDENCE_MATRIX.md), [PRIVACY_CONTROL_MATRIX.md](docs/compliance/PRIVACY_CONTROL_MATRIX.md), [SECURITY_CONTROL_MATRIX.md](docs/compliance/SECURITY_CONTROL_MATRIX.md), [REGULATORY_OPEN_ITEMS.md](docs/compliance/REGULATORY_OPEN_ITEMS.md) |
| InsureMO | [INSUREMO_ASSUMPTIONS.md](docs/integrations/INSUREMO_ASSUMPTIONS.md), [INSUREMO_MAPPING_TEMPLATE.md](docs/integrations/INSUREMO_MAPPING_TEMPLATE.md) |
| Operations | [OBSERVABILITY.md](docs/operations/OBSERVABILITY.md), [DEPLOYMENT.md](docs/operations/DEPLOYMENT.md), [ENVIRONMENTS.md](docs/operations/ENVIRONMENTS.md), [INCIDENT_RUNBOOK.md](docs/operations/INCIDENT_RUNBOOK.md), [BCP_DR_READINESS.md](docs/operations/BCP_DR_READINESS.md), [RELEASE_CHECKLIST.md](docs/operations/RELEASE_CHECKLIST.md) |
| Decisions | [docs/adr/](docs/adr/) |
| Readiness | [PROJECT_READINESS.md](PROJECT_READINESS.md) |

---

## Verified dependency versions

Confirmed working together in this environment (see `PROJECT_READINESS.md` for the
evidence):

| Component | Version |
|---|---|
| Python | 3.12.10 |
| FastAPI | 0.141.1 |
| Starlette | 1.6.0 |
| Pydantic | 2.13.5 |
| **strands-agents** | **1.54.0** |
| PyJWT[crypto] | 2.13.0 |
| ragas | 0.4.3 |
| deepeval | 4.2.2 |

FastAPI was upgraded from 0.115 because `strands-agents` 1.54 requires Starlette 1.6,
which FastAPI 0.115 pins against. That constraint is recorded rather than assumed.

---

## What this template does *not* claim

* It is **not** certified as compliant with IRDAI, DPDP or CERT-In requirements.
  It provides compliance-*enabling* controls and evidence; interpretation and sign-off
  belong to Legal, Compliance and InfoSec.
* No InsureMO specification was supplied. The adapters implement the same Protocols as
  the mocks, and every unsupplied mapping raises `InsureMoContractNotConfigured`
  rather than guessing an endpoint or field.
* Mock premium arithmetic is a placeholder so flows are executable. Real rating must
  come from the authoritative rating engine.
* Stores are Protocols with two implementations each: in-memory (single process) and
  Redis (multi-instance). Production refuses in-memory. The Redis adapters are proven
  against an in-process emulator; confirming against a managed Redis in preprod is a
  recorded next action.
* LLM-as-judge eval metrics are `NOT_EVIDENCED` until a judge model is configured.
