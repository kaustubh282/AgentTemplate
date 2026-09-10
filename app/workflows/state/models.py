"""Typed, versioned workflow state (master prompt §16, §2.2).

Transactional truth lives here as structured data - never in the LLM context window
(§58.1). The state is versioned for optimistic locking and carries only the fields the
flow genuinely needs, so resuming a journey never requires replaying a transcript.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FlowStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"


def _now() -> datetime:
    return datetime.now(UTC)


class WorkflowState(BaseModel):
    """Persisted deterministic state for one transactional journey."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    flow_id: str = Field(default_factory=lambda: f"flow_{uuid.uuid4().hex}")
    workflow_id: str
    workflow_version: str
    conversation_id: str
    #: Authenticated owner. Set server-side; never from a payload (§13.2).
    owner_subject_id: str
    tenant_id: str | None = None
    state: str
    status: FlowStatus = FlowStatus.ACTIVE
    #: Optimistic-locking version, incremented on every persisted transition.
    version: int = 0
    #: Collected, validated business data. Domain-scoped and schema-checked.
    data: dict[str, Any] = Field(default_factory=dict)
    #: Ordered transition history for audit and resume, without conversation text.
    history: list[TransitionRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    expires_at: datetime = Field(default_factory=lambda: _now() + timedelta(hours=1))
    #: Idempotency keys already applied, each bound to the action and payload it was
    #: used for, so a duplicate submit is a safe replay and a *reused* key for a
    #: different action or payload is a conflict rather than a silent no-op (§8, M-3).
    applied_idempotency: list[IdempotencyRecord] = Field(default_factory=list)
    #: Set when a high-risk step has been explicitly confirmed by the user.
    confirmations: list[str] = Field(default_factory=list)
    #: A side-effecting transition that has been *reserved* but not yet committed. While
    #: live, no other request may run a transition on this flow, so a provider call can
    #: never be executed twice for one user decision (§8 "no silent duplication").
    pending_action: PendingReservation | None = None

    @property
    def is_expired(self) -> bool:
        return _now() >= self.expires_at

    def live_reservation(self, ttl_seconds: int) -> PendingReservation | None:
        """The current reservation if it has not been abandoned (older than ``ttl``)."""
        pending = self.pending_action
        if pending is None:
            return None
        if (_now() - pending.reserved_at).total_seconds() > ttl_seconds:
            return None
        return pending

    @property
    def is_active(self) -> bool:
        return self.status is FlowStatus.ACTIVE and not self.is_expired

    def find_idempotency(self, key: str) -> IdempotencyRecord | None:
        return next((record for record in self.applied_idempotency if record.key == key), None)

    def summary_for_context(self, fields: tuple[str, ...]) -> dict[str, Any]:
        """Minimal projection for model context (§58.2): only named fields, no history."""
        return {
            "workflow": self.workflow_id,
            "state": self.state,
            "collected": {k: v for k, v in self.data.items() if k in fields},
        }


class PendingReservation(BaseModel):
    """A transition reserved ahead of its side effect (§8, H-1)."""

    model_config = ConfigDict(extra="forbid")

    action: str
    request_id: str
    idempotency_key: str | None = None
    reserved_at: datetime = Field(default_factory=_now)


class IdempotencyRecord(BaseModel):
    """An idempotency key as applied: which action, with which payload."""

    model_config = ConfigDict(extra="forbid")

    key: str
    action: str
    #: Stable digest of the submitted payload; never the payload itself.
    payload_hash: str
    applied_at: datetime = Field(default_factory=_now)


class TransitionRecord(BaseModel):
    """One accepted transition. Contains no free text or model output."""

    model_config = ConfigDict(extra="forbid")

    from_state: str
    to_state: str
    action: str
    at: datetime = Field(default_factory=_now)
    actor_ref: str
    request_id: str
    #: Names of fields written by this transition, never their values.
    fields_written: list[str] = Field(default_factory=list)


WorkflowState.model_rebuild()
