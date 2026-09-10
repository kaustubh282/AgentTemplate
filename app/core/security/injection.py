"""Prompt-injection detection for user input and retrieved documents (§12).

Both user input and retrieved text are untrusted. This detector is a *defence in
depth* signal, not the security boundary: authorization, tool allow-lists and schema
validation are what actually stop an attack. Retrieved text is always wrapped as DATA
and never promoted to instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class InjectionCategory(StrEnum):
    INSTRUCTION_OVERRIDE = "INSTRUCTION_OVERRIDE"
    PROMPT_EXTRACTION = "PROMPT_EXTRACTION"
    ROLE_ESCALATION = "ROLE_ESCALATION"
    TOOL_MANIPULATION = "TOOL_MANIPULATION"
    DATA_EXFILTRATION = "DATA_EXFILTRATION"
    COST_ABUSE = "COST_ABUSE"
    EMBEDDED_DIRECTIVE = "EMBEDDED_DIRECTIVE"
    SUSPICIOUS_URL = "SUSPICIOUS_URL"


@dataclass(frozen=True, slots=True)
class InjectionSignal:
    category: InjectionCategory
    pattern_id: str
    weight: float


@dataclass(frozen=True, slots=True)
class InjectionAssessment:
    score: float
    signals: tuple[InjectionSignal, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({s.category.value for s in self.signals}))


_PATTERNS: tuple[tuple[str, InjectionCategory, float, re.Pattern[str]], ...] = (
    (
        "ignore_previous",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        0.7,
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all)\b[^.\n]{0,30}\b(instruction|rule|prompt|direction|constraint)"
        ),
    ),
    (
        "new_instructions",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        0.6,
        re.compile(r"(?i)\b(new|updated|revised)\s+(system\s+)?(instructions?|rules?|prompt)\b\s*[:\-]"),
    ),
    (
        "reveal_prompt",
        InjectionCategory.PROMPT_EXTRACTION,
        0.7,
        re.compile(
            r"(?i)\b(show|reveal|print|repeat|output|display|what\s+(is|are))\b[^.\n]{0,40}\b(system\s+prompt|your\s+(instructions?|prompt|rules?|guidelines)|initial\s+prompt)\b"
        ),
    ),
    (
        "verbatim_dump",
        InjectionCategory.PROMPT_EXTRACTION,
        0.6,
        re.compile(r"(?i)\brepeat\b[^.\n]{0,30}\b(verbatim|word\s+for\s+word|exactly)\b"),
    ),
    (
        "role_escalation",
        InjectionCategory.ROLE_ESCALATION,
        0.7,
        re.compile(
            r"(?i)\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be|from\s+now\s+on\s+you)\b[^.\n]{0,40}\b(admin|administrator|developer|root|system|dan|unrestricted|jailbreak)\b"
        ),
    ),
    (
        "privilege_claim",
        InjectionCategory.ROLE_ESCALATION,
        0.6,
        re.compile(
            r"(?i)\b(i\s+am\s+(an?\s+)?(admin|administrator|developer|underwriter|employee)|grant\s+me\s+(access|permission))\b"
        ),
    ),
    (
        "developer_mode",
        InjectionCategory.ROLE_ESCALATION,
        0.6,
        re.compile(r"(?i)\b(developer|debug|god|sudo)\s+mode\b"),
    ),
    (
        "tool_manipulation",
        InjectionCategory.TOOL_MANIPULATION,
        0.7,
        re.compile(
            r"(?i)\b(call|invoke|execute|run|use)\b[^.\n]{0,30}\b(tool|function|api|endpoint|command)\b[^.\n]{0,30}\b(directly|without|bypass|regardless)\b"
        ),
    ),
    (
        "sql_or_query",
        InjectionCategory.TOOL_MANIPULATION,
        0.6,
        re.compile(r"(?i)\b(select\s+.+\s+from|drop\s+table|delete\s+from|union\s+select)\b"),
    ),
    (
        "cross_customer",
        InjectionCategory.DATA_EXFILTRATION,
        0.7,
        re.compile(
            r"(?i)\b(all|other|another|every|any)\s+(customers?|users?|policyholders?|policies)\b[^.\n]{0,30}\b(details?|data|records?|list|premium|numbers?)\b"
        ),
    ),
    (
        # A request for a *named third party's* records is an exfiltration attempt, not
        # a knowledge question. The evidence gate cannot catch it - the corpus really
        # does discuss policy numbers - so it has to be refused here (§12, §13.2).
        "third_party_personal_data",
        InjectionCategory.DATA_EXFILTRATION,
        0.7,
        re.compile(
            r"(?i)\b(my\s+)?(neighbour|neighbor|friend|colleague|wife|husband|spouse|brother"
            r"|sister|father|mother|son|daughter|someone\s+else|somebody\s+else|another\s+person"
            r"|his|her|their)\b[^.\n]{0,30}"
            r"\b(policy|claim|premium|account|profile|details|number|record|data)\b"
        ),
    ),
    (
        "pii_extraction",
        InjectionCategory.DATA_EXFILTRATION,
        0.6,
        re.compile(
            r"(?i)\b(give|send|show|list|dump|export)\b[^.\n]{0,30}\b(aadhaar|pan\s+numbers?|passwords?|api\s+keys?|tokens?|credentials?|database)\b"
        ),
    ),
    (
        # Denial-of-wallet is an attack, not a quirky request: it blocks on its own
        # rather than needing a second corroborating signal (§12, §31).
        "cost_abuse",
        InjectionCategory.COST_ABUSE,
        0.65,
        re.compile(
            r"(?i)\b(repeat|loop|generate|output|print)\b[^.\n]{0,40}"
            r"\b(\d{3,}|forever|infinitely|endlessly|until\s+i\s+say|as\s+many\s+as)\b"
        ),
    ),
    (
        "embedded_directive",
        InjectionCategory.EMBEDDED_DIRECTIVE,
        0.8,
        re.compile(
            r"(?i)(<\s*/?\s*system\s*>|\[\s*system\s*\]|###\s*system|<\|im_start\|>|assistant\s*:\s*$)"
        ),
    ),
    (
        "suspicious_url",
        InjectionCategory.SUSPICIOUS_URL,
        0.4,
        re.compile(r"(?i)https?://(?!(?:[a-z0-9-]+\.)*protec\.example)[^\s]{4,}"),
    ),
)


class InjectionDetector:
    """Scores text for prompt-injection indicators."""

    def assess(self, text: str) -> InjectionAssessment:
        if not text:
            return InjectionAssessment(0.0, ())
        signals: list[InjectionSignal] = []
        for pattern_id, category, weight, pattern in _PATTERNS:
            if pattern.search(text):
                signals.append(InjectionSignal(category, pattern_id, weight))
        if not signals:
            return InjectionAssessment(0.0, ())
        # Combine with diminishing returns so one strong signal blocks, and several
        # weak signals accumulate without a single pattern dominating.
        score = 1.0
        for signal in signals:
            score *= 1.0 - signal.weight
        return InjectionAssessment(round(1.0 - score, 4), tuple(signals))


injection_detector = InjectionDetector()


def wrap_untrusted(text: str, source_label: str) -> str:
    """Wrap retrieved/untrusted content so it is presented to the model as DATA (§12).

    Delimiters are neutralised inside the payload so a document cannot close the block
    and continue as instructions.
    """
    neutralised = text.replace("<<<", "< <<").replace(">>>", ">> >")
    return f'<<<UNTRUSTED_DOCUMENT source="{source_label}">>>\n{neutralised}\n<<<END_UNTRUSTED_DOCUMENT>>>'
