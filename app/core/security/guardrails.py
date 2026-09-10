"""Layered guardrail service (master prompt §25).

Input, output and business guardrails live here as a *code* boundary. Nothing in this
module relies on asking the model nicely; every decision is deterministic, observable
and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.observability.metrics import GUARDRAIL_INTERVENTIONS_TOTAL, metrics
from app.core.privacy.masking import MaskingService, masking_service
from app.core.security.injection import InjectionDetector, injection_detector

#: Bumped whenever guardrail policy changes; stamped into audit and cache keys (§24).
GUARDRAIL_POLICY_VERSION = "1.0.0"


class GuardrailDecision(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    SANITIZE = "SANITIZE"


class GuardrailReason(StrEnum):
    OK = "OK"
    INPUT_TOO_LONG = "BLOCK_INPUT_TOO_LONG"
    INPUT_EMPTY = "BLOCK_INPUT_EMPTY"
    PROMPT_INJECTION = "BLOCK_PROMPT_INJECTION"
    ABUSIVE_CONTENT = "BLOCK_ABUSIVE_CONTENT"
    SECRET_IN_INPUT = "BLOCK_SECRET_IN_INPUT"
    UNSUPPORTED_CONTENT = "BLOCK_UNSUPPORTED_CONTENT"
    OUTPUT_PII_LEAK = "BLOCK_OUTPUT_PII_LEAK"
    OUTPUT_SECRET_LEAK = "BLOCK_OUTPUT_SECRET_LEAK"
    PROHIBITED_CLAIM = "BLOCK_PROHIBITED_CLAIM"
    OUTPUT_SCHEMA_INVALID = "BLOCK_OUTPUT_SCHEMA_INVALID"
    UNSAFE_MARKUP = "BLOCKUNSAFE_MARKUP_PATTERN"


@dataclass(slots=True)
class GuardrailResult:
    decision: GuardrailDecision
    reason: GuardrailReason = GuardrailReason.OK
    sanitized_text: str | None = None
    signals: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def blocked(self) -> bool:
        return self.decision is GuardrailDecision.BLOCK


#: Claims the assistant may never make about regulated insurance facts (§25 business).
_PROHIBITED_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "guaranteed_approval",
        re.compile(
            r"(?i)\b(guarantee[d]?|assured|certain)\b[^.\n]{0,40}"
            r"\b(approv\w+|accept\w+|settle\w+|payout|claim)\b"
        ),
    ),
    (
        "guaranteed_premium",
        re.compile(r"(?i)\byour\s+premium\s+(will\s+be|is)\s+(exactly\s+)?(rs\.?|inr|₹)?\s*[\d,]+"),
    ),
    (
        "regulatory_claim",
        re.compile(r"(?i)\b(irdai[- ]?(compliant|approved|certified)|fully\s+compliant\s+with\s+irdai)\b"),
    ),
    (
        "legal_advice",
        re.compile(
            r"(?i)\b(i\s+(certify|guarantee)|this\s+is\s+legal\s+advice"
            r"|you\s+are\s+legally\s+entitled)\b"
        ),
    ),
    (
        "coverage_promise",
        re.compile(
            r"(?i)\b(you\s+are\s+(definitely|certainly)\s+covered"
            r"|this\s+will\s+definitely\s+be\s+covered|100%\s+covered)\b"
        ),
    ),
    (
        "eligibility_promise",
        re.compile(
            r"(?i)\byou\s+(?:will|are)\s+(?:definitely\s+|certainly\s+|surely\s+)?(?:be\s+)?"
            r"(?:eligible|approved|accepted)\b|\byou\s+qualify\s+for\s+sure\b"
        ),
    ),
)

#: Markup that could execute on a client. The single definition used for user input,
#: model output and UI-directive payloads (L-4): the union of the former two regexes.
UNSAFE_MARKUP_PATTERN = re.compile(
    r"(?is)(<\s*script\b|<\s*iframe\b|<\s*object\b|<\s*embed\b|javascript\s*:|data\s*:\s*text/html"
    r"|on(?:error|load|click|focus|mouseover)\s*=)"
)

_ABUSE_TERMS = re.compile(
    r"(?i)\b(kill\s+yourself|kys|fuck\s+you|bastard|bitch|motherfucker|rape|terrorist\s+attack\s+plan)\b"
)

#: Credential shapes. A user pasting one of these is a credential-exposure event: the
#: request is refused and audited rather than quietly masked, because masking would
#: leave the user believing the secret was safely handled.
_SECRET_LIKE = re.compile(
    r"(?i)("
    r"\bsk-[A-Za-z0-9]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bghp_[A-Za-z0-9]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}"  # JWT
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9\-._~+/=]{16,}"  # authorization header value
    r")"
)


class GuardrailService:
    """The single implementation of guardrail policy used by the Harness."""

    def __init__(
        self,
        *,
        max_input_chars: int = 4_000,
        injection_enabled: bool = True,
        injection_block_score: float = 0.6,
        output_scan_enabled: bool = True,
        toxicity_enabled: bool = True,
        detector: InjectionDetector | None = None,
        masker: MaskingService | None = None,
    ) -> None:
        self._max_input_chars = max_input_chars
        self._injection_enabled = injection_enabled
        self._injection_block_score = injection_block_score
        self._output_scan_enabled = output_scan_enabled
        self._toxicity_enabled = toxicity_enabled
        self._detector = detector or injection_detector
        self._masker = masker or masking_service

    # ---------------------------------------------------------------- input ---
    def check_input(self, text: str) -> GuardrailResult:
        if text is None or not text.strip():
            return self._blocked(GuardrailReason.INPUT_EMPTY)
        if len(text) > self._max_input_chars:
            return self._blocked(GuardrailReason.INPUT_TOO_LONG)
        if UNSAFE_MARKUP_PATTERN.search(text):
            return self._blocked(GuardrailReason.UNSAFE_MARKUP)
        if _SECRET_LIKE.search(text):
            return self._blocked(GuardrailReason.SECRET_IN_INPUT)
        if self._toxicity_enabled and _ABUSE_TERMS.search(text):
            return self._blocked(GuardrailReason.ABUSIVE_CONTENT)
        if self._injection_enabled:
            assessment = self._detector.assess(text)
            if assessment.score >= self._injection_block_score:
                result = self._blocked(GuardrailReason.PROMPT_INJECTION)
                result.signals = list(assessment.categories)
                result.score = assessment.score
                return result
            if assessment.signals:
                return GuardrailResult(
                    GuardrailDecision.ALLOW,
                    GuardrailReason.OK,
                    signals=list(assessment.categories),
                    score=assessment.score,
                )
        return GuardrailResult(GuardrailDecision.ALLOW)

    def check_retrieved_document(self, text: str) -> GuardrailResult:
        """Indirect injection check for retrieved content (§12)."""
        if not self._injection_enabled:
            return GuardrailResult(GuardrailDecision.ALLOW)
        assessment = self._detector.assess(text)
        if assessment.score >= self._injection_block_score:
            result = self._blocked(GuardrailReason.PROMPT_INJECTION)
            result.signals = list(assessment.categories)
            result.score = assessment.score
            return result
        return GuardrailResult(GuardrailDecision.ALLOW, score=assessment.score)

    # --------------------------------------------------------------- output ---
    def check_output(self, text: str, *, allow_pii: bool = False) -> GuardrailResult:
        """Scan model output for leakage and prohibited claims before it reaches a user."""
        if not self._output_scan_enabled:
            return GuardrailResult(GuardrailDecision.ALLOW)
        if _SECRET_LIKE.search(text) or re.search(r"\beyJ[A-Za-z0-9_-]{5,}\.", text):
            return self._blocked(GuardrailReason.OUTPUT_SECRET_LEAK)
        if UNSAFE_MARKUP_PATTERN.search(text):
            return self._blocked(GuardrailReason.UNSAFE_MARKUP)
        for claim_id, pattern in _PROHIBITED_CLAIM_PATTERNS:
            if pattern.search(text):
                result = self._blocked(GuardrailReason.PROHIBITED_CLAIM)
                result.signals = [claim_id]
                return result
        if not allow_pii:
            detected = self._masker.detect(text)
            leaks = [d for d in detected if d in {"aadhaar", "pan", "card", "jwt", "authorization_header"}]
            if leaks:
                result = GuardrailResult(
                    GuardrailDecision.SANITIZE,
                    GuardrailReason.OUTPUT_PII_LEAK,
                    sanitized_text=self._masker.mask_text(text),
                    signals=leaks,
                )
                metrics.increment(
                    GUARDRAIL_INTERVENTIONS_TOTAL,
                    labels={"stage": "output", "reason": result.reason.value},
                )
                return result
        return GuardrailResult(GuardrailDecision.ALLOW)

    def _blocked(self, reason: GuardrailReason) -> GuardrailResult:
        metrics.increment(
            GUARDRAIL_INTERVENTIONS_TOTAL, labels={"stage": "guardrail", "reason": reason.value}
        )
        return GuardrailResult(GuardrailDecision.BLOCK, reason)
