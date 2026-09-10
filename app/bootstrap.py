"""Composition root (master prompt §53: centralized dependency injection).

Everything is constructed exactly once, here, from validated configuration. Nothing
below this module reaches for a global: services receive their collaborators.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.agents.faq_agent import FaqAgent
from app.ai.agents.intent_agent import IntentAgent
from app.ai.agents.supervisor import SupervisorAgent
from app.ai.harness.context.budget import BudgetLimits, TokenBudgetService
from app.ai.harness.context.builder import ContextBuilder
from app.ai.harness.service import HarnessService
from app.ai.models.factory import build_model_invoker
from app.ai.models.provider import ModelInvoker, ScriptedResponder
from app.ai.prompts.registry import PromptRegistry
from app.ai.tools.authoritative import AuthoritativeDataTools, OwnershipResolverAdapter
from app.channels.adapters import ChannelRegistry, TextChannelAdapter, WebChannelAdapter
from app.core.audit.service import AuditService, AuditSink, build_audit_sink
from app.core.config.settings import Settings
from app.core.context.request_context import Channel
from app.core.errors.taxonomy import ConfigurationError
from app.core.logging.structured import configure_logging, get_logger
from app.core.observability.tracing import configure_tracing
from app.core.policy.enforcement import PolicyEnforcementPoint
from app.core.privacy.masking import MaskingService
from app.core.privacy.model_boundary import ModelInputSanitizer, ModelOutputScanner
from app.core.privacy.pseudonym import configure_pseudonymizer
from app.core.registry.capability import CapabilityRegistry
from app.core.resilience.policies import CircuitBreaker, ResiliencePolicy, RetryPolicy
from app.core.resource_scope.scope import ResourceScopeInterceptor
from app.core.security.cache import build_cache
from app.core.security.guardrails import GuardrailService
from app.core.security.rate_limit import RateLimiter, RateLimitStore, build_rate_limit_store
from app.core.storage.redis_client import RedisClients, build_redis_clients
from app.domains.contract import DomainRegistry
from app.domains.motor.directives import MotorDirectiveBuilder
from app.domains.motor.module import (
    CAPABILITY_MOTOR_START,
    CAPABILITY_MOTOR_WORKFLOW_ACTION,
    MotorDomainModule,
)
from app.domains.motor.services import MotorSalesService
from app.domains.motor.workflow import WORKFLOW_ID as MOTOR_WORKFLOW_ID
from app.domains.travel.module import (
    CAPABILITY_TRAVEL_ACTION,
    CAPABILITY_TRAVEL_START,
    TravelDirectiveBuilder,
    TravelDomainModule,
    TravelSalesService,
)
from app.domains.travel.workflow import WORKFLOW_ID as TRAVEL_WORKFLOW_ID
from app.integrations.contracts.providers import ProviderBundle
from app.integrations.factory import assert_no_mocks_in_production, build_provider_bundle
from app.integrations.mock.providers import FaultInjection
from app.orchestration.capabilities import (
    CAPABILITY_FAQ,
    CAPABILITY_INTENT,
    CAPABILITY_SUPERVISOR,
    platform_capabilities,
)
from app.orchestration.conversation import ConversationOrchestrator, DomainBinding
from app.orchestration.router import CapabilityRouter
from app.orchestration.session import ConversationStore, build_conversation_store
from app.rag.governance.documents import KnowledgeCorpus
from app.rag.ingestion.loader import KnowledgeIngestionService
from app.rag.retrieval.grounding import GroundingService
from app.rag.retrieval.retriever import HybridRetriever, LexicalOverlapReranker
from app.ui_directives.registry.registry import DirectiveRegistry
from app.workflows.engine.confirmation import ConfirmationService
from app.workflows.engine.definition import WorkflowRegistry
from app.workflows.engine.engine import WorkflowEngine
from app.workflows.engine.service import WorkflowService
from app.workflows.state.store import WorkflowStateStore, build_workflow_store

logger = get_logger(__name__)


@dataclass
class Container:
    """Every constructed service. Handed to the API layer as request state."""

    settings: Settings
    providers: ProviderBundle
    capability_registry: CapabilityRegistry
    workflow_registry: WorkflowRegistry
    domain_registry: DomainRegistry
    directive_registry: DirectiveRegistry
    audit: AuditService
    audit_sink: AuditSink
    rate_limiter: RateLimiter
    rate_limit_store: RateLimitStore
    scope: ResourceScopeInterceptor
    pep: PolicyEnforcementPoint
    masking: MaskingService
    sanitizer: ModelInputSanitizer
    output_scanner: ModelOutputScanner
    guardrails: GuardrailService
    budgets: TokenBudgetService
    context_builder: ContextBuilder
    grounding: GroundingService
    corpus: KnowledgeCorpus
    ingestion: KnowledgeIngestionService
    retriever: HybridRetriever
    prompts: PromptRegistry
    invoker: ModelInvoker
    harness: HarnessService
    workflow_engine: WorkflowEngine
    workflow_service: WorkflowService
    conversations: ConversationStore
    router: CapabilityRouter
    orchestrator: ConversationOrchestrator
    tools: AuthoritativeDataTools
    faults: FaultInjection
    workflow_store: WorkflowStateStore
    provider_policy: ResiliencePolicy
    confirmations: ConfirmationService
    channels: ChannelRegistry
    redis: RedisClients | None = None


def build_container(
    settings: Settings,
    *,
    responder: ScriptedResponder | None = None,
    faults: FaultInjection | None = None,
    providers: ProviderBundle | None = None,
) -> Container:
    """Construct the whole application graph from validated settings.

    ``providers`` lets tests and the recovery drill hand several in-process instances
    one provider double, mirroring the fact that the real System of Record is shared.
    Production never passes it: the bundle is always built from configuration.
    """
    configure_logging(
        service=settings.app_name,
        environment=settings.app_env.value,
        version=settings.app_version,
        level=settings.log_level,
    )
    configure_tracing(
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        exporter_endpoint=settings.otel_exporter_otlp_endpoint,
        sample_rate=settings.trace_sample_rate,
        environment=settings.app_env.value,
        app_version=settings.app_version,
    )
    # One keyed pseudonym per subject across every instance and restart (§10.2, §22).
    configure_pseudonymizer(settings.pseudonym_secret)

    # ---- integrations -----------------------------------------------------
    fault_injection = faults or FaultInjection()
    if providers is not None and settings.is_hardened:
        raise ConfigurationError("production_refuses_injected_providers")
    providers = providers or build_provider_bundle(settings, faults=fault_injection)

    # ---- shared state backend --------------------------------------------
    redis_clients = (
        build_redis_clients(settings.redis_url or "", key_prefix=settings.app_name)
        if settings.uses_redis
        else None
    )

    # ---- core services ----------------------------------------------------
    masking = MaskingService()
    audit_sink = build_audit_sink(
        settings.audit_store_provider, settings.audit_file_path, chain_secret=settings.audit_chain_secret
    )
    audit = AuditService(audit_sink, masker=masking, fail_closed=settings.audit_fail_closed)

    rate_limit_store = build_rate_limit_store(settings.rate_limit_store_provider, redis_clients)
    rate_limiter = RateLimiter(
        rate_limit_store,
        enabled=settings.rate_limit_enabled,
        ai_per_minute=settings.rate_limit_ai_per_minute,
        deterministic_per_minute=settings.rate_limit_deterministic_per_minute,
        public_per_minute=settings.rate_limit_public_per_minute,
        burst_multiplier=settings.rate_limit_burst_multiplier,
    )

    scope = ResourceScopeInterceptor(OwnershipResolverAdapter(providers))

    capability_registry = CapabilityRegistry()
    pep = PolicyEnforcementPoint(
        capability_registry, rate_limiter, scope, audit, environment=settings.app_env.value
    )

    sanitizer = ModelInputSanitizer(masking)
    output_scanner = ModelOutputScanner(masking)
    guardrails = GuardrailService(
        max_input_chars=settings.guardrail_input_max_chars,
        injection_enabled=settings.guardrail_injection_enabled,
        injection_block_score=settings.guardrail_injection_block_score,
        output_scan_enabled=settings.guardrail_output_scan_enabled,
        toxicity_enabled=settings.guardrail_toxicity_enabled,
        masker=masking,
    )
    budgets = TokenBudgetService(
        BudgetLimits(
            max_context_tokens=settings.max_context_tokens,
            max_history_tokens=settings.max_history_tokens,
            max_history_turns=settings.max_history_turns,
            max_retrieval_tokens=settings.max_retrieval_tokens,
            max_tool_result_tokens=settings.max_tool_result_tokens,
            max_output_tokens=settings.max_output_tokens,
            max_agent_steps=settings.max_agent_steps,
            max_model_calls=settings.max_model_calls_per_request,
            max_tool_calls=settings.max_tool_calls_per_request,
        )
    )
    context_builder = ContextBuilder(
        sanitizer, injection_block_score=settings.guardrail_injection_block_score
    )

    # ---- knowledge / RAG --------------------------------------------------
    corpus = KnowledgeCorpus()
    ingestion = KnowledgeIngestionService(
        corpus,
        max_chunk_tokens=settings.rag_max_chunk_tokens,
        lifecycle_state_path=settings.rag_lifecycle_state_path,
    )
    ingestion.ingest_directory(settings.rag_knowledge_dir)
    retriever = HybridRetriever(
        corpus,
        reranker=LexicalOverlapReranker() if settings.rag_reranker_enabled else None,
        top_k=settings.rag_top_k,
        candidate_k=settings.rag_candidate_k,
        min_score=settings.rag_min_score,
        max_retrieval_tokens=settings.max_retrieval_tokens,
    )
    grounding = GroundingService(min_evidence_score=settings.guardrail_grounding_min_score)

    # ---- model / prompts / harness ---------------------------------------
    prompts = PromptRegistry().load()
    invoker = build_model_invoker(settings, responder=responder)

    harness = HarnessService(
        pep=pep,
        sanitizer=sanitizer,
        output_scanner=output_scanner,
        guardrails=guardrails,
        budgets=budgets,
        context_builder=context_builder,
        grounding=grounding,
        invoker=invoker,
        audit=audit,
        prompt_version=prompts.combined_version(),
        corpus_version_provider=lambda: corpus.version,
        environment=settings.app_env.value,
        record_retention=settings.harness_record_retention,
    )

    faq_cache = build_cache(
        settings.cache_provider if settings.faq_cache_enabled else "disabled",
        settings.faq_cache_max_entries,
        settings.faq_cache_ttl_seconds,
    )
    faq_agent = FaqAgent(retriever, prompts, cache=faq_cache, cache_enabled=settings.faq_cache_enabled)
    intent_agent = IntentAgent(prompts)
    supervisor_agent = SupervisorAgent(prompts, frozenset({CAPABILITY_FAQ, CAPABILITY_MOTOR_START}))

    harness.register_handler(CAPABILITY_FAQ, faq_agent.handle)
    harness.register_handler(CAPABILITY_INTENT, intent_agent.handle)
    harness.register_handler(CAPABILITY_SUPERVISOR, supervisor_agent.handle)

    # ---- workflows / domains ---------------------------------------------
    workflow_registry = WorkflowRegistry()
    domain_registry = DomainRegistry()
    directive_registry = DirectiveRegistry()

    workflow_store = build_workflow_store(settings.workflow_store_provider, redis_clients)
    workflow_engine = WorkflowEngine(
        workflow_registry,
        workflow_store,
        audit,
        session_ttl_seconds=settings.session_ttl_seconds,
        reservation_ttl_seconds=settings.workflow_reservation_ttl_seconds,
    )
    workflow_service = WorkflowService(workflow_engine, audit)

    provider_policy = ResiliencePolicy(
        "provider",
        timeout_ms=settings.provider_timeout_ms,
        retry=RetryPolicy(max_attempts=settings.provider_max_retries + 1),
        breaker=CircuitBreaker(
            "provider",
            failure_threshold=settings.provider_circuit_failure_threshold,
            reset_seconds=settings.provider_circuit_reset_seconds,
        ),
    )

    capability_registry.register_all(platform_capabilities(public_faq=settings.feature_public_faq_enabled))

    motor = domain_registry.register(MotorDomainModule())
    _register_domain(capability_registry, workflow_registry, motor)
    motor_service = MotorSalesService(providers, audit, policy=provider_policy)
    workflow_service.register_service_action(MOTOR_WORKFLOW_ID, "create_quote", motor_service.create_quote)
    workflow_service.register_service_action(
        MOTOR_WORKFLOW_ID, "initiate_payment", motor_service.initiate_payment
    )
    workflow_service.register_service_action(MOTOR_WORKFLOW_ID, "issue_policy", motor_service.issue_policy)

    bindings: dict[str, DomainBinding] = {
        "motor": DomainBinding(
            domain="motor",
            workflow_id=MOTOR_WORKFLOW_ID,
            start_capability_id=CAPABILITY_MOTOR_START,
            action_capability_id=CAPABILITY_MOTOR_WORKFLOW_ACTION,
            directive_builder=MotorDirectiveBuilder(directive_registry),
        )
    }

    if settings.feature_travel_domain_enabled:
        travel = domain_registry.register(TravelDomainModule())
        _register_domain(capability_registry, workflow_registry, travel)
        travel_service = TravelSalesService(providers, audit, policy=provider_policy)
        workflow_service.register_service_action(
            TRAVEL_WORKFLOW_ID, "create_quote", travel_service.create_quote
        )
        bindings["travel"] = DomainBinding(
            domain="travel",
            workflow_id=TRAVEL_WORKFLOW_ID,
            start_capability_id=CAPABILITY_TRAVEL_START,
            action_capability_id=CAPABILITY_TRAVEL_ACTION,
            directive_builder=TravelDirectiveBuilder(directive_registry),
        )

    # ---- orchestration ----------------------------------------------------
    conversations = build_conversation_store(
        settings.session_store_provider, settings.session_ttl_seconds, redis_clients
    )
    confirmations = ConfirmationService(settings.confirmation_token_secret)
    router = CapabilityRouter(
        capability_registry,
        faq_capability_id=CAPABILITY_FAQ,
        intent_capability_id=CAPABILITY_INTENT,
        supervisor_capability_id=CAPABILITY_SUPERVISOR,
        purchase_capability_by_domain={d: b.start_capability_id for d, b in bindings.items()},
        agentic_escalation_enabled=settings.feature_agentic_escalation_enabled,
    )
    orchestrator = ConversationOrchestrator(
        router=router,
        harness=harness,
        workflow_service=workflow_service,
        conversations=conversations,
        directives=directive_registry,
        pep=pep,
        audit=audit,
        confirmations=confirmations,
        bindings=bindings,
        faq_capability_id=CAPABILITY_FAQ,
        intent_capability_id=CAPABILITY_INTENT,
        supervisor_capability_id=CAPABILITY_SUPERVISOR,
    )

    tools = AuthoritativeDataTools(providers, scope, audit, policy=provider_policy)

    # ---- channels ---------------------------------------------------------
    channels = ChannelRegistry()
    channels.register(WebChannelAdapter())
    if settings.feature_whatsapp_channel_enabled:
        channels.register(TextChannelAdapter(Channel.WHATSAPP))

    logger.info(
        "container_built",
        extra={
            "capabilities": len(capability_registry),
            "workflows": len(workflow_registry),
            "domains": domain_registry.domains(),
            "knowledgeDocuments": len(corpus),
            "corpusVersion": corpus.version,
            "modelProvider": settings.model_provider,
            "mockProviders": providers.is_mock,
        },
    )

    return Container(
        settings=settings,
        providers=providers,
        capability_registry=capability_registry,
        workflow_registry=workflow_registry,
        domain_registry=domain_registry,
        directive_registry=directive_registry,
        audit=audit,
        audit_sink=audit_sink,
        rate_limiter=rate_limiter,
        rate_limit_store=rate_limit_store,
        scope=scope,
        pep=pep,
        masking=masking,
        sanitizer=sanitizer,
        output_scanner=output_scanner,
        guardrails=guardrails,
        budgets=budgets,
        context_builder=context_builder,
        grounding=grounding,
        corpus=corpus,
        ingestion=ingestion,
        retriever=retriever,
        prompts=prompts,
        invoker=invoker,
        harness=harness,
        workflow_engine=workflow_engine,
        workflow_service=workflow_service,
        conversations=conversations,
        router=router,
        orchestrator=orchestrator,
        tools=tools,
        faults=fault_injection,
        workflow_store=workflow_store,
        provider_policy=provider_policy,
        confirmations=confirmations,
        channels=channels,
        redis=redis_clients,
    )


def _register_domain(capabilities: CapabilityRegistry, workflows: WorkflowRegistry, module: Any) -> None:
    """Register a domain's contributions. Core knows only the DomainModule protocol."""
    for definition in module.workflows():
        workflows.register(definition)
    capabilities.register_all(module.capabilities())


def validate_startup(settings: Settings) -> None:
    """Fail fast on unsafe configuration before serving traffic (§14, §15.3).

    ``Settings`` already refuses an unsafe production configuration at construction;
    this repeats the critical provider check as a defence in depth so the guard also
    holds when a Settings object is built by other means (e.g. a test fixture).
    """
    assert_no_mocks_in_production(settings)
    if settings.is_hardened and settings.model_provider == "deterministic":
        raise ConfigurationError("production_refuses_deterministic_model")
