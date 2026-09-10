# ADR 0011 - Centralized in-process Harness as the AI control plane

## Status
Accepted.

## Context
AI-assisted execution needs many controls: authorization, workflow-state
preconditions, PII minimisation, prompt-injection checks, tool allow-listing, token and
step budgets, structured-output validation, grounding, leakage scanning, fallback
policy, audit and telemetry.

If each agent applies these itself, they drift. The second agent forgets the PII check;
the third forgets the budget. The drift is silent and is discovered in production.

## Decision
One public entry point for all AI-assisted execution:
`HarnessService.execute(request_context, capability_request)`.

The Harness **orchestrates**; it does not implement. It composes the specialised
services - PEP, sanitizer, guardrails, budget service, context builder, grounding,
invoker, output scanner, audit - each independently testable and replaceable.

Agents stay thin: domain instructions, task reasoning, output schema, prompt logic.
They must not re-implement authorization, RBAC, ownership, PII masking, injection
policy, token limits, tool permissions, logging redaction, audit or retry policy. A
test scans agent modules for those patterns and fails if it finds them.

Deterministic traffic must **not** enter the Harness, and this is enforced
structurally: a deterministic capability inside the Harness is rejected, and a test
asserts deterministic actions produce no execution record.

Every execution ends in an explicit outcome - ALLOW, BLOCK, ABSTAIN, FALLBACK,
ESCALATE - with a machine-readable reason code and a non-sensitive execution record.

The Harness is an **in-process module**, not a service.

## Alternatives considered
1. **Controls inside each agent.** Rejected: guaranteed drift.
2. **A Harness HTTP microservice.** Rejected: another network hop, another deployment,
   another failure domain, duplicated observability plumbing - for no current benefit.
   The abstraction makes extraction possible without requiring it.
3. **Middleware-only controls.** Rejected: middleware cannot see capability-level
   declarations such as tool allow-lists, workflow-state preconditions or per-capability
   budgets.
4. **A Harness that owns every subsystem.** Rejected explicitly: that produces a god
   service. It stays under 500 lines, holds no regex, and makes no network call - all
   three are asserted by tests.

## Consequences
Positive: ten centralization proofs pass, including a rogue agent whose malformed
payload and prohibited claim are both caught by the Harness rather than the agent, and
a brand-new agent that inherits every control without copying a line of it.

Negative: the Harness is a single point of change - a bug there affects every AI path.
Mitigated by keeping it thin, delegating everything, and testing it directly.

Latency overhead is measured: FAQ p95 2.3 ms end to end with the offline model, so the
control plane itself is not a latency source.

## Security impact
Strongly positive. It is the reason "two different agents receive identical auth and PII
treatment" is a provable statement rather than a hope. Tool authorization is injected
by the Harness, so an agent cannot self-authorize - and an `AgentExecutionContext`
without an injected authorizer denies by default.

## Compliance impact
Central audit and explainability emission means every AI-assisted request produces
evidence with a version stamp, without any agent having to remember to do it. Supports
§22 and §23 directly.

