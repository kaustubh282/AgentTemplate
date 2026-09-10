# Extending the template

How to add a new bot, product domain, capability, tool, provider, model or channel
without touching core infrastructure.

The rule: **domains depend on published core interfaces; core never imports a domain.**
113 mechanical checks in
[`tests/unit/test_architecture_boundaries.py`](../../tests/unit/test_architecture_boundaries.py)
enforce that direction, so this is not advice — it is a build gate.

---

## The proof that this works

The travel domain exists solely to prove the extension model. It is **two files**:

```text
app/domains/travel/
  workflow.py    4 states, 4 transitions, its own validators
  module.py      2 capabilities, service action, directive builder
```

It adds **no** infrastructure. It reuses auth, resource scope, the PEP, guardrails, PII
handling, observability, audit, the directive registry, resilience policy and provider
contracts. A test asserts it stays that way:

```python
def test_travel_domain_adds_no_new_core_files()
def test_travel_domain_reuses_shared_platform_services()
def test_domains_do_not_reimplement_core_infrastructure()
```

And it completes a full journey through the same API surface as motor
(`test_e2e_travel_domain_completes_its_own_journey`).

---

## Adding a domain: the five steps

### 1. Declare the journey

`app/domains/health/workflow.py` — a workflow is data, not branches:

```python
WORKFLOW_ID = "health_sales"
WORKFLOW_VERSION = "1.0.0"

STATE_ENTRY = "ENTRY"
STATE_COLLECT_MEMBERS = "COLLECT_MEMBERS"
STATE_QUOTE = "QUOTE"
STATE_COMPLETE = "COMPLETE"

#: Only these fields are ever projected into model context.
AI_CONTEXT_FIELDS = ("sum_insured", "member_count", "quote_reference")


def validate_members(payload, _state) -> dict:
    """Business rules live HERE, in the domain - never in a prompt."""
    require_fields(payload, ("member_count", "sum_insured"))
    count = int(payload["member_count"])
    if not 1 <= count <= 6:
        raise ValidationError("member_count_out_of_appetite", details={"max": 6})
    return {"member_count": count, "sum_insured": float(payload["sum_insured"])}


def build_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        domain="health",
        initial_state=STATE_ENTRY,
        states=(STATE_ENTRY, STATE_COLLECT_MEMBERS, STATE_QUOTE, STATE_COMPLETE),
        terminal_states=frozenset({STATE_COMPLETE}),
        ai_context_fields=AI_CONTEXT_FIELDS,
        transitions=(
            Transition(
                action="BEGIN",
                from_state=STATE_ENTRY,
                to_state=STATE_COLLECT_MEMBERS,
                description="Start the health purchase journey.",
                required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
                validator=no_payload,
            ),
            Transition(
                action="SUBMIT_MEMBERS",
                from_state=STATE_COLLECT_MEMBERS,
                to_state=STATE_QUOTE,
                description="Submit members and request a quote.",
                required_permissions=frozenset({Permission.QUOTE_CREATE}),
                fields_written=("member_count", "sum_insured"),
                validator=validate_members,
                side_effect_class=SideEffectClass.LOW_RISK_WRITE,
                service_action="create_quote",
            ),
        ),
    )
```

`fields_written` is a contract: the engine rejects a validator that writes anything
else. That is what stops a validator quietly setting `is_admin`.

### 2. Declare the capabilities

`app/domains/health/module.py`:

```python
Capability(
    id="health.workflow.action",
    domain="health",
    description="Advance the health purchase journey (deterministic).",
    input_schema=WorkflowActionInput,
    output_schema=WorkflowActionOutput,
    execution_mode=ExecutionMode.DETERMINISTIC,  # zero model calls
    model_required=False,
    risk_level=RiskLevel.MEDIUM,
    required_auth=True,
    allowed_actor_types=frozenset({ActorType.CUSTOMER, ActorType.AGENT}),
    allowed_roles=frozenset({Role.CUSTOMER, Role.AGENT}),
    required_permissions=frozenset({Permission.WORKFLOW_ADVANCE}),
    allowed_workflow_states=frozenset(wf.STATES),
    side_effect_class=SideEffectClass.LOW_RISK_WRITE,
    service_binding="workflows.health_sales",
    budgets=BudgetSpec(timeout_ms=4_000),
    rate_limit_class=RateLimitClass.DETERMINISTIC,
    audit_policy=AuditPolicy.FULL,
    allowed_channels=frozenset({Channel.WEB_CUSTOMER, Channel.MOBILE}),
)
```

The registry validates these declarations at construction: a deterministic capability
cannot require a model, a non-model capability cannot hold a model budget, and a
`HIGH_RISK_WRITE` or `IRREVERSIBLE` capability **must** require an idempotency key.

### 3. Build the directives

Reuse the registry; do not invent a component type:

```python
class HealthDirectiveBuilder:
    def __init__(self, registry: DirectiveRegistry) -> None:
        self._registry = registry

    def for_state(self, state: str, data: dict) -> Directive:
        if state == wf.STATE_COLLECT_MEMBERS:
            return self._registry.build(
                DirectiveType.SHOW_FORM,
                ShowFormPayload(
                    form_id="health_members",
                    title="Who is being covered?",
                    accessibility=a11y("Member details form", role="form"),
                    fields=[...],
                ),
            )
        ...
```

Every payload needs `accessibility` with a real `aria_label` — the registry refuses a
directive without one.

If you genuinely need a new component type, add it to `DirectiveType` **and**
`PAYLOAD_BY_TYPE` in the same change; a native eval asserts those two stay in step.

### 4. Bind the service actions

Provider-backed work goes through the provider abstraction, so it works identically
against the mock and against InsureMO:

```python
class HealthSalesService:
    async def create_quote(self, ctx, state, _payload) -> dict:
        request = CreateQuoteRequest(
            customer_id=ctx.auth.subject_id,   # from the token, never the payload
            product_code="HLT-FAMILY",
            domain="health",
            attributes={"member_count": state.data.get("member_count")},
            idempotency_key=ctx.idempotency_key,
        )
        response = await self._policy.execute(
            lambda: self._providers.quote.create_quote(request, ctx),
            side_effect=SideEffectClass.LOW_RISK_WRITE,
            idempotency_key=ctx.idempotency_key,
        )
        return {"quote_id": response.quote.quote_id, ...}
```

### 5. Register in the composition root

The **only** core file you touch — `app/bootstrap.py`:

```python
health = domain_registry.register(HealthDomainModule())
_register_domain(capability_registry, workflow_registry, health)
health_service = HealthSalesService(providers, audit, policy=provider_policy)
workflow_service.register_service_action(HEALTH_WORKFLOW_ID, "create_quote", health_service.create_quote)
bindings["health"] = DomainBinding(
    domain="health",
    workflow_id=HEALTH_WORKFLOW_ID,
    start_capability_id=CAPABILITY_HEALTH_START,
    action_capability_id=CAPABILITY_HEALTH_ACTION,
    directive_builder=HealthDirectiveBuilder(directive_registry),
)
```

Everything else — routing, authorization, guardrails, audit, telemetry, cost tracking,
the API surface — now works for the new domain with no further code.

---

## Adding domain knowledge

Drop a Markdown file under `knowledge/health/` with complete governance front-matter:

```markdown
---
document_id: KB-HEALTH-FAQ
document_name: Health Insurance FAQ
version: "1.0"
effective_date: 2026-06-01
source_system: KNOWLEDGE_PORTAL
classification: PUBLIC
document_type: FAQ
domain: health
product: Family Floater
audience: PUBLIC
language: en
status: ACTIVE
approved_by: health-product-owner
---

## What is a waiting period for pre-existing conditions
...
```

Ingestion **refuses** a document with incomplete metadata rather than guessing a
version or status. Only `ACTIVE` documents are retrievable, and adding a document
changes the corpus version, which invalidates cached answers automatically.

---

## Adding an authoritative read tool

Never let an agent reach a database or provider directly. Add a typed tool:

```python
async def get_health_claim_summary(self, ctx, claim_id, *, requested_customer_id=None):
    await self._scope.authorize_resource(
        ctx.auth,
        ResourceType.CLAIM,
        claim_id,
        requested_customer_id=requested_customer_id,
    )
    claim = await self._call("get_claim", lambda: self._providers.claims.get_claim(claim_id, ctx))
    if claim is None:
        return await self._unavailable(ctx, "GetHealthClaimSummary", ResourceNotFoundError("claim_not_found"))
    await self._audit_read(ctx, "GetHealthClaimSummary", "CLAIM", claim_id)
    return ToolResult(
        available=True, category="CLAIM_STATUS", data=AiClaimStatus.from_claim(claim).model_dump(mode="json")
    )
```

Then list it in the capability's `allowed_tools`. A tool not in that set is refused by
the PEP regardless of which agent asks.

Prefer a few broader typed capabilities over dozens of tiny tools.

---

## Adding an agent

Read the complexity budget first (§5.6). Three agents exist; each has a documented
distinct responsibility. Before adding a fourth, document:

* its distinct responsibility
* why a deterministic service or an existing agent is insufficient
* expected model-call and token cost
* latency impact
* the security-boundary benefit
* who owns its evaluation dataset

Then write it **thin**. An agent contains domain instructions, task reasoning, its
output schema and its prompt logic. It must not contain authorization, RBAC, ownership
checks, PII masking, injection policy, token limits, tool permission rules, logging
redaction, audit policy or retry policy. A test scans agent modules for exactly those
patterns and fails if it finds them.

```python
class MyAgent:
    async def handle(self, agent_ctx: AgentExecutionContext) -> HarnessResult:
        agent_ctx.agent_id = "my_agent"
        built = agent_ctx.builder.build(
            system_prompt="",
            question=agent_ctx.request.user_message,
            ledger=agent_ctx.ledger,
        )
        agent_ctx.ledger.check_model_call()
        agent_ctx.ledger.record_agent_step()
        result = await agent_ctx.invoker.generate([user_message(rendered)], ...)
        agent_ledger_record(...)
        return HarnessResult(HarnessOutcome.ALLOW, ReasonCode.OK, result.text, payload)
```

Register it with `harness.register_handler(capability_id, agent.handle)`. It inherits
every control automatically — proven by
`test_a_new_agent_inherits_every_control_without_copying_code`.

---

## Adding a provider (or wiring InsureMO)

1. Implement the Protocol in `app/integrations/contracts/providers.py`.
2. Return the platform error taxonomy; never leak an upstream payload.
3. Add the selection branch in `app/integrations/factory.py`.
4. The existing contract suite applies automatically — method sets, signatures, return
   annotations, error mapping, idempotency, timeout and outage behaviour.

For InsureMO specifically, complete
[`INSUREMO_MAPPING_TEMPLATE.md`](../integrations/INSUREMO_MAPPING_TEMPLATE.md) and
populate `InsureMoMapping`. Until then every adapter method raises
`InsureMoContractNotConfigured` — deliberately, so a missing mapping fails loudly
instead of silently sending a guessed shape.

---

## Adding a model provider

Extend `build_model` in `app/ai/models/factory.py`. Business services are unaffected:
they depend on `ModelInvoker`, which owns timeouts, usage accounting, cost estimation
and fallback policy. Use conservative generation settings for regulated tasks, and
remember that high-risk capabilities refuse model fallback by design.

---

## Adding a prompt

Create `app/ai/prompts/templates/<area>/<name>.yaml` with full metadata (id, version,
purpose, owner, change note, evaluation baseline). The registry refuses an incomplete
prompt. Keep prompts **short**: security and workflow rules that can be enforced
deterministically belong in code, not restated to the model on every request. The FAQ
system prompt is 124 tokens.

Any prompt change alters the prompt version, which appears in audit records and cache
keys, and should trigger the regression evals.

---

## Checklist before you open the PR

```bash
python -m ruff check . && python -m ruff format --check .
python -m mypy
python -m pytest -q
python -m evals.native.run
python -m evals.ragas.runners.rag_eval
python -m evals.deepeval.run
python scripts/benchmark.py          # compares against the accepted baseline
```

Or all of it: `python scripts/run_evals.py all`

Your domain should also have:

- [ ] its own tests (determinism, validation, ownership)
- [ ] eval cases added to the shared datasets
- [ ] knowledge documents with complete governance metadata
- [ ] no new core files, and no copied infrastructure
- [ ] an ADR if you changed a protected core contract

---

## Changing a core contract

Treat core modules as stable extension infrastructure. If a change is genuinely
required, the change must include:

* an ADR recording the decision and its alternatives
* a backward-compatibility analysis (directive `schemaVersion`, API version, workflow
  version)
* regression tests
* execution of every affected domain's tests

Directive schema changes must either stay backward compatible or ship a controlled
migration — clients negotiate on `schemaVersion`.
