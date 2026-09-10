# MASTER TEMPLATE ACCEPTANCE REPORT

**Subject:** `protec-insurance-ai-template` 0.1.0 (FastAPI + Strands `Model` adapter, deterministic workflow engine, RAG FAQ, Harness control plane)
**Audit date:** 2026-09-08
**Auditor role:** independent Verification & Validation. No application code was modified. The only files written are this report and the project's own gitignored `evals/reports/*.json`, which the project's eval commands regenerate when run.
**Question answered:** can every future Customer Bot, Agent Bot, WhatsApp Bot, Voice Bot and insurance workflow safely build on this architecture today?

## FINAL DECISION: `MASTER_TEMPLATE_NEEDS_REMEDIATION`

No Critical finding. Nine High findings, of which four are architectural (transaction integrity under concurrency, grounding certification, cross-instance identity, absence of a channel abstraction). The approval conditions are not all met:

| Approval condition | Result |
|---|---|
| No Critical findings | Met |
| No High architectural flaws | **Not met** (H-1, H-2, H-3, H-6, H-8) |
| Deterministic workflow proven | Met for single requests; **not met under concurrency** (H-1) |
| Ownership proven | Met (authenticated); weak for anonymous (M-6) |
| AI boundaries proven | Met (Harness is the only AI entry; deterministic paths reach no model) |
| Harness enforcement proven | Met for 9 of 11 controls; grounding and audit-on-exception are PARTIAL |
| Authoritative data proven | Met (premium, add-ons, claims, payments, policy all provider-sourced) |
| Tests provide convincing evidence | **Partially** (7 of 9 PEP deny branches never execute; rate limiting disabled in every fixture; hallucination gate passes fabricated negations) |

The template is a strong foundation with genuinely enforced controls in the places that matter most (budgets, tool allow-lists, ownership, confirmation tokens, PII masking). It is not yet safe to clone as the master for financial flows and multi-channel bots until the High findings are fixed and re-verified.

---

## 1. Executive summary

**What was executed.** All build gates were run independently: `ruff format --check` (236 files clean), `ruff check` (clean), `mypy` (140 files, no issues), `pytest` (655 passed in 13 s, 90% line coverage), native evals (128/128), Ragas runner (PASS, 7 judge metrics NOT_EVIDENCED), DeepEval runner (PASS, 8 judge metrics NOT_EVIDENCED), benchmark (14/14 gates), load test mixed profile (535 requests, 100% success), recovery drill (9/9), OpenAPI generation (17 paths, 20 schemas), a real `uvicorn` start with live/ready/chat/openapi calls, `pip-audit` (3 known vulnerabilities, all in eval-only extras), and `pip check` (clean). On top of the project's own suite, 7 probe scripts (about 170 individual checks) were written and executed against the HTTP boundary and the orchestrator: authentication, authorization, workflow bypass, concurrency races, Harness control bypass, prompt injection variants, grounding manipulation, performance and memory.

**What is proven.** JWT validation rejects every forged, expired, unsigned, wrong-issuer, wrong-audience, refresh-token and alg=none attempt. Cross-customer, cross-tenant and unassigned-agent reads are denied identically to missing resources. Conversation and flow ownership gates hold for authenticated subjects. Every illegal workflow transition is refused with a conflict; confirmation tokens bound to flow, action and state version cannot be forged, reused across flows, or replayed after the state moves. Model calls outside a governed budget ledger are refused structurally; a greedy handler is stopped after exactly one call and telemetry reports exactly one. Undeclared tool requests, schema-violating outputs, prohibited claims, secret leaks and PII in output are blocked or sanitised centrally. Premium, add-ons, claims, payments and policy values come only from providers; provider timeout and malformed responses return controlled unavailability, never invented data. Deterministic paths create zero model calls and zero Harness records. Reported `modelCalls` equals the real model invocation count in every case tested.

**What is not proven or is broken.**
1. Two concurrent `CONFIRM_PURCHASE` submits with different idempotency keys initiate two payments at the provider while only one commits to state. Three submits initiate three. The side effect runs before the optimistic-lock commit and nothing reserves the transition.
2. `COMPLETE_PAYMENT` issues the policy while the payment provider still reports `INITIATED`. Payment success is asserted by the client action, not verified against the provider.
3. Grounding is a lexical-overlap heuristic. A negation flip ("a deductible is never paid by the policyholder") and a competitor entity swap pass as `VERIFIED` and `PARTIALLY_VERIFIED`. A non-answer ("I do not have enough information.") is labelled `VERIFIED` with a citation. The project's "hallucination rate 0.000" measures substring absence, not truth.
4. `subject_ref` and `conversation_ref` derive from Python's per-process `hash()`. Three processes produced three different pseudonyms for the same customer. Audit `actor_ref` cannot be joined across instances or restarts, and per-subject rate-limit keys in Redis are per-instance.
5. A blocked injection message is stored verbatim in conversation history and replayed into the next model prompt as `RECENT_TURNS`. The injection detector inspects only the current message.
6. There is no channel abstraction. `Channel` is a five-value enum used for allow-lists; nothing in `app/`, `tests/` or `docs/` mentions WhatsApp, voice, SMS or telephony. UI directives assume a rich web client.
7. Knowledge governance has no version or effective-date selection. Two ACTIVE versions of one document are both served; a future-dated document is served today. The grounding policy grades answers against retriever chunks, including chunks the context builder dropped, so an answer can be certified against evidence the model never received.
8. Rate limiting on the AI path returns HTTP 200 with a chat message rather than 429 with `Retry-After`. OpenTelemetry tracing is a no-op despite documentation claiming spans are exported. Preprod accepts every unsafe setting production refuses.

**Documentation drift.** README claims 553 tests and 133 mypy files; actual is 655 and 140. `PROJECT_READINESS.md` scores itself 9.0 overall; this audit lands at 6.3. The gap is mostly in areas the project's own tests cannot see: concurrency, cross-process identity, semantic grounding, and multi-channel readiness.

---

## 2. Top 25 strengths

1. Single AI entry point. `HarnessService.execute` is the only path to a model; a deterministic capability inside it is structurally refused (`BLOCK_DETERMINISTIC_CAPABILITY_IN_HARNESS`).
2. Budget enforcement is structural, not cooperative: `ModelInvoker` reads a `ContextVar` ledger and refuses ungoverned calls (`BLOCK_UNGOVERNED_MODEL_CALL`). Verified with a rogue handler.
3. Telemetry truthfulness: `meta.modelCalls` equalled real model invocations in every probe, including blocked, cached and abstained paths.
4. Confirmation tokens are HMAC-bound to flow id, action and state version; garbage, cross-flow and stale tokens were all refused with 403.
5. Idempotency keys are scoped to action and payload hash; same key for a different action yields `IDEMPOTENCY_CONFLICT`, not a silent no-op.
6. Illegal transitions from every probed state return `FLOW_STATE_CONFLICT`; skipping steps is impossible in a single request.
7. Ownership gate on conversations and flows; another subject presenting a conversation id is refused and audited, whether or not the conversation exists.
8. Authorization denials are indistinguishable from missing resources (same status, same message body).
9. Agent authority requires explicit assignment; the `AGENT` role alone grants nothing.
10. Tenant is taken from the System of Record, not the caller, so the tenant check compares caller with resource.
11. JWT boundary: alg allow-list with `none` refused at configuration and at validation, required claims, `token_use` check, unknown roles dropped, indeterminate actor type refused.
12. Authoritative data discipline: premium, add-ons, claims, payments and policy come from typed tools that enforce scope before any provider call, and return `available=false` on timeout or malformed responses.
13. Provider failure mid-workflow leaves state uncommitted and offers a retry directive; no fabricated quote.
14. PII masking before model context was verified: Aadhaar, PAN and mobile numbers never reached the model.
15. Injection detection blocked 8 of 8 canonical attacks and the fullwidth variant with zero model calls; homoglyph and zero-width variants reached the model but produced only a clarification.
16. Output guardrails block prohibited regulatory claims, `<script>` markup and JWT-shaped secrets; PII in output is masked.
17. FAQ interruption mid-flow leaves state unchanged and offers `CONTINUE`; resume replays persisted state with zero model calls.
18. Abstention costs zero model calls when evidence is weak or absent; revoking a document via the admin API makes the next answer abstain.
19. Redis adapters use `WATCH/MULTI` compare-and-set, `INCR+EXPIRE` pipelines and TTLs; store outage fails closed with 503 and the rate limiter denies rather than opens.
20. Hash-chained audit sink with a verifier that detects edited, deleted, reordered and re-keyed records.
21. Production configuration refuses 17 unsafe combinations including mock providers, deterministic model, symmetric JWT, in-memory stores, fakeredis, missing secrets and non-chained audit.
22. Clean layering: `core` imports nothing outside `core`; no runtime import cycles across 136 modules; every store pair shares a Protocol.
23. Ops pack (5 Grafana dashboards, 17 Prometheus alerts) is drift-tested against the metric catalogue; the Prometheus endpoint is permission-gated.
24. Recovery drill and multi-instance tests exercise failover, concurrent writers and duplicate-after-failover with an in-process Redis emulator.
25. Honest labelling of what is not evidenced: judge metrics, InsureMO adapters that raise `InsureMoContractNotConfigured`, and placeholder pricing are all marked `REQUIRES_VERIFICATION`.

## 3. Top 25 weaknesses

1. Side effects execute before state commit; concurrent high-risk submits multiply provider calls (H-1).
2. Policy issuance does not verify payment success (H-2).
3. Grounding is lexical; negation and entity substitution pass as verified (H-3).
4. Per-process `hash()` pseudonyms break audit joinability and cross-instance rate limiting (H-4).
5. Blocked injection text is replayed into later model prompts via history (H-5).
6. No channel abstraction for WhatsApp, voice or SMS bots (H-6).
7. No knowledge version or effective-date selection; future-dated documents are served (H-7).
8. Grounding certifies against evidence the model never received when a chunk exceeds the retrieval budget (H-8).
9. Conflict detection is metadata-only; same-version contradictions are merged into one verified answer (H-9).
10. AI-path rate limiting returns 200 rather than 429; no `Retry-After`; no `RATE_LIMITED` audit event (M-1).
11. OpenTelemetry is a no-op; no `TracerProvider`, exporter or sampler; documentation says spans are exported (M-2).
12. Preprod accepts mocks, deterministic model, in-memory stores, CORS wildcard and debug logging (M-3).
13. Harness emits no audit or execution record when a handler raises a non-application exception (M-4).
14. Explainability `DecisionRecord` is a log line, not an audit-sink record; grounding sources, citations and cost are not durable (M-5).
15. Anonymous callers share one owner identity; any anonymous client with a conversation id can append to and read another anonymous session's history (M-6).
16. Client-chosen conversation ids allow squatting and an existence oracle (M-7).
17. `execution_records`, `usage_log`, in-memory conversations and exact-value histograms grow without bound in a long-lived process (M-8).
18. Seven of nine PEP deny branches, the Harness tool-authorization path and all Redis error branches are never executed by tests; rate limiting is disabled in every fixture (M-9).
19. Strands is a `Model` interface only; no `strands.Agent` is constructed; OpenAI and Bedrock adapter branches have zero test coverage (M-10).
20. Malicious knowledge documents are not scanned or quarantined at ingestion; one poisoned document turns a topic into a permanent block (M-11).
21. Direct-read ownership denials by `ResourceScopeInterceptor` are not audited; `RESOURCE_SCOPE_DENIED` and five other audit actions have zero emission sites (M-12).
22. Audit chain tail truncation is undetectable; no anchoring or checkpoint (M-13).
23. Sixteen settings fields are read by nothing, including retention days, OTLP endpoint, sample rate and three feature flags; a compliance matrix cites one of them as a live control (M-14).
24. Ten empty scaffold packages; duplicated token estimator, XSS regex and guard blocks; `Any` typing at the container seam where Protocols exist (L-group).
25. README status table and readiness document carry stale or contradictory figures; the CI supply-chain job is an `echo` that exits 0 (L-group).

---

## 4. Findings

Severity scale: Critical = exploitable loss of money, data or control with no compensating control; High = architectural or security defect that a future bot would inherit and that a realistic scenario triggers; Medium = defect with a compensating control or limited blast radius; Low = hygiene.

### 4.1 Critical

None.

### 4.2 High

**H-1. Side effect before commit; concurrent high-risk submits multiply provider calls.**
Evidence: probe `probe_race.py` with 50 ms provider latency. Two `CONFIRM_PURCHASE` requests with distinct idempotency keys and the same valid confirmation token: provider `initiate_payment` calls = 2, new payment records = 2, `PAYMENT_INITIATED` audit events = 2, committed transitions = 1 (second fails `optimistic_lock_conflict`). Three keys: 3 payments, 1 commit. Same key twice: 1 payment (mock provider dedupes by key), 1 commit.
Location: `app/workflows/engine/service.py:94-141` (`precheck_action` then service action then `apply_action`); no reservation, lock or single-use token consumption between precheck and commit. The confirmation token is bound to state version but is not consumed, so all concurrent requests see version N and all pass.
Impact: in a real deployment the exposure depends entirely on the payment provider's own idempotency. The template documents "duplicate submits replay safely"; that holds only for identical keys.
Classification: architectural. Remediation: acquire a per-flow lock or increment the version (reserve) before the side effect, or persist a single-use nonce per issued confirmation token.

**H-2. Policy issued without payment verification.**
Evidence: `probe_workflow.py`. After `CONFIRM_PURCHASE`, `COMPLETE_PAYMENT` with the server-issued token immediately issued the policy; mock payment status at issue time was `INITIATED`. Nothing calls `mark_success` or queries payment status.
Location: `app/domains/motor/workflow.py` (`COMPLETE_PAYMENT` transition requires only `payment_reference` and `quote_id`); `app/domains/motor/services.py:issue_policy` does not read payment status.
Impact: the reference flow teaches every future bot that a client action can assert payment completion. Not rated Critical only because a real issuance system may independently verify payment; the template must not depend on that.

**H-3. Grounding is lexical and certifies fabricated answers.**
Evidence: `probe_grounding.py` forcing the model double's output for "What is a deductible?": negation flip ("never paid by the policyholder; the insurer pays") -> `ALLOW`, `VERIFIED`, cited; competitor entity swap ("ProTec waives every deductible") -> `ALLOW`, `PARTIALLY_VERIFIED`; earlier variant "always exactly 7 percent of the vehicle price by law" -> `ALLOW`. Non-answer "I do not have enough information." -> `VERIFIED` with a glossary citation (also observed on the live `uvicorn` server with the default responder).
Location: `app/rag/retrieval/grounding.py:171-217` (term-overlap >= 0.55 per factual sentence; sentences without factual markers are treated as grounded).
Impact: the "hallucination rate 0.000" gate in `PROJECT_READINESS.md` measures forbidden-substring absence. Faithfulness is `NOT_EVIDENCED` and the judge path in the Ragas runner is dead code (`_run_judge_metrics` returns placeholders unconditionally).

**H-4. Per-process pseudonyms break audit joinability and shared rate limiting.**
Evidence: three separate Python processes evaluating `AuthContext(subject_id='CUST-1001').subject_ref` returned `sub_400588118743`, `sub_641761075131`, `sub_670615655114`.
Location: `app/core/auth/auth_context.py:121` and `app/core/context/request_context.py:81` use `abs(hash(...))`. `actor_ref` in every audit event and `TransitionRecord`, and the `sub:`/`conv:` rate-limit dimensions in Redis, all derive from it. `Dockerfile` does not set `PYTHONHASHSEED`.
Impact: an auditor cannot correlate one customer's events across instances or restarts; the Redis-backed per-subject budget is effectively per-instance. The multi-instance tests pass because both "instances" share one process.

**H-5. Blocked injection text is replayed into later model prompts.**
Evidence: `probe_race.py`. Turn 1: injection with marker -> `BLOCK`. Turn 2: ambiguous message -> intent extraction. The model prompt for turn 2 contained the turn-1 injection text under `RECENT_TURNS`. Conversation history retained the blocked message verbatim.
Location: `app/orchestration/conversation.py:_record_turns` stores every user message regardless of outcome; `app/ai/harness/context/builder.py:130-146` sanitises history for PII only; `GuardrailService.check_input` is applied to the current message only.
Impact: the injection guardrail is bypassable by splitting an attack across turns. Downstream controls (structured output, allow-lists, output scan) still bound the effect, which is why this is High rather than Critical.

**H-6. No channel abstraction for the bots the template is meant to spawn.**
Evidence: `grep -rniE "whatsapp|voice|telephony|sms" app/ tests/ docs/` returns nothing. `Channel` is a 5-value enum (`WEB_CUSTOMER`, `WEB_AGENT`, `MOBILE`, `PUBLIC_WEB`, `INTERNAL`) derived from actor type and one header in `app/api/deps.py:62-72`. Directive payloads (`ShowFaqAnswerPayload`, `ShowRetryPayload`, mandatory ARIA fields) assume a rich web client. `DomainBinding.directive_builder` is typed `Any`; there is no directive renderer interface.
Impact: adding WhatsApp or voice requires a new inbound adapter, a channel-specific renderer, and changes to `_channel_for` and to how directives are consumed. Domain extensibility (motor -> travel) is real; channel extensibility is not designed.

**H-7. Knowledge version and effective-date selection are not implemented.**
Evidence (RAG sub-audit scripts): two ACTIVE versions of one document with distinct ids are both served; a v3 with `effective_date=2099-01-01` is served today; same `document_id` in two files resolves by file sort order. `effective_date` is not referenced by any retrieval filter. Lifecycle changes are in-memory only and are reverted by re-ingestion. `status` defaults to `ACTIVE` and `approved_by` is optional, so an unapproved document is served.
Location: `app/rag/governance/documents.py:63,107-134`, `app/rag/retrieval/retriever.py:315-333`, `app/rag/ingestion/loader.py:277-290`.

**H-8. Grounding certifies against evidence the model never received.**
Evidence: a single paragraph larger than `max_retrieval_tokens` is admitted by the retriever's budget (`_apply_budget` admits the first chunk unconditionally) but dropped by the context builder; the model prompt's evidence block was empty; the Harness still returned `ALLOW`, `VERIFIED`, with a citation, because `GroundingPolicy` assesses `agent_ctx.retrieval.chunks` rather than `built.evidence_document_ids`.
Location: `app/rag/retrieval/retriever.py:430`, `app/ai/harness/context/builder.py:120-127`, `app/ai/harness/policies/grounding_policy.py:enforce`.

**H-9. Conflict detection is metadata-only.**
Evidence: two contradictory ACTIVE documents with the same version string ("30 days" vs "90 days") were both passed to the model and the merged answer was returned `VERIFIED` citing both. Two agreeing documents with different version strings triggered a false conflict abstention.
Location: `app/rag/retrieval/retriever.py:463-476` (`_is_conflict` compares version strings, score proximity and scope; never content).

### 4.3 Medium

**M-1. AI-path rate limiting yields HTTP 200.** With `RATE_LIMIT_AI_PER_MINUTE=3`, requests 4-6 returned 200 with `meta.reasonCode=BLOCK_RATE_LIMIT`, no `Retry-After`. Deterministic routes correctly return 429 with `Retry-After: 60`. `AuditAction.RATE_LIMITED` has zero emission sites. Location: `app/ai/harness/service.py:194-195`.

**M-2. Tracing is a no-op.** With `OTEL_ENABLED=true` the tracer is a `ProxyTracer` and spans are `NonRecordingSpan`; no `TracerProvider`, exporter or sampler exists in `app/`; `otel_exporter_otlp_endpoint` and `trace_sample_rate` are read by nothing; `opentelemetry` is not a declared dependency. `docs/operations/OBSERVABILITY.md:89` states spans are emitted.

**M-3. Preprod is not separated.** A preprod `Settings` with mock providers, deterministic model, in-memory stores, `DEBUG_ENDPOINTS_ENABLED`, CORS `*`, `LOG_LEVEL=DEBUG`, `RATE_LIMIT_ENABLED=false` and HS256 is accepted. Production also accepts `mock` for product, document and reference-data providers, plaintext `redis://`, one-character secrets and `AUDIT_FAIL_CLOSED=false`.

**M-4. No audit or execution record when a handler raises a non-`AppError`.** A `RuntimeError` in a handler produced a safe 500 but `AI_EXECUTION_COMPLETED` delta = 0 and `execution_records` delta = 0. Location: `app/ai/harness/service.py:execute` (`except AppError: raise`; evidence emission is after the try block).

**M-5. Explainability is not durable.** `AuditService.record_decision` only logs. `evidence_document_ids`, citations and `estimated_cost` live in the in-process `execution_records` list and the log stream; they are not in the audit sink. `KNOWLEDGE_SOURCE_USED`, `ABSTAINED_INSUFFICIENT_EVIDENCE`, `MODEL_FALLBACK`, `USER_CONFIRMATION_RECORDED`, `CONFIG_DECISION_CONTEXT` and `RESOURCE_SCOPE_DENIED` are declared and never emitted.

**M-6. Anonymous callers share one owner.** `owner_subject_id="anonymous"` for every unauthenticated conversation; two anonymous requests with the same id both succeeded and the second saw the first's history in model context.

**M-7. Client-chosen conversation ids.** A conversation id supplied in the body or header is created on first use. Existing-other-owner returns 403, fresh returns 200 (existence oracle). An attacker can pre-create an id so the victim later receives 403 (squatting).

**M-8. Unbounded in-process growth.** 1000 new-conversation requests grew memory by 8.3 MiB; `execution_records` and `usage_log` have no cap; in-memory conversations are only expired on read; the metrics registry keeps exact values for summaries. `/admin/finops/report` reads the per-instance list, so multi-instance cost reports are incomplete.

**M-9. Test suite does not exercise the failing direction of several controls.** From the coverage run: PEP deny branches for environment, authentication, actor type, role, confirmation, idempotency and rate limit never execute; `HarnessService._authorize_tool` never executes; `BLOCK_PERMISSION_MISSING` is never raised in engine or PEP; `JwksKeyResolver` is 0% covered; all fixtures set `RATE_LIMIT_ENABLED=false`; `tests/security/test_adversarial.py:302` contains a literal tautology; the injection HTTP tests do not assert `outcome == BLOCK`.

**M-10. Strands runtime unexercised; provider adapters untested.** `strands.Agent` is never constructed; `ModelInvoker` calls `model.stream()` directly. The OpenAI and Bedrock branches in `app/ai/models/factory.py` have 0% coverage. Whether a real Strands model's stream event shapes match the parsing in `provider.py:450-466` is unverified. ADR 0003 documents this honestly.

**M-11. Malicious knowledge documents are not quarantined.** Ingestion performs no injection scan. Detection is per request in the FAQ agent, after the conflict gate, so a poisoned document makes every question on that topic a `BLOCK` indefinitely, and a differing version string can mask the attack as a conflict. A subtle instruction without trigger words reached the model wrapped as untrusted; the output scanner caught it because it happened to match a prohibited-claim pattern.

**M-12. Direct-read scope denials are not audited.** A customer reading another customer's policy received 403 but produced no audit event; only PEP-level and conversation-level denials are recorded.

**M-13. Audit chain tail truncation is undetectable.** Deleting the last three records left the verifier reporting "13 records verified, chain intact". Records carry no instance id; two pods appending to one file fork the chain. WORM storage is documentation only.

**M-14. Inert configuration.** Sixteen `Settings` fields are read by nothing: `rag_provider`, `vector_store_provider`, `rag_allow_draft_sources`, `conversation_retention_days`, `audit_retention_days`, `otel_exporter_otlp_endpoint`, `trace_sample_rate`, `telemetry_fail_open`, three feature flags, two SLO fields, plus `rag_timeout_ms` read only by tests. `docs/compliance/PRIVACY_CONTROL_MATRIX.md:15` cites `CONVERSATION_RETENTION_DAYS` as control PRV-04.

**M-15. Body-size middleware trusts `Content-Length`.** A 70 KB chunked body bypassed the 413 path and was rejected only by the Pydantic `max_length` on `message`. Other fields (`payload` dict) have no size bound.

**M-16. Prometheus `route` label uses the raw path.** `protec_requests_total{route="/api/v1/policies/POL-1"}` leaks resource ids into metrics and has unbounded cardinality.

### 4.4 Low

- L-1. README status table stale (553 tests / 133 files vs 655 / 140); `PROJECT_READINESS.md` says both 140 and 133; mypy is called "strict" but `strict = true` is not set.
- L-2. CI `supply-chain` job is `echo ... exit 0`; `SECURITY_CONTROL_MATRIX.md` LLM05 claims "pinned dependencies" but `pyproject.toml` uses `>=` ranges and there is no lock file; Docker base image is tag-pinned, not digest-pinned; no `HEALTHCHECK`.
- L-3. Ten empty scaffold packages (`app/ai/guardrails`, `app/ai/orchestration`, `app/ai/harness/execution`, `app/ai/harness/telemetry`, `app/common`, `app/rag/ranking`, `app/rag/stores`, `app/ui_directives/validators`, `app/workflows/definitions`, `app/workflows/registry`).
- L-4. Duplicated logic: token estimator in three places with two formulas; `_UNSAFE_MARKUP` regex in two places with different coverage; `precheck_action` and `apply_action` repeat 35 lines of guards; `_copy_ledger` and `_finalize_record` copy the same 14 fields.
- L-5. About 20 dead symbols including `require_roles`, `KnowledgeInsufficientError`, `ToolCatalog`, `otel_available`, `FLOW_ABANDONMENT_TOTAL` (referenced by a dashboard panel that will always be empty).
- L-6. `Container` fields `audit_sink`, `conversations`, `workflow_store` and `DomainBinding.directive_builder` are typed `Any` although Protocols exist; module-level singletons contradict the composition-root docstring.
- L-7. `app/api/routers/policies.py` imports the motor domain; `_routed_response` hard-codes a `motor` fallback; `_flow_message` hard-codes motor and travel state copy.
- L-8. Client-supplied `X-Correlation-Id` and `X-Request-Id` are honoured unbounded (5000 chars echoed).
- L-9. `X-Channel: MOBILE` header is believed for customers; unsalted 16-hex SHA-256 of IPv4 is reversible.
- L-10. Golden retrieval set is 10 of 11 verbatim section headings; all six retrieval configurations score identically, so the experiment matrix cannot discriminate.
- L-11. `pip-audit` reports 3 vulnerabilities (`diskcache`, `nltk`, `ragas`), all in the `evals` extra, none imported by `app/`.
- L-12. COMPLIANCE_REGISTER says audit integrity is a `GAP` while PROJECT_READINESS says the chain is shipped; the two documents disagree.

---

## 5. Architecture review

**End-to-end trace (verified by reading and by probes).** HTTP -> `CorrelationMiddleware` and `BodySizeLimitMiddleware` -> FastAPI dependency `protected_request_context` or `public_request_context` (JWT parsed here and nowhere else) -> router handler (thin) -> `ConversationOrchestrator` -> `_open_conversation` ownership gate -> `CapabilityRouter.route_message` (pure string rules, no model) -> either the deterministic branch (`WorkflowService.execute` -> `WorkflowEngine.precheck_action` -> bound service action -> provider through `ResiliencePolicy` -> `WorkflowEngine.apply_action` -> store CAS) or the AI branch (`HarnessService.execute` -> `PolicyEnforcementPoint.authorize` -> input guardrails -> budget ledger -> handler -> `ModelInvoker` -> Strands `Model.stream` -> output validation -> grounding policy -> audit) -> directive built through `DirectiveRegistry` -> `AssistantResponse`.

**Confirmations.**
- No hidden bypass to a model: `ModelInvoker.generate` and `generate_structured` refuse without a governing ledger; only the Harness binds one.
- No duplicated orchestration: one orchestrator, one router, one engine. The three agents are single-call handlers.
- Deterministic paths avoid AI: verified by `modelCalls=0` on flow start, actions, navigation phrases, purchase intent, policy reads, and by the absence of Harness records for those paths.
- AI paths always use the Harness: the API layer has no reference to `ModelInvoker`; `app/ai/**` cannot import `WorkflowEngine.apply_action` (import test).
- Provider boundaries respected: business services depend on `ProviderBundle` Protocols; InsureMO adapters raise `InsureMoContractNotConfigured` rather than guessing.

**Concerns.** Side-effect-before-commit ordering (H-1); `ai -> workflows` and `ai -> integrations` imports couple the Harness to the domain model; `api -> domains.motor` leak; no channel adapter layer (H-6); the composition root must be edited to add a domain's service actions.

## 6. Security review

Attempted and blocked: direct override, system-prompt extraction, role escalation, tool abuse, embedded `<system>` directive, cross-customer exfiltration, third-party lookup, denial-of-wallet phrasing, fullwidth Unicode, PII extraction request; all with zero model calls. Reached the model but produced only a clarification: Cyrillic homoglyph, zero-width joiners, base64 smuggle, leetspeak, polite framing. Prompt template text never appeared in any response. No secrets, stack traces, hosts or SQL in any response body, including invalid JSON and unhandled exceptions. Security headers present; CORS disabled by default; docs disabled in prod by code.

Defeated or weak: cross-turn injection via history (H-5); grounding acceptance of fabricated claims (H-3); AI-path rate limit is a soft 200 (M-1); chunked bodies skip the size middleware (M-15); anonymous shared identity (M-6); conversation-id squatting (M-7); correlation header unbounded (L-8).

Malformed inputs: malformed provider response -> `UPSTREAM_INVALID_RESPONSE`, `available=false`; non-JSON structured model output -> `ESCALATE` with a clarifying question; `<script>` in model output -> `BLOCK_GUARDRAIL_OUTPUT`; 700-word message -> supervisor path, 1 model call, 1069 input tokens (bounded by the 4000-char schema limit and the 3500-token context ceiling).

## 7. Harness review

| Control | Classification | Evidence |
|---|---|---|
| Authentication context | ENFORCED | PEP `_check_authentication` and `allowed_actor_types`; `SERVICE` actor refused for customer scope |
| Ownership | ENFORCED | `_open_conversation`, `WorkflowEngine._assert_ownership`, `ResourceScopeInterceptor`; probes 403 in every cross-subject case |
| Budgets | ENFORCED | greedy handler stopped at 1 call; ungoverned call refused; record equals actual |
| Grounding | PARTIAL | central and mandatory, but lexical (H-3) and assessed against undelivered evidence (H-8) |
| Tool authorization | ENFORCED | undeclared tool -> `BLOCK_AUTHORIZATION`; agents cannot authorise themselves |
| Schema validation | ENFORCED | input via PEP, output via `validate_output`; extra field -> `BLOCK_OUTPUT_SCHEMA_INVALID` |
| Output validation | ENFORCED | prohibited claim, markup, secret -> BLOCK; PII -> SANITIZE |
| Tracing | METADATA ONLY | in-process ring buffer; OTEL no-op (M-2) |
| Audit | PARTIAL | emitted on every non-exception path; missing on unexpected exceptions (M-4); decision record not durable (M-5) |
| PII | ENFORCED | question and history masked before model; Aadhaar, PAN, mobile absent from prompts |
| Prompt protection | PARTIAL | current message scanned; history not scanned (H-5); retrieved documents scanned only at query time (M-11) |

## 8. Workflow review

Deterministic routing, transitions, invalid transitions, confirmation, idempotency, expiry, interrupt and resume all verified at the HTTP boundary (35 PASS). Optimistic locking works for a single conflicting pair (one commit, one conflict). Failures: concurrency multiplies side effects (H-1); payment completion is client-asserted (H-2). Observation: an agent can drive the motor flow to a quote whose `customer_id` is the agent's own subject, because the flow has no customer-selection step (weak). Extra undeclared payload fields are silently ignored rather than rejected.

## 9. RAG review

Verified: metadata filtering by domain, product and audience; lifecycle exclusion at index build; revoked document rejection end to end; genuine RRF; exact-duplicate dedup; abstention at zero model calls; citations always a subset of retrieved evidence. Partial or missing: no version or effective-date logic (H-7); grounding against undelivered evidence (H-8); metadata-only conflict detection (H-9); "hybrid" is BM25 plus a hashed bag-of-words cosine over the same tokens (cosine of "car" and "automobile" is 0.0), so there is no semantic channel; dedup uses a 400-character prefix and runs after top-k; citations are document-level and over-inclusive at section level; paraphrased questions frequently abstain because the evidence gate is lexical. The Ragas report is a deterministic regression harness over 5 documents and 22 cases; the judge path cannot produce a number even with credentials.

## 10. AI review

Strands is used as the `Model` interface only; no agent loop, hooks or handoffs run, consistent with ADR 0003. Model routing is configuration (`deterministic`, `openai`, `bedrock`) with production refusing the double; the real adapters are untested (M-10). Token accounting flags estimates (`tokens_estimated`) and the deterministic model reports usage so most records are not estimated. Structured output is validated by Pydantic and constrained to allow-listed intents and capabilities; unknown entities are dropped. Refusal paths (BLOCK, ABSTAIN, ESCALATE, FALLBACK) all produce safe messages. Telemetry is truthful. Hallucination handling is the weak point (H-3, H-8).

## 11. Authoritative data

Premium, add-ons, claims, payments and policy details were read only through `AuthoritativeDataTools` with scope enforcement before the provider call, zero model calls, and controlled `available=false` on timeout and malformed response. A history assertion ("my premium is Rs 999") did not surface in a later answer. A chat question "What is my premium?" routes to the FAQ path and returns a glossary definition, not a value; the intent `GET_POLICY_PREMIUM` exists but no chat path binds it to the premium tool, so the conversational route to authoritative premium is not implemented.

## 12. Testing review

655 tests, 90% line coverage, deterministic across repeated and reverse-order runs. Strong deny-path tests exist for ownership and budgets. Weaknesses: seven PEP deny branches, `_authorize_tool`, `BLOCK_PERMISSION_MISSING`, `missing_prerequisite_data`, JWKS resolution, indirect-injection block and all rate-limit enforcement paths are never executed; every fixture disables rate limiting; permissive status-code sets and one literal tautology in the security suite; injection HTTP tests do not assert the block outcome; performance gates compare against no stored baseline in pytest (the benchmark script does); the real provider path has zero coverage. The tests prove the single-request contract convincingly and prove nothing about concurrency, cross-process identity or semantic grounding.

## 13. Operational review

| Item | Classification |
|---|---|
| Liveness | SUPPORTED |
| Readiness (corpus, providers, breaker, Redis ping, model degradation) | SUPPORTED; 503 on dead Redis after about 2 s |
| Environment separation | SUPPORTED for prod with gaps; NOT IMPLEMENTED for preprod |
| Secrets | SUPPORTED (env only, never logged); no strength check |
| Configuration | SUPPORTED (env plus overlay templates) |
| Redis integration | SUPPORTED (CAS, TTLs, fail closed); BLOCKED EXTERNAL for managed Redis; per-instance subject keys (H-4) |
| Logging | SUPPORTED (structured JSON, masked, correlation ids) |
| Tracing | NOT IMPLEMENTED |
| Metrics | SUPPORTED (Prometheus exposition, gated); cardinality defect |
| Rate limiting | SUPPORTED on deterministic routes; soft on AI routes |
| Audit sink | SUPPORTED (tamper-evident); WORM BLOCKED EXTERNAL; truncation undetected |
| Container and CI | SUPPORTED (non-root, multi-stage, all gates); supply-chain job placeholder; no lock file |

## 14. Regulatory evidence review

Durable and joinable by request id: workflow start, state change, completion, rejection, idempotent replay, quote received, payment initiated, PEP authorization denials, guardrail blocks, AI execution completed with model id, prompt version, corpus version, guardrail policy version, token counts and latency. Not durable: grounding sources, citations, cost, decision records (log only). Not recorded: direct-read scope denials, rate limiting, model fallback, user confirmation as a distinct event. Not joinable across instances: `actor_ref` (H-4). Retention: configured but unimplemented. The control evidence matrix points to real tests; the compliance register and readiness document disagree on audit integrity. Technical evidence therefore exists for workflow decisions and model usage, is partial for ownership and traceability, and is absent for grounding provenance and retention.

## 15. Long-term maintainability, scalability, technical debt

Maintainability is good at the module level: Protocols at boundaries, single composition root, clean lint and types, meaningful docstrings, no swallowed exceptions. Debt: empty scaffold packages, duplicated guard and ledger code in the two most security-relevant files, `Any` at the container seam, motor-specific fallbacks inside generic orchestration, a 625-line orchestrator that also builds presentation, test doubles shipped in the production model module. Scalability: the app is stateless with Redis adapters, but per-instance pseudonyms (H-4), per-instance FinOps records and exact-value histograms undermine horizontal scaling until fixed. Extensibility for domains is demonstrated; for channels it is absent (H-6).

## 16. Things intentionally external and not penalised

Managed Redis confirmation, WORM object-lock storage, penetration test, InsureMO specification and sandbox, judge-model credentials, platform-level disaster recovery, container and SBOM scanning, WCAG audit, legal and compliance sign-off, real model and provider latency. These are recorded honestly in `PROJECT_READINESS.md` and are not counted against the template except where the code contradicts its own documentation (tracing, pinned dependencies, retention).

---

## 17. Phase 2 build verification record

| Gate | Result | Note |
|---|---|---|
| `ruff format --check .` | PASS | 236 files |
| `ruff check .` | PASS | |
| `mypy` | PASS | 140 source files |
| `pytest -q` | PASS | 655 passed, 0 failed, 13.2 s, 90% line coverage |
| Security tests | PASS | 146 collected in `tests/security` |
| Native evals | PASS | 128/128 across 8 suites |
| Ragas | PASS (proxy metrics only) | 7 judge metrics NOT_EVIDENCED; judge path is dead code |
| DeepEval | PASS (proxy metrics only) | 8 judge metrics NOT_EVIDENCED |
| Benchmark | PASS | 14/14 gates, no regression vs baseline |
| Load test (mixed) | PASS | 535 requests, 100% success, p95 152 ms in-process |
| Recovery drill | PASS | 9/9 scenarios |
| OpenAPI generation | PASS | 3.1.0, 17 paths, 20 schemas, error schema and bearer scheme injected |
| Application startup (`uvicorn`) | PASS | live, ready (5 dependencies up), chat and openapi served |
| Dependency audit (`pip-audit`) | PASS with notes | 3 findings, all eval extras: `diskcache`, `nltk`, `ragas`; `pip check` clean |

## 18. Phase 11 performance record

All figures are in-process (`TestClient`), with the deterministic model double and mock providers. They exclude network, real model and real provider latency and must not be read as production numbers.

| Measurement | Value |
|---|---|
| Deterministic UI action, p50 / p95 | 3.3 ms / 6.0 ms |
| FAQ uncached, p50 / p95 | 4.5 ms / 6.4 ms |
| FAQ cached, p50 / p95; cache hit rate for 50 identical public questions | 3.7 ms / 6.4 ms; 100% |
| Abstention, p50; model calls for 30 requests | 2.8 ms; 0 |
| Authoritative premium read, p50 / p95 | 2.0 ms / 3.1 ms |
| FAQ input tokens per model call (deterministic model reported) | 268 |
| Zero-model share of Harness records in the probe workload | 92% (deterministic HTTP paths create no record) |
| Agent steps / handoffs per request, max | 1 / 0 |
| 200 concurrent FAQs through the orchestrator | 190 ms wall, about 1050 req/s |
| 200 sequential HTTP FAQs (cached) | about 280 req/s |
| Memory growth for 1000 new-conversation requests | 8.3 MiB (conversations and execution records retained) |
| Project load test, mixed profile, concurrency 16 | 232 req/s, p95 152 ms, 100% success |

---

## FINAL SCORECARD

Scores are independent of the project's self-assessment. 10 = proven by execution with no material defect; 7 = works with gaps a future bot would inherit; 5 = present but a High finding sits on it; 3 = largely missing.

| Area | Score | Basis |
|---|---|---|
| Architecture | 7.0 | clean layering, single AI entry, no cycles; side-effect ordering (H-1), no channel layer (H-6) |
| Security | 6.5 | strong boundary controls; cross-turn injection (H-5), soft AI rate limit, chunked body bypass |
| Authentication | 8.5 | every forged-token probe refused; JWKS untested, no `jti` replay cache |
| Ownership | 7.5 | authenticated scope proven and non-disclosing; anonymous shared identity, unaudited direct-read denials |
| Harness | 7.5 | 9 of 11 controls enforced structurally; grounding and exception-path audit partial |
| Workflow | 6.0 | single-request contract proven; concurrency multiplies side effects; payment unverified |
| RAG | 5.0 | lifecycle, RRF, abstention real; no version logic, content-blind conflicts, undelivered-evidence grounding |
| AI Runtime | 6.5 | governed invoker, truthful telemetry; Strands loop unused, real adapters untested |
| Guardrails | 7.0 | input, output, tool and business layers enforced; history not scanned, documents not quarantined |
| Hallucination | 4.5 | lexical grounding accepts negations and entity swaps; non-answers labelled VERIFIED |
| Token Efficiency | 8.5 | budgets enforced, 74% zero-model in benchmark, minimal context assembly |
| Performance | 7.0 | in-process SLOs met with wide margin; no real provider or model latency measured |
| Scalability | 6.0 | stateless with Redis adapters; per-process pseudonyms and per-instance records undermine multi-instance |
| Reliability | 7.5 | timeouts, retries, breaker, fail-closed stores, drill passes; no reservation for side effects |
| Observability | 5.0 | metrics, dashboards and structured logs real; tracing no-op; cardinality defect |
| Auditability | 6.0 | hash chain, verifier, version stamps; unstable actor_ref, missing event types, truncation undetected |
| Test Quality | 6.5 | 655 deterministic tests with real crypto; deny branches and concurrency unexercised |
| Code Quality | 7.0 | lint and types clean, Protocols; duplication, `Any` seams, dead code |
| Maintainability | 7.0 | cohesive modules and DI; scaffold residue and orchestrator size |
| Documentation | 6.5 | extensive and mostly honest; stale figures, false tracing and pinning claims, internal contradictions |
| Environment Separation | 6.0 | broad unbypassable prod guard; preprod unguarded; three providers and secret strength unchecked |
| Operational Readiness | 6.0 | readiness measured, runbooks, drift-tested ops pack; soft AI limits, unbounded growth, placeholder supply-chain gate |
| Regulatory Evidence | 5.0 | workflow and model events durable; grounding, citations, cost and retention not durable |
| Overall Engineering Quality | 6.5 | mean of the above rows, rounded |
| Master Template Readiness | 5.5 | not yet safe to clone for financial flows and multi-channel bots |

---

## Remediation required before re-audit (in priority order)

1. Reserve the transition before executing a side effect: take a per-flow lock or bump the state version (or consume a single-use confirmation nonce) before calling the provider; re-run the race probe. (H-1)
2. Verify payment status from the payment provider before `COMPLETE_PAYMENT` is legal; make the transition's prerequisite a provider-confirmed status, not a client action. (H-2)
3. Replace `hash()` in `subject_ref` and `conversation_ref` with a keyed stable digest; set `PYTHONHASHSEED` in the image as defence in depth. (H-4)
4. Scan history turns with the injection detector before they enter model context, or exclude blocked turns from history. (H-5)
5. Ground against `built.evidence_document_ids`, split or reject oversized chunks, and require an NLI-style or judge check for negation and entity substitution; stop labelling marker-free sentences as VERIFIED. (H-3, H-8)
6. Implement version and effective-date selection per document lineage; persist lifecycle state; require `approved_by` for ACTIVE. (H-7)
7. Introduce a channel adapter interface (inbound normalisation, outbound directive renderer) and prove it with one non-web channel, as travel proves domains. (H-6)
8. Return 429 with `Retry-After` for AI-path rate limits and emit `RATE_LIMITED`; wire a real `TracerProvider` or remove the OTEL settings and claim; apply the production guard to preprod. (M-1, M-2, M-3)
9. Emit audit evidence on exception paths and persist decision records, sources and cost in the sink. (M-4, M-5)
10. Add tests that execute every PEP deny branch, `_authorize_tool`, JWKS resolution, rate limiting end to end, and the two race scenarios above. (M-9)

## Evidence index

- Probe scripts and JSON results: session scratchpad `probe_auth.py`, `probe_workflow.py`, `probe_harness.py`, `probe_security.py`, `probe_grounding.py`, `probe_race.py`, `probe_perf.py`, `results_*.json`; sub-audit scripts under `scratchpad/rag/`, `scratchpad/ops/`, `scratchpad/tests_audit/`.
- Gate outputs: `ruff_format.txt`, `ruff_check.txt`, `mypy.txt`, `pytest.txt`, `eval_native.txt`, `eval_ragas.txt`, `eval_deepeval.txt`, `benchmark.txt`, `loadtest.txt`, `drill.txt`, `pip_audit.txt`, `pip_check.txt`, `openapi.json`, `uvicorn.txt`.
- Project artefacts regenerated by running its own gates: `evals/reports/*.json` (gitignored).
