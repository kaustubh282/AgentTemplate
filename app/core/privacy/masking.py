"""Reusable PII masking / redaction service (master prompt §10.2).

Used by logs, traces, analytics, error reports, prompt telemetry, audit records and
the model input boundary. It performs both *structural* redaction (by field name)
and *content* redaction (by pattern) because free text also carries personal data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.core.privacy.classification import (
    DataClass,
    at_least,
    classify_field,
    is_never_loggable,
)

REDACTED: Final = "[REDACTED]"
MAX_REDACTION_DEPTH: Final = 8


def _mask_tail(value: str, keep: int = 4, fill: str = "*") -> str:
    """Mask everything but the last ``keep`` characters."""
    cleaned = value.strip()
    if len(cleaned) <= keep:
        return fill * len(cleaned)
    return fill * (len(cleaned) - keep) + cleaned[-keep:]


def mask_mobile(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not digits:
        return REDACTED
    return _mask_tail(digits, keep=4)


def mask_email(value: str) -> str:
    if "@" not in value:
        return REDACTED
    local, _, domain = value.partition("@")
    if not local:
        return REDACTED
    return f"{local[0]}***@{domain}"


def mask_pan(value: str) -> str:
    cleaned = value.strip().upper()
    return _mask_tail(cleaned, keep=2)


def mask_aadhaar(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return _mask_tail(digits, keep=4) if digits else REDACTED


def mask_identifier(value: str, keep: int = 4) -> str:
    return _mask_tail(value, keep=keep)


#: Content patterns applied to free text. Order matters: most specific first.
_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str], Any], ...] = (
    (
        "authorization_header",
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{10,}"),
        lambda m: f"{m.group(1)} {REDACTED}",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        lambda m: REDACTED,
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        lambda m: mask_email(m.group(0)),
    ),
    (
        "pan",
        re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
        lambda m: mask_pan(m.group(0)),
    ),
    # Card numbers run *before* Aadhaar: a 16-digit card contains a 12-digit run that
    # the Aadhaar pattern would otherwise consume, leaving the last 8 digits exposed.
    # Covers 16-digit solid/spaced/dashed PANs and 15-digit AMEX (4-6-5 grouping).
    (
        "card",
        re.compile(r"\b(?:[0-9]{4}[ -]?){3}[0-9]{4}\b|\b3[47][0-9]{2}[ -]?[0-9]{6}[ -]?[0-9]{5}\b"),
        lambda m: REDACTED,
    ),
    (
        "aadhaar",
        re.compile(r"\b[2-9][0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}\b"),
        lambda m: mask_aadhaar(m.group(0)),
    ),
    (
        "indian_mobile",
        re.compile(r"(?<![0-9])(?:\+?91[ -]?)?[6-9][0-9]{9}(?![0-9])"),
        lambda m: mask_mobile(m.group(0)),
    ),
    (
        "vehicle_registration",
        re.compile(r"\b[A-Z]{2}[ -]?[0-9]{1,2}[ -]?[A-Z]{1,3}[ -]?[0-9]{4}\b"),
        lambda m: mask_identifier(m.group(0).replace(" ", ""), keep=4),
    ),
)

_MASKERS_BY_CLASS: dict[DataClass, Any] = {
    DataClass.SECRET: lambda _value: REDACTED,
    DataClass.SENSITIVE_PII: lambda value: mask_identifier(str(value), keep=4),
    DataClass.PII: lambda value: mask_identifier(str(value), keep=2),
    DataClass.CONFIDENTIAL: lambda value: mask_identifier(str(value), keep=4),
}

#: Numeric business values about an identifiable person that must not appear in logs
#: even under an arbitrary key (L-3). Matched as substrings of the normalised key.
#: They are redacted *for logs only*: the model boundary still receives them, because
#: "what is my premium?" cannot be answered with a redacted premium.
BUSINESS_VALUE_KEYS: tuple[str, ...] = (
    "idv",
    "premium",
    "sum_insured",
    "suminsured",
    "amount",
    "balance",
    "income",
    "salary",
    "payout",
    "settlement",
)


def is_business_value_key(field_name: str) -> bool:
    normalized = field_name.strip().lower().replace("-", "_")
    return any(key in normalized for key in BUSINESS_VALUE_KEYS)


_FIELD_MASKERS: dict[str, Any] = {
    "mobile": mask_mobile,
    "phone": mask_mobile,
    "phone_number": mask_mobile,
    "email": mask_email,
    "pan": mask_pan,
    "aadhaar": mask_aadhaar,
    "aadhar": mask_aadhaar,
}


class MaskingService:
    """Single implementation of masking policy, injected wherever data leaves the core."""

    def mask_value(self, field_name: str, value: Any) -> Any:
        """Mask one field according to its classification."""
        if is_never_loggable(field_name):
            return REDACTED
        if value is None or isinstance(value, bool | int | float):
            # Numeric values are masked when the field name is classified as personal
            # data, or names a business value about a person (idv, premium, amount...).
            classification = classify_field(field_name)
            if at_least(classification, DataClass.PII) or (
                value is not None and not isinstance(value, bool) and is_business_value_key(field_name)
            ):
                return REDACTED
            return value

        text = str(value)
        normalized = field_name.strip().lower().replace("-", "_")
        specific = _FIELD_MASKERS.get(normalized)
        if specific is not None:
            return specific(text)

        classification = classify_field(field_name)
        masker = _MASKERS_BY_CLASS.get(classification)
        if masker is not None:
            return masker(text)
        return self.mask_text(text)

    def mask_text(self, text: str) -> str:
        """Redact personal data and secrets found inside free text."""
        if not text:
            return text
        result = text
        for _name, pattern, replacement in _TEXT_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def redact(self, payload: Any, *, _depth: int = 0) -> Any:
        """Recursively redact an arbitrary structure for safe logging."""
        if _depth > MAX_REDACTION_DEPTH:
            return "[TRUNCATED_DEPTH]"
        if isinstance(payload, Mapping):
            return {
                str(key): (
                    REDACTED
                    if is_never_loggable(str(key))
                    else self._redact_child(str(key), value, _depth + 1)
                )
                for key, value in payload.items()
            }
        if isinstance(payload, str):
            return self.mask_text(payload)
        if isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
            return [self.redact(item, _depth=_depth + 1) for item in payload]
        if isinstance(payload, bytes):
            return REDACTED
        return payload

    def _redact_child(self, key: str, value: Any, depth: int) -> Any:
        classification = classify_field(key)
        scalar = not isinstance(value, Mapping | list | tuple)
        if scalar and (at_least(classification, DataClass.CONFIDENTIAL) or is_business_value_key(key)):
            return self.mask_value(key, value)
        return self.redact(value, _depth=depth)

    def detect(self, text: str) -> list[str]:
        """Return the names of PII/secret patterns present in ``text`` (detection only)."""
        found: list[str] = []
        for name, pattern, _replacement in _TEXT_PATTERNS:
            if pattern.search(text):
                found.append(name)
        return found


#: Module-level default instance. Injected explicitly in services for testability.
masking_service = MaskingService()
