"""Deterministic workflow engine (master prompt §2.1, §2.2, §8).

Rules enforced here, in code:

* only allow-listed transitions run; anything else is a ``FLOW_STATE_CONFLICT``
* every transition is schema/business validated before it is committed
* every transition is authorization-checked against the caller's trusted identity
* every transition is audited with the fields written (never their values)
* duplicate submits with the same idempotency key replay safely
* the model can never mutate state: only :meth:`apply_action` writes, and it is only
  reachable from the deterministic router and workflow service
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.audit.events import AuditAction, AuditResult, VersionStamp
from app.core.audit.service import AuditService
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import (
    FlowStateConflictError,
    ForbiddenError,
    IdempotencyConflictError,
    ValidationError,
)
from app.core.observability.metrics import (
    FLOW_COMPLETIONS_TOTAL,
    FLOW_STARTS_TOTAL,
    FLOW_STEP_COMPLETIONS_TOTAL,
    FLOW_VALIDATION_FAILURES_TOTAL,
    metrics,
)
from app.core.observability.tracing import tracer
from app.workflows.engine.definition import Transition, WorkflowDefinition, WorkflowRegistry
from app.workflows.state.models import (
    FlowStatus,
    IdempotencyRecord,
    PendingReservation,
    TransitionRecord,
    WorkflowState,
)
from app.workflows.state.store import WorkflowStateStore


def payload_hash(payload: dict[str, Any] | None) -> str:
    """Stable digest of a submitted payload, used to scope idempotency keys (§8)."""
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    """Result of applying an action, including whether it was an idempotent replay."""

    state: WorkflowState
    transition: Transition | None
    replayed: bool = False
    allowed_actions: tuple[str, ...] = ()


class WorkflowEngine:
    """Executes allow-listed transitions against persisted structured state."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        store: WorkflowStateStore,
        audit: AuditService,
        *,
        session_ttl_seconds: int = 3_600,
        reservation_ttl_seconds: int = 60,
    ) -> None:
        self._registry = registry
        self._store = store
        self._audit = audit
        self._ttl = session_ttl_seconds
        self._reservation_ttl = reservation_ttl_seconds

    # ---------------------------------------------------------------- read ---
    async def get_active(self, conversation_id: str, ctx: RequestContext) -> WorkflowState | None:
        """The caller's active flow for a conversation - scope-aware (§13.2, C-1).

        Another subject's in-flight journey is never returned: the same ownership
        rule that protects mutation protects *reading*, so a conversation id alone
        cannot disclose someone else's vehicle, IDV, add-ons or premium.
        """
        state = await self._store.get_active_for_conversation(conversation_id)
        if state is None or state.is_expired:
            return None
        self._assert_ownership(state, ctx)
        return state

    def confirmation_required_actions(self, state: WorkflowState) -> tuple[str, ...]:
        """Actions legal from this state that need a server-issued confirmation token."""
        definition = self._registry.get(state.workflow_id)
        return tuple(
            action
            for action in definition.actions_for(state.state)
            if (transition := definition.find(state.state, action)) is not None
            and transition.requires_confirmation
        )

    def is_idempotent_replay(self, state: WorkflowState, ctx: RequestContext, action: str) -> bool:
        """True when the request carries a key already applied to this same action."""
        if not ctx.idempotency_key:
            return False
        record = state.find_idempotency(ctx.idempotency_key)
        return record is not None and record.action == action

    async def load(self, flow_id: str, ctx: RequestContext) -> WorkflowState:
        state = await self._store.get(flow_id)
        if state is None:
            raise FlowStateConflictError("flow_not_found")
        self._assert_ownership(state, ctx)
        return state

    def definition(self, workflow_id: str) -> WorkflowDefinition:
        return self._registry.get(workflow_id)

    def allowed_actions(self, state: WorkflowState) -> tuple[str, ...]:
        return self._registry.get(state.workflow_id).actions_for(state.state)

    # --------------------------------------------------------------- start ---
    async def start(
        self,
        ctx: RequestContext,
        workflow_id: str,
        *,
        conversation_id: str,
        initial_data: dict[str, Any] | None = None,
    ) -> TransitionOutcome:
        definition = self._registry.get(workflow_id)
        existing = await self._store.get_active_for_conversation(conversation_id)
        if existing is not None and not existing.is_expired:
            # Resuming rather than restarting keeps transactional truth stable (§16) -
            # but only for the subject who owns the journey (C-1).
            self._assert_ownership(existing, ctx)
            return TransitionOutcome(
                existing, None, replayed=True, allowed_actions=self.allowed_actions(existing)
            )

        state = WorkflowState(
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            conversation_id=conversation_id,
            owner_subject_id=ctx.auth.subject_id,
            tenant_id=ctx.auth.tenant_id,
            state=definition.initial_state,
            data=dict(initial_data or {}),
            expires_at=datetime.now(UTC) + timedelta(seconds=self._ttl),
        )
        created = await self._store.create(state)
        metrics.increment(FLOW_STARTS_TOTAL, labels={"workflow": workflow_id})
        await self._audit.record(
            ctx,
            AuditAction.FLOW_STARTED,
            AuditResult.SUCCESS,
            resource_type="WORKFLOW",
            reason_code="flow_started",
            versions=VersionStamp(workflow_version=definition.version),
            attributes={"workflowId": workflow_id, "state": created.state},
        )
        return TransitionOutcome(created, None, allowed_actions=self.allowed_actions(created))

    # ------------------------------------------------------------ precheck ---
    async def precheck_action(
        self,
        ctx: RequestContext,
        state: WorkflowState,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
    ) -> Transition | None:
        """Run every guard *without* mutating, so a side effect is never performed
        before authorization succeeds.

        Returns ``None`` when the action is an idempotent replay that must be skipped.
        """
        self._assert_ownership(state, ctx)

        if not state.is_active:
            raise FlowStateConflictError("flow_not_active", details={"status": state.status.value})

        if self._idempotency_replay(state, ctx, action, payload):
            return None

        self._assert_not_reserved(state, ctx)
        transition = await self._resolve_transition(ctx, state, action)
        self._assert_guards(ctx, state, transition, confirmed=confirmed)
        return transition

    async def _resolve_transition(self, ctx: RequestContext, state: WorkflowState, action: str) -> Transition:
        """Find the allow-listed transition or reject (audited) - shared by every path."""
        definition = self._registry.get(state.workflow_id)
        transition = definition.find(state.state, action)
        if transition is None:
            metrics.increment(
                FLOW_VALIDATION_FAILURES_TOTAL,
                labels={"workflow": state.workflow_id, "reason": "illegal_transition"},
            )
            await self._audit.record(
                ctx,
                AuditAction.FLOW_TRANSITION_REJECTED,
                AuditResult.DENIED,
                reason_code="illegal_transition",
                attributes={
                    "workflowId": state.workflow_id,
                    "fromState": state.state,
                    "action": action,
                },
            )
            raise FlowStateConflictError(
                "illegal_transition", details={"fromState": state.state, "action": action}
            )
        return transition

    def _assert_guards(
        self, ctx: RequestContext, state: WorkflowState, transition: Transition, *, confirmed: bool
    ) -> None:
        self._assert_permissions(ctx, transition)
        self._assert_confirmation(transition, confirmed=confirmed)
        self._assert_idempotency(ctx, transition)
        self._assert_prerequisites(state, transition)

    # ------------------------------------------------------------ reserve ---
    async def reserve(
        self, ctx: RequestContext, state: WorkflowState, transition: Transition
    ) -> WorkflowState:
        """Reserve a side-effecting transition *before* its provider call runs (§8, H-1).

        The reservation is a compare-and-set write that bumps the state version, so of
        N concurrent submits exactly one holds the reservation and the others fail here
        with ``FLOW_STATE_CONFLICT`` - before any payment, quote or issuance call is
        made. A reservation older than the configured TTL is treated as abandoned.
        """
        self._assert_ownership(state, ctx)
        self._assert_not_reserved(state, ctx)
        reserved = state.model_copy(deep=True)
        reserved.pending_action = PendingReservation(
            action=transition.action, request_id=ctx.request_id, idempotency_key=ctx.idempotency_key
        )
        try:
            persisted = await self._store.update(reserved, expected_version=state.version)
        except FlowStateConflictError as exc:
            metrics.increment(
                FLOW_VALIDATION_FAILURES_TOTAL,
                labels={"workflow": state.workflow_id, "reason": "concurrent_submit"},
            )
            raise FlowStateConflictError(
                "transition_in_progress", details={"action": transition.action}
            ) from exc
        return persisted

    async def release(self, ctx: RequestContext, reserved: WorkflowState) -> WorkflowState:
        """Drop a reservation after its side effect failed; state data is untouched."""
        self._assert_ownership(reserved, ctx)
        released = reserved.model_copy(deep=True)
        released.pending_action = None
        return await self._store.update(released, expected_version=reserved.version)

    def assert_not_reserved(self, state: WorkflowState, ctx: RequestContext) -> None:
        """Refuse while another request holds this flow's transition reservation."""
        pending = state.live_reservation(self._reservation_ttl)
        if pending is not None and pending.request_id != ctx.request_id:
            raise FlowStateConflictError("transition_in_progress", details={"action": pending.action})

    _assert_not_reserved = assert_not_reserved

    # --------------------------------------------------------------- apply ---
    async def apply_action(
        self,
        ctx: RequestContext,
        state: WorkflowState,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        service_result: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> TransitionOutcome:
        """Apply one action. This is the *only* path that mutates workflow state."""
        with tracer.span("workflow.apply_action", workflow=state.workflow_id, action=action):
            self._assert_ownership(state, ctx)
            definition = self._registry.get(state.workflow_id)

            if not state.is_active:
                raise FlowStateConflictError("flow_not_active", details={"status": state.status.value})

            if self._idempotency_replay(state, ctx, action, payload):
                await self._audit.record(
                    ctx,
                    AuditAction.IDEMPOTENT_REPLAY,
                    AuditResult.SUCCESS,
                    reason_code="duplicate_submit_replayed",
                    attributes={"workflowId": state.workflow_id, "action": action},
                )
                return TransitionOutcome(
                    state, None, replayed=True, allowed_actions=self.allowed_actions(state)
                )

            self._assert_not_reserved(state, ctx)
            transition = await self._resolve_transition(ctx, state, action)
            self._assert_guards(ctx, state, transition, confirmed=confirmed)

            try:
                written = transition.validator(payload or {}, state.data)
            except ValidationError:
                metrics.increment(
                    FLOW_VALIDATION_FAILURES_TOTAL,
                    labels={"workflow": state.workflow_id, "reason": "validation_failed"},
                )
                raise

            unexpected = set(written) - set(transition.fields_written)
            if unexpected:
                raise ValidationError(
                    "validator_wrote_undeclared_fields", details={"fields": sorted(unexpected)}
                )

            updated = state.model_copy(deep=True)
            updated.pending_action = None
            updated.data.update(written)
            if service_result:
                updated.data.update(service_result)
            updated.state = transition.to_state
            updated.history.append(
                TransitionRecord(
                    from_state=state.state,
                    to_state=transition.to_state,
                    action=action,
                    actor_ref=ctx.auth.subject_ref,
                    request_id=ctx.request_id,
                    fields_written=sorted(set(written) | set(service_result or {})),
                )
            )
            if ctx.idempotency_key:
                updated.applied_idempotency.append(
                    IdempotencyRecord(
                        key=ctx.idempotency_key, action=action, payload_hash=payload_hash(payload)
                    )
                )
            if confirmed and transition.requires_confirmation:
                updated.confirmations.append(action)
            if definition.is_terminal(transition.to_state):
                updated.status = FlowStatus.COMPLETED

            persisted = await self._store.update(updated, expected_version=state.version)

            metrics.increment(
                FLOW_STEP_COMPLETIONS_TOTAL,
                labels={"workflow": state.workflow_id, "toState": transition.to_state},
            )
            await self._audit.record(
                ctx,
                AuditAction.FLOW_STATE_CHANGED,
                AuditResult.SUCCESS,
                resource_type="WORKFLOW",
                reason_code=action,
                versions=VersionStamp(workflow_version=definition.version),
                attributes={
                    "workflowId": state.workflow_id,
                    "fromState": state.state,
                    "toState": transition.to_state,
                    "fieldsWritten": sorted(written),
                    "sideEffect": transition.side_effect_class.value,
                },
            )
            if persisted.status is FlowStatus.COMPLETED:
                metrics.increment(FLOW_COMPLETIONS_TOTAL, labels={"workflow": state.workflow_id})
                await self._audit.record(
                    ctx,
                    AuditAction.FLOW_COMPLETED,
                    AuditResult.SUCCESS,
                    resource_type="WORKFLOW",
                    reason_code="flow_completed",
                    attributes={"workflowId": state.workflow_id},
                )

            return TransitionOutcome(persisted, transition, allowed_actions=self.allowed_actions(persisted))

    # --------------------------------------------------------------- guards ---
    @staticmethod
    def _idempotency_replay(
        state: WorkflowState, ctx: RequestContext, action: str, payload: dict[str, Any] | None
    ) -> bool:
        """Same key + same action + same payload = safe replay. Same key for anything
        else is a conflict: a *different* legitimate action must never be silently
        no-oped because a client reused a key (§8, M-3)."""
        if not ctx.idempotency_key:
            return False
        record = state.find_idempotency(ctx.idempotency_key)
        if record is None:
            return False
        if record.action != action or record.payload_hash != payload_hash(payload):
            raise IdempotencyConflictError("idempotency_key_reused_for_different_request")
        return True

    @staticmethod
    def _assert_ownership(state: WorkflowState, ctx: RequestContext) -> None:
        """A flow may only be advanced by the authenticated subject that owns it."""
        if state.owner_subject_id != ctx.auth.subject_id:
            raise ForbiddenError("workflow_owner_mismatch")
        if state.tenant_id and ctx.auth.tenant_id and state.tenant_id != ctx.auth.tenant_id:
            raise ForbiddenError("cross_tenant_workflow_access")

    @staticmethod
    def _assert_permissions(ctx: RequestContext, transition: Transition) -> None:
        missing = [p for p in transition.required_permissions if not ctx.auth.has_permission(p)]
        if missing:
            raise ForbiddenError("BLOCK_PERMISSION_MISSING")

    @staticmethod
    def _assert_confirmation(transition: Transition, *, confirmed: bool) -> None:
        if transition.requires_confirmation and not confirmed:
            raise ForbiddenError("BLOCK_CONFIRMATION_REQUIRED")

    @staticmethod
    def _assert_idempotency(ctx: RequestContext, transition: Transition) -> None:
        """A sensitive write must carry an idempotency key *before* it runs (§8).

        Without this guard a high-risk transition would reach its provider action and
        fail there, which is both later than necessary and after a possible side
        effect. The capability-level check in the PEP cannot cover this because one
        capability serves every action in a workflow; the requirement is declared per
        transition.
        """
        if transition.requires_idempotency_key and not ctx.idempotency_key:
            raise ForbiddenError("BLOCK_IDEMPOTENCY_KEY_REQUIRED", details={"action": transition.action})

    @staticmethod
    def _assert_prerequisites(state: WorkflowState, transition: Transition) -> None:
        missing = [f for f in transition.requires_fields if state.data.get(f) in (None, "", [])]
        if missing:
            raise FlowStateConflictError("missing_prerequisite_data", details={"fields": missing})
