# ADR 0002 - UI directives as versioned server-owned contracts

## Status
Accepted.

## Context
The assistant must drive rich interactions: forms, option lists, quote summaries,
review screens, payment handoff, retry states. The tempting shortcut is to let the
model emit HTML or a component description, which the client renders.

That shortcut hands a client-side execution surface to an untrusted output. It also
makes the UI untestable and accessibility impossible to guarantee.

## Decision
The server returns a directive drawn from a closed, registered set. Thirteen types
exist. Each has a Pydantic payload schema. The registry validates the payload,
recursively scans every payload string for script, markup, `javascript:` and event
handlers, and requires accessibility metadata with a real `aria_label`. An
unregistered type fails closed.

The model never selects a directive. It produces text or a structured payload; the
server chooses the component.

Responses carry `schemaVersion`, so clients can negotiate and a change can be migrated
rather than broken.

## Alternatives considered
1. **Model-generated HTML.** Rejected: an XSS surface driven by an untrusted producer.
2. **Model-generated JSON component tree.** Rejected: still unbounded, and it moves the
   validation problem rather than solving it.
3. **Free-text only.** Rejected: no forms, no structured review, no payment handoff -
   the transactional half of the product becomes impossible.
4. **A registry without accessibility fields.** Rejected: accessibility added later is
   accessibility not added. Making `aria_label` mandatory at the contract level is the
   only reliable way to get it.

## Consequences
Positive: no executable content can reach a client; the client renders only
allow-listed components; accessibility is structurally guaranteed; the contract is
independently versioned and testable (22 native checks).

Negative: a genuinely new component requires a code change in two places
(`DirectiveType` and `PAYLOAD_BY_TYPE`), and a native eval enforces they stay in step.
That friction is intentional.

## Security impact
Removes the client-side execution surface entirely, which is OWASP LLM02.

## Compliance impact
Directly supports the accessibility requirement (IRDAI-PP-07): semantic contracts,
mandatory labels, non-colour status text, live regions and accessible validation
messages. Also supports conduct requirements, because the server controls what is
presented as a factual statement.

