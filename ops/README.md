# Operations pack

Dashboards and alert rules as code, so operators do not start from a blank screen and the
readiness claim "observability" rests on shipped artefacts rather than a list of metric
names (master prompt §21, §37).

| Path | What it is | Consumed by |
|---|---|---|
| `prometheus/alerts.yml` | 17 alerting rules, one per row of `docs/operations/OBSERVABILITY.md` §5 | Prometheus / Alertmanager |
| `grafana/service-health.json` | request rate, success rate, latency quantiles, inflight, rate-limit rejects, errors by code | Grafana (import) |
| `grafana/ai-efficiency.json` | model calls/request, **zero-model-call rate**, tokens p50/p95/p99, agent steps and handoffs, cost per 1000, estimated-token share | Grafana |
| `grafana/answer-quality.json` | abstention rate, grounding failures, zero-result retrievals, stale hits, conflicting sources, guardrail interventions by reason | Grafana |
| `grafana/business-flow.json` | flow starts, step funnel, validation failures, provider failures by operation, completions | Grafana |
| `grafana/security.json` | auth failures by reason, authorization denials, guardrail blocks by category, rate-limit rejects by dimension | Grafana |

## Scrape target

The application exposes the registry in Prometheus text format at

```
GET /api/v1/internal/metrics/prometheus      (requires permission finops:read)
```

Scrape it with a service-identity bearer token. The endpoint is internal: put it behind
the platform network boundary, not the public gateway.

## Drift protection

`tests/unit/test_observability_pack.py` parses every dashboard and rule file and fails
if an expression references a metric the application does not emit. Adding a metric
means adding it to `app/core/observability/metrics.py`; renaming one without updating
the pack fails CI.

## Not included, deliberately

* Alertmanager routing and on-call schedules — organisational, live in the platform repo.
* SLO burn-rate alerts — add once error budgets are agreed (see `OBSERVABILITY.md` §9).
* Log-based alerts — logs are structured JSON; wire them in your log platform.
