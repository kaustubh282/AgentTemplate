"""Workflow service: binds deterministic transitions to business/provider actions.

This is the deterministic execution path. It uses **zero model calls** (§2.3, §58.5):
an action arrives already mapped to a known transition, is authorized, validated, has
its bound service action executed against an authoritative provider, and produces a
registered UI directive.

Ordering matters and is deliberate:

    precheck (auth + validation + prerequisites)
        -> reserve the transition (compare-and-set, bumps the state version)
        -> execute service action (side effect)
        -> commit state (clears the reservation)

so a side effect is never performed before authorization, at most one request can be
inside the side effect for a given flow at a time (§8: no duplicated payment or
issuance on a double submit), and a *failed* service action releases the reservation
and leaves the business data untouched (§20, Demo F).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.audit.events import AuditAction, AuditResult
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import AppError
from app.core.logging.structured import get_logger
from app.core.observability.metrics import FLOW_API_FAILURES_TOTAL, metrics
from app.workflows.engine.engine import TransitionOutcome, WorkflowEngine
from app.workflows.state.models import WorkflowState

logger = get_logger(__name__)

#: A service action receives the trusted context and current state and returns the
#: authoritative fields to persist alongside the transition.
ServiceAction = Callable[[RequestContext, WorkflowState, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class ActionResult:
    outcome: TransitionOutcome
    service_failed: bool = False
    error: AppError | None = None
    service_fields: dict[str, Any] = field(default_factory=dict)


class WorkflowService:
    """Executes a deterministic workflow action end to end."""

    def __init__(self, engine: WorkflowEngine, audit: AuditService) -> None:
        self._engine = engine
        self._audit = audit
        self._actions: dict[tuple[str, str], ServiceAction] = {}

    def register_service_action(self, workflow_id: str, name: str, action: ServiceAction) -> None:
        key = (workflow_id, name)
        if key in self._actions:
            raise ValueError(f"duplicate service action: {key}")
        self._actions[key] = action

    @property
    def engine(self) -> WorkflowEngine:
        return self._engine

    async def execute(
        self,
        ctx: RequestContext,
        state: WorkflowState,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
    ) -> ActionResult:
        transition = await self._engine.precheck_action(ctx, state, action, payload, confirmed=confirmed)

        if transition is None:
            # Idempotent replay: the engine returns the unchanged state safely.
            outcome = await self._engine.apply_action(ctx, state, action, payload, confirmed=confirmed)
            return ActionResult(outcome)

        service_fields: dict[str, Any] = {}
        if transition.service_action:
            handler = self._actions.get((state.workflow_id, transition.service_action))
            if handler is None:
                raise AppError(
                    code=_configuration_error_code(),
                    reason=f"unbound_service_action:{transition.service_action}",
                )
            # Reserve first: of N concurrent submits only one passes this CAS write, so the
            # provider is called at most once per user decision (§8, H-1).
            state = await self._engine.reserve(ctx, state, transition)
            try:
                service_fields = await handler(ctx, state, dict(payload or {}))
            except AppError as exc:
                metrics.increment(
                    FLOW_API_FAILURES_TOTAL,
                    labels={"workflow": state.workflow_id, "action": action},
                )
                await self._audit.record(
                    ctx,
                    AuditAction.PROVIDER_CALL_OUTCOME,
                    AuditResult.TIMEOUT if exc.retryable else AuditResult.FAILURE,
                    reason_code=exc.code.value,
                    attributes={
                        "workflowId": state.workflow_id,
                        "action": action,
                        "serviceAction": transition.service_action,
                    },
                )
                logger.warning(
                    "workflow_service_action_failed",
                    extra={
                        "workflowId": state.workflow_id,
                        "action": action,
                        "code": exc.code.value,
                    },
                )
                # Business data is *not* committed: the journey is preserved exactly as
                # it was; only the reservation is released so the user can retry.
                released = await self._engine.release(ctx, state)
                return ActionResult(
                    TransitionOutcome(released, None, allowed_actions=self._engine.allowed_actions(released)),
                    service_failed=True,
                    error=exc,
                )

        outcome = await self._engine.apply_action(
            ctx, state, action, payload, service_result=service_fields, confirmed=confirmed
        )
        return ActionResult(outcome, service_fields=service_fields)


def _configuration_error_code() -> Any:
    from app.core.errors.taxonomy import ErrorCode

    return ErrorCode.CONFIGURATION_ERROR
