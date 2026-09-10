"""Environment-driven application configuration.

Every setting is documented, typed and validated at startup (master prompt §14, §15).
No secret is stored in source control; secrets arrive from the environment or an
enterprise secret manager that projects them into the environment.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    """Supported deployment environments (§15)."""

    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PREPROD = "preprod"
    PROD = "prod"


MOCK_PROVIDER = "mock"

#: Providers that must never be "mock" in preprod/prod (§15.2, §15.3 startup guard).
#: Every provider is critical: a mock product catalogue or reference table in
#: production is as wrong as a mock quote.
CRITICAL_PROVIDER_FIELDS = (
    "customer_provider",
    "policy_provider",
    "quote_provider",
    "product_provider",
    "claims_provider",
    "payment_provider",
    "document_provider",
    "reference_data_provider",
)

#: Minimum length for shared HMAC secrets in hardened environments (§14).
MIN_SECRET_LENGTH = 32

IN_MEMORY_STORE = "inmemory"

#: Stores that must never be process-local in prod: more than one instance would
#: multiply rate limits and lose in-flight journeys (§19, H-5).
CRITICAL_STATE_STORE_FIELDS = (
    "session_store_provider",
    "workflow_store_provider",
    "rate_limit_store_provider",
)


class Settings(BaseSettings):
    """Validated application settings.

    Loaded from the process environment with an optional .env file for local work.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        protected_namespaces=(),
        populate_by_name=True,
    )

    # ---------------------------------------------------------------- app ---
    app_env: AppEnv = Field(default=AppEnv.LOCAL, alias="APP_ENV")
    app_name: str = Field(default="protec-insurance-ai-template", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    debug_endpoints_enabled: bool = Field(default=False, alias="DEBUG_ENDPOINTS_ENABLED")
    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    # --------------------------------------------------------------- auth ---
    jwt_issuer: str = Field(default="https://auth.local.protec.example/", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="protec-insurance-ai", alias="JWT_AUDIENCE")
    jwt_jwks_uri: str | None = Field(default=None, alias="JWT_JWKS_URI")
    jwt_allowed_algorithms: str = Field(default="RS256,ES256", alias="JWT_ALLOWED_ALGORITHMS")
    jwt_clock_skew_seconds: int = Field(default=30, ge=0, le=300, alias="JWT_CLOCK_SKEW_SECONDS")
    jwt_public_key_pem: str | None = Field(default=None, alias="JWT_PUBLIC_KEY_PEM")
    jwt_jwks_cache_seconds: int = Field(default=300, ge=0, alias="JWT_JWKS_CACHE_SECONDS")
    #: Local-only symmetric fallback. Refused outside local/dev/test by the validator below.
    jwt_dev_hs256_secret: str | None = Field(default=None, alias="JWT_DEV_HS256_SECRET")

    # -------------------------------------------------------------- model ---
    model_provider: Literal["deterministic", "openai", "bedrock"] = Field(
        default="deterministic", alias="MODEL_PROVIDER"
    )
    model_id: str = Field(default="deterministic-test-model", alias="MODEL_ID")
    model_timeout_ms: int = Field(default=12_000, ge=200, alias="MODEL_TIMEOUT_MS")
    model_max_tokens: int = Field(default=800, ge=16, alias="MODEL_MAX_TOKENS")
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0, alias="MODEL_TEMPERATURE")
    model_streaming_enabled: bool = Field(default=True, alias="MODEL_STREAMING_ENABLED")
    model_fallback_provider: Literal["none", "deterministic"] = Field(
        default="none", alias="MODEL_FALLBACK_PROVIDER"
    )
    model_api_key: str | None = Field(default=None, alias="MODEL_API_KEY")
    model_base_url: str | None = Field(default=None, alias="MODEL_BASE_URL")
    #: Pricing assumptions used by the FinOps reporter (§35). REQUIRES_VERIFICATION.
    model_input_cost_per_1k: float = Field(default=0.003, ge=0.0, alias="MODEL_INPUT_COST_PER_1K")
    model_output_cost_per_1k: float = Field(default=0.015, ge=0.0, alias="MODEL_OUTPUT_COST_PER_1K")
    model_currency: str = Field(default="USD", alias="MODEL_CURRENCY")

    # ---------------------------------------------------------------- rag ---
    rag_knowledge_dir: str = Field(default="knowledge", alias="RAG_KNOWLEDGE_DIR")
    #: Persisted lifecycle overrides (activate / supersede / revoke / quarantine) so a
    #: re-ingestion never silently reinstates a revoked document (§34).
    rag_lifecycle_state_path: str = Field(
        default="var/knowledge/lifecycle.json", alias="RAG_LIFECYCLE_STATE_PATH"
    )
    rag_top_k: int = Field(default=4, ge=1, le=20, alias="RAG_TOP_K")
    rag_candidate_k: int = Field(default=12, ge=1, le=100, alias="RAG_CANDIDATE_K")
    rag_min_score: float = Field(default=0.12, ge=0.0, le=1.0, alias="RAG_MIN_SCORE")
    rag_reranker_enabled: bool = Field(default=False, alias="RAG_RERANKER_ENABLED")
    rag_timeout_ms: int = Field(default=1_500, ge=50, alias="RAG_TIMEOUT_MS")
    rag_allow_draft_sources: bool = Field(default=False, alias="RAG_ALLOW_DRAFT_SOURCES")
    rag_max_chunk_tokens: int = Field(default=320, ge=32, alias="RAG_MAX_CHUNK_TOKENS")

    # ---------------------------------------------------------- providers ---
    customer_provider: str = Field(default=MOCK_PROVIDER, alias="CUSTOMER_PROVIDER")
    policy_provider: str = Field(default=MOCK_PROVIDER, alias="POLICY_PROVIDER")
    quote_provider: str = Field(default=MOCK_PROVIDER, alias="QUOTE_PROVIDER")
    product_provider: str = Field(default=MOCK_PROVIDER, alias="PRODUCT_PROVIDER")
    claims_provider: str = Field(default=MOCK_PROVIDER, alias="CLAIMS_PROVIDER")
    payment_provider: str = Field(default=MOCK_PROVIDER, alias="PAYMENT_PROVIDER")
    document_provider: str = Field(default=MOCK_PROVIDER, alias="DOCUMENT_PROVIDER")
    reference_data_provider: str = Field(default=MOCK_PROVIDER, alias="REFERENCE_DATA_PROVIDER")
    provider_timeout_ms: int = Field(default=4_000, ge=100, alias="PROVIDER_TIMEOUT_MS")
    provider_max_retries: int = Field(default=2, ge=0, le=5, alias="PROVIDER_MAX_RETRIES")
    provider_circuit_failure_threshold: int = Field(
        default=5, ge=1, alias="PROVIDER_CIRCUIT_FAILURE_THRESHOLD"
    )
    provider_circuit_reset_seconds: float = Field(default=15.0, gt=0, alias="PROVIDER_CIRCUIT_RESET_SECONDS")
    insuremo_base_url: str | None = Field(default=None, alias="INSUREMO_BASE_URL")
    insuremo_api_key: str | None = Field(default=None, alias="INSUREMO_API_KEY")
    insuremo_tenant: str | None = Field(default=None, alias="INSUREMO_TENANT")

    # ------------------------------------------------------------ storage ---
    #: Critical state stores. ``inmemory`` serves exactly one process and is refused in
    #: production; ``redis`` is the declared shared-store contract (H-5).
    session_store_provider: Literal["inmemory", "redis"] = Field(
        default="inmemory", alias="SESSION_STORE_PROVIDER"
    )
    workflow_store_provider: Literal["inmemory", "redis"] = Field(
        default="inmemory", alias="WORKFLOW_STORE_PROVIDER"
    )
    rate_limit_store_provider: Literal["inmemory", "redis"] = Field(
        default="inmemory", alias="RATE_LIMIT_STORE_PROVIDER"
    )
    #: Shared-store connection. ``redis://`` / ``rediss://`` in deployments;
    #: ``fakeredis://<name>`` is an in-process emulator for tests and refused in prod.
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    #: ``chained_file`` is the tamper-evident sink (HMAC hash chain); required in prod.
    audit_store_provider: Literal["inmemory", "file", "chained_file", "chained_memory"] = Field(
        default="inmemory", alias="AUDIT_STORE_PROVIDER"
    )
    audit_file_path: str = Field(default="var/audit/audit.log", alias="AUDIT_FILE_PATH")
    #: HMAC key for the audit hash chain. Held by the verifier, not only the writer.
    audit_chain_secret: str | None = Field(default=None, alias="AUDIT_CHAIN_SECRET")
    cache_provider: Literal["inmemory", "disabled"] = Field(default="inmemory", alias="CACHE_PROVIDER")
    session_ttl_seconds: int = Field(default=3_600, ge=60, alias="SESSION_TTL_SECONDS")
    conversation_retention_days: int = Field(default=30, ge=1, alias="CONVERSATION_RETENTION_DAYS")
    audit_retention_days: int = Field(default=2_555, ge=1, alias="AUDIT_RETENTION_DAYS")

    # ------------------------------------------------------ observability ---
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_service_name: str = Field(default="protec-insurance-ai", alias="OTEL_SERVICE_NAME")
    otel_exporter_otlp_endpoint: str | None = Field(default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0, alias="TRACE_SAMPLE_RATE")
    audit_fail_closed: bool = Field(default=True, alias="AUDIT_FAIL_CLOSED")
    #: Upper bound on per-process Harness execution records kept for FinOps/evals (§35).
    harness_record_retention: int = Field(default=5_000, ge=100, alias="HARNESS_RECORD_RETENTION")

    # ------------------------------------------------------------ privacy ---
    #: Keyed pseudonymisation of subject/conversation identifiers in logs, audit and
    #: rate-limit keys. Must be shared by every instance so one customer has one
    #: pseudonym across pods and restarts (§10.2, §22). Required in preprod/prod.
    pseudonym_secret: str | None = Field(default=None, alias="PSEUDONYM_SECRET")

    # --------------------------------------------------------- guardrails ---
    guardrail_input_max_chars: int = Field(default=4_000, ge=64, alias="GUARDRAIL_INPUT_MAX_CHARS")
    guardrail_injection_enabled: bool = Field(default=True, alias="GUARDRAIL_INJECTION_ENABLED")
    guardrail_injection_block_score: float = Field(
        default=0.6, ge=0.0, le=1.0, alias="GUARDRAIL_INJECTION_BLOCK_SCORE"
    )
    guardrail_output_scan_enabled: bool = Field(default=True, alias="GUARDRAIL_OUTPUT_SCAN_ENABLED")
    guardrail_grounding_min_score: float = Field(
        default=0.35, ge=0.0, le=1.0, alias="GUARDRAIL_GROUNDING_MIN_SCORE"
    )
    guardrail_toxicity_enabled: bool = Field(default=True, alias="GUARDRAIL_TOXICITY_ENABLED")

    # ------------------------------------------------------ confirmation ---
    #: HMAC key for server-issued confirmation tokens (§8, M-2). Shared across
    #: instances; required in production. Absent -> per-process random key (local only).
    confirmation_token_secret: str | None = Field(default=None, alias="CONFIRMATION_TOKEN_SECRET")
    #: A side-effecting transition is *reserved* (state version bumped) before its
    #: provider call runs, so concurrent submits cannot multiply the side effect (§8).
    #: A reservation older than this is treated as abandoned and may be retaken.
    workflow_reservation_ttl_seconds: int = Field(
        default=60, ge=5, le=600, alias="WORKFLOW_RESERVATION_TTL_SECONDS"
    )

    # ------------------------------------------------------ rate limiting ---
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_ai_per_minute: int = Field(default=20, ge=1, alias="RATE_LIMIT_AI_PER_MINUTE")
    rate_limit_deterministic_per_minute: int = Field(
        default=240, ge=1, alias="RATE_LIMIT_DETERMINISTIC_PER_MINUTE"
    )
    rate_limit_public_per_minute: int = Field(default=10, ge=1, alias="RATE_LIMIT_PUBLIC_PER_MINUTE")
    rate_limit_burst_multiplier: float = Field(default=1.5, ge=1.0, alias="RATE_LIMIT_BURST_MULTIPLIER")

    # ------------------------------------------------ context / token gate ---
    max_context_tokens: int = Field(default=3_500, ge=256, alias="MAX_CONTEXT_TOKENS")
    max_history_tokens: int = Field(default=600, ge=0, alias="MAX_HISTORY_TOKENS")
    max_history_turns: int = Field(default=4, ge=0, alias="MAX_HISTORY_TURNS")
    max_retrieval_tokens: int = Field(default=1_400, ge=0, alias="MAX_RETRIEVAL_TOKENS")
    max_tool_result_tokens: int = Field(default=400, ge=0, alias="MAX_TOOL_RESULT_TOKENS")
    max_output_tokens: int = Field(default=500, ge=16, alias="MAX_OUTPUT_TOKENS")
    max_agent_steps: int = Field(default=3, ge=1, le=10, alias="MAX_AGENT_STEPS")
    max_model_calls_per_request: int = Field(default=2, ge=0, le=10, alias="MAX_MODEL_CALLS_PER_REQUEST")
    max_tool_calls_per_request: int = Field(default=4, ge=0, le=20, alias="MAX_TOOL_CALLS_PER_REQUEST")

    # --------------------------------------------------------- latency SLO ---
    slo_deterministic_p95_ms: int = Field(default=300, ge=1, alias="SLO_DETERMINISTIC_P95_MS")
    slo_cached_faq_p95_ms: int = Field(default=1_000, ge=1, alias="SLO_CACHED_FAQ_P95_MS")
    slo_faq_p95_ms: int = Field(default=3_000, ge=1, alias="SLO_FAQ_P95_MS")
    slo_first_token_p95_ms: int = Field(default=1_500, ge=1, alias="SLO_FIRST_TOKEN_P95_MS")
    slo_extraction_p95_ms: int = Field(default=1_500, ge=1, alias="SLO_EXTRACTION_P95_MS")

    # ------------------------------------------------------------- finops ---
    max_cost_per_1000_faq: float = Field(default=25.0, ge=0.0, alias="MAX_COST_PER_1000_FAQ")
    max_cost_per_1000_mixed_requests: float = Field(
        default=15.0, ge=0.0, alias="MAX_COST_PER_1000_MIXED_REQUESTS"
    )
    max_cost_per_completed_transaction: float = Field(
        default=0.10, ge=0.0, alias="MAX_COST_PER_COMPLETED_TRANSACTION"
    )

    # -------------------------------------------------------------- cache ---
    faq_cache_enabled: bool = Field(default=True, alias="FAQ_CACHE_ENABLED")
    faq_cache_ttl_seconds: int = Field(default=900, ge=1, alias="FAQ_CACHE_TTL_SECONDS")
    faq_cache_max_entries: int = Field(default=512, ge=1, alias="FAQ_CACHE_MAX_ENTRIES")

    # ------------------------------------------------------ feature flags ---
    feature_public_faq_enabled: bool = Field(default=True, alias="FEATURE_PUBLIC_FAQ_ENABLED")
    feature_travel_domain_enabled: bool = Field(default=True, alias="FEATURE_TRAVEL_DOMAIN_ENABLED")
    feature_agentic_escalation_enabled: bool = Field(default=True, alias="FEATURE_AGENTIC_ESCALATION_ENABLED")
    feature_streaming_enabled: bool = Field(default=False, alias="FEATURE_STREAMING_ENABLED")
    #: Text-only channel adapter (WhatsApp-style rendering of UI directives) (§45).
    feature_whatsapp_channel_enabled: bool = Field(default=True, alias="FEATURE_WHATSAPP_CHANNEL_ENABLED")

    # ------------------------------------------------------------ helpers ---
    @property
    def allowed_algorithms(self) -> list[str]:
        return [a.strip() for a in self.jwt_allowed_algorithms.split(",") if a.strip()]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PROD

    @property
    def is_hardened(self) -> bool:
        """Preprod mirrors production (§15.2): the same startup refusals apply to both."""
        return self.app_env in (AppEnv.PREPROD, AppEnv.PROD)

    @property
    def allows_mock_providers(self) -> bool:
        return self.app_env in (AppEnv.LOCAL, AppEnv.DEV, AppEnv.TEST)

    @property
    def uses_insuremo(self) -> bool:
        return any(value == "insuremo" for value in self.configured_providers().values())

    def configured_providers(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in CRITICAL_PROVIDER_FIELDS}

    def configured_state_stores(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in CRITICAL_STATE_STORE_FIELDS}

    # --------------------------------------------------------- validators ---
    @field_validator("jwt_allowed_algorithms")
    @classmethod
    def _reject_none_algorithm(cls, value: str) -> str:
        algorithms = [a.strip() for a in value.split(",") if a.strip()]
        if not algorithms:
            raise ValueError("JWT_ALLOWED_ALGORITHMS must list at least one algorithm")
        for algorithm in algorithms:
            if algorithm.lower() in {"none", "null"}:
                raise ValueError("alg=none is never an acceptable JWT algorithm")
        return ",".join(algorithms)

    @property
    def uses_redis(self) -> bool:
        return any(value == "redis" for value in self.configured_state_stores().values())

    @model_validator(mode="after")
    def _validate_environment_invariants(self) -> Settings:
        """Fail fast on unsafe production configuration (§14, §15.3)."""
        if self.jwt_dev_hs256_secret and self.app_env not in (
            AppEnv.LOCAL,
            AppEnv.DEV,
            AppEnv.TEST,
        ):
            raise ValueError("JWT_DEV_HS256_SECRET is only permitted in local/dev/test")
        if self.uses_redis and not self.redis_url:
            raise ValueError("REDIS_URL is required when a store provider is redis")
        if self.audit_store_provider.startswith("chained") and not self.audit_chain_secret:
            raise ValueError("AUDIT_CHAIN_SECRET is required for a chained audit sink")
        if not self.is_hardened:
            # Below preprod an InsureMO adapter without a base URL is a *deliberate*
            # structural configuration: every call raises InsureMoContractNotConfigured
            # rather than guessing an endpoint, which is how the contract suite proves
            # substitution without a sandbox (§9.2).
            return self
        if self.uses_insuremo and not self.insuremo_base_url:
            raise ValueError(
                f"{self.app_env.value} startup refused: INSUREMO_BASE_URL is required "
                "when a provider is insuremo"
            )

        env = self.app_env.value
        refused = f"{env} startup refused: "
        mocked = [name for name, value in self.configured_providers().items() if value == MOCK_PROVIDER]
        if mocked:
            raise ValueError(refused + "mock providers configured for " + ", ".join(sorted(mocked)))
        if self.model_provider == "deterministic":
            raise ValueError(refused + "MODEL_PROVIDER=deterministic is a test double")
        if self.model_fallback_provider == "deterministic":
            raise ValueError(refused + "MODEL_FALLBACK_PROVIDER=deterministic is a test double")
        if not (self.jwt_jwks_uri or self.jwt_public_key_pem):
            raise ValueError(refused + "JWT_JWKS_URI or JWT_PUBLIC_KEY_PEM is required")
        if self.jwt_jwks_uri and not self.jwt_jwks_uri.lower().startswith("https://"):
            raise ValueError(refused + "JWT_JWKS_URI must use https")
        if self.jwt_issuer == "https://auth.local.protec.example/":
            raise ValueError(refused + "JWT_ISSUER is still the local default")
        if any(alg.upper().startswith("HS") for alg in self.allowed_algorithms):
            raise ValueError(refused + "symmetric JWT algorithms are not permitted")
        if self.debug_endpoints_enabled:
            raise ValueError(refused + "DEBUG_ENDPOINTS_ENABLED must be false")
        if self.log_level == "DEBUG":
            raise ValueError(refused + "LOG_LEVEL=DEBUG is not permitted")
        if not self.rate_limit_enabled:
            raise ValueError(refused + "rate limiting must be enabled")
        if any(origin == "*" or origin.endswith("://*") or origin == "null" for origin in self.cors_origins):
            raise ValueError(refused + "wildcard or null CORS origin is not permitted")
        local_stores = [n for n, v in self.configured_state_stores().items() if v == IN_MEMORY_STORE]
        if local_stores:
            raise ValueError(
                refused
                + "in-memory critical state stores cannot serve more than one instance: "
                + ", ".join(sorted(local_stores))
            )
        for name, value in (
            ("CONFIRMATION_TOKEN_SECRET", self.confirmation_token_secret),
            ("AUDIT_CHAIN_SECRET", self.audit_chain_secret),
            ("PSEUDONYM_SECRET", self.pseudonym_secret),
        ):
            if not value:
                raise ValueError(refused + f"{name} is required so every instance derives the same values")
            if len(value) < MIN_SECRET_LENGTH:
                raise ValueError(refused + f"{name} must be at least {MIN_SECRET_LENGTH} characters")
        if self.redis_url and self.redis_url.startswith("fakeredis://"):
            raise ValueError(refused + "fakeredis is a test double, not a store")
        if self.is_production and self.redis_url and not self.redis_url.startswith("rediss://"):
            raise ValueError(refused + "REDIS_URL must use TLS (rediss://) in production")
        if self.audit_store_provider != "chained_file":
            raise ValueError(refused + "AUDIT_STORE_PROVIDER must be the tamper-evident chained_file sink")
        if not self.audit_fail_closed:
            raise ValueError(refused + "AUDIT_FAIL_CLOSED must be true")
        if not (self.guardrail_injection_enabled and self.guardrail_output_scan_enabled):
            raise ValueError(refused + "guardrail injection and output scanning must stay enabled")
        if self.rag_allow_draft_sources:
            raise ValueError(refused + "RAG_ALLOW_DRAFT_SOURCES must be false")
        if self.otel_enabled and not self.otel_exporter_otlp_endpoint:
            raise ValueError(refused + "OTEL_EXPORTER_OTLP_ENDPOINT is required when OTEL_ENABLED is true")
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cleared in tests via reset_settings_cache)."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings; used by tests that mutate the environment."""
    get_settings.cache_clear()
