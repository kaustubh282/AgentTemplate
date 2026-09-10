"""Typed agent inputs and outputs (master prompt §5.3, §26.2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Intent(StrEnum):
    """The complete set of intents the platform recognises."""

    FAQ_QUESTION = "FAQ_QUESTION"
    START_PURCHASE = "START_PURCHASE"
    CONTINUE_PURCHASE = "CONTINUE_PURCHASE"
    GET_POLICY_DETAILS = "GET_POLICY_DETAILS"
    GET_POLICY_PREMIUM = "GET_POLICY_PREMIUM"
    GET_CLAIM_STATUS = "GET_CLAIM_STATUS"
    GET_PAYMENT_STATUS = "GET_PAYMENT_STATUS"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    UNSUPPORTED = "UNSUPPORTED"


class FaqRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)
    domain: str | None = None
    product: str | None = None


class FaqAnswer(BaseModel):
    """Output contract for the FAQ capability."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    verification: str
    citations: list[dict[str, str]] = Field(default_factory=list)
    #: Internal-only evidence signal. Not rendered to users as a confidence score (§7).
    evidence_score: float = 0.0


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4_000)
    active_workflow: str | None = None


class ExtractedIntent(BaseModel):
    """Structured extraction result. Entities are never invented (§5.3)."""

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Intent.UNSUPPORTED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entities: dict[str, Any] = Field(default_factory=dict)
    ambiguous: bool = False
    clarifying_question: str = ""


class SupervisorDecision(BaseModel):
    """Escalation routing decision. Grants no permission by itself (§5.1)."""

    model_config = ConfigDict(extra="forbid")

    capability: str = "UNSUPPORTED"
    reason_category: str = "unclassified"
    needs_clarification: bool = False
    clarifying_question: str = ""
