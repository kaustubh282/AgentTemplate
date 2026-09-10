# ADR 0004 - Model provider abstraction with a single invoker

## Status
Accepted.

## Context
Model providers change: pricing, availability, regional rules and capability all move.
Business services must not be coupled to any of it. Equally, the platform needs a
single place where timeouts, usage accounting, cost estimation and fallback policy are
enforced - otherwise each call site invents its own.

## Decision
Concrete adapters implement the Strands `Model` interface, and configuration selects
one (`deterministic`, `openai`, `bedrock`). Above them sits exactly one
`ModelInvoker`, which owns generation settings, timeout, token and cost accounting,
metrics, and the fallback decision. Nothing else calls a model.

A `DeterministicModel` test double ships with the template so the full test and eval
suite runs offline with no credentials. Configuration refuses it in production, and the
factory refuses it again.

Fallback is opt-in per call and refused entirely for high-risk capabilities: a
regulated decision must not silently degrade to a weaker model.

## Alternatives considered
1. **Call the provider SDK directly from services.** Rejected: no single place for
   budgets, cost or timeout, and a provider change becomes a wide refactor.
2. **A provider-agnostic gateway service.** Rejected for now: another network hop and
   deployment. The invoker boundary makes it addable later.
3. **A recorded-cassette test double.** Rejected: cassettes drift and hide behaviour.
   A scripted/extractive double is deterministic and fast.
4. **Automatic fallback everywhere.** Rejected: for a high-risk decision, no answer is
   safer than a worse answer.

## Consequences
Positive: the whole suite runs offline; token and cost telemetry exist for every call;
a provider swap is configuration; cost per 1000 FAQ is a measured number (2.07) rather
than an estimate.

Negative: the offline double is not a language model, so *answer relevance* cannot be
measured without a real provider or a judge. That limitation is recorded rather than
papered over.

Notable: making the double extractive-from-evidence rather than rule-scripted was a
deliberate later change, because a rule-scripted double makes an eval measure the
script's coverage instead of the platform's retrieval.

## Security impact
The invoker is the single place that enforces the model timeout and records usage,
which is also where denial-of-wallet is contained.

## Compliance impact
Model id and provider appear in every audit version stamp, so a decision can be
attributed to a specific model configuration. Supports cross-border and vendor
disclosure questions (DPDP-10, PRV-18) because provider choice is explicit
configuration.

