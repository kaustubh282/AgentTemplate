# Environments

Five environments, one immutable artifact, configuration injected externally.

The build produces a container image; the same image is promoted through the
environments with different configuration. Nothing about the environment is baked into
the artifact.

---

## Matrix

| | `local` | `dev` | `test` | `preprod` | `prod` |
|---|---|---|---|---|---|
| Mock providers | allowed | allowed | allowed | **exception only** | **refused** |
| Deterministic test model | allowed | allowed | allowed | **exception only** | **refused** |
| Symmetric JWT secret | allowed | allowed | allowed | **refused** | **refused** |
| JWKS required | no | recommended | no | **yes** | **yes** |
| Debug endpoints | allowed | allowed | allowed | no | **refused** |
| `LOG_LEVEL=DEBUG` | allowed | allowed | allowed | no | **refused** |
| Rate limiting | optional | on | off (tests) | **on** | **refused if off** |
| Wildcard CORS | allowed | allowed | allowed | no | **refused** |
| Real customer data | **never** | **never** | **never** | masked/synthetic only | yes |
| API docs (`/docs`) | on | on | on | on | **off** |
| HSTS | off | off | off | off | **on** |
| Trace sampling | 1.0 | 1.0 | 1.0 | 1.0 | 0.1 |
| Audit sink | in-memory | file | in-memory | **tamper-evident** | **tamper-evident** |
| Stores (session / workflow / rate-limit) | in-memory | in-memory | in-memory or `fakeredis://` | **redis** | **redis (in-memory and fakeredis refused)** |
| Audit sink | in-memory | file | in-memory / chained_memory | **chained_file** | **chained_file (others refused)** |
| `CONFIRMATION_TOKEN_SECRET`, `AUDIT_CHAIN_SECRET` | optional | optional | optional | set | **required** |

"Refused" means the application will not start. `Settings` raises during validation and
`validate_startup` repeats the critical checks. Verified by
`test_production_configuration_guard_is_comprehensive`.

---

## local

Everything offline. No credentials, no network, no real data.

```bash
cp .env.example .env
python -m uvicorn app.main:app --reload
```

Mock providers with fault injection, a deterministic scripted model, local knowledge
fixtures, verbose logs. This is the environment the whole eval suite runs in.

**Never** point local at production data or a production provider.

## dev

Shared integration environment. Mocks still permitted so a broken upstream does not
block feature work. Prefer JWKS here so the auth path matches production.

## test

What CI runs. Rate limiting is off so parallel tests do not throttle each other; short
provider timeouts (300 ms) keep the fault-injection suite fast — the timeout *behaviour*
under test is identical at production values.

## preprod

**Mirrors production as closely as practical**, and it is where the readiness evidence
that matters is produced:

- production-like auth (real IdP, JWKS, asymmetric keys)
- production-like network controls
- production-like observability with a real OTLP endpoint
- **shared** session, workflow and rate-limit stores
- **tamper-evident** audit sink
- InsureMO sandbox, not mocks
- synthetic or masked data only, unless separately approved

Required activities before promotion:

- integration certification against the InsureMO sandbox
- load and performance tests at expected peak
- security testing including a penetration test
- release-candidate eval run and a re-baselined benchmark
- DR failover test

If preprod must temporarily run mocks during integration work, that is an **explicit,
recorded exception** in `PROJECT_READINESS.md`, not a default.

## prod

Hardened. Mocks and the test model are refused. Debug endpoints and API docs are off.
Secrets come from the secret manager. CORS origins are explicit. Rate limiting is
mandatory. Audit is fail-closed to a tamper-evident store.

---

## Promotion

```text
local ──▶ dev ──▶ test (CI) ──▶ preprod ──▶ prod
                     │              │
                     │              └─ integration cert, load, pentest, DR, evals
                     └─ lint, format, types, tests, native evals, ragas, deepeval
```

Promotion gate at every step:

```bash
python scripts/run_evals.py all
```

Non-zero exit blocks promotion. A critical regression is a block, not a warning.

---

## Configuration precedence

1. process environment (what the platform injects)
2. `.env` file (local only)
3. defaults in `app/core/config/settings.py`

101 settings, all documented in `.env.example`, all validated at startup. A test
asserts the documentation stays complete, so an undocumented variable cannot be added.

---

## Data policy

| Environment | Permitted data |
|---|---|
| local, dev, test | synthetic only — the fixtures in `app/integrations/mock/fixtures.py` |
| preprod | synthetic or masked; real data requires separate approval |
| prod | real, subject to the privacy controls |

No fixture contains real personal data, and a test asserts no credential-shaped string
in application code is real.

---

## Verify an environment before you trust it

```bash
curl -s $BASE/api/v1/health/ready | python -m json.tool
python -c "from app.core.config.settings import get_settings; s=get_settings(); \
  print(s.app_env, s.model_provider, s.configured_providers())"
```

For preprod and prod, confirm: no provider reads `mock`, `model_provider` is not
`deterministic`, `jwt_jwks_uri` is set, no `HS*` algorithm is allowed, and rate
limiting is on. If any of those is wrong the application will not have started — but
check anyway.
