# ADR 0010 - Environment strategy: one artifact, fail-fast configuration

## Status
Accepted.

## Context
The most dangerous configuration error in a system like this is mock data reaching
production - a fabricated premium presented to a customer as real. The second most
dangerous is a development authentication shortcut surviving into production.

Both are configuration mistakes, and both are silent by default.

## Decision
Build one immutable artifact and promote it through local, dev, test, preprod and prod
with configuration injected externally. Then make unsafe configuration **impossible to
start**, not merely discouraged.

Production refuses to start with: any mock critical provider, the deterministic test
model, a symmetric JWT algorithm, a dev secret, no JWKS or public key, debug endpoints
enabled, `LOG_LEVEL=DEBUG`, disabled rate limiting, or a wildcard CORS origin.

`validate_startup` repeats the critical checks so the guard also holds for a Settings
object constructed by other means.

All 101 settings are documented in `.env.example`, and a test asserts the documentation
stays complete - so an undocumented environment variable cannot be introduced.

## Alternatives considered
1. **Warn on unsafe configuration.** Rejected: a warning at 3 a.m. during an incident
   is not a control.
2. **Separate builds per environment.** Rejected: it breaks promotion, and the thing
   tested is no longer the thing deployed.
3. **Runtime checks at first use.** Rejected: the failure then happens mid-request,
   possibly after a side effect.
4. **Trust deployment automation to set it correctly.** Rejected: automation is
   configuration too.

## Consequences
Positive: mock data cannot reach production; a misconfigured deployment fails visibly
at startup rather than serving unsafely; the documentation gate keeps configuration
discoverable.

Negative: a legitimately unusual production setup may be blocked and require a code
change to the validator. That friction is the point, and it is cheap to review.

Also: preprod must mirror production, which means shared stores and a tamper-evident
audit sink before it is a valid source of readiness evidence.

## Security impact
Strongly positive. Nine unsafe production settings are refused outright, tested by
`test_production_configuration_guard_is_comprehensive`. A valid production
configuration is also tested, so the guard is not so broad that nothing can start.

## Compliance impact
Supports IRDAI-IS-09 (change management) and the environment-separation requirement.
Prevents synthetic data being presented as authoritative, which supports IRDAI-PP-02
and IRDAI-PP-03.

