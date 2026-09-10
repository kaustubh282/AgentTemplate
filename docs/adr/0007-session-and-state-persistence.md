# ADR 0007 - Session and state persistence behind Protocols

## Status
Accepted.

## Context
Four kinds of state exist: conversation memory, deterministic workflow state,
authenticated user context, and authoritative policy and customer data. Conflating them
is the common failure - typically by treating the conversation as the place where
everything lives.

## Decision
Keep them separate, with different guarantees:

- **conversation memory** - bounded recent turns, expiring, explicitly *not*
  authoritative
- **workflow state** - typed, versioned, optimistically locked, persisted via a
  Protocol
- **auth context** - derived per request from a validated token, never stored
- **authoritative data** - owned by the System of Record, read through typed tools

Every store is a Protocol with an in-memory implementation. The API process holds no
state, so it is horizontally scalable once shared implementations exist.

After a transaction completes, durable truth lives in the SoR. A later question such as
"what was my premium?" reads it back through a typed tool rather than replaying the
conversation.

## Alternatives considered
1. **Everything in the LLM context.** Rejected: the context window is not a database.
   It truncates, it summarises, and it cannot be queried or audited.
2. **One combined store.** Rejected: conversation, workflow and audit data have
   different retention, encryption and access requirements. Combining them makes the
   strictest requirement apply to all of it, or - worse - the loosest.
3. **A shared store implementation shipped now.** Rejected for this build: it would add
   infrastructure the template cannot test here. The Protocol boundary makes it a
   drop-in.

## Consequences
Positive: interrupt/resume works from structured state, so context does not grow with
conversation length (measured 0.0% growth over 12 turns); a completed policy survives a
session store loss; retention can differ per store.

Negative: the shipped in-memory stores mean a single instance only. This is blocker B1
in `PROJECT_READINESS.md`, and the rate limiter is the sharpest edge - per-process
limits multiply by instance count.

## Security impact
Positive. Conversation memory is never authoritative, so a manipulated transcript
cannot change a premium or a policy status. Optimistic locking makes concurrent writes
conflict loudly. Idempotency keys make a post-failover retry safe.

## Compliance impact
Separate stores allow separate retention and erasure treatment, which is what
DPDP-03 to DPDP-05 need. Conversation data is bounded and expiring by default rather
than retained indefinitely.

