"""Directive registry and fail-closed validator (master prompt §2.3).

An unregistered directive type, an invalid payload, or any attempt to smuggle markup
or script through a payload string is rejected before the response leaves the server.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.errors.taxonomy import ValidationError
from app.core.observability.metrics import SCHEMA_VALIDATION_FAILURES_TOTAL, metrics

#: Anything that could execute on a client is refused outright; one definition shared
#: with the guardrail service so input, output and directive checks cannot drift (L-4).
from app.core.security.guardrails import UNSAFE_MARKUP_PATTERN
from app.ui_directives.schemas.directives import (
    DIRECTIVE_SCHEMA_VERSION,
    AccessibilityHints,
    Directive,
    DirectiveType,
    ShowCompletionPayload,
    ShowConfirmationPayload,
    ShowErrorPayload,
    ShowFaqAnswerPayload,
    ShowFormPayload,
    ShowHumanHandoffPayload,
    ShowMessagePayload,
    ShowOptionsPayload,
    ShowPaymentHandoffPayload,
    ShowPolicySelectorPayload,
    ShowQuoteSummaryPayload,
    ShowRetryPayload,
    ShowReviewPayload,
)

#: The only directive types the platform will ever emit.
PAYLOAD_BY_TYPE: dict[DirectiveType, type[BaseModel]] = {
    DirectiveType.SHOW_MESSAGE: ShowMessagePayload,
    DirectiveType.SHOW_FORM: ShowFormPayload,
    DirectiveType.SHOW_OPTIONS: ShowOptionsPayload,
    DirectiveType.SHOW_POLICY_SELECTOR: ShowPolicySelectorPayload,
    DirectiveType.SHOW_QUOTE_SUMMARY: ShowQuoteSummaryPayload,
    DirectiveType.SHOW_REVIEW: ShowReviewPayload,
    DirectiveType.SHOW_CONFIRMATION: ShowConfirmationPayload,
    DirectiveType.SHOW_PAYMENT_HANDOFF: ShowPaymentHandoffPayload,
    DirectiveType.SHOW_COMPLETION: ShowCompletionPayload,
    DirectiveType.SHOW_ERROR: ShowErrorPayload,
    DirectiveType.SHOW_RETRY: ShowRetryPayload,
    DirectiveType.SHOW_HUMAN_HANDOFF: ShowHumanHandoffPayload,
    DirectiveType.SHOW_FAQ_ANSWER: ShowFaqAnswerPayload,
}


class DirectiveRegistry:
    """Validates directives against the registered contracts."""

    def __init__(self, payload_by_type: dict[DirectiveType, type[BaseModel]] | None = None) -> None:
        self._payloads = payload_by_type or dict(PAYLOAD_BY_TYPE)

    @property
    def schema_version(self) -> str:
        return DIRECTIVE_SCHEMA_VERSION

    def is_registered(self, directive_type: str) -> bool:
        return directive_type in {t.value for t in self._payloads}

    def payload_model(self, directive_type: DirectiveType) -> type[BaseModel]:
        model = self._payloads.get(directive_type)
        if model is None:
            raise ValidationError("unregistered_directive_type", details={"type": directive_type})
        return model

    def build(self, directive_type: DirectiveType, payload: BaseModel | dict[str, Any]) -> Directive:
        """Construct and validate a directive. Fails closed on any deviation."""
        model = self.payload_model(directive_type)
        try:
            validated = (
                payload
                if isinstance(payload, model)
                else model.model_validate(payload.model_dump() if isinstance(payload, BaseModel) else payload)
            )
        except PydanticValidationError as exc:
            metrics.increment(
                SCHEMA_VALIDATION_FAILURES_TOTAL,
                labels={"stage": "directive", "type": directive_type.value},
            )
            raise ValidationError(
                "invalid_directive_payload",
                details={"type": directive_type.value, "errorCount": exc.error_count()},
            ) from None

        rendered = validated.model_dump(mode="json")
        self._assert_no_markup(rendered)
        self._assert_accessible(directive_type, rendered)
        return Directive(type=directive_type, payload=rendered)

    def validate_untrusted(self, raw: dict[str, Any]) -> Directive:
        """Validate a directive proposed by a non-authoritative source (e.g. a model).

        Unknown types fail closed rather than being passed through.
        """
        directive_type_raw = raw.get("type")
        if not isinstance(directive_type_raw, str) or not self.is_registered(directive_type_raw):
            metrics.increment(
                SCHEMA_VALIDATION_FAILURES_TOTAL, labels={"stage": "directive", "type": "unknown"}
            )
            raise ValidationError("unregistered_directive_type", details={"type": str(directive_type_raw)})
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("invalid_directive_payload", details={"type": directive_type_raw})
        return self.build(DirectiveType(directive_type_raw), payload)

    @staticmethod
    def _assert_no_markup(payload: Any) -> None:
        if isinstance(payload, dict):
            for value in payload.values():
                DirectiveRegistry._assert_no_markup(value)
        elif isinstance(payload, list):
            for item in payload:
                DirectiveRegistry._assert_no_markup(item)
        elif isinstance(payload, str) and UNSAFE_MARKUP_PATTERN.search(payload):
            metrics.increment(
                SCHEMA_VALIDATION_FAILURES_TOTAL, labels={"stage": "directive", "type": "markup"}
            )
            raise ValidationError("directive_payload_contains_markup")

    @staticmethod
    def _assert_accessible(directive_type: DirectiveType, payload: dict[str, Any]) -> None:
        """Every directive must carry an accessible name (§36)."""
        hints = payload.get("accessibility")
        if not isinstance(hints, dict) or not str(hints.get("aria_label", "")).strip():
            raise ValidationError("directive_missing_accessibility", details={"type": directive_type.value})


def a11y(label: str, *, role: str = "group", status_text: str | None = None) -> AccessibilityHints:
    """Convenience builder so no call site forgets accessibility metadata."""
    return AccessibilityHints(aria_label=label, role=role, status_text=status_text)


directive_registry = DirectiveRegistry()
