"""Harness policy outcomes and execution records (master prompt §5.7.7, §5.7.8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HarnessOutcome(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    ABSTAIN = "ABSTAIN"
    FALLBACK = "FALLBACK"
    ESCALATE = "ESCALATE"


class ReasonCode(StrEnum):
    """Machine-readable reason attached to every outcome."""

    OK = "OK"
    BLOCK_UNAUTHORIZED_TOOL = "BLOCK_UNAUTHORIZED_TOOL"
    BLOCK_INVALID_WORKFLOW_STATE = "BLOCK_INVALID_WORKFLOW_STATE"
    BLOCK_PII_POLICY = "BLOCK_PII_POLICY"
    BLOCK_TOKEN_BUDGET = "BLOCK_TOKEN_BUDGET"
    BLOCK_AGENT_STEP_BUDGET = "BLOCK_AGENT_STEP_BUDGET"
    BLOCK_TOOL_CALL_BUDGET = "BLOCK_TOOL_CALL_BUDGET"
    BLOCK_PROMPT_INJECTION = "BLOCK_PROMPT_INJECTION"
    BLOCK_GUARDRAIL_INPUT = "BLOCK_GUARDRAIL_INPUT"
    BLOCK_GUARDRAIL_OUTPUT = "BLOCK_GUARDRAIL_OUTPUT"
    BLOCK_OUTPUT_SCHEMA_INVALID = "BLOCK_OUTPUT_SCHEMA_INVALID"
    BLOCK_AUTHORIZATION = "BLOCK_AUTHORIZATION"
    BLOCK_RATE_LIMIT = "BLOCK_RATE_LIMIT"
    #: A handler or dependency failed unexpectedly; evidence is still emitted (§22).
    BLOCK_INTERNAL_ERROR = "BLOCK_INTERNAL_ERROR"
    ABSTAIN_INSUFFICIENT_EVIDENCE = "ABSTAIN_INSUFFICIENT_EVIDENCE"
    ABSTAIN_CONFLICTING_SOURCES = "ABSTAIN_CONFLICTING_SOURCES"
    ABSTAIN_OUT_OF_DOMAIN = "ABSTAIN_OUT_OF_DOMAIN"
    FALLBACK_MODEL_UNAVAILABLE = "FALLBACK_MODEL_UNAVAILABLE"
    FALLBACK_RAG_UNAVAILABLE = "FALLBACK_RAG_UNAVAILABLE"
    ESCALATE_UNSUPPORTED_SENSITIVE_CASE = "ESCALATE_UNSUPPORTED_SENSITIVE_CASE"
    ESCALATE_NEEDS_CLARIFICATION = "ESCALATE_NEEDS_CLARIFICATION"


class HarnessExecutionRecord(BaseModel):
    """Non-sensitive evidence emitted for every AI-assisted request (§5.7.8).

    Contains no raw secrets, no unredacted PII, and no chain-of-thought.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    request_id: str
    correlation_id: str
    capability_id: str
    execution_mode: str
    agent_id: str | None = None
    model_id: str | None = None
    workflow_state: str | None = None
    authorization_decision: str = "ALLOW"
    guardrail_decision: str = "ALLOW"
    model_calls: int = 0
    agent_steps: int = 0
    agent_handoffs: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    retrieval_tokens: int = 0
    history_tokens: int = 0
    system_prompt_tokens: int = 0
    tool_result_tokens: int = 0
    latency_ms: float = 0.0
    grounding_decision: str | None = None
    fallback_decision: str | None = None
    result_category: str = "UNKNOWN"
    #: Exception class name when the execution failed unexpectedly (never its message).
    error_type: str | None = None
    #: Tool names the capability actually invoked, for the §23 decision record.
    tools_used: list[str] = Field(default_factory=list)
    outcome: HarnessOutcome = HarnessOutcome.ALLOW
    reason_code: ReasonCode = ReasonCode.OK
    estimated_cost: float = 0.0
    #: True when at least one call's token counts were estimated locally because the
    #: provider reported no usage. Estimated and provider-reported figures are never
    #: silently mixed without this flag (§58.12).
    tokens_estimated: bool = False
    prompt_version: str | None = None
    knowledge_corpus_version: str | None = None
    guardrail_policy_version: str | None = None
    evidence_document_ids: list[str] = Field(default_factory=list)
    environment: str = "local"


@dataclass(slots=True)
class HarnessResult:
    """What the Harness hands back to the caller."""

    outcome: HarnessOutcome
    reason_code: ReasonCode
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    record: HarnessExecutionRecord | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is HarnessOutcome.ALLOW
