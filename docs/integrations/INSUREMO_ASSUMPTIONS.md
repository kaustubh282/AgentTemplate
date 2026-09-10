# InsureMO integration assumptions

> **REQUIRES_VERIFICATION — read this first.**
>
> **No InsureMO specification was supplied to this build.** Nothing in this document
> describes a real InsureMO endpoint, field, payload or error body. Every statement
> below is either (a) something the *template* requires of any provider, or (b) an
> explicitly labelled assumption awaiting confirmation.
>
> No InsureMO API has been invented. Where a mapping is unknown, the adapter raises
> `InsureMoContractNotConfigured("REQUIRES_VERIFICATION:...")` rather than sending a
> guessed shape.

---

## 1. What is actually implemented

| Component | Status | File |
|---|---|---|
| HTTP transport (timeout, headers, correlation propagation) | **implemented** | `app/integrations/insuremo/client.py` |
| Status-code to error-taxonomy mapping | **implemented** | same |
| Non-JSON and unexpected-shape rejection | **implemented** | same |
| Payload-leakage prevention in errors | **implemented** | same |
| Idempotency-key header propagation | **implemented** | same |
| Nine provider adapters implementing the same Protocols as the mocks | **implemented** | `app/integrations/insuremo/providers.py` |
| Configuration-driven selection | **implemented** | `app/integrations/factory.py` |
| **Endpoint paths** | **absent — raises** | `InsureMoMapping.endpoint()` |
| **Field mappings** | **absent — raises** | `InsureMoMapping.map_response()` |
| **Ownership lookups** | **absent — raises** | `get_*_owner()` |
| **Tenant lookup** (the SoR tenant of a customer, used by the cross-tenant check) | **absent — raises** | `get_customer_tenant()` |

So the *shape* of the integration is done and tested; the *contract* is not, because it
was not supplied.

---

## 2. What the template requires of any provider

These are **our** requirements, not assumptions about InsureMO. Any implementation must
satisfy them, and the contract suite enforces them.

1. **Same interface as the mock.** Identical method names, signatures and return
   annotations. Enforced by `tests/contract/test_provider_contracts.py`.
2. **Trusted context, not caller-asserted identity.** Every method takes a
   `RequestContext`; the customer scope comes from the validated token.
3. **Platform error taxonomy only.** Never a raw upstream exception or payload.
4. **Idempotency for writes.** A retryable write must accept an idempotency key and
   return the original result on replay.
5. **Ownership resolution.** The provider must be able to answer "who owns this
   resource id?" without the caller asserting it — this is what the resource-scope
   layer depends on.
6. **No raw payload into model context.** The adapter returns a Domain DTO; the
   AI-facing projection happens above it.
7. **Bounded latency.** A configured timeout must be honoured, and a timeout must
   surface as `UPSTREAM_TIMEOUT`, never as a success or an empty result.

---

## 3. Assumptions awaiting confirmation

Each is labelled with what breaks if it is wrong.

| # | Assumption | If wrong |
|---|---|---|
| A-01 | InsureMO is reachable over HTTPS with JSON request and response bodies | The transport layer needs replacing (e.g. SOAP), though the Protocols stay |
| A-02 | Authentication is a bearer token or API key in a header | `_headers()` changes; nothing above it changes |
| A-03 | A tenant identifier may be required | `INSUREMO_TENANT` is already threaded through |
| A-04 | An `Idempotency-Key` header (or equivalent) is honoured for writes | Duplicate-submit safety must move to a different mechanism; the retry policy already refuses to retry writes without a key |
| A-05 | HTTP status codes carry the error class | The status mapping must move to reading an error body |
| A-06 | List responses are paginated under a container key | `list_*` methods need a pagination loop |
| A-07 | Quote, payment and issuance are separate calls | The motor service actions need restructuring; the workflow states may change |
| A-08 | Premium is computed upstream, not by us | If we must supply rating inputs, the domain validators expand |
| A-09 | Ownership can be resolved per resource id | **This one matters most.** If ownership cannot be resolved independently, the resource-scope design needs a different strategy — see below |
| A-10 | Policy issuance is idempotent upstream | Issuance is currently `IRREVERSIBLE` and never retried, which is safe either way |
| A-11 | Correlation headers are accepted and echoed | Only trace continuity is affected |
| A-12 | Response times fit within a 4 s default timeout | `PROVIDER_TIMEOUT_MS` is configurable |

### Why A-09 matters most

The resource-scope layer settles the caller's authority and then asks the provider
*who owns this id*, comparing the two. If InsureMO cannot answer that question
independently of the caller, one of these is needed instead:

* a local ownership index maintained from InsureMO events, or
* an InsureMO call that is itself scoped to the customer (list-then-filter), or
* an upstream authorization assertion we can verify.

Guessing here would be a security decision disguised as an integration detail, so the
adapter raises `InsureMoContractNotConfigured("ownership_lookup:...")` instead.

---

## 4. What must be supplied to complete the integration

For each of the nine providers:

1. Endpoint path and HTTP method
2. Request body shape and required fields
3. Response body shape
4. Error body shape and error-code semantics
5. Pagination mechanism for list operations
6. Idempotency semantics for writes
7. Field-level mapping to our Domain DTOs
8. Ownership-resolution mechanism
9. Rate limits and quotas
10. Sandbox environment and test credentials

Record all of it in
[`INSUREMO_MAPPING_TEMPLATE.md`](INSUREMO_MAPPING_TEMPLATE.md).

---

## 5. How the switch will work

No application code changes:

```bash
CUSTOMER_PROVIDER=insuremo
POLICY_PROVIDER=insuremo
QUOTE_PROVIDER=insuremo
CLAIMS_PROVIDER=insuremo
PAYMENT_PROVIDER=insuremo
INSUREMO_BASE_URL=https://<supplied>
INSUREMO_API_KEY=<from the secret manager>
INSUREMO_TENANT=<supplied>
```

Proven today by
`tests/contract/test_provider_contracts.py::test_provider_selection_is_configuration_driven`,
which builds both bundles and asserts each satisfies the same runtime-checkable
Protocols.

---

## 6. Certification checklist

Before InsureMO is used in preprod:

- [ ] mapping template completed and reviewed
- [ ] `InsureMoMapping` populated
- [ ] contract suite passing against a sandbox
- [ ] ownership resolution implemented and its cross-customer denial tested
- [ ] idempotency verified with a real duplicate submission
- [ ] timeout, 4xx, 5xx and malformed-response behaviour verified against sandbox
- [ ] no raw upstream payload appears in logs, errors or model context
- [ ] latency measured and the SLOs re-checked against real numbers
- [ ] cost per transaction re-measured
- [ ] `PROJECT_READINESS.md` integration score updated with real evidence

---

## 7. Deliberate non-decisions

The template does **not** decide: which InsureMO product configuration to use, how
quote versioning works upstream, whether endorsements are in scope, how documents are
retrieved, or how reconciliation happens. Those are product and integration decisions,
recorded in
[`REGULATORY_OPEN_ITEMS.md`](../compliance/REGULATORY_OPEN_ITEMS.md) as OI-11 to OI-15.
