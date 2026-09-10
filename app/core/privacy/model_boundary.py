"""Explicit model input/output boundary (master prompt §10.3, §10.4, §58.9).

Everything crossing into model context passes through :class:`ModelInputSanitizer`:
secrets are refused outright, unnecessary PII is removed, and only the declared
AI-facing fields of a DTO survive. Provider and domain DTOs never reach the model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.errors.taxonomy import GuardrailBlockedError
from app.core.observability.metrics import GUARDRAIL_INTERVENTIONS_TOTAL, metrics
from app.core.privacy.classification import (
    FORBIDDEN_IN_MODEL_CONTEXT,
    DataClass,
    at_least,
    classify_field,
    is_never_loggable,
)
from app.core.privacy.masking import MaskingService, masking_service


@dataclass(slots=True)
class SanitizationReport:
    """What the sanitizer changed, recorded for audit without echoing values."""

    removed_fields: list[str] = field(default_factory=list)
    masked_fields: list[str] = field(default_factory=list)
    masked_text_patterns: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None


class AiFacingDto:
    """Marker base class: only fields listed in ``ai_fields`` may reach the model.

    Subclasses are plain Pydantic models elsewhere in the codebase; this class exists
    so the boundary can assert a DTO was *deliberately* shaped for AI consumption.
    """

    #: Field names permitted in model context.
    ai_fields: tuple[str, ...] = ()


class ModelInputSanitizer:
    """Classifies, minimises and redacts data before model invocation."""

    def __init__(
        self,
        masker: MaskingService | None = None,
        *,
        allowed_pii_fields: frozenset[str] = frozenset(),
    ) -> None:
        self._masker = masker or masking_service
        #: Fields the current purpose genuinely requires (data minimisation, §10.1).
        self._allowed_pii_fields = allowed_pii_fields

    def sanitize_text(self, text: str) -> tuple[str, SanitizationReport]:
        report = SanitizationReport()
        detected = self._masker.detect(text)
        if detected:
            report.masked_text_patterns = detected
            metrics.increment(
                GUARDRAIL_INTERVENTIONS_TOTAL, labels={"stage": "model_input", "reason": "text_pii"}
            )
        return self._masker.mask_text(text), report

    def sanitize_payload(
        self, payload: Mapping[str, Any], *, purpose_fields: frozenset[str] | None = None
    ) -> tuple[dict[str, Any], SanitizationReport]:
        """Reduce a payload to the minimum authorized context for the current purpose."""
        allowed = purpose_fields if purpose_fields is not None else self._allowed_pii_fields
        report = SanitizationReport()
        result: dict[str, Any] = {}

        for key, value in payload.items():
            name = str(key)
            classification = classify_field(name)

            if is_never_loggable(name) or classification in FORBIDDEN_IN_MODEL_CONTEXT:
                report.removed_fields.append(name)
                metrics.increment(
                    GUARDRAIL_INTERVENTIONS_TOTAL,
                    labels={"stage": "model_input", "reason": "secret_removed"},
                )
                continue

            if at_least(classification, DataClass.PII) and name not in allowed:
                report.removed_fields.append(name)
                continue

            if isinstance(value, Mapping):
                nested, nested_report = self.sanitize_payload(value, purpose_fields=allowed)
                report.removed_fields.extend(f"{name}.{f}" for f in nested_report.removed_fields)
                report.masked_fields.extend(f"{name}.{f}" for f in nested_report.masked_fields)
                result[name] = nested
                continue

            if isinstance(value, list):
                result[name] = [
                    self.sanitize_payload(item, purpose_fields=allowed)[0]
                    if isinstance(item, Mapping)
                    else (self._masker.mask_text(item) if isinstance(item, str) else item)
                    for item in value
                ]
                continue

            if at_least(classification, DataClass.CONFIDENTIAL):
                result[name] = self._masker.mask_value(name, value)
                report.masked_fields.append(name)
                continue

            result[name] = self._masker.mask_text(value) if isinstance(value, str) else value

        return result, report

    def assert_no_secrets(self, rendered_context: str) -> None:
        """Final structural check: nothing secret-shaped may reach the provider."""
        findings = self._masker.detect(rendered_context)
        blocking = [f for f in findings if f in {"jwt", "authorization_header", "card"}]
        if blocking:
            metrics.increment(
                GUARDRAIL_INTERVENTIONS_TOTAL,
                labels={"stage": "model_input", "reason": "secret_blocked"},
            )
            raise GuardrailBlockedError("BLOCK_PII_POLICY", details={"patterns": blocking})


class ModelOutputScanner:
    """Post-response leakage scanning (§10.3)."""

    def __init__(self, masker: MaskingService | None = None) -> None:
        self._masker = masker or masking_service

    def scan(self, text: str) -> tuple[str, list[str]]:
        findings = self._masker.detect(text)
        if not findings:
            return text, []
        return self._masker.mask_text(text), findings


model_input_sanitizer = ModelInputSanitizer()
model_output_scanner = ModelOutputScanner()
