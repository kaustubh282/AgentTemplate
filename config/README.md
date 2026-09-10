# Environment configuration samples

Per-environment overlays. These carry **no secrets** — secret values come from the
platform secret manager and are injected as environment variables at runtime.

`.env.example` in the repository root is the authoritative reference for every
variable; a test asserts it stays complete. The files here are the deltas that a
deployment applies on top of the defaults.

| File | Purpose |
|---|---|
| `local.env` | fully offline: mocks, offline model, verbose logs |
| `dev.env` | shared integration; mocks still permitted |
| `test.env` | what CI uses; rate limiting off, short dependency budgets |
| `preprod.env` | mirrors production; real IdP, shared stores, InsureMO sandbox |
| `prod.env` | hardened; the application refuses to start if anything here is unsafe |

See [`docs/operations/ENVIRONMENTS.md`](../docs/operations/ENVIRONMENTS.md) for the
full matrix and the promotion path.
