"""Centralized Harness Service - the AI runtime control plane (master prompt §5.7).

One public entry point, :meth:`HarnessService.execute`, for *all* AI-assisted and
agentic execution. Deterministic traffic never enters here (§5.7.1).

The Harness **orchestrates**; it does not implement every subsystem. It composes:

    AuthorizationService (PolicyEnforcementPoint)   ModelInputSanitizer
    ResourceScopeInterceptor                        GuardrailService
    TokenBudgetService                              ContextBuilder
    ToolRegistry                                    SchemaValidationService (PEP)
    GroundingService                                RetryFallbackPolicy
    AuditService                                    ObservabilityService

Agents stay thin: they contribute domain instructions and a task, and receive the
already-validated context. They do not re-implement any control above.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai.harness.context.budget import BudgetLedger, TokenBudgetService, govern
from app.ai.harness.context.builder import BuiltContext, ContextBuilder, ConversationTurn
from app.ai.harness.decisions import (
    HarnessExecutionRecord,
    HarnessOutcome,
    HarnessResult,
    ReasonCode,
)
from app.ai.harness.policies.grounding_policy import GroundingPolicy
from app.ai.harness.telemetry import EvidenceEmitter
from app.ai.models.provider import ModelInvoker
from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    AuthenticationRequiredError,
    BudgetExceededError,
    ForbiddenError,
    GuardrailBlockedError,
    ModelTimeoutError,
    ModelUnavailableError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging.structured import get_logger
from app.core.observability.metrics import (
    FALLBACK_TOTAL,
    GUARDRAIL_INTERVENTIONS_TOTAL,
    metrics,
)
from app.core.observability.tracing import tracer
from app.core.policy.enforcement import PolicyEnforcementPoint
from app.core.privacy.model_boundary import ModelInputSanitizer, ModelOutputScanner
from app.core.registry.capability import Capability, ExecutionMode, RiskLevel
from app.core.security.guardrails import (
    GUARDRAIL_POLICY_VERSION,
    GuardrailDecision,
    GuardrailService,
)
from app.rag.retrieval.grounding import GroundingAssessment, GroundingService
from app.rag.retrieval.retriever import RetrievalResult
from app.workflows.state.models import WorkflowState

logger = get_logger(__name__)


@dataclass(slots=True)
class CapabilityRequest:
    """What the caller asks the Harness to run."""

    capability_id: str
    user_message: str
    payload: dict[str, Any] = field(default_factory=dict)
    workflow_state: WorkflowState | None = None
    history: list[ConversationTurn] = field(default_factory=list)
    confirmed: bool = False
    #: Fields the current purpose genuinely needs in model context (§10.1).
    purpose_fields: frozenset[str] = frozenset()


@dataclass(slots=True)
class AgentExecutionContext:
    """Everything an agent is given. Controls are already applied."""

    request: CapabilityRequest
    ctx: RequestContext
    capability: Capability
    ledger: BudgetLedger
    invoker: ModelInvoker
    builder: ContextBuilder
    guardrails: GuardrailService
    grounding: GroundingService
    sanitizer: ModelInputSanitizer
    #: Populated by the agent so the Harness can record evidence centrally.
    built_context: BuiltContext | None = None
    #: The evidence the answer must be grounded in. For a capability that requires
    #: grounding the Harness assesses the final answer against this itself (§6.3).
    retrieval: RetrievalResult | None = None
    grounding_assessment: GroundingAssessment | None = None
    evidence_document_ids: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    result_category: str = "UNKNOWN"
    agent_id: str = "unknown"
    #: Injected by the Harness; agents never authorize a tool themselves (§5.7.3).
    tool_authorizer: Callable[[str], None] | None = None

    def authorize_tool(self, tool_name: str) -> None:
        """Route every tool request back through the central Harness policy."""
        if self.tool_authorizer is None:
            raise ForbiddenError("BLOCK_UNAUTHORIZED_TOOL")
        self.tool_authorizer(tool_name)


AgentHandler = Callable[[AgentExecutionContext], Awaitable[HarnessResult]]


class HarnessService:
    """Single control point for AI-assisted execution."""

    def __init__(
        self,
        *,
        pep: PolicyEnforcementPoint,
        sanitizer: ModelInputSanitizer,
        output_scanner: ModelOutputScanner,
        guardrails: GuardrailService,
        budgets: TokenBudgetService,
        context_builder: ContextBuilder,
        grounding: GroundingService,
        invoker: ModelInvoker,
        audit: AuditService,
        prompt_version: str = "unknown",
        corpus_version_provider: Callable[[], str] = lambda: "unknown",
        environment: str = "local",
        record_retention: int = 5_000,
    ) -> None:
        self._pep = pep
        self._sanitizer = sanitizer
        self._output_scanner = output_scanner
        self._guardrails = guardrails
        self._budgets = budgets
        self._builder = context_builder
        self._grounding = grounding
        self._grounding_policy = GroundingPolicy(grounding)
        self._invoker = invoker
        self._audit = audit
        self._evidence = EvidenceEmitter(audit)
        self._prompt_version = prompt_version
        self._corpus_version = corpus_version_provider
        self._environment = environment
        self._handlers: dict[str, AgentHandler] = {}
        #: Recent records produced by this process, bounded (§19); the FinOps reporter and
        #: evals read it. The durable copy of every record is the audit sink (§22, §23).
        self.execution_records: deque[HarnessExecutionRecord] = deque(maxlen=record_retention)

    # -------------------------------------------------------- registration ---
    def register_handler(self, capability_id: str, handler: AgentHandler) -> None:
        """Bind a capability to the agent/handler that performs it."""
        if capability_id in self._handlers:
            raise ValueError(f"duplicate harness handler: {capability_id}")
        self._handlers[capability_id] = handler

    def registered_capabilities(self) -> frozenset[str]:
        return frozenset(self._handlers)

    # ------------------------------------------------------------- execute ---
    async def execute(self, ctx: RequestContext, request: CapabilityRequest) -> HarnessResult:
        """The single public Harness interface (§5.7.2).

        Runs the lifecycle in §5.7.6, skipping stages that do not apply so latency is
        not spent on irrelevant work.
        """
        started = time.perf_counter()
        record = HarnessExecutionRecord(
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            capability_id=request.capability_id,
            execution_mode="UNKNOWN",
            model_id=self._invoker.model_id,
            workflow_state=request.workflow_state.state if request.workflow_state else None,
            prompt_version=self._prompt_version,
            knowledge_corpus_version=self._corpus_version(),
            guardrail_policy_version=GUARDRAIL_POLICY_VERSION,
            environment=self._environment,
        )

        with tracer.span("harness.execute", capability=request.capability_id) as span:
            try:
                result = await self._run(ctx, request, record)
            except (ForbiddenError, AuthenticationRequiredError) as exc:
                result = self._blocked(record, ReasonCode.BLOCK_AUTHORIZATION, exc.reason)
            except RateLimitedError as exc:
                # A rate limit is transport-level (§31): it must surface as HTTP 429 with
                # Retry-After so clients and gateways back off, not as a chat message.
                self._blocked(record, ReasonCode.BLOCK_RATE_LIMIT, exc.reason)
                record.latency_ms = (time.perf_counter() - started) * 1000
                self.execution_records.append(record)
                await self._audit.record(
                    ctx,
                    AuditAction.RATE_LIMITED,
                    AuditResult.BLOCKED,
                    reason_code=exc.reason,
                    attributes={"capabilityId": request.capability_id},
                )
                raise
            except BudgetExceededError as exc:
                reason = (
                    ReasonCode(exc.reason)
                    if exc.reason in ReasonCode.__members__.values()
                    else ReasonCode.BLOCK_TOKEN_BUDGET
                )
                result = self._blocked(record, reason, exc.reason)
            except GuardrailBlockedError as exc:
                result = self._blocked(record, ReasonCode.BLOCK_PII_POLICY, exc.reason)
            except ValidationError as exc:
                result = self._blocked(record, ReasonCode.BLOCK_OUTPUT_SCHEMA_INVALID, exc.reason)
            except (ModelTimeoutError, ModelUnavailableError) as exc:
                metrics.increment(
                    FALLBACK_TOTAL,
                    labels={"kind": "model", "capability": request.capability_id},
                )
                record.fallback_decision = ReasonCode.FALLBACK_MODEL_UNAVAILABLE.value
                result = HarnessResult(
                    HarnessOutcome.FALLBACK,
                    ReasonCode.FALLBACK_MODEL_UNAVAILABLE,
                    "The assistant is temporarily unavailable. Your details have been kept safe.",
                    record=record,
                )
                record.outcome = HarnessOutcome.FALLBACK
                record.reason_code = ReasonCode.FALLBACK_MODEL_UNAVAILABLE
                record.result_category = "FALLBACK"
                logger.warning(
                    "harness_model_fallback",
                    extra={"capabilityId": request.capability_id, "errorType": exc.code.value},
                )
            except Exception as exc:
                # Any other failure (an application error or a defect in a handler) still
                # leaves evidence behind (§22): the record is emitted before re-raising, so
                # an auditor never finds a request with no AI execution trail.
                record.latency_ms = (time.perf_counter() - started) * 1000
                record.outcome = HarnessOutcome.BLOCK
                record.reason_code = ReasonCode.BLOCK_INTERNAL_ERROR
                record.result_category = "ERROR"
                record.error_type = type(exc).__name__
                self.execution_records.append(record)
                await self._emit_evidence(ctx, record)
                raise

            record.latency_ms = (time.perf_counter() - started) * 1000
            span.attributes["outcome"] = record.outcome.value
            span.attributes["reason"] = record.reason_code.value
            span.attributes["model.calls"] = record.model_calls

        self.execution_records.append(record)
        await self._emit_evidence(ctx, record)
        result.record = record
        return result

    # ------------------------------------------------------------ lifecycle ---
    async def _run(
        self, ctx: RequestContext, request: CapabilityRequest, record: HarnessExecutionRecord
    ) -> HarnessResult:
        # 1-4: capability resolution, auth, authorization, workflow-state preconditions,
        # rate/environment/channel checks, and input schema validation - all in the PEP.
        decision = await self._pep.authorize(
            ctx,
            request.capability_id,
            request.payload,
            workflow_state=request.workflow_state.state if request.workflow_state else None,
            confirmed=request.confirmed,
            requester="harness",
        )
        capability = decision.capability
        record.execution_mode = capability.execution_mode.value

        if capability.execution_mode is ExecutionMode.DETERMINISTIC:
            # Deterministic traffic must not reach the Harness (§5.7.1). This is a
            # structural assertion, not a suggestion.
            raise ForbiddenError("BLOCK_DETERMINISTIC_CAPABILITY_IN_HARNESS")

        handler = self._handlers.get(capability.id)
        if handler is None:
            raise ValidationError("no_handler_registered", details={"capabilityId": capability.id})

        # 7: input guardrails (applies to the user message, before any model sees it).
        guard = self._guardrails.check_input(request.user_message)
        record.guardrail_decision = guard.decision.value
        if guard.blocked:
            metrics.increment(
                GUARDRAIL_INTERVENTIONS_TOTAL,
                labels={"stage": "harness_input", "reason": guard.reason.value},
            )
            await self._audit.record(
                ctx,
                AuditAction.GUARDRAIL_BLOCKED,
                AuditResult.BLOCKED,
                reason_code=guard.reason.value,
                attributes={"capabilityId": capability.id, "signals": guard.signals},
            )
            reason = (
                ReasonCode.BLOCK_PROMPT_INJECTION
                if guard.reason.value == "BLOCK_PROMPT_INJECTION"
                else ReasonCode.BLOCK_GUARDRAIL_INPUT
            )
            return self._blocked(record, reason, guard.reason.value)

        # 8: budgets.
        ledger = self._budgets.ledger_for(
            model_call_budget=capability.budgets.model_call_budget,
            token_budget=capability.budgets.token_budget,
            agent_step_budget=capability.budgets.agent_step_budget,
            tool_call_budget=capability.budgets.tool_call_budget,
        )

        agent_ctx = AgentExecutionContext(
            request=request,
            ctx=ctx,
            capability=capability,
            ledger=ledger,
            invoker=self._invoker,
            builder=self._builder,
            guardrails=self._guardrails,
            grounding=self._grounding,
            sanitizer=self._sanitizer,
        )
        # 10-11: tool requests are validated centrally, regardless of requesting agent.
        agent_ctx.tool_authorizer = lambda tool_name: self._authorize_tool(
            capability, ledger, agent_ctx, tool_name
        )

        try:
            # 9: invoke the agent/model. The ledger is bound for the duration, so every
            # model call the handler causes - by any path - is budget-checked and
            # charged by the invoker itself (§5.7.2, H-2).
            with govern(ledger):
                result = await handler(agent_ctx)

            # 12-14: output validation, grounding, leakage scanning - all central.
            result = self._validate_output(result, agent_ctx, record)
            self._finalize_record(record, agent_ctx, result)
            ledger.publish(capability.id)
            return result
        finally:
            # Whatever happened - including a budget or guardrail block raised mid-way -
            # the record reports the spend that actually occurred.
            self._copy_ledger(record, agent_ctx)

    def _authorize_tool(
        self,
        capability: Capability,
        ledger: BudgetLedger,
        agent_ctx: AgentExecutionContext,
        tool_name: str,
    ) -> None:
        self._pep.authorize_tool_request(capability, tool_name)
        ledger.check_tool_call()
        ledger.record_tool_call()
        agent_ctx.tools_used.append(tool_name)

    def _validate_output(
        self,
        result: HarnessResult,
        agent_ctx: AgentExecutionContext,
        record: HarnessExecutionRecord,
    ) -> HarnessResult:
        if result.outcome is not HarnessOutcome.ALLOW:
            return result

        # 12: structured-output validation against the capability contract.
        if result.payload:
            validated = self._pep.validate_output(agent_ctx.capability, result.payload)
            result.payload = validated.model_dump(mode="json")

        # 14: output leakage + prohibited claims, applied to every agent identically.
        # Security controls run before grounding so a prohibited claim is BLOCKED, not
        # merely abstained.
        out_guard = self._guardrails.check_output(result.message)
        if out_guard.blocked:
            return self._blocked(record, ReasonCode.BLOCK_GUARDRAIL_OUTPUT, out_guard.reason.value)
        if out_guard.decision is GuardrailDecision.SANITIZE and out_guard.sanitized_text:
            result.message = out_guard.sanitized_text

        scanned, findings = self._output_scanner.scan(result.message)
        if findings:
            result.message = scanned
            record.guardrail_decision = "SANITIZE"

        # 13: grounding is decided *here* for every capability that requires it. The
        # agent's opinion is not consulted: the policy re-assesses the final answer
        # against the evidence the agent supplied and abstains centrally when that
        # evidence is missing or does not support the claims (§6.3, §7).
        if agent_ctx.capability.requires_grounding:
            replaced = self._grounding_policy.enforce(result, agent_ctx, record)
            if replaced is not None:
                return replaced
        assessment = agent_ctx.grounding_assessment
        if assessment is not None:
            record.grounding_decision = assessment.decision.value
        return result

    @staticmethod
    def _copy_ledger(record: HarnessExecutionRecord, agent_ctx: AgentExecutionContext) -> None:
        """Copy measured consumption into the record. Idempotent; runs on every path."""
        ledger = agent_ctx.ledger
        record.agent_id = agent_ctx.agent_id
        record.model_calls = ledger.model_calls
        record.agent_steps = ledger.agent_steps
        record.agent_handoffs = ledger.agent_handoffs
        record.tool_calls = ledger.tool_calls
        record.input_tokens = ledger.input_tokens
        record.output_tokens = ledger.output_tokens
        record.cached_input_tokens = ledger.cached_input_tokens
        record.retrieval_tokens = ledger.retrieval_tokens
        record.history_tokens = ledger.history_tokens
        record.system_prompt_tokens = ledger.system_prompt_tokens
        record.tool_result_tokens = ledger.tool_result_tokens
        record.estimated_cost = ledger.estimated_cost
        record.tokens_estimated = ledger.estimated_token_calls > 0
        record.tools_used = list(dict.fromkeys(agent_ctx.tools_used))

    def _finalize_record(
        self,
        record: HarnessExecutionRecord,
        agent_ctx: AgentExecutionContext,
        result: HarnessResult,
    ) -> None:
        self._copy_ledger(record, agent_ctx)
        record.evidence_document_ids = list(dict.fromkeys(agent_ctx.evidence_document_ids))
        record.result_category = agent_ctx.result_category
        record.outcome = result.outcome
        record.reason_code = result.reason_code

    @staticmethod
    def _blocked(record: HarnessExecutionRecord, reason: ReasonCode, detail: str) -> HarnessResult:
        record.outcome = HarnessOutcome.BLOCK
        record.reason_code = reason
        record.result_category = "BLOCKED"
        if reason is ReasonCode.BLOCK_AUTHORIZATION:
            record.authorization_decision = "DENY"
        logger.info("harness_blocked", extra={"reasonCode": reason.value, "detail": detail})
        return HarnessResult(
            HarnessOutcome.BLOCK,
            reason,
            "I cannot help with that request here.",
            record=record,
        )

    async def _emit_evidence(self, ctx: RequestContext, record: HarnessExecutionRecord) -> None:
        """16: audit + explainability evidence, emitted centrally for every agent."""
        await self._evidence.emit(ctx, record)

    # ------------------------------------------------------------- helpers ---
    def allows_fallback(self, capability: Capability) -> bool:
        """High-risk business decisions never silently downgrade to a weaker model."""
        return capability.risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM)
