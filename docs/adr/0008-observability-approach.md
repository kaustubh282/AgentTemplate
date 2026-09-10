# ADR 0008 - Observability: three streams, redaction by construction

## Status
Accepted.

## Context
The platform must be operable and auditable while handling personal data. The usual
outcome is a choice between useful telemetry and safe telemetry, resolved by asking
developers to be careful. That reliably fails.

## Decision
Three streams with different rules, and redaction enforced by construction rather than
by discipline:

- **logs** - structured JSON; the formatter routes every message *and* every extra
  field through the masking service, so a careless log line cannot leak PII. Stack
  traces never reach the log stream; the error type and a masked message do.
- **metrics** - numeric and labelled only; an in-process registry with bounded
  reservoir histograms (exact count/sum/max), scraped as JSON or Prometheus text.
  Request labels carry the route template, never the raw path.
- **traces** - a real OpenTelemetry `TracerProvider` (resource, ratio sampler, OTLP/HTTP
  exporter from the optional `otel` extra) plus an in-process recorder that always
  receives the same spans, so traces stay inspectable in tests without a collector. A
  forbidden-key filter drops prompt, message, document and answer attributes if any
  code attempts to attach them; exceptions contribute `error.type` only.

Audit is a deliberately separate fourth stream, described in ADR 0009 and §22.

Identifiers in logs and audit are pseudonyms (`subject_ref`, `conversation_ref`,
hashed IP), so an operator can correlate a session without learning identity.

Two failure policies, one explicit and one structural: audit is fail-closed by the
`AUDIT_FAIL_CLOSED=true` setting; telemetry is fail-open by construction, because span
export runs on the SDK batch processor, which drops on collector failure and never
raises into a request. There is deliberately no flag that could make telemetry loss
fail a request.

## Alternatives considered
1. **Log everything, redact at the sink.** Rejected: the raw data has already left the
   process, and the sink becomes a compliance dependency.
2. **Ask developers to redact at call sites.** Rejected: it fails the first time
   someone logs a request body under pressure.
3. **Require an OTEL collector.** Rejected: it would make the template unrunnable
   offline and untestable without infrastructure. The collector is optional: the
   provider is always real when `OTEL_ENABLED=true`, the exporter is attached only
   when an endpoint is configured, and the in-process recorder is always present.
4. **Fail closed on telemetry too.** Rejected: losing observability degrades
   operability; losing an audit event degrades accountability. Only the first is
   acceptable.

## Consequences
Positive: 57 PII tests and a native suite prove nothing leaks; the whole template runs
and is testable with no observability infrastructure; the fail-closed audit policy is
a visible decision and the fail-open telemetry policy is a property of the wiring
rather than emergent behaviour.

Negative: prompt and completion content is not available for debugging. Reproduction is
by *version* - prompt, corpus, guardrail and model versions are stamped on every audit
record - rather than by stored content. That is a deliberate trade.

Also: the metrics registry is per-process, so cross-instance aggregation is the
platform's job.

## Security impact
Positive. Telemetry cannot become an exfiltration channel. Pseudonymous identifiers
mean log access is not identity access.

## Compliance impact
Supports PRV-13 to PRV-15 and CERTIN-04. The fail-closed audit policy supports
IRDAI-IS-03. The absence of chain-of-thought storage supports §23 explicitly.

