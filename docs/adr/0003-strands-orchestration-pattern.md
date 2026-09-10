# ADR 0003 - Strands orchestration: escalation, not default entry

## Status
Accepted.

## Context
The Strands Agents SDK provides agents, tool loops, hooks and multi-agent
orchestration. The default way to use such a framework is to make an agent the entry
point for every request and let it decide what to do.

Applied here, that would mean a button click travels through a supervisor agent to
reach a state machine - paying latency, tokens and a probabilistic failure mode for a
decision that is already unambiguous.

## Decision
Use Strands where reasoning genuinely adds value, and nowhere else.

A deterministic structural router runs first and classifies every request. Known
actions, form submissions, navigation, deterministic validation, provider invocation
and explicit purchase intent are resolved with zero model calls. Only free-text
understanding, grounded FAQ, ambiguous intent and genuinely agentic selection reach the
AI path.

Three agents exist, each with a distinct documented responsibility: FAQ/knowledge,
intent extraction, and a supervisor that is explicitly an escalation component. The
supervisor carries a written complexity-budget justification, and a test asserts that
justification is present.

Strands is used **minimally and honestly**: it supplies the `Model` abstraction that
every provider adapter implements, so the same objects can be handed to a Strands
`Agent` later. No `strands.Agent` tool loop runs today - each agent makes a single
governed call through the Harness's `ModelInvoker` - and therefore no Strands
`HookProvider` is shipped either. A hook bridge was removed because nothing invoked it
(§1.2: a module without a runtime responsibility is architecture theatre). When a
genuinely agentic capability needs the SDK loop, the Harness will register hooks that
delegate to the same composed services; until then the controls live in the Harness
lifecycle itself.

## Alternatives considered
1. **Agent-first for everything.** Rejected: measured cost and latency for no quality
   benefit, plus a probabilistic path where a deterministic one exists.
2. **Many specialised agents with handoffs.** Rejected: each handoff multiplies tokens
   and latency. Measured agent handoffs in this template: **0**.
3. **No framework, direct provider calls only.** Rejected: loses the shared `Model`
   contract, the tool-loop structure and the ability to grow into genuine agentic
   work later without re-plumbing every provider adapter.
4. **A supervisor that also executes.** Rejected: it must select from a server-supplied
   allow-list and execute nothing, so that the PEP remains the only authority.

## Consequences
Positive: 74% of benchmark traffic uses zero model calls; deterministic p95 is 0.6 ms;
no request in the benchmark performs an agent handoff; the framework is still available
for future agentic capabilities.

Negative: the router needs maintaining as new phrasings appear, and it is deliberately
conservative - anything it cannot classify safely escalates rather than guessing.

## Security impact
Positive. Fewer requests reach a model, so the injection surface is smaller. An agent
that does reach a model selects from a server-supplied allow-list and grants itself
nothing.

## Compliance impact
Supports cost control and auditability: most requests have no model involvement at all,
and those that do carry an execution record naming the agent, the model and the
versions involved.

