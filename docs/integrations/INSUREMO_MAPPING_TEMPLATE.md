# InsureMO mapping template

Complete this document from the **approved InsureMO specification**, then populate
`InsureMoMapping` in `app/integrations/insuremo/providers.py`.

Leave a cell as `REQUIRES_VERIFICATION` rather than guessing. An unpopulated mapping
fails loudly at runtime, which is the intended behaviour — a guessed field is far worse
than a clear error.

---

## 0. Connection

| Setting | Value | Source |
|---|---|---|
| Base URL (sandbox) | `REQUIRES_VERIFICATION` | |
| Base URL (production) | `REQUIRES_VERIFICATION` | |
| Auth scheme | `REQUIRES_VERIFICATION` | bearer / API key / OAuth2 client credentials? |
| Auth header name | `REQUIRES_VERIFICATION` | |
| Token endpoint and lifetime (if OAuth2) | `REQUIRES_VERIFICATION` | |
| Tenant header name | `REQUIRES_VERIFICATION` | |
| Idempotency header name | `REQUIRES_VERIFICATION` | |
| Correlation header name(s) | `REQUIRES_VERIFICATION` | |
| Content type | `REQUIRES_VERIFICATION` | |
| Rate limits / quotas | `REQUIRES_VERIFICATION` | |
| Expected p95 latency | `REQUIRES_VERIFICATION` | drives `PROVIDER_TIMEOUT_MS` |

---

## 1. Endpoints

Populate `InsureMoMapping.endpoints`. Use `{placeholder}` for path parameters.

| Operation | Method | Path | Notes |
|---|---|---|---|
| `get_customer` | | `REQUIRES_VERIFICATION` | e.g. `/customers/{customer_id}` |
| `find_customer_by_mobile` | | `REQUIRES_VERIFICATION` | |
| `list_policies` | | `REQUIRES_VERIFICATION` | pagination? |
| `get_policy` | | `REQUIRES_VERIFICATION` | |
| `create_quote` | | `REQUIRES_VERIFICATION` | idempotent? |
| `get_quote` | | `REQUIRES_VERIFICATION` | |
| `list_products` | | `REQUIRES_VERIFICATION` | |
| `get_product` | | `REQUIRES_VERIFICATION` | |
| `list_claims` | | `REQUIRES_VERIFICATION` | |
| `get_claim` | | `REQUIRES_VERIFICATION` | |
| `initiate_payment` | | `REQUIRES_VERIFICATION` | idempotent? |
| `get_payment` | | `REQUIRES_VERIFICATION` | |
| `issue_policy` | | `REQUIRES_VERIFICATION` | **irreversible** — never auto-retried |
| `list_documents` | | `REQUIRES_VERIFICATION` | |
| `list_reference` | | `REQUIRES_VERIFICATION` | |

---

## 2. Field mappings

Populate `InsureMoMapping.field_map` as `{our_field: their_field}`.

### 2.1 Policy → `app.integrations.contracts.dtos.Policy`

| Our field | Type | Their field | Transform | Required |
|---|---|---|---|---|
| `policy_id` | str | `REQUIRES_VERIFICATION` | | yes |
| `policy_number` | str | `REQUIRES_VERIFICATION` | | yes |
| `customer_id` | str | `REQUIRES_VERIFICATION` | | yes |
| `product` | str | `REQUIRES_VERIFICATION` | | yes |
| `domain` | str | `REQUIRES_VERIFICATION` | may need deriving from product | yes |
| `status` | enum | `REQUIRES_VERIFICATION` | map to ACTIVE/LAPSED/EXPIRED/CANCELLED/PENDING | yes |
| `annual_premium.amount` | float | `REQUIRES_VERIFICATION` | minor units? | yes |
| `annual_premium.currency` | str | `REQUIRES_VERIFICATION` | | yes |
| `addons` | list[str] | `REQUIRES_VERIFICATION` | | no |
| `inception_date` | date | `REQUIRES_VERIFICATION` | date format? | no |
| `renewal_due` | date | `REQUIRES_VERIFICATION` | | no |
| `sum_insured` | Money | `REQUIRES_VERIFICATION` | | no |
| `source_system` | str | set to `"INSUREMO"` | constant | yes |

**Status value mapping**

| Their value | Our `PolicyStatus` |
|---|---|
| `REQUIRES_VERIFICATION` | `ACTIVE` |
| `REQUIRES_VERIFICATION` | `LAPSED` |
| `REQUIRES_VERIFICATION` | `EXPIRED` |
| `REQUIRES_VERIFICATION` | `CANCELLED` |
| `REQUIRES_VERIFICATION` | `PENDING` |

An unmapped upstream status must raise `UPSTREAM_INVALID_RESPONSE`, **not** default to
`ACTIVE`.

### 2.2 Quote → `Quote`

| Our field | Their field | Notes |
|---|---|---|
| `quote_id` | `REQUIRES_VERIFICATION` | |
| `quote_reference` | `REQUIRES_VERIFICATION` | customer-visible? |
| `customer_id` | `REQUIRES_VERIFICATION` | |
| `product` | `REQUIRES_VERIFICATION` | |
| `lines[].label` | `REQUIRES_VERIFICATION` | must be presentable to a customer |
| `lines[].amount` | `REQUIRES_VERIFICATION` | |
| `total` | `REQUIRES_VERIFICATION` | is tax included? |
| `valid_until` | `REQUIRES_VERIFICATION` | |

**Create-quote request** — which of these does InsureMO require?

| Our input | Their field | Required |
|---|---|---|
| `customer_id` | `REQUIRES_VERIFICATION` | |
| `product_code` | `REQUIRES_VERIFICATION` | |
| `attributes.idv` | `REQUIRES_VERIFICATION` | |
| `attributes.manufacture_year` | `REQUIRES_VERIFICATION` | |
| `attributes.fuel_type` | `REQUIRES_VERIFICATION` | value mapping? |
| `attributes.region` (travel) | `REQUIRES_VERIFICATION` | |
| `attributes.trip_days` (travel) | `REQUIRES_VERIFICATION` | |
| `addons` | `REQUIRES_VERIFICATION` | codes or names? |

### 2.3 Claim → `Claim`

| Our field | Their field |
|---|---|
| `claim_id` | `REQUIRES_VERIFICATION` |
| `claim_number` | `REQUIRES_VERIFICATION` |
| `policy_id` | `REQUIRES_VERIFICATION` |
| `customer_id` | `REQUIRES_VERIFICATION` |
| `status` | `REQUIRES_VERIFICATION` (map to REGISTERED/UNDER_REVIEW/APPROVED/REJECTED/SETTLED) |
| `registered_on` | `REQUIRES_VERIFICATION` |
| `last_updated_on` | `REQUIRES_VERIFICATION` |
| `estimated_amount` | `REQUIRES_VERIFICATION` |

### 2.4 Payment → `PaymentRecord` and `InitiatePaymentResponse`

| Our field | Their field | Notes |
|---|---|---|
| `payment_id` | `REQUIRES_VERIFICATION` | |
| `payment_reference` | `REQUIRES_VERIFICATION` | |
| `status` | `REQUIRES_VERIFICATION` | map to INITIATED/PENDING/SUCCESS/FAILED/REFUNDED |
| `amount` | `REQUIRES_VERIFICATION` | |
| `handoff_token` | `REQUIRES_VERIFICATION` | **must not be a card number or credential** |
| `expires_in_seconds` | `REQUIRES_VERIFICATION` | |

### 2.5 Customer → `Customer`

| Our field | Their field | Classification |
|---|---|---|
| `customer_id` | `REQUIRES_VERIFICATION` | `PII` |
| `full_name` | `REQUIRES_VERIFICATION` | `PII` |
| `mobile` | `REQUIRES_VERIFICATION` | `PII` |
| `email` | `REQUIRES_VERIFICATION` | `PII` |
| `date_of_birth` | `REQUIRES_VERIFICATION` | `PII` |
| `pincode` | `REQUIRES_VERIFICATION` | `PII` |

Any additional upstream field must be classified before it is mapped. **Do not map a
field the application does not need** — see `PII_HANDLING.md`.

---

## 3. Error mapping

| Upstream signal | Our error | Retryable | Notes |
|---|---|---|---|
| 400 / 422 | `VALIDATION_ERROR` | no | |
| 401 | `UPSTREAM_UNAVAILABLE` | yes | our credential problem, not the user's |
| 403 | `FORBIDDEN` | no | |
| 404 | `RESOURCE_NOT_FOUND` | no | |
| 409 | `IDEMPOTENCY_CONFLICT` | no | |
| 429 | `RATE_LIMITED` | yes | is there a `Retry-After`? |
| 5xx | `UPSTREAM_UNAVAILABLE` | yes | |
| timeout | `UPSTREAM_TIMEOUT` | yes | never a success |
| non-JSON / unexpected shape | `UPSTREAM_INVALID_RESPONSE` | no | |
| business rejection in a 200 body | `REQUIRES_VERIFICATION` | | does InsureMO return failures inside a 200? |

**Error body shape:** `REQUIRES_VERIFICATION`. No part of it may be echoed to a user or
placed in model context.

---

## 4. Ownership resolution

The security-critical gap. Complete one row:

| Strategy | Feasible? | Detail |
|---|---|---|
| Direct: an endpoint returns the owner of a resource id | `REQUIRES_VERIFICATION` | |
| Scoped read: the read is itself customer-scoped upstream | `REQUIRES_VERIFICATION` | |
| Local index maintained from InsureMO events | `REQUIRES_VERIFICATION` | needs an event feed |
| Upstream authorization assertion we can verify | `REQUIRES_VERIFICATION` | |

Until one is chosen, `get_*_owner()` raises deliberately. Do **not** substitute
"trust the caller-supplied customer id" — that would remove the cross-customer control.

---

## 5. Idempotency

| Operation | Idempotent upstream? | Mechanism | Replay behaviour |
|---|---|---|---|
| `create_quote` | `REQUIRES_VERIFICATION` | | |
| `initiate_payment` | `REQUIRES_VERIFICATION` | | |
| `issue_policy` | `REQUIRES_VERIFICATION` | | our policy: never auto-retried regardless |

---

## 6. Sign-off

| Role | Name | Date | Confirms |
|---|---|---|---|
| Integration lead | | | endpoints and payloads match the specification |
| InsureMO counterpart | | | the specification is current and correct |
| InfoSec | | | auth, ownership and data handling are acceptable |
| Compliance | | | data fields and their retention are acceptable |
| Actuarial / Product | | | premium and appetite semantics are correct |

Once signed off, run:

```bash
python -m pytest tests/contract -q          # against the sandbox
python scripts/benchmark.py                 # re-measure latency and cost
python scripts/run_evals.py all
```

and update the InsureMO replaceability score in `PROJECT_READINESS.md` with real
evidence rather than mock evidence.
