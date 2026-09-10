# ADR 0001 - Deterministic workflow, not LLM-owned state

## Status
Accepted.

## Context
An insurance purchase or service journey has mandatory ordered steps, business rules
that must hold, and side effects involving money. An LLM-driven agent loop could in
principle drive such a journey by keeping state in its context and deciding the next
step. That approach is attractive because it is quick to build and flexible.

It is also unauditable. The same input can produce a different sequence, the state
lives in a context window that is truncated and summarised, and there is no way to
answer "why did this customer reach payment without validation?" after the fact.

## Decision
Model every journey as an explicit, versioned state machine. Transitions are data:
each declares its action, source and target state, required permissions, the fields it
writes, its prerequisites, its validator, its side-effect class, and whether it needs
confirmation and an idempotency key. Anything not declared is rejected.

Only `WorkflowEngine.apply_action` writes state. The AI layer cannot import it, and a
test enforces that at import level. The LLM may interpret language and map it to a
permitted action; it may not reorder, skip or invent one.

`WorkflowService` runs precheck, then the side effect, then the commit - so a provider
failure leaves nothing committed.

## Alternatives considered
1. **LLM-driven agent loop with tools.** Rejected: unreproducible, unauditable, and it
   would make the model the system of record for money.
2. **LLM proposes, code validates.** Rejected as the primary mechanism: it still spends
   a model call on a decision that has none, and the validation surface becomes the
   real state machine anyway - with the model as an unnecessary source of latency,
   cost and failure.
3. **A workflow engine as a separate service (BPMN or similar).** Rejected for now:
   another deployment and failure domain for a state machine that is a few hundred
   lines. The `WorkflowRegistry` boundary makes extraction possible later.

## Consequences
Positive: journeys are reproducible and testable to exact PASS/FAIL; the whole
transactional path costs zero model calls (measured p95 0.6 ms); a rejected transition
provably does not mutate state; adding a domain means adding data, not branches.

Negative: a new journey requires an explicit definition rather than a prompt, so
"just try it" prototyping is slower. Conversational flexibility must be added
deliberately - through the router and the intent agent - rather than emerging.

Also: 33 determinism tests and 17 native workflow eval cases exist because this
decision makes such tests meaningful.

## Security impact
Strongly positive. The model cannot mutate authoritative state, skip authorization, or
reach payment or issuance out of order. High-risk transitions require explicit
confirmation and an idempotency key before execution, checked in the engine rather
than discovered inside a provider call.

## Compliance impact
Positive. Every transition is audited with the fields written (never their values) and
a version stamp, so an interaction can be reconstructed without storing conversation
content or chain-of-thought. Supports IRDAI-PP-05, IRDAI-PP-06 and IRDAI-PP-08.

