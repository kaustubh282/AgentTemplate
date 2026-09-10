"""Versioned UI directive contracts (master prompt §2.3, §36).

Directives are *contracts*, not free-form model output:

* the model never emits HTML, JavaScript or executable code
* only registered directive types may be returned; unknown types fail closed
* every payload is schema-validated server-side before it reaches a client
* every component carries accessibility fields (labels, semantics, states)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bumped on breaking directive changes; clients negotiate on it (§24).
DIRECTIVE_SCHEMA_VERSION = "1.0"


class DirectiveType(StrEnum):
    """The complete allow-list of renderable components."""

    SHOW_MESSAGE = "SHOW_MESSAGE"
    SHOW_FORM = "SHOW_FORM"
    SHOW_OPTIONS = "SHOW_OPTIONS"
    SHOW_POLICY_SELECTOR = "SHOW_POLICY_SELECTOR"
    SHOW_QUOTE_SUMMARY = "SHOW_QUOTE_SUMMARY"
    SHOW_REVIEW = "SHOW_REVIEW"
    SHOW_CONFIRMATION = "SHOW_CONFIRMATION"
    SHOW_PAYMENT_HANDOFF = "SHOW_PAYMENT_HANDOFF"
    SHOW_COMPLETION = "SHOW_COMPLETION"
    SHOW_ERROR = "SHOW_ERROR"
    SHOW_RETRY = "SHOW_RETRY"
    SHOW_HUMAN_HANDOFF = "SHOW_HUMAN_HANDOFF"
    SHOW_FAQ_ANSWER = "SHOW_FAQ_ANSWER"


class ResponseType(StrEnum):
    UI_DIRECTIVE = "UI_DIRECTIVE"
    MESSAGE = "MESSAGE"
    ERROR = "ERROR"


class MessageSeverity(StrEnum):
    """Semantic severity. Clients must not rely on colour alone (§36)."""

    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"


class FieldType(StrEnum):
    TEXT = "TEXT"
    NUMBER = "NUMBER"
    DATE = "DATE"
    SELECT = "SELECT"
    MULTISELECT = "MULTISELECT"
    CHECKBOX = "CHECKBOX"
    REGISTRATION_NUMBER = "REGISTRATION_NUMBER"
    EMAIL = "EMAIL"
    MOBILE = "MOBILE"


class AccessibilityHints(BaseModel):
    """Accessibility contract carried by every renderable element (§36)."""

    model_config = ConfigDict(extra="forbid")

    #: Screen-reader label. Required so no component ships without an accessible name.
    aria_label: str
    aria_described_by: str | None = None
    #: Semantic role hint, e.g. "form", "status", "alert", "group".
    role: str = "group"
    keyboard_focusable: bool = True
    #: Non-colour indicator (icon name / text prefix) so meaning is not colour-only.
    status_text: str | None = None
    live_region: Literal["off", "polite", "assertive"] = "polite"


class SelectOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    description: str | None = None
    disabled: bool = False


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    field_type: FieldType
    required: bool = True
    placeholder: str | None = None
    help_text: str | None = None
    options: list[SelectOption] = Field(default_factory=list)
    max_length: int | None = None
    pattern: str | None = None
    #: Message rendered when validation fails, announced to assistive technology.
    validation_message: str | None = None
    accessibility: AccessibilityHints


class LoadingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_loading: bool = False
    loading_label: str = "Loading"
    #: Whether the client may offer a retry affordance for this directive.
    retryable: bool = False
    retry_label: str | None = None


# ------------------------------------------------------------ payload types ---
class ShowMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: MessageSeverity = MessageSeverity.INFO
    heading: str | None = None
    body: str
    accessibility: AccessibilityHints


class ShowFormPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_id: str
    title: str
    fields: list[FormField]
    submit_label: str = "Continue"
    accessibility: AccessibilityHints
    loading: LoadingState = Field(default_factory=LoadingState)


class ShowOptionsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    title: str
    options: list[SelectOption]
    multi_select: bool = False
    accessibility: AccessibilityHints


class PolicyListItem(BaseModel):
    """Masked policy view for a selector. Never carries full policy records (§58.9)."""

    model_config = ConfigDict(extra="forbid")

    policy_ref: str
    policy_number_masked: str
    product: str
    status: str
    renewal_due: str | None = None


class ShowPolicySelectorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "Select a policy"
    policies: list[PolicyListItem]
    accessibility: AccessibilityHints


class MoneyAmount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    currency: str = "INR"


class QuoteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    amount: MoneyAmount
    note: str | None = None


class ShowQuoteSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_ref: str
    product: str
    lines: list[QuoteLine]
    total: MoneyAmount
    valid_until: str | None = None
    #: The authoritative source that produced these numbers (§2.1: never the model).
    source_system: str
    accessibility: AccessibilityHints


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    editable: bool = False
    edit_action: str | None = None


class ShowReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = "Review your details"
    items: list[ReviewItem]
    accessibility: AccessibilityHints


class ShowConfirmationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    confirm_label: str = "Confirm"
    cancel_label: str = "Cancel"
    #: Explicit user confirmation is required before high-risk writes (§8).
    confirm_action: str
    accessibility: AccessibilityHints


class ShowPaymentHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_ref: str
    amount: MoneyAmount
    #: Opaque server-issued handoff token; never a card number or credential.
    handoff_token: str
    expires_in_seconds: int
    accessibility: AccessibilityHints


class ShowCompletionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    reference_masked: str
    body: str
    accessibility: AccessibilityHints


class ShowErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_code: str
    body: str
    retryable: bool = False
    accessibility: AccessibilityHints


class ShowRetryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str
    retry_action: str
    retry_label: str = "Try again"
    accessibility: AccessibilityHints
    loading: LoadingState = Field(default_factory=lambda: LoadingState(retryable=True))


class ShowHumanHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str
    handoff_reason: str
    contact_channel: str
    accessibility: AccessibilityHints


class SourceCitation(BaseModel):
    """Internal provenance for a grounded answer (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    version: str
    section: str | None = None
    effective_date: str | None = None


class ShowFaqAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    #: VERIFIED / PARTIALLY_VERIFIED / UNSUPPORTED / OUT_OF_DOMAIN / CONFLICTING (§6.3).
    verification: str
    citations: list[SourceCitation] = Field(default_factory=list)
    accessibility: AccessibilityHints


DirectivePayload = (
    ShowMessagePayload
    | ShowFormPayload
    | ShowOptionsPayload
    | ShowPolicySelectorPayload
    | ShowQuoteSummaryPayload
    | ShowReviewPayload
    | ShowConfirmationPayload
    | ShowPaymentHandoffPayload
    | ShowCompletionPayload
    | ShowErrorPayload
    | ShowRetryPayload
    | ShowHumanHandoffPayload
    | ShowFaqAnswerPayload
)


class Directive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: DirectiveType
    payload: dict[str, Any]


class AssistantResponse(BaseModel):
    """The single response contract returned to every channel."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Annotated[str, Field(pattern=r"^\d+\.\d+$")] = DIRECTIVE_SCHEMA_VERSION
    conversation_id: str
    request_id: str
    response_type: ResponseType
    message: str
    directive: Directive | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    #: Non-sensitive execution evidence for the client/telemetry (never chain-of-thought).
    meta: dict[str, Any] = Field(default_factory=dict)
