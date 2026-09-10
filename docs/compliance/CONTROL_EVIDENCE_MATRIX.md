# Control evidence matrix

Every implemented control, the code that implements it, and the **exact command** that
proves it. A reviewer should be able to run any row and see the result.

Status values: `PASS` (executed and passing), `NOT_EVIDENCED` (no executable proof),
`GAP` (not implemented).

Reproduce everything: `python scripts/run_evals.py all`

---

## Authentication and identity

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| AUTH-01 | Missing bearer token rejected on a protected route | `app/api/deps.py` | `pytest tests/security/test_jwt_boundary.py -k missing_bearer` | `PASS` |
| AUTH-02 | Malformed JWT rejected | `app/core/auth/jwt_service.py` | `pytest -k malformed_jwt` | `PASS` |
| AUTH-03 | Invalid signature rejected | same | `pytest -k invalid_signature` | `PASS` |
| AUTH-04 | Expired token rejected | same | `pytest -k expired_token` | `PASS` |
| AUTH-05 | Wrong issuer rejected | same | `pytest -k wrong_issuer` | `PASS` |
| AUTH-06 | Wrong audience rejected | same | `pytest -k wrong_audience` | `PASS` |
| AUTH-07 | Unsupported algorithm and `alg=none` rejected | same + `Settings` validator | `pytest -k "unsupported_algorithm or alg_none"` | `PASS` |
| AUTH-08 | Tampered claim rejected | same | `pytest -k tampered_claim` | `PASS` |
| AUTH-09 | Unknown roles discarded, not trusted | `_to_auth_context` | `pytest -k unknown_roles_in_a_valid_token` | `PASS` |
| AUTH-10 | Refresh/ID token cannot authorize | same | `pytest -k refresh_token_cannot_authorize` | `PASS` |
| AUTH-11 | Customer cannot use an agent-scoped lookup | PEP + scope | `pytest -k customer_cannot_use_agent_scoped` | `PASS` |
| AUTH-12 | Agent without assignment refused | `ClaimBasedAgentAssignmentPolicy` | `pytest -k agent_without_assignment` | `PASS` |
| AUTH-13 | Valid token yields a trusted context free of credentials | `AuthContext` | `pytest -k valid_token_creates_trusted` | `PASS` |
| AUTH-14 | `Authorization` header never reaches logs | `RedactingJsonFormatter` | `pytest -k authorization_header_never_reaches_logs` | `PASS` |
| AUTH-15 | JWT never crosses the model boundary | `ModelInputSanitizer` | `pytest -k jwt_cannot_cross_the_model_boundary` | `PASS` |
| AUTH-16 | Refresh token stripped before model context | same | `pytest -k refresh_token_field_is_stripped` | `PASS` |
| AUTH-17 | JWKS preferred; dev secret refused above test | `build_key_resolver`, `Settings` | `pytest -k "jwks_configuration or dev_secret_is_refused"` | `PASS` |
| AUTH-18 | Public FAQ works unauthenticated but still guardrailed | `optional_auth_context` | `pytest -k public_faq_route_works` | `PASS` |

## Authorization and resource scope

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| AZ-01 | Customer reads own policy | `AuthoritativeDataTools` | `pytest tests/security/test_resource_scope.py -k own_policy` | `PASS` |
| AZ-02 | Customer cannot read another customer's policy | `ResourceScopeInterceptor` | `pytest -k another_customers_policy` | `PASS` |
| AZ-03 | Forged `customer_id` in payload ignored | `effective_customer_id` | `pytest -k forged_customer_id` | `PASS` |
| AZ-04 | Model-generated alternate id blocked | same | `pytest -k model_generated_customer_id` | `PASS` |
| AZ-05 | Assigned agent allowed | assignment policy | `pytest -k assigned_agent_can_read` | `PASS` |
| AZ-06 | Unassigned agent forbidden | same | `pytest -k unassigned_agent_is_forbidden` | `PASS` |
| AZ-07 | `AGENT` role alone grants nothing | same | `pytest -k agent_role_alone_grants_no` | `PASS` |
| AZ-08 | Cross-tenant access denied | `ResourceScopeInterceptor.authorize_resource` builds the reference from the SoR tenant | `pytest -k cross_tenant` (tool path, not helper) | `PASS` |
| AZ-09 | Conversation / in-flight journey isolated per subject | `ConversationOrchestrator._open_conversation`, `WorkflowEngine.get_active(ctx)` | `pytest -k TestC1` | `PASS` |
| AZ-09 | Denial does not disclose existence | `authorize_resource` | `pytest -k does_not_disclose_existence` | `PASS` |
| AZ-10 | Denial is audited | PEP | `pytest -k pep_denial_emits_audit_event` | `PASS` |
| AZ-11 | Authority settled before any SoR read | `authorize_resource` ordering | `python -m evals.native.run auth` | `PASS` |
| AZ-12 | Workflow owner mismatch refused | `WorkflowEngine._assert_ownership` | `pytest -k someone_elses_flow` | `PASS` |

## Determinism and workflow safety

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| DET-01 | All legal transitions pass | `WorkflowEngine` | `pytest tests/unit/test_workflow_determinism.py -k valid_transitions` | `PASS` (8/8) |
| DET-02 | All prohibited transitions rejected | same | `pytest -k out_of_order_transitions` | `PASS` (9/9) |
| DET-03 | A rejected transition does not mutate state | same | included above | `PASS` |
| DET-04 | Business validation rejects bad input | domain validators | `pytest -k business_validation` | `PASS` (6/6) |
| DET-05 | Duplicate submit replays safely | idempotency keys | `pytest -k duplicate_submit` | `PASS` |
| DET-06 | High-risk write needs confirmation | transition flags + server-issued token (`ConfirmationService`) | `pytest -k "confirmation_required or TestM2"` | `PASS` |
| DET-10 | Idempotency key scoped to action + payload | `WorkflowEngine._idempotency_replay` | `pytest -k TestM3` | `PASS` |
| DET-07 | High-risk write needs an idempotency key *before* execution | `_assert_idempotency` | `pytest -k requires_an_idempotency_key` | `PASS` |
| DET-08 | Optimistic locking rejects a stale write | store | `pytest -k optimistic_locking` | `PASS` |
| DET-09 | Validator cannot write undeclared fields | engine | `pytest -k undeclared_fields` | `PASS` |
| DET-10 | FAQ interruption does not mutate state | orchestrator | `pytest -k faq_interruption_preserves` | `PASS` |
| DET-11 | Resume reads structured state, not a transcript | engine + store | `pytest -k resumes_from_persisted` | `PASS` |
| DET-12 | The model cannot reach the workflow engine | import boundary | `pytest -k agents_do_not_import_the_workflow_engine` | `PASS` |
| DET-13 | Full journey completes and persists to the SoR | end to end | `pytest -k full_journey_completes` | `PASS` |
| DET-14 | Native determinism suite | datasets | `python -m evals.native.run workflow` | `PASS` (17/17) |

## Guardrails and adversarial resistance

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| GR-01 | Direct injection blocked (16 attacks) | `InjectionDetector` | `pytest tests/security/test_adversarial.py -k direct_prompt_injection` | `PASS` |
| GR-02 | Indirect injection via a document refused | doc scan + wrapping | `pytest -k indirect_injection` | `PASS` |
| GR-03 | Retrieved text wrapped as data | `wrap_untrusted` | `pytest -k wrapped_as_data` | `PASS` |
| GR-04 | Unauthorized tool refused regardless of agent | PEP | `pytest -k unauthorized_tool` | `PASS` |
| GR-05 | Agent cannot self-authorize a tool | `AgentExecutionContext` | `pytest -k cannot_self_authorize` | `PASS` |
| GR-06 | Cost-abuse attempt blocked | detector weight 0.65 | `python -m evals.native.run security` | `PASS` |
| GR-07 | Third-party data request blocked | detector | `pytest -k third_party_data_request` | `PASS` |
| GR-08 | Credential in input blocked, not masked | `_SECRET_LIKE` | `python -m evals.native.run pii` | `PASS` |
| GR-09 | Unsafe markup blocked on input and output | guardrail | `pytest -k unsafe_markup` | `PASS` |
| GR-10 | Prohibited claims blocked | claim patterns | `pytest -k prohibited_claims` | `PASS` |
| GR-11 | Oversized and malformed payloads rejected | middleware + schemas | `pytest -k "oversized or malformed_chat"` | `PASS` |
| GR-12 | Rate limiting separates cheap and expensive tiers | `RateLimiter` | `pytest -k rate_limiting_blocks_expensive` | `PASS` |
| GR-13 | Blocked and abstained requests cost zero tokens | Harness ordering | `pytest -k "blocked_requests_spend_no_model_call or abstention_spends_no"` | `PASS` |
| GR-14 | Adversarial dataset pass rate | 19 cases | `python scripts/benchmark.py` | `PASS` (1.000) |

## Privacy and PII

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| PII-01 | Field classification | `classification.py` | `pytest tests/unit/test_pii_boundaries.py -k classification` | `PASS` |
| PII-02 | Masking keeps a usable suffix only | `MaskingService` | `pytest -k masking_hides_values` | `PASS` |
| PII-03 | Free-text PII masked | patterns | `pytest -k free_text_pii` | `PASS` |
| PII-04 | Nested structures redacted | recursive redact | `pytest -k nested_structures` | `PASS` |
| PII-05 | No PII or secret reaches the log stream (12 field types) | formatter | `pytest -k no_pii_or_secret_reaches_the_log` | `PASS` |
| PII-06 | Log message body masked | formatter | `pytest -k log_message_body_is_masked` | `PASS` |
| PII-07 | Exception logs omit stack traces | formatter | `pytest -k exception_logging_omits` | `PASS` |
| PII-08 | Secrets never cross the model boundary (8 types) | sanitizer | `pytest -k secrets_never_cross` | `PASS` |
| PII-09 | Unnecessary PII removed from model context | purpose fields | `pytest -k unnecessary_pii_is_removed` | `PASS` |
| PII-10 | Structural secret check blocks a rendered context | `assert_no_secrets` | `pytest -k structural_secret_check` | `PASS` |
| PII-11 | Built context free of raw PII end to end | ContextBuilder | `pytest -k built_context_never_contains` | `PASS` |
| PII-12 | Provider payload projected, not forwarded | AI-facing DTO | `pytest -k projected_not_forwarded` | `PASS` |
| PII-13 | Traces carry no prompt or document text | tracer filter | `pytest -k traces_carry_no_prompt` | `PASS` |
| PII-14 | Audit records contain no unredacted PII | AuditService | `pytest -k audit_records_contain_no` | `PASS` |
| PII-15 | Log fields are pseudonymous | RequestContext | `pytest -k log_fields_are_pseudonymous` | `PASS` |
| PII-16 | Native PII suite | datasets | `python -m evals.native.run pii` | `PASS` (17/17) |
| PII-17 | Benchmark PII leakage incidents | benchmark | `python scripts/benchmark.py` | `PASS` (0) |

## Hallucination control and grounding

| ID | Control | Code | Command | Result |
|---|---|---|---|---|
| HAL-01 | Evidence gate before the model call | `GroundingService` | `pytest tests/integration/test_rag_pipeline.py -k evidence_gate` | `PASS` |
| HAL-02 | Post-answer grounding check | same | `pytest -k ungrounded_answer_is_refused` | `PASS` |
| HAL-03 | Relevance is absolute, not rank-based | retriever | `pytest -k relevance_is_absolute` | `PASS` |
| HAL-04 | 15 supported questions retrieve evidence | corpus | `pytest -k supported_questions_retrieve` | `PASS` |
| HAL-05 | 6 unsupported questions abstain | gate | `pytest -k unsupported_questions_fail` | `PASS` |
| HAL-06 | Superseded sources excluded | lifecycle | `pytest -k superseded_documents` | `PASS` |
| HAL-07 | Draft sources excluded | lifecycle | `pytest -k draft_documents` | `PASS` |
| HAL-08 | Revoked sources removed and index rebuilt | corpus version | `pytest -k "revoking or index_is_rebuilt"` | `PASS` |
| HAL-09 | Genuine conflict surfaced, not resolved | conflict detection | `pytest -k same_document_surface_a_conflict` | `PASS` |
| HAL-10 | Unrelated documents are not a false conflict | same | `pytest -k unrelated_documents_are_not` | `PASS` |
| HAL-11 | Premium never invented on provider failure | tools | `pytest -k timeout_returns_unavailable` | `PASS` |
| HAL-12 | No user-facing confidence score | directive schema | `pytest -k never_presented_as_user_confidence` | `PASS` |
| HAL-13 | Hallucination rate | Ragas | `python -m evals.ragas.runners.rag_eval` | `PASS` (0.000) |
| HAL-14 | Citation correctness | Ragas | same | `PASS` (1.000) |
| HAL-15 | Abstention correctness | Ragas | same | `PASS` (1.000) |

## Harness centralization

| ID | Control | Command | Result |
|---|---|---|---|
| HRN-01 | Identical auth enforcement across agents | `pytest tests/unit/test_harness_centralization.py -k auth_enforcement` | `PASS` |
| HRN-02 | Identical PII sanitization across agents | `pytest -k pii_sanitization_is_identical` | `PASS` |
| HRN-03 | Unauthorized tools blocked regardless of agent | `pytest -k unauthorized_tools_blocked_regardless` | `PASS` |
| HRN-04 | Budgets enforced across agents (invoker-level, not cooperative) | `pytest -k "budgets_are_applied or TestH2"` | `PASS` |
| HRN-05 | Workflow-state restriction cannot be bypassed | `pytest -k workflow_state_restriction` | `PASS` |
| HRN-06 | Malformed output rejected centrally | `pytest -k "malformed_agent_output or rogue_agent"` | `PASS` |
| HRN-07 | Grounding failure abstains centrally (`GroundingPolicy` in the Harness, rogue ALLOW abstained) | `pytest -k "grounding_failure_causes or TestH3"` | `PASS` |
| HRN-08 | Audit and trace fire for every agent | `pytest -k audit_and_trace_fire` | `PASS` |
| HRN-09 | Deterministic requests do not enter the Harness | `pytest -k "deterministic_capability_is_refused or records_no_harness"` | `PASS` |
| HRN-10 | A new agent needs no copied infrastructure | `pytest -k new_agent_inherits_every_control` | `PASS` |
| HRN-11 | Agents do not duplicate control logic | `pytest -k agents_do_not_reimplement` | `PASS` |
| HRN-12 | The Harness is in-process, not a service | `pytest -k harness_is_in_process` | `PASS` |
| HRN-13 | The Harness delegates rather than implementing | `pytest -k harness_delegates` | `PASS` |

## Integration replaceability

| ID | Control | Command | Result |
|---|---|---|---|
| INT-01 | Both implementations expose the same methods | `pytest tests/contract -k same_methods` | `PASS` (6 protocols) |
| INT-02 | Signatures match | `pytest -k signatures_match` | `PASS` |
| INT-03 | Return annotations match | `pytest -k return_annotations_match` | `PASS` |
| INT-04 | Mocks satisfy every runtime Protocol | `pytest -k satisfies_every_runtime_checkable` | `PASS` |
| INT-05 | Idempotency on quote and payment | `pytest -k idempotency` | `PASS` |
| INT-06 | Timeout, outage, malformed, not-found simulated | `pytest -k "mock_simulates"` | `PASS` |
| INT-07 | 9 upstream status codes map to the taxonomy | `pytest -k status_codes_map` | `PASS` |
| INT-08 | Errors do not leak the upstream payload | `pytest -k do_not_leak_the_upstream` | `PASS` |
| INT-09 | Unsupplied mapping fails loudly | `pytest -k fails_loudly_rather_than_inventing` | `PASS` |
| INT-10 | Provider selection is configuration-driven | `pytest -k selection_is_configuration_driven` | `PASS` |

## Efficiency, latency and cost

| ID | Gate | Command | Result |
|---|---|---|---|
| EFF-01 | Deterministic actions: 0 model calls, 0 handoffs, 0 tokens | `pytest tests/performance -k fast_path_gate` | `PASS` |
| EFF-02 | Deterministic p95 < 300 ms | `pytest -k deterministic_action_latency` | `PASS` (0.6 ms) |
| EFF-03 | FAQ p95 < 3000 ms | `pytest -k faq_latency` | `PASS` (2.3 ms) |
| EFF-04 | FAQ median input <= 1500 tokens | `pytest -k normal_faq_gate` | `PASS` (438) |
| EFF-05 | FAQ p95 input <= 2500 tokens | same | `PASS` (537) |
| EFF-06 | FAQ median output <= 300 tokens | same | `PASS` (55) |
| EFF-07 | Routing p95 input <= 1000 tokens | `pytest -k routing_extraction_gate` | `PASS` (166) |
| EFF-08 | Context does not grow with conversation length | `pytest -k interrupt_resume_does_not_replay` | `PASS` (0.0%) |
| EFF-09 | Tool results compressed, not forwarded whole | `pytest -k tool_results_are_compressed` | `PASS` |
| EFF-10 | Cost within configured ceilings | `pytest -k cost_per_1000` | `PASS` (2.07 of 25.0) |
| EFF-11 | Zero-model-call rate measured | `python scripts/benchmark.py` | `PASS` (74%) |
| EFF-12 | Full token telemetry populated | `pytest -k full_token_telemetry` | `PASS` |
| EFF-13 | All 14 benchmark gates | `python scripts/benchmark.py` | `PASS` (14/14) |
| EFF-14 | 4 load profiles | `python scripts/load_test.py <profile>` | `PASS` (4/4) |

## Environment, configuration and secrets

| ID | Control | Command | Result |
|---|---|---|---|
| ENV-01 | Every setting documented in `.env.example` | `pytest tests/unit/test_configuration.py -k documents_every_setting` | `PASS` (101) |
| ENV-02 | No stale documented setting | `pytest -k documents_nothing_the_app_ignores` | `PASS` |
| ENV-03 | No credentials in application code | `pytest -k no_credentials_are_committed` | `PASS` |
| ENV-04 | Fixture secrets are recognisably synthetic | `pytest -k fixture_secrets_are_unmistakably` | `PASS` |
| ENV-05 | Secret values empty in `.env.example` | `pytest -k no_populated_secret_values` | `PASS` |
| ENV-06 | `.gitignore` excludes local env files | `pytest -k gitignore_excludes` | `PASS` |
| ENV-07 | Out-of-range configuration rejected | `pytest -k out_of_range_configuration` | `PASS` (8 cases) |
| ENV-08 | Production guard is comprehensive (9 unsafe settings) | `pytest -k production_configuration_guard` | `PASS` |
| ENV-09 | A valid production configuration still starts | same | `PASS` |
| ENV-10 | Container builds for every lower environment | `pytest -k container_builds_for_every` | `PASS` |

## Reliability

| ID | Control | Command | Result |
|---|---|---|---|
| REL-01 | Model timeout becomes a controlled fallback | `pytest tests/integration/test_resilience.py -k model_timeout` | `PASS` |
| REL-02 | Deterministic workflow works while the model is down | `pytest -k still_works_while_the_model_is_down` | `PASS` |
| REL-03 | Fallback refused for high-risk capabilities | `pytest -k fallback_is_refused_for_high_risk` | `PASS` |
| REL-04 | RAG failure returns unavailability, not model memory | `pytest -k rag_failure_returns` | `PASS` |
| REL-05 | Empty corpus abstains | `pytest -k empty_corpus_abstains` | `PASS` |
| REL-06 | Provider failure preserves transaction state | `pytest -k preserves_transaction_state` | `PASS` |
| REL-07 | Malformed upstream rejected | `pytest -k malformed_upstream` | `PASS` |
| REL-08 | Partial outage leaves other capabilities working | `pytest -k partial_dependency_outage` | `PASS` |
| REL-09 | Circuit breaker opens, half-opens, recovers | `pytest -k circuit_breaker` | `PASS` |
| REL-10 | Retry only when duplication is prevented (8 cases) | `pytest -k retry_is_only_allowed` | `PASS` |
| REL-11 | Irreversible action attempted exactly once | `pytest -k attempted_exactly_once` | `PASS` |
| REL-12 | Failing audit sink fails closed | `pytest -k audit_sink_fails_closed` | `PASS` |
| REL-13 | Unsupported sensitive case escalates | `pytest -k escalates_to_a_human_path` | `PASS` |

## Observability and audit

| ID | Control | Command | Result |
|---|---|---|---|
| OBS-01 | Structured JSON logs with required fields | `pytest -k logs_are_valid_json` | `PASS` |
| OBS-02 | Correlation id echoed; request id present | `python -m evals.native.run contract` | `PASS` |
| OBS-03 | Secure headers on every response | `pytest -k secure_headers_are_present` | `PASS` |
| OBS-04 | Flow, provider and AI events audited | `pytest -k emits_audit_and_metrics` | `PASS` |
| OBS-05 | Every AI execution produces a record | `pytest -k produces_an_execution_record` | `PASS` |
| OBS-06 | Version stamps on audit events | `pytest -k audit_and_trace_fire` | `PASS` |
| OBS-07 | Health and readiness endpoints reflect measured dependency health (503 when degraded) | `pytest -k TestH4` | `PASS` |
| OBS-08 | Metrics snapshot exposed under permission | `app/api/routers/health.py` | `PASS` |

## Extensibility

| ID | Control | Command | Result |
|---|---|---|---|
| EXT-01 | Core never imports a domain | `pytest tests/unit/test_architecture_boundaries.py` | `PASS` (113 checks) |
| EXT-02 | No circular core/domain dependency | same | `PASS` |
| EXT-03 | Only the composition root knows every domain | same | `PASS` |
| EXT-04 | Domains do not reimplement core infrastructure | same | `PASS` |
| EXT-05 | Second domain adds no core files | `pytest -k travel_domain_adds_no_new_core` | `PASS` |
| EXT-06 | Second domain reuses shared services | `pytest -k travel_domain_reuses_shared` | `PASS` |
| EXT-07 | Second domain completes its own journey | `pytest -k travel_domain_completes` | `PASS` |
| EXT-08 | Every capability declares budget and audit policy | `pytest -k declares_a_budget` | `PASS` |
| EXT-09 | No capability exceeds the complexity budget | `pytest -k exceeds_the_agent_complexity` | `PASS` |
| EXT-10 | Only three platform agents, each justified | `pytest -k "only_three_platform_agents or documents_its_complexity"` | `PASS` |

---

## Not evidenced

| ID | Control | Why | Owner |
|---|---|---|---|
| NE-01 | Ragas faithfulness, answer relevancy, context precision/recall, factual correctness, semantic similarity, noise sensitivity | No judge model configured | AI Platform |
| NE-02 | DeepEval AnswerRelevancy, Faithfulness, Hallucination, ContextualPrecision/Recall, Bias, Toxicity, PromptAlignment | No judge model configured | AI Platform |
| NE-03 | Dependency vulnerability scan | Not executed | Platform |
| NE-04 | Container scan | Not executed | Platform |
| NE-05 | SBOM generation | Not executed | Platform |
| NE-06 | Licence inventory | Not implemented | Platform |
| NE-07 | Penetration test | Not performed | InfoSec |
| NE-08 | DR failover test | Not performed | Platform |
| NE-09 | InsureMO integration certification | No specification supplied | Integration |
| NE-10 | Multi-instance scaling test | Shared stores not implemented | Platform |
| NE-11 | Real-provider latency and cost | Only the offline double measured | Architecture |
| NE-12 | Accessibility audit against WCAG | Contracts carry the fields; no audit performed | UX |

`NOT_EVIDENCED` is reported as `NOT_EVIDENCED`, never as a passing score.
