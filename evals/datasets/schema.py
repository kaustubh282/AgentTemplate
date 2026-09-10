"""Shared eval case schema (master prompt §26.4, §26.11).

One versioned case format is consumed by all three layers - Ragas, DeepEval and the
native deterministic harness - so a scenario is written once and evaluated from
whichever angle is appropriate. Framework-specific code stays in ``evals/ragas`` and
``evals/deepeval``; nothing here imports either framework.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DATASETS_DIR = Path(__file__).parent

#: Bumped whenever the case schema or the corpus of cases changes materially.
DATASET_VERSION = "1.0.0"


class Category(StrEnum):
    FAQ = "FAQ"
    RETRIEVAL = "RETRIEVAL"
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    WORKFLOW = "WORKFLOW"
    ADVERSARIAL = "ADVERSARIAL"
    PII_SECURITY = "PII_SECURITY"
    RELIABILITY = "RELIABILITY"
    PERFORMANCE = "PERFORMANCE"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExpectedOutcome(StrEnum):
    ANSWER_VERIFIED = "ANSWER_VERIFIED"
    ANSWER_PARTIAL = "ANSWER_PARTIAL"
    ABSTAIN = "ABSTAIN"
    SURFACE_CONFLICT = "SURFACE_CONFLICT"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"
    START_FLOW = "START_FLOW"
    ADVANCE_FLOW = "ADVANCE_FLOW"
    REJECT_TRANSITION = "REJECT_TRANSITION"
    FORBIDDEN = "FORBIDDEN"
    UNAVAILABLE = "UNAVAILABLE"


class EvalCase(BaseModel):
    """One regression case. Every field the master prompt lists is representable."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    version: str = DATASET_VERSION
    category: Category
    input: str
    description: str = ""

    # ---- expectations -----------------------------------------------------
    expected_intent: str | None = None
    expected_sources: list[str] = Field(default_factory=list)
    expected_outcome: ExpectedOutcome | None = None
    #: Substrings that must appear in a correct answer (used by the native harness).
    expected_contains: list[str] = Field(default_factory=list)
    #: Substrings that must never appear (hallucination / leakage guards).
    forbidden_contains: list[str] = Field(default_factory=list)
    #: Reference answer for Ragas answer-similarity / correctness metrics.
    reference_answer: str | None = None
    #: Reference contexts for Ragas non-LLM context precision/recall.
    reference_contexts: list[str] = Field(default_factory=list)

    # ---- permissions ------------------------------------------------------
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_workflow_state: str | None = None

    # ---- budgets ----------------------------------------------------------
    max_model_calls: int | None = None
    max_agent_steps: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    latency_budget_ms: int | None = None

    # ---- classification ---------------------------------------------------
    risk_level: RiskLevel = RiskLevel.LOW
    domain: str | None = None
    #: Actor the case runs as: PUBLIC / CUSTOMER / CUSTOMER_B / AGENT / AGENT_UNASSIGNED.
    actor: str = "PUBLIC"
    tags: list[str] = Field(default_factory=list)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    cases: list[EvalCase]

    def by_category(self, category: Category) -> list[EvalCase]:
        return [c for c in self.cases if c.category is category]

    def __len__(self) -> int:
        return len(self.cases)


def load_jsonl(path: str | Path) -> Dataset:
    """Load a ``.jsonl`` dataset, validating every case."""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = DATASETS_DIR / resolved
    cases: list[EvalCase] = []
    for line_number, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(stripped)))
        except Exception as exc:
            raise ValueError(f"{resolved.name}:{line_number}: invalid case: {exc}") from exc
    return Dataset(name=resolved.stem, version=DATASET_VERSION, cases=cases)


def load_all() -> dict[str, Dataset]:
    """Load every dataset in the datasets directory."""
    return {p.stem: load_jsonl(p) for p in sorted(DATASETS_DIR.glob("*.jsonl"))}


def iter_cases(*names: str) -> Iterator[EvalCase]:
    for name in names:
        yield from load_jsonl(f"{name}.jsonl").cases
