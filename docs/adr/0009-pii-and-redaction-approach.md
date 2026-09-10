# ADR 0009 - PII: classification-driven, remove-not-mask for secrets

## Status
Accepted.

## Context
Insurance workloads involve high-risk personal data: identifiers, health information,
payment references. That data must move through an application that also sends text to
a third-party model. Pattern-matching alone is not sufficient - a novel format evades
it - and hand-applied masking at call sites is not reliable.

## Decision
Classify first, then act on the classification.

Six levels: PUBLIC, INTERNAL, CONFIDENTIAL, PII, SENSITIVE_PII, SECRET. Field names
map to levels, with substring matching for variants. The action depends on the level:

- **SECRET** - removed entirely. Not masked. Absence is a stronger guarantee than
  obfuscation, and a masked secret still tells an attacker it exists.
- **PII / SENSITIVE_PII** - removed at the model boundary unless the capability declared
  the field as needed for the current purpose. Default deny.
- **CONFIDENTIAL** - masked, keeping only a short suffix for operational reference.
- free text - additionally pattern-masked for mobile, email, PAN, Aadhaar, card
  numbers, vehicle registrations, JWTs and Authorization values.

A structural `assert_no_secrets` check runs on the fully rendered context before it
reaches a provider. Provider and domain objects never reach a model: an AI-facing DTO
declares `ai_fields`, and the projection drops everything else.

A credential pasted by a user is **blocked and audited**, not masked - masking would
leave the user believing the secret was handled safely.

## Alternatives considered
1. **Pattern matching only.** Rejected as the primary control: it cannot know that
   `policy_holder_identifier` is personal data.
2. **Mask secrets rather than remove them.** Rejected: it preserves the shape and the
   existence of the secret for no benefit.
3. **Allow-list fields for the model.** This is effectively what `purpose_fields`
   provides, and it is the mechanism used - the classification decides the default, and
   the capability opts in.
4. **Redact only on the way out.** Rejected: personal data reaching a third-party model
   is the event to prevent, not the event to log.

## Consequences
Positive: 57 boundary tests, native suite 17/17, benchmark PII leakage incidents 0; a
new PII field is a one-line classification change; the model receives masked,
field-limited views only.

Negative: an unclassified new field defaults to INTERNAL and would pass. Mitigated by
the release checklist and by the fact that most genuinely sensitive names match the
substring rules.

Trade-off accepted: masked identifiers keep four characters, which is a small
information disclosure permitted for operational usability. Recorded as an assumption
in `PRIVACY_CONTROL_MATRIX.md`.

## Security impact
Strongly positive. Removes the most likely LLM-application data-leak paths: PII in
prompts, secrets in logs, raw upstream payloads in context.

## Compliance impact
Directly supports DPDP-01 (minimisation), DPDP-02 (purpose limitation) and DPDP-07
(safeguards). `purpose_fields` is how purpose limitation becomes concrete rather than
aspirational.

