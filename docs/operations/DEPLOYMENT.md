# Deployment

The application is a stateless ASGI service. Build one immutable image, promote it
through the environments, inject configuration externally.

---

## 1. Build

```dockerfile
# Dockerfile
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.lock pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.lock \
 && pip install --no-cache-dir --no-deps .

FROM python:3.12-slim
# Run as a non-root user.
RUN useradd --create-home --uid 10001 protec
WORKDIR /srv
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY app ./app
COPY knowledge ./knowledge
USER protec
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=3).status==200 else 1)"
# No secrets, no .env, no tests, no evals in the image.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Notes that matter:

* **dependencies come from `requirements.lock`** — the exact runtime closure that the
  CI `supply-chain` job audits (`pip-audit --strict`), inventories and describes in the
  SBOM. The package is then installed with `--no-deps`, so the image cannot pick up a
  version the audit did not see. Regenerate the lock whenever `pyproject.toml`
  dependencies change (a CI step fails if a core dependency is missing from it).
* **no `.env` in the image** — configuration is injected at runtime
* **non-root user**; a Docker `HEALTHCHECK` on the liveness endpoint
* `knowledge/` is copied because the shipped local retrieval provider reads it from
  disk. Swapping in a hosted vector store is a composition-root change (`bootstrap.py`),
  not a setting; drop the copy once nothing reads the directory.
* tests and evals are excluded from the runtime image but must have passed to produce it
* the base image is tag-pinned, not digest-pinned; digest pinning and image scanning are
  recorded gaps in `SECURITY_CONTROL_MATRIX.md` LLM05

---

## 2. Run

```bash
docker run -p 8000:8000 \
  -e APP_ENV=preprod \
  -e JWT_ISSUER=https://idp.example/ \
  -e JWT_AUDIENCE=protec-insurance-ai \
  -e JWT_JWKS_URI=https://idp.example/.well-known/jwks.json \
  -e JWT_ALLOWED_ALGORITHMS=RS256 \
  -e MODEL_PROVIDER=openai \
  -e MODEL_ID=<approved-model> \
  -e MODEL_API_KEY=<from secret manager> \
  -e POLICY_PROVIDER=insuremo \
  -e QUOTE_PROVIDER=insuremo \
  -e CUSTOMER_PROVIDER=insuremo \
  -e CLAIMS_PROVIDER=insuremo \
  -e PAYMENT_PROVIDER=insuremo \
  -e INSUREMO_BASE_URL=<supplied> \
  -e INSUREMO_API_KEY=<from secret manager> \
  -e CORS_ALLOWED_ORIGINS=https://app.protec.example \
  -e OTEL_ENABLED=true \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.example:4317 \
  protec-insurance-ai:<sha>
```

**The application will refuse to start** if the configuration is unsafe for the
environment — mock providers in prod, the deterministic model in prod, a symmetric JWT
algorithm, a dev secret, wildcard CORS, debug endpoints, `DEBUG` logging, or disabled
rate limiting. That is intentional: fail fast at startup rather than serve unsafely.

---

## 3. Health probes

| Probe | Endpoint | Expectation |
|---|---|---|
| Liveness | `GET /api/v1/health/live` | `200 {"status":"alive"}` |
| Readiness | `GET /api/v1/health/ready` | `200 {"status":"ready"}` |

Suggested Kubernetes settings:

```yaml
livenessProbe:
  httpGet: { path: /api/v1/health/live, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 15
readinessProbe:
  httpGet: { path: /api/v1/health/ready, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 10
```

Readiness reflects coarse dependency health and exposes **no** credentials, hostnames or
provider detail — verified by the native `contract` suite.

---

## 4. Scaling

The API process is stateless: identity comes from the token, and all state lives behind
a store Protocol.

**Blocking requirement before scaling beyond one instance:**

| Component | Shipped | Configurable | Required for scale |
|---|---|---|---|
| Session / conversation store | in-memory | `SESSION_STORE_PROVIDER=inmemory\|redis` | shared (`redis`) |
| Workflow state store | in-memory | `WORKFLOW_STORE_PROVIDER=inmemory\|redis` | shared, with optimistic locking preserved |
| Rate-limit store | in-memory | `RATE_LIMIT_STORE_PROVIDER=inmemory\|redis` | **shared, or limits are multiplied by instance count** |
| FAQ cache | in-memory | `CACHE_PROVIDER` | shared or per-instance (per-instance is acceptable) |
| Audit sink | in-memory / file | `AUDIT_STORE_PROVIDER` | shared tamper-evident store |

**Production refuses `inmemory` for the first three** (`Settings` raises at startup),
so a single-instance deployment cannot silently become a multi-instance one.

The `redis` adapters are shipped (`app/workflows/state/redis_store.py`,
`app/orchestration/session_redis.py`, `app/core/security/rate_limit_redis.py`) and
connect through `REDIS_URL` (`rediss://` in production). Their multi-instance semantics
are proven offline against an in-process Redis emulator by
`tests/integration/test_shared_stores.py` and `scripts/recovery_drill.py`: state written
by one instance is read by another, optimistic locking holds across instances, and the
rate limit is one budget rather than one per instance. **Confirming against a managed
Redis in preprod is a recorded next action**, not assumed.

The audit sink in production must be `AUDIT_STORE_PROVIDER=chained_file` with
`AUDIT_CHAIN_SECRET` set: an HMAC hash chain that makes any edit, deletion or reorder
detectable, plus a signed head file (`<log>.head`, rewritten atomically after every
append) that makes **tail truncation** detectable (`scripts/verify_audit_chain.py`
reports `tail_truncated`). Keep the head file next to the log on the same WORM /
object-lock storage, and keep the chain secret with the verifier, not only the writer.
Each record names the writing instance (`hostname:pid`); two instances appending to one
file verify but are reported as a `multi_writer` warning, so give each instance its own
log. A restarted instance refuses to start on a log shorter than its head
(`audit_chain_tail_truncated`) rather than overwrite the evidence.
`PROJECT_READINESS.md` blocker B1.

Autoscale on `protec_inflight_requests` and `protec_request_latency_ms` p95 rather than
CPU: this is an I/O-bound service.

---

## 5. Gateway and platform responsibilities

Not implemented in the application, and required in front of it:

* TLS termination and certificate management
* WAF / API protection
* DDoS protection
* private networking to providers and the model endpoint
* outbound allow-listing
* least-privilege service identity
* secret manager integration
* encryption at rest for every store

---

## 6. Rollout

```text
build image  ──▶  run all gates  ──▶  dev  ──▶  preprod  ──▶  prod (canary)  ──▶  prod
```

```bash
python scripts/run_evals.py all      # must exit 0 to produce a promotable image
```

Canary with feature flags: `FEATURE_PUBLIC_FAQ_ENABLED`,
`FEATURE_TRAVEL_DOMAIN_ENABLED`, `FEATURE_AGENTIC_ESCALATION_ENABLED`,
`FEATURE_STREAMING_ENABLED`.

Watch during canary: error rate by taxonomy code, deterministic p95, FAQ p95,
**zero-model-call rate**, abstention rate, cost per 1000, guardrail interventions.

---

## 7. Rollback

```bash
kubectl rollout undo deployment/protec-insurance-ai
```

Prompts, knowledge, model configuration, guardrail thresholds and feature flags roll
back independently of the application version — see the rollback table in
[`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md).

In-flight workflows carry their own `workflow_version`, so a rolled-back deployment
does not corrupt a journey started under the newer definition.

---

## 8. Knowledge deployment

Knowledge is data, not code, and has its own lifecycle:

```bash
# inspect
curl -s $BASE/api/v1/admin/knowledge -H "Authorization: Bearer $TOKEN"

# revoke immediately (invalidates caches via the corpus version)
curl -X POST $BASE/api/v1/admin/knowledge/lifecycle \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"document_id":"KB-MOTOR-FAQ","status":"REVOKED"}'
```

A knowledge change alters the corpus version, which appears in audit records and in
every FAQ cache key — so a stale answer cannot be served from cache after a change.

---

## 9. Pre-deployment checklist

- [ ] `python scripts/run_evals.py all` exits 0
- [ ] benchmark shows no regression against the accepted baseline
- [ ] configuration reviewed for the target environment
- [ ] secrets present in the secret manager, absent from the image
- [ ] `/health/ready` returns `ready` in the target environment
- [ ] dashboards and alerts in place
- [ ] rollback plan confirmed
- [ ] for preprod/prod: shared stores and a tamper-evident audit sink configured
- [ ] release checklist completed — see [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md)

---

## 10. CI pipeline (starter)

```yaml
# .github/workflows/ci.yml
name: ci
on: [push, pull_request]
jobs:
  gates:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev,evals]"
      - run: python -m ruff check .
      - run: python -m ruff format --check .
      - run: python -m mypy
      - run: python -m pytest -q
      - run: python -m evals.native.run
      - run: python -m evals.ragas.runners.rag_eval
      - run: python -m evals.deepeval.run
      - run: python scripts/benchmark.py
  supply-chain:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.lock && pip install --no-deps .
      - run: pip install pip-audit && pip-audit -r requirements.lock --strict   # release blocking
      - run: pip install pip-licenses && pip-licenses --format=markdown --output-file=licenses.md
      - run: pip install cyclonedx-bom && cyclonedx-py environment -o sbom.json
      # licenses.md, sbom.json and requirements.lock are uploaded as the supply-chain artifact
```

Container image scanning (for example `grype` on the built image) is **not** in the
pipeline yet; it is recorded as a gap in `SECURITY_CONTROL_MATRIX.md` LLM05 rather than
presented as done.
