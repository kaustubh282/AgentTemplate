# ADR 0006 - InsureMO adapter boundary with explicit unconfigured failure

## Status
Accepted.

## Context
The template must be able to switch from mocks to InsureMO with minimal change. No
InsureMO specification was supplied to this build - no endpoints, payloads, error
bodies, auth scheme or idempotency semantics.

There were two ways to handle that absence. Guess a plausible API so the code looks
complete, or make the absence explicit and loud.

## Decision
Depend only on Protocols. Implement the parts that do not require the specification -
transport, timeouts, correlation propagation, status-to-taxonomy mapping, payload-leak
prevention. For everything that does require it, raise
`InsureMoContractNotConfigured("REQUIRES_VERIFICATION:<what>")`.

Select the provider once, from configuration, in a factory. Run one contract suite
against both implementations: identical method sets, signatures, return annotations,
error mapping, idempotency and failure behaviour.

Record every assumption in `INSUREMO_ASSUMPTIONS.md` with what breaks if it is wrong,
and provide `INSUREMO_MAPPING_TEMPLATE.md` to be completed from the real specification.

## Alternatives considered
1. **Invent a plausible InsureMO API.** Rejected. It would look finished, pass its own
   tests, and fail on first contact with reality - and a reviewer could not tell which
   parts were real.
2. **Omit the adapters entirely.** Rejected: the substitution boundary would be
   untested, and the mock would quietly become the design.
3. **A generic HTTP mapping driven by configuration alone.** Rejected: the interesting
   part is not the transport, it is the semantics - especially ownership resolution.

## Consequences
Positive: substitution is proven today by contract tests; a missing mapping fails
immediately and unmistakably; the assumption list is a concrete work item rather than a
discovery during integration.

Negative: the InsureMO path cannot be exercised end to end, so the integration score is
evidenced against mocks only. `PROJECT_READINESS.md` records this as blocker B5.

The most important open item is ownership resolution: if InsureMO cannot answer "who
owns this id?" independently of the caller, the resource-scope strategy needs a
different mechanism. Guessing here would be a security decision disguised as an
integration detail, so the adapter refuses.

## Security impact
Neutral to positive. Refusing to guess prevents a fabricated integration from silently
weakening the cross-customer control. Error mapping already prevents upstream payloads
reaching users, logs or model context.

## Compliance impact
Supports the outsourcing and vendor requirements by keeping the third-party boundary
explicit and independently testable. The unresolved items are recorded as OI-11 to
OI-15 rather than hidden.

