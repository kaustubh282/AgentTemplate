"""Append-oriented audit event model (master prompt §22, §23).

Audit events are structurally distinct from debug logs: they are typed, versioned,
addressed to a separate store, and contain no raw prompts, chain-of-thought or
unredacted PII. They exist so Security / Compliance / Audit can reconstruct *what*
happened, *under which versions*, without reading conversation content.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditAction(StrEnum):
    AUTH_CONTEXT_ESTABLISHED = "AUTH_CONTEXT_ESTABLISHED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    RESOURCE_SCOPE_DENIED = "RESOURCE_SCOPE_DENIED"
    FLOW_STARTED = "FLOW_STARTED"
    FLOW_STATE_CHANGED = "FLOW_STATE_CHANGED"
    FLOW_COMPLETED = "FLOW_COMPLETED"
    FLOW_TRANSITION_REJECTED = "FLOW_TRANSITION_REJECTED"
    HIGH_RISK_TOOL_REQUESTED = "HIGH_RISK_TOOL_REQUESTED"
    USER_CONFIRMATION_RECORDED = "USER_CONFIRMATION_RECORDED"
    PROVIDER_CALL_OUTCOME = "PROVIDER_CALL_OUTCOME"
    QUOTE_RECEIVED = "QUOTE_RECEIVED"
    POLICY_ACTION_SUBMITTED = "POLICY_ACTION_SUBMITTED"
    CLAIM_SUBMITTED = "CLAIM_SUBMITTED"
    PAYMENT_INITIATED = "PAYMENT_INITIATED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    #: Machine-readable explainability record for an AI-assisted decision (§23).
    AI_DECISION_RECORDED = "AI_DECISION_RECORDED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    MODEL_FALLBACK = "MODEL_FALLBACK"
    KNOWLEDGE_SOURCE_USED = "KNOWLEDGE_SOURCE_USED"
    KNOWLEDGE_LIFECYCLE_CHANGED = "KNOWLEDGE_LIFECYCLE_CHANGED"
    CONFIG_DECISION_CONTEXT = "CONFIG_DECISION_CONTEXT"
    AI_EXECUTION_COMPLETED = "AI_EXECUTION_COMPLETED"
    ABSTAINED_INSUFFICIENT_EVIDENCE = "ABSTAINED_INSUFFICIENT_EVIDENCE"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    RATE_LIMITED = "RATE_LIMITED"


class AuditResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    ABSTAINED = "ABSTAINED"
    TIMEOUT = "TIMEOUT"


class VersionStamp(BaseModel):
    """Version identifiers relevant to a decision (§24)."""

    model_config = ConfigDict(extra="forbid")

    app_version: str | None = None
    api_version: str | None = None
    directive_schema_version: str | None = None
    workflow_version: str | None = None
    prompt_version: str | None = None
    knowledge_corpus_version: str | None = None
    model_id: str | None = None
    guardrail_policy_version: str | None = None
    capability_version: str | None = None


class AuditEvent(BaseModel):
    """One immutable audit record."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str
    request_id: str
    #: Pseudonymous actor reference, never the raw subject id.
    actor_ref: str
    actor_type: str
    channel: str
    action: AuditAction
    result: AuditResult
    resource_type: str | None = None
    #: Masked resource reference (e.g. "******1234").
    resource_ref: str | None = None
    source_system: str | None = None
    reason_code: str | None = None
    environment: str = "local"
    versions: VersionStamp = Field(default_factory=VersionStamp)
    #: Small, non-sensitive decision metadata only. Validated by the AuditService.
    attributes: dict[str, Any] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """Machine-readable explainability record for AI-assisted decisions (§23).

    Deliberately excludes hidden chain-of-thought; it captures the *evidence and
    outcome categories* instead.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    request_id: str
    correlation_id: str
    capability_id: str
    intent_selected: str | None = None
    rules_evaluated: list[str] = Field(default_factory=list)
    retrieved_source_ids: list[str] = Field(default_factory=list)
    tools_selected: list[str] = Field(default_factory=list)
    tool_result_categories: list[str] = Field(default_factory=list)
    guardrail_result: str | None = None
    grounding_result: str | None = None
    final_action_category: str | None = None
    versions: VersionStamp = Field(default_factory=VersionStamp)
