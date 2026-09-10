# Observability

What the platform emits, where to look when something is wrong, and what is
deliberately absent.

Three streams with three different redaction policies: **logs** (structured, fully
redacted), **metrics** (numeric, labelled), **traces** (identifiers and measurements
only). Audit events are a fourth, separate stream — see
[`PII_HANDLING.md`](../security/PII_HANDLING.md).

---

## 1. Logs

Structured JSON on stdout. Every message and every extra field passes through the
masking service, so a careless log line cannot leak PII.

```json
{
  "timestamp": "2026-09-08T14:22:11.482Z",
  "severity": "INFO",
  "service": "protec-insurance-ai",
  "environment": "preprod",
  "version": "0.1.0",
  "logger": "app.ai.harness.service",
  "requestId": "req_8f2c...",
  "correlationId": "corr_a91d...",
  "conversationRef": "conv_004821337265",
  "actorRef": "sub_918273645500",
  "actorType": "CUSTOMER",
  "channel": "WEB_CUSTOMER",
  "message": "harness_blocked",
  "reasonCode": "BLOCK_PROMPT_INJECTION"
}
```

`actorRef` and `conversationRef` are stable pseudonyms: an operator can follow a
session end to end without learning who the customer is.

Exceptions log `errorType` plus a masked `errorMessage`. **No stack traces reach the
log stream** — deliberate, per §44.

---

## 2. Metrics

`MetricsRegistry` holds counters, gauges and bounded histograms in process, exported
as JSON at `GET /api/v1/internal/metrics` and in Prometheus text format at
`GET /api/v1/internal/metrics/prometheus` (both require `finops:read`). Histograms keep
exact `count`, `sum` and `max`; quantiles come from a fixed 10 000-sample uniform
reservoir, so memory is constant however long the process lives. Metrics are **not**
pushed over OTLP; scrape them.

Request metrics are labelled by the matched **route template**
(`route="/api/v1/policies/{policy_id}"`), never by the raw path, so resource
identifiers do not enter metric labels and cardinality stays bounded. Requests that
match no route are labelled `route="unmatched"`.

### Platform
`protec_requests_total`, `protec_request_errors_total`,
`protec_request_latency_ms` (p50/p95/p99), `protec_inflight_requests`,
`protec_rate_limit_rejects_total`

### AI
`protec_model_calls_total`, `protec_model_latency_ms`,
`protec_model_first_token_ms`, `protec_model_input_tokens`,
`protec_model_output_tokens`, `protec_model_estimated_cost`,
`protec_tool_calls_per_request`, `protec_agent_steps_per_request`,
`protec_agent_handoffs_per_request`, `protec_guardrail_interventions_total`,
`protec_schema_validation_failures_total`, `protec_fallback_total`,
`protec_abstention_total`, `protec_zero_model_call_requests_total`

### RAG
`protec_retrieval_latency_ms`, `protec_retrieval_documents`,
`protec_retrieval_zero_result_total`, `protec_grounding_failure_total`,
`protec_stale_document_hits_total`, `protec_conflicting_source_total`,
`protec_retrieval_tokens`, `protec_reranker_latency_ms`

### Cache
`protec_cache_hits_total`, `protec_cache_misses_total`,
`protec_cache_stale_prevented_total`

### Business flow
`protec_flow_starts_total`, `protec_flow_step_completions_total`,
`protec_flow_abandonment_total`, `protec_flow_validation_failures_total`,
`protec_flow_api_failures_total`, `protec_flow_completions_total`

### Provider and auth
`protec_provider_calls_total`, `protec_provider_latency_ms`,
`protec_provider_failures_total`, `protec_circuit_open_total`,
`protec_auth_failures_total`, `protec_authorization_denials_total`

---

## 3. Traces

Real OpenTelemetry. `configure_tracing` (called once from the composition root) builds
an SDK `TracerProvider` with:

* a `Resource` carrying `service.name` (`OTEL_SERVICE_NAME`), `service.version`
  (`APP_VERSION`) and `deployment.environment` (`APP_ENV`);
* a `ParentBased(TraceIdRatioBased(TRACE_SAMPLE_RATE))` sampler;
* when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, an OTLP/HTTP `OTLPSpanExporter` behind a
  `BatchSpanProcessor`. The endpoint is the collector base URL; `/v1/traces` is
  appended if absent.

The exporter lives in the optional extra: `pip install ".[otel]"` (the container image
installs it from `requirements.lock`). With `OTEL_ENABLED=true` but the extra missing,
setting an endpoint fails startup with `CONFIGURATION_ERROR
otel_exporter_not_installed` rather than silently tracing nowhere. With
`OTEL_ENABLED=false` the application tracer is a plain in-process recorder.

Whether or not export is configured, every span is also copied into the in-process
recorder with the same shape, so traces stay inspectable in tests and local runs.
Spans carry `request.id`, `correlation.id` and `channel` from the request context;
an exception inside a span sets status `ERROR` and `error.type` (the class name only,
never the message, which may contain PII). The provider is created once per process;
a second call adds an exporter for a new endpoint but does not change the sampler.

```text
HTTP request
 └─ orchestrator.handle_message
     ├─ workflow.apply_action            (deterministic path)
     └─ harness.execute
         ├─ faq.retrieve
         ├─ agent.model_call
         └─ tool.GetPolicyDetails
```

A forbidden-key filter drops `prompt`, `system_prompt`, `messages`, `document_text`,
`chunk_text`, `answer` and `user_message` if any code attempts to attach them.

---

## 4. Dashboards (shipped as code)

Five Grafana dashboards ship in `ops/grafana/` and scrape the Prometheus exposition at
`GET /api/v1/internal/metrics/prometheus` (permission `finops:read`). A unit test fails
if any panel references a metric the application does not emit. The panels:

**Service health** — request rate, success rate, latency p50/p95/p99, inflight
concurrency, rate-limit rejects, error rate by taxonomy code.

**AI efficiency (§58.12)** — model calls per request, **zero-model-call rate**, input
tokens p50/p95/p99, output tokens, tokens by capability, tokens by agent, agent steps,
**agent handoffs (should be 0)**, estimated cost per request, cost per 1000, cost per
successful FAQ.

**Answer quality** — abstention rate, grounding failure rate, retrieval zero-result
rate, stale-document hits, conflicting-source events, guardrail interventions by
reason.

**Business flow** — flow starts, step completion funnel, abandonment by state,
validation failures by field, provider failures by operation, completion rate.

**Security** — auth failures by reason, authorization denials by capability, guardrail
blocks by category, rate-limit rejects by dimension.

---

## 5. Alerts (shipped as code)

`ops/prometheus/alerts.yml` carries one rule per row below, drift-checked against the
metric catalogue by `tests/unit/test_observability_pack.py`.

| Alert | Condition | Severity | Why |
|---|---|---|---|
| Zero-model-call rate fell | drops more than 10% below baseline | **High** | a deterministic path started calling a model |
| Agent handoffs above zero | any handoff observed | **High** | an unjustified multi-agent path appeared |
| Median input tokens grew | more than 20% above baseline | **High** | token regression (§58.13) |
| Abstention rate spike | more than 2x baseline | Medium | knowledge gap or retrieval regression |
| Grounding failure spike | more than 2x baseline | **High** | possible hallucination pressure |
| Stale-document hits | greater than 0 | **High** | lifecycle governance failure |
| Guardrail blocks spike | more than 5x baseline | Medium | attack in progress |
| Authorization denials spike | more than 5x baseline | **High** | possible enumeration attempt |
| Circuit breaker open | any | **High** | upstream degraded |
| Provider failure rate | above 5% | **High** | integration degraded |
| Model fallback engaged | any in production | Medium | provider instability |
| Cost per 1000 above ceiling | configured ceiling breached | **High** | FinOps breach (§35.1) |
| Deterministic p95 above SLO | above 300 ms | Medium | application regression |
| FAQ p95 above SLO | above 3000 ms | Medium | retrieval or model regression |
| Audit sink failure | any | **Critical** | audit is fail-closed; requests will error |

Thresholds are starting points. Re-baseline after the first accepted preprod run.

---

## 6. Investigating an incident

**"A customer says they got a wrong answer."**
1. Get the `requestId` or `correlationId` from the response.
2. Find the `AI_EXECUTION_COMPLETED` audit event → capability, outcome, reason code,
   grounding decision, model calls, and the **version stamp** (prompt version,
   corpus version, guardrail version, model id).
3. Find the decision record → intent, retrieved source ids, tools, final action.
4. Reproduce with the same corpus and prompt versions.

You will **not** find the answer text or the prompt in telemetry — that is deliberate.
Reproduction is by version, not by stored content.

**"Costs jumped."**
1. `GET /api/v1/admin/finops/report` → cost by capability, by model, by outcome.
2. Compare `medianInputTokens` and `modelCallsPerRequest` against
   `evals/regression/baseline.json`.
3. Check the zero-model-call rate — a fall means a deterministic path regressed.

**"Latency degraded."**
1. Split by path: `protec_request_latency_ms{route}`.
2. Deterministic p95 up → application regression.
3. FAQ p95 up → check `protec_retrieval_latency_ms` then `protec_model_latency_ms`.
4. Check `protec_circuit_open_total` and provider latency.

**"Possible attack."**
1. `protec_guardrail_interventions_total` by reason.
2. `protec_authorization_denials_total` by capability — repeated denials from one
   subject suggests enumeration.
3. `protec_rate_limit_rejects_total` by dimension.
4. Audit `GUARDRAIL_BLOCKED` and `AUTHORIZATION_DENIED` events for the actor ref.

---

## 7. Configuration

```bash
OTEL_ENABLED=true
OTEL_SERVICE_NAME=protec-insurance-ai
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.example:4318   # OTLP/HTTP collector base URL
TRACE_SAMPLE_RATE=0.1          # ratio sampler; sample in production
AUDIT_FAIL_CLOSED=true         # a required audit event is never dropped
LOG_LEVEL=INFO                 # DEBUG is refused in prod
```

The §20.1 policy: losing telemetry degrades observability, losing an audit event
degrades accountability, and only the first is acceptable. Telemetry is fail-open by
construction rather than by flag: span export runs on the SDK's batch processor, which
drops on collector failure and never raises into a request. Audit is fail-closed by
the explicit `AUDIT_FAIL_CLOSED` setting.

---

## 8. Deliberately absent

* prompt and completion text in logs or traces
* retrieved document text in telemetry
* chain-of-thought anywhere
* raw customer identifiers in logs
* unmasked policy, claim or payment references
* stack traces in the log stream
* upstream payloads in errors

---

## 9. Gaps

| Gap | Impact | Owner |
|---|---|---|
| Dashboards and alert rules are shipped for Prometheus/Grafana only | Other backends need a translation of `ops/` | Platform |
| Metrics registry is per-process and scrape-only | Aggregation needed across instances; no OTLP metrics export | Platform |
| Sampler and resource are fixed by the first `configure_tracing` call | Changing `TRACE_SAMPLE_RATE` needs a restart | Platform |
| No log shipping configured | Depends on the platform | Platform |
| Trace sampling untested under load | May need tuning | Platform |
| No SLO burn-rate alerting | Error budgets not tracked | SRE |
