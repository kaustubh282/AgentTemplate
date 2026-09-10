"""Conversation orchestration: the application entry point behind the API (§4).

Responsibilities:

* normalise the request and resolve conversation/flow state
* run the deterministic router first (§2.3)
* execute deterministic paths with **zero model calls**
* enter the Harness only when language understanding is genuinely required
* translate every outcome into a registered UI directive
* keep FAQ interruptions from mutating transactional state (§16, Demo D)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.harness.context.builder import ConversationTurn
from app.ai.harness.decisions import HarnessOutcome, HarnessResult, ReasonCode
from app.ai.harness.service import CapabilityRequest, HarnessService
from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext, new_id
from app.core.errors.taxonomy import (
    AppError,
    ErrorCode,
    FlowStateConflictError,
    ForbiddenError,
    ValidationError,
)
from app.core.logging.structured import get_logger
from app.core.observability.metrics import (
    ZERO_MODEL_CALL_REQUESTS_TOTAL,
    metrics,
)
from app.core.observability.tracing import tracer
from app.core.policy.enforcement import PolicyEnforcementPoint
from app.orchestration.router import CapabilityRouter, RouteDecision, RoutePath
from app.orchestration.session import Conversation, ConversationStore
from app.ui_directives.registry.registry import DirectiveRegistry, a11y
from app.ui_directives.schemas.directives import (
    AssistantResponse,
    Directive,
    DirectiveType,
    MessageSeverity,
    ResponseType,
    ShowErrorPayload,
    ShowFaqAnswerPayload,
    ShowHumanHandoffPayload,
    ShowMessagePayload,
    ShowRetryPayload,
    SourceCitation,
)
from app.workflows.engine.confirmation import ConfirmationService
from app.workflows.engine.service import WorkflowService
from app.workflows.state.models import WorkflowState

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DomainBinding:
    """What a domain contributes to conversation handling."""

    domain: str
    workflow_id: str
    start_capability_id: str
    action_capability_id: str
    directive_builder: Any


class ConversationOrchestrator:
    """The single application service the API routes call."""

    def __init__(
        self,
        *,
        router: CapabilityRouter,
        harness: HarnessService,
        workflow_service: WorkflowService,
        conversations: ConversationStore,
        directives: DirectiveRegistry,
        pep: PolicyEnforcementPoint,
        audit: AuditService,
        confirmations: ConfirmationService,
        bindings: dict[str, DomainBinding],
        faq_capability_id: str,
        intent_capability_id: str,
        supervisor_capability_id: str,
    ) -> None:
        self._router = router
        self._harness = harness
        self._workflows = workflow_service
        self._conversations = conversations
        self._directives = directives
        self._pep = pep
        self._audit = audit
        self._confirmations = confirmations
        self._bindings = bindings
        self._binding_by_capability = {
            cap: binding
            for binding in bindings.values()
            for cap in (binding.start_capability_id, binding.action_capability_id)
        }
        self._binding_by_workflow = {b.workflow_id: b for b in bindings.values()}
        self._faq = faq_capability_id
        self._intent = intent_capability_id
        self._supervisor = supervisor_capability_id

    def binding_for_workflow(self, workflow_id: str) -> DomainBinding:
        """The domain binding that owns a workflow (used by channel adapters)."""
        return self._binding_by_workflow[workflow_id]

    # ----------------------------------------------------------- lifecycle ---
    async def start_conversation(self, ctx: RequestContext) -> Conversation:
        conversation = Conversation(
            conversation_id=new_id("conv"),
            owner_subject_id=ctx.auth.subject_id,
            tenant_id=ctx.auth.tenant_id,
            channel=ctx.channel.value,
        )
        return await self._conversations.create(conversation)

    async def _open_conversation(self, ctx: RequestContext, conversation_id: str) -> Conversation:
        """Resolve a conversation *for this caller* - the C-1 ownership gate.

        Every chat, action and flow-start passes through here first. A conversation is
        bound to the authenticated subject (and tenant) that created it; any other
        subject presenting its id is refused with FORBIDDEN and audited, whether or not
        the conversation exists, so neither the transcript nor an in-flight journey can
        be read by supplying an id (§13.2, §16).
        """
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            if not ctx.auth.is_authenticated:
                # Anonymous callers share one subject, so a client-chosen id would let any
                # anonymous client read or extend another anonymous session (M-6/M-7). They
                # must use a server-minted id from POST /conversations or omit the id.
                await self._audit.record(
                    ctx,
                    AuditAction.AUTHORIZATION_DENIED,
                    AuditResult.DENIED,
                    resource_type="CONVERSATION",
                    reason_code="conversation_not_server_minted",
                )
                raise ForbiddenError("conversation_owner_mismatch")
            conversation = Conversation(
                conversation_id=conversation_id,
                owner_subject_id=ctx.auth.subject_id,
                tenant_id=ctx.auth.tenant_id,
                channel=ctx.channel.value,
            )
            return await self._conversations.create(conversation)

        owner_mismatch = conversation.owner_subject_id != ctx.auth.subject_id
        tenant_mismatch = (
            conversation.tenant_id is not None
            and ctx.auth.tenant_id is not None
            and conversation.tenant_id != ctx.auth.tenant_id
        )
        if owner_mismatch or tenant_mismatch:
            await self._audit.record(
                ctx,
                AuditAction.AUTHORIZATION_DENIED,
                AuditResult.DENIED,
                resource_type="CONVERSATION",
                reason_code="conversation_owner_mismatch",
            )
            raise ForbiddenError("conversation_owner_mismatch")
        return conversation

    # ---------------------------------------------------------------- chat ---
    async def handle_message(self, ctx: RequestContext, message: str) -> AssistantResponse:
        """Free-text entry point. Routes deterministically before any model."""
        with tracer.span("orchestrator.handle_message"):
            conversation_id = ctx.conversation_id
            if conversation_id is None:
                conversation_id = (await self.start_conversation(ctx)).conversation_id
            await self._open_conversation(ctx, conversation_id)
            active_flow = await self._workflows.engine.get_active(conversation_id, ctx)
            decision = self._router.route_message(message, active_flow=active_flow)

            if decision.is_deterministic:
                metrics.increment(ZERO_MODEL_CALL_REQUESTS_TOTAL, labels={"path": decision.path.value})
                response = await self._handle_deterministic_message(
                    ctx, conversation_id, decision, active_flow
                )
            else:
                response = await self._handle_ai_message(ctx, conversation_id, message, decision, active_flow)

            # A blocked turn is never remembered: replaying it as RECENT_TURNS would hand the
            # injected text to the model on the next request (H-5).
            if response.meta.get("outcome") != HarnessOutcome.BLOCK.value:
                await self._record_turns(conversation_id, message, response.message)
            response.meta.setdefault("routePath", decision.path.value)
            response.meta.setdefault("routeReason", decision.reason)
            return response

    async def _handle_deterministic_message(
        self,
        ctx: RequestContext,
        conversation_id: str,
        decision: RouteDecision,
        active_flow: WorkflowState | None,
    ) -> AssistantResponse:
        if decision.path is RoutePath.UI_ACTION and active_flow is not None:
            if decision.action == "CONTINUE":
                # Resume from persisted structured state - no transcript replay (§58.11).
                return self._flow_response(ctx, conversation_id, active_flow, replayed=True)

            allowed = self._workflows.engine.allowed_actions(active_flow)
            if decision.action not in allowed:
                # A navigation phrase that is not legal here is not an error: the state
                # machine stays authoritative and the user is shown what they can do,
                # rather than receiving a FLOW_STATE_CONFLICT for typing "back".
                return self._flow_response(ctx, conversation_id, active_flow, replayed=True)

            return await self.handle_action(
                ctx,
                self._binding_by_workflow[active_flow.workflow_id].action_capability_id,
                decision.action or "",
                {},
            )

        if decision.path is RoutePath.WORKFLOW_TRANSITION and decision.capability_id:
            return await self.start_flow(ctx, conversation_id, decision.capability_id)

        return self._message_response(
            ctx,
            conversation_id,
            "I can help with questions about our products, or start a purchase for you.",
        )

    async def _handle_ai_message(
        self,
        ctx: RequestContext,
        conversation_id: str,
        message: str,
        decision: RouteDecision,
        active_flow: WorkflowState | None,
    ) -> AssistantResponse:
        conversation = await self._conversations.get(conversation_id)
        history = list(conversation.turns) if conversation else []

        payload: dict[str, Any] = (
            {"question": message} if decision.capability_id == self._faq else {"message": message}
        )
        if decision.capability_id == self._faq and active_flow is not None:
            payload["domain"] = self._binding_by_workflow[active_flow.workflow_id].domain

        # An FAQ interruption receives *only* FAQ-relevant context. The workflow state
        # is not passed in and cannot be mutated by this path (§58.11, Demo D).
        include_state = decision.capability_id != self._faq

        result = await self._harness.execute(
            ctx.child(conversation_id=conversation_id),
            CapabilityRequest(
                capability_id=decision.capability_id or self._supervisor,
                user_message=message,
                payload=payload,
                workflow_state=active_flow if include_state else None,
                history=history,
            ),
        )

        if decision.capability_id == self._faq:
            return self._faq_response(ctx, conversation_id, result, active_flow)
        return await self._routed_response(ctx, conversation_id, result, active_flow)

    # -------------------------------------------------------------- actions ---
    async def start_flow(
        self, ctx: RequestContext, conversation_id: str, capability_id: str
    ) -> AssistantResponse:
        """Deterministic flow start. Zero model calls."""
        binding = self._binding_by_capability.get(capability_id)
        if binding is None:
            raise ValidationError("unknown_flow_capability", details={"capabilityId": capability_id})

        await self._open_conversation(ctx, conversation_id)
        await self._pep.authorize(
            ctx.child(conversation_id=conversation_id),
            capability_id,
            {"action": "BEGIN", "payload": {}},
            requester="router",
        )
        outcome = await self._workflows.engine.start(
            ctx.child(conversation_id=conversation_id),
            binding.workflow_id,
            conversation_id=conversation_id,
        )
        metrics.increment(ZERO_MODEL_CALL_REQUESTS_TOTAL, labels={"path": "flow_start"})
        return self._flow_response(ctx, conversation_id, outcome.state, replayed=outcome.replayed)

    async def handle_action(
        self,
        ctx: RequestContext,
        capability_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        confirmed: bool = False,
        confirmation_token: str | None = None,
    ) -> AssistantResponse:
        """Structured UI action. Deterministic, allow-listed, zero model calls (§59.1).

        ``confirmed`` is only honoured together with the server-issued token for this
        exact flow, action and state version (§8, M-2). A client that always sends
        ``confirmed=true`` satisfies nothing.
        """
        with tracer.span("orchestrator.handle_action", action=action):
            conversation_id = ctx.conversation_id or new_id("conv")
            binding = self._binding_by_capability.get(capability_id)
            if binding is None:
                raise ValidationError("unknown_action_capability", details={"capabilityId": capability_id})

            await self._open_conversation(ctx, conversation_id)
            state = await self._workflows.engine.get_active(conversation_id, ctx)
            if state is None:
                raise FlowStateConflictError("no_active_flow")
            # Another request holds this flow's transition reservation: report the race as
            # a conflict (retry later), not as a forged confirmation token (§8, H-1).
            self._workflows.engine.assert_not_reserved(state, ctx)

            if (
                confirmed
                and not self._workflows.engine.is_idempotent_replay(state, ctx, action)
                and not self._confirmations.verify(confirmation_token, state.flow_id, action, state.version)
            ):
                await self._audit.record(
                    ctx,
                    AuditAction.AUTHORIZATION_DENIED,
                    AuditResult.DENIED,
                    resource_type="WORKFLOW",
                    reason_code="BLOCK_CONFIRMATION_TOKEN_INVALID",
                    attributes={"action": action},
                )
                raise ForbiddenError("BLOCK_CONFIRMATION_TOKEN_INVALID", details={"action": action})

            scoped = ctx.child(conversation_id=conversation_id)
            await self._pep.authorize(
                scoped,
                capability_id,
                {"action": action, "payload": payload},
                workflow_state=state.state,
                confirmed=confirmed,
                requester="ui_action",
            )

            result = await self._workflows.execute(scoped, state, action, payload, confirmed=confirmed)
            metrics.increment(ZERO_MODEL_CALL_REQUESTS_TOTAL, labels={"path": "ui_action"})

            if result.service_failed and result.error is not None:
                # The reservation was released; the returned state carries the new version.
                return self._retry_response(ctx, conversation_id, action, result.error, result.outcome.state)

            return self._flow_response(
                ctx, conversation_id, result.outcome.state, replayed=result.outcome.replayed
            )

    # ------------------------------------------------------------ responses ---
    def _flow_response(
        self,
        ctx: RequestContext,
        conversation_id: str,
        state: WorkflowState,
        *,
        replayed: bool = False,
    ) -> AssistantResponse:
        binding = self._binding_by_workflow[state.workflow_id]
        directive = binding.directive_builder.for_state(state.state, state.data)
        meta: dict[str, Any] = {
            "flowId": state.flow_id,
            "workflowId": state.workflow_id,
            "workflowVersion": state.workflow_version,
            "state": state.state,
            "flowVersion": state.version,
            "modelCalls": 0,
            "replayed": replayed,
        }
        # Server-issued evidence that the user was shown *this* review and confirmed it:
        # one token per confirmation-requiring action, bound to flow + action + version.
        tokens = {
            action: self._confirmations.issue(state.flow_id, action, state.version)
            for action in self._workflows.engine.confirmation_required_actions(state)
        }
        if tokens:
            meta["confirmationTokens"] = tokens
        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.UI_DIRECTIVE,
            message=self._flow_message(state.state),
            directive=directive,
            allowed_actions=list(self._workflows.engine.allowed_actions(state)),
            meta=meta,
        )

    def _faq_response(
        self,
        ctx: RequestContext,
        conversation_id: str,
        result: HarnessResult,
        active_flow: WorkflowState | None,
    ) -> AssistantResponse:
        payload = result.payload or {}
        citations = [
            SourceCitation(
                document_id=c["document_id"],
                document_name=c["document_name"],
                version=c["version"],
                section=c.get("section") or None,
                effective_date=c.get("effective_date"),
            )
            for c in payload.get("citations", [])
        ]
        directive = self._directives.build(
            DirectiveType.SHOW_FAQ_ANSWER,
            ShowFaqAnswerPayload(
                answer=result.message,
                verification=str(payload.get("verification", "UNSUPPORTED")),
                citations=citations,
                accessibility=a11y("Answer to your question", role="region"),
            ),
        )
        allowed: list[str] = []
        if active_flow is not None:
            # The transaction is untouched; the user can continue where they left off.
            allowed = ["CONTINUE", *self._workflows.engine.allowed_actions(active_flow)]

        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.UI_DIRECTIVE,
            message=result.message,
            directive=directive,
            allowed_actions=allowed,
            meta=self._ai_meta(result, active_flow),
        )

    async def _routed_response(
        self,
        ctx: RequestContext,
        conversation_id: str,
        result: HarnessResult,
        active_flow: WorkflowState | None,
    ) -> AssistantResponse:
        """Turn an intent/supervisor decision into the next deterministic step."""
        if result.outcome is HarnessOutcome.ALLOW:
            payload = result.payload or {}
            intent = str(payload.get("intent", ""))
            capability = str(payload.get("capability", ""))

            if intent in ("START_PURCHASE",) or capability in self._binding_by_capability:
                target = capability if capability in self._binding_by_capability else None
                if target is None:
                    domain = str(payload.get("entities", {}).get("domain") or "motor")
                    binding = self._bindings.get(domain) or self._bindings["motor"]
                    target = binding.start_capability_id
                return await self.start_flow(ctx, conversation_id, target)

            if intent == "CONTINUE_PURCHASE" and active_flow is not None:
                return self._flow_response(ctx, conversation_id, active_flow, replayed=True)

            if intent == "FAQ_QUESTION":
                # One extraction call resolved the ambiguity; now answer it grounded.
                faq_result = await self._harness.execute(
                    ctx.child(conversation_id=conversation_id),
                    CapabilityRequest(
                        capability_id=self._faq,
                        user_message=str(payload.get("entities", {}).get("question", "")) or ctx.request_id,
                        payload={"question": str(payload.get("original_message", "")) or " "},
                    ),
                )
                return self._faq_response(ctx, conversation_id, faq_result, active_flow)

        if result.outcome is HarnessOutcome.ESCALATE:
            return self._handoff_response(ctx, conversation_id, result, active_flow)

        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.MESSAGE,
            message=result.message,
            directive=self._directives.build(
                DirectiveType.SHOW_MESSAGE,
                ShowMessagePayload(
                    body=result.message,
                    severity=MessageSeverity.INFO,
                    accessibility=a11y("Assistant message", role="status"),
                ),
            ),
            allowed_actions=list(self._workflows.engine.allowed_actions(active_flow)) if active_flow else [],
            meta=self._ai_meta(result, active_flow),
        )

    def _handoff_response(
        self,
        ctx: RequestContext,
        conversation_id: str,
        result: HarnessResult,
        active_flow: WorkflowState | None,
    ) -> AssistantResponse:
        if result.reason_code is ReasonCode.ESCALATE_NEEDS_CLARIFICATION:
            directive: Directive = self._directives.build(
                DirectiveType.SHOW_MESSAGE,
                ShowMessagePayload(
                    body=result.message,
                    severity=MessageSeverity.INFO,
                    accessibility=a11y("Clarifying question", role="status"),
                ),
            )
        else:
            directive = self._directives.build(
                DirectiveType.SHOW_HUMAN_HANDOFF,
                ShowHumanHandoffPayload(
                    body=result.message,
                    handoff_reason=result.reason_code.value,
                    contact_channel="SUPPORT",
                    accessibility=a11y("Connect with our support team", role="region"),
                ),
            )
        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.UI_DIRECTIVE,
            message=result.message,
            directive=directive,
            allowed_actions=list(self._workflows.engine.allowed_actions(active_flow)) if active_flow else [],
            meta=self._ai_meta(result, active_flow),
        )

    def _retry_response(
        self,
        ctx: RequestContext,
        conversation_id: str,
        action: str,
        error: AppError,
        state: WorkflowState,
    ) -> AssistantResponse:
        """Provider failure: no fabricated result, state preserved, retry offered."""
        retry_action = "RETRY_QUOTE" if action.endswith("QUOTE") else action
        if retry_action not in self._workflows.engine.allowed_actions(state):
            retry_action = action
        directive = self._directives.build(
            DirectiveType.SHOW_RETRY,
            ShowRetryPayload(
                body=("We could not complete that step just now. Nothing has been lost - you can try again."),
                retry_action=retry_action,
                accessibility=a11y("Retry the previous step", role="alert", status_text="Action needed"),
            ),
        )
        meta: dict[str, Any] = {
            "flowId": state.flow_id,
            "state": state.state,
            "flowVersion": state.version,
            "errorCode": error.code.value,
            "retryable": error.retryable,
            "modelCalls": 0,
        }
        # Fresh tokens: the failed attempt moved the state version, so the tokens the
        # client holds are stale by design and the retry needs the current ones.
        tokens = {
            a: self._confirmations.issue(state.flow_id, a, state.version)
            for a in self._workflows.engine.confirmation_required_actions(state)
        }
        if tokens:
            meta["confirmationTokens"] = tokens
        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.UI_DIRECTIVE,
            message="We could not complete that step just now.",
            directive=directive,
            allowed_actions=list(self._workflows.engine.allowed_actions(state)),
            meta=meta,
        )

    def _message_response(self, ctx: RequestContext, conversation_id: str, text: str) -> AssistantResponse:
        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.MESSAGE,
            message=text,
            directive=self._directives.build(
                DirectiveType.SHOW_MESSAGE,
                ShowMessagePayload(
                    body=text,
                    accessibility=a11y("Assistant message", role="status"),
                ),
            ),
            meta={"modelCalls": 0},
        )

    def error_response(self, ctx: RequestContext, conversation_id: str, error: AppError) -> AssistantResponse:
        directive = self._directives.build(
            DirectiveType.SHOW_ERROR,
            ShowErrorPayload(
                error_code=error.code.value,
                body=error.safe_message,
                retryable=error.retryable,
                accessibility=a11y("Error", role="alert", status_text="Error"),
            ),
        )
        return AssistantResponse(
            conversation_id=conversation_id,
            request_id=ctx.request_id,
            response_type=ResponseType.ERROR,
            message=error.safe_message,
            directive=directive,
            meta={"errorCode": error.code.value, "retryable": error.retryable},
        )

    # -------------------------------------------------------------- helpers ---
    @staticmethod
    def _ai_meta(result: HarnessResult, active_flow: WorkflowState | None) -> dict[str, Any]:
        record = result.record
        meta: dict[str, Any] = {
            "outcome": result.outcome.value,
            "reasonCode": result.reason_code.value,
        }
        if record is not None:
            meta.update(
                {
                    "modelCalls": record.model_calls,
                    "agentSteps": record.agent_steps,
                    "agentHandoffs": record.agent_handoffs,
                    "inputTokens": record.input_tokens,
                    "outputTokens": record.output_tokens,
                    "latencyMs": round(record.latency_ms, 2),
                    "capabilityId": record.capability_id,
                }
            )
        if active_flow is not None:
            # Proof for the caller that the transaction was untouched by the FAQ turn.
            meta["flowStateUnchanged"] = active_flow.state
            meta["flowVersion"] = active_flow.version
        return meta

    @staticmethod
    def _flow_message(state: str) -> str:
        return {
            "ENTRY": "Let us get started.",
            "IDENTIFY_CUSTOMER": "What would you like to insure?",
            "COLLECT_DATA": "Please share a few details.",
            "COLLECT_TRIP": "Please share your trip details.",
            "VALIDATE": "Would you like to add any optional cover?",
            "QUOTE": "Here is your quote.",
            "REVIEW": "Please review before you continue.",
            "PAYMENT": "Please complete the payment to continue.",
            "COMPLETE": "All done.",
        }.get(state, "Here is the next step.")

    async def _record_turns(self, conversation_id: str, user_text: str, reply: str) -> None:
        await self._conversations.append_turn(conversation_id, ConversationTurn(role="user", text=user_text))
        if reply:
            await self._conversations.append_turn(
                conversation_id, ConversationTurn(role="assistant", text=reply)
            )


__all__ = ["ConversationOrchestrator", "DomainBinding", "ErrorCode"]
