"""Intentional context construction (master prompt §17, §58.2, §58.7, §58.8).

Context is *assembled*, never concatenated. Priority order is fixed:

1. security / system instructions
2. workflow-required structured state (named fields only)
3. authoritative retrieved evidence (bounded, wrapped as untrusted DATA)
4. a small bounded recent-turn window
5. optional older context (dropped first)

The full conversation history is never resent by default, and interrupt/resume works
from persisted structured state rather than transcript replay (§58.11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.harness.context.budget import BudgetLedger
from app.ai.models.provider import approx_tokens
from app.core.privacy.model_boundary import ModelInputSanitizer, SanitizationReport
from app.core.security.injection import InjectionDetector, wrap_untrusted
from app.core.security.injection import injection_detector as default_injection_detector
from app.rag.retrieval.retriever import RetrievalResult
from app.workflows.state.models import WorkflowState


@dataclass(slots=True)
class ConversationTurn:
    """One prior turn, held as structured data rather than a rendered transcript."""

    role: str
    text: str
    #: Turn ordinal; higher is more recent.
    index: int = 0


@dataclass(slots=True)
class BuiltContext:
    """Assembled model context with a full token accounting.

    Both a composed form and its parts are exposed. An agent whose prompt template
    already has slots for evidence and question must use the *parts*
    (:attr:`evidence_block`, :attr:`question`) - composing ``user_content`` into a
    template that also renders the evidence would send it twice and double the input
    tokens (§58.2, §58.8).
    """

    system_prompt: str
    user_content: str
    #: The sanitized user question on its own.
    question: str = ""
    #: The bounded, untrusted-wrapped evidence on its own ("" when none was supplied).
    evidence_block: str = ""
    system_prompt_tokens: int = 0
    history_tokens: int = 0
    retrieval_tokens: int = 0
    state_tokens: int = 0
    question_tokens: int = 0
    tool_result_tokens: int = 0
    total_input_tokens: int = 0
    dropped_turns: int = 0
    #: Prior turns excluded because they score as prompt injection (§12).
    dropped_injected_turns: int = 0
    dropped_chunks: int = 0
    evidence_document_ids: list[str] = field(default_factory=list)
    #: Exactly which chunks were rendered into the prompt; grounding is assessed against
    #: these and nothing else (§6.3).
    evidence_chunk_ids: list[str] = field(default_factory=list)
    sanitization: SanitizationReport = field(default_factory=SanitizationReport)

    def accounting(self) -> dict[str, int]:
        return {
            "systemPromptTokens": self.system_prompt_tokens,
            "historyTokens": self.history_tokens,
            "retrievalTokens": self.retrieval_tokens,
            "stateTokens": self.state_tokens,
            "questionTokens": self.question_tokens,
            "toolResultTokens": self.tool_result_tokens,
            "totalInputTokens": self.total_input_tokens,
        }


class ContextBuilder:
    """Builds the minimum context needed for the current decision."""

    def __init__(
        self,
        sanitizer: ModelInputSanitizer,
        *,
        injection_detector: InjectionDetector | None = None,
        injection_block_score: float = 0.6,
    ) -> None:
        self._sanitizer = sanitizer
        #: Prior turns are untrusted text too (§12): a message that was blocked, or one
        #: that scores as injection now, is never replayed into model context.
        self._detector = injection_detector or default_injection_detector
        self._injection_block_score = injection_block_score

    def build(
        self,
        *,
        system_prompt: str,
        question: str,
        ledger: BudgetLedger,
        retrieval: RetrievalResult | None = None,
        workflow_state: WorkflowState | None = None,
        workflow_context_fields: tuple[str, ...] = (),
        history: list[ConversationTurn] | None = None,
        tool_results: dict[str, Any] | None = None,
        purpose_fields: frozenset[str] = frozenset(),
    ) -> BuiltContext:
        limits = ledger.limits
        sanitized_question, question_report = self._sanitizer.sanitize_text(question)

        built = BuiltContext(system_prompt=system_prompt, user_content="", question=sanitized_question)
        built.sanitization = question_report
        built.system_prompt_tokens = approx_tokens(system_prompt)
        built.question_tokens = approx_tokens(sanitized_question)

        sections: list[str] = []

        # 2. Workflow state - only the declared fields, never the history list.
        if workflow_state is not None and workflow_context_fields:
            projection = workflow_state.summary_for_context(workflow_context_fields)
            safe_projection, state_report = self._sanitizer.sanitize_payload(
                projection, purpose_fields=purpose_fields
            )
            rendered = _render_kv("CURRENT_JOURNEY", safe_projection)
            built.state_tokens = approx_tokens(rendered)
            built.sanitization.removed_fields.extend(state_report.removed_fields)
            sections.append(rendered)

        # 3. Retrieved evidence, bounded and wrapped as untrusted data.
        if retrieval is not None and retrieval.chunks:
            evidence_parts: list[str] = []
            used = 0
            for scored in retrieval.chunks:
                tokens = scored.chunk.token_estimate
                if used + tokens > limits.max_retrieval_tokens:
                    built.dropped_chunks += 1
                    continue
                evidence_parts.append(wrap_untrusted(scored.chunk.text, scored.chunk.citation_label))
                built.evidence_document_ids.append(scored.chunk.document_id)
                built.evidence_chunk_ids.append(scored.chunk.chunk_id)
                used += tokens
            if evidence_parts:
                # The parts are exposed separately so a prompt template that has its
                # own evidence slot never receives the evidence a second time.
                built.evidence_block = "\n\n".join(evidence_parts)
                rendered = "EVIDENCE:\n" + built.evidence_block
                built.retrieval_tokens = approx_tokens(rendered)
                sections.append(rendered)

        # 4. Bounded recent turns. Oldest are dropped first.
        if history:
            recent = sorted(history, key=lambda t: t.index)[-limits.max_history_turns :]
            built.dropped_turns = max(0, len(history) - len(recent))
            rendered_turns: list[str] = []
            used = 0
            for turn in reversed(recent):
                if self._detector.assess(turn.text).score >= self._injection_block_score:
                    built.dropped_turns += 1
                    built.dropped_injected_turns += 1
                    continue
                safe_text, _ = self._sanitizer.sanitize_text(turn.text)
                line = f"{turn.role}: {safe_text}"
                tokens = approx_tokens(line)
                if used + tokens > limits.max_history_tokens:
                    built.dropped_turns += 1
                    continue
                rendered_turns.insert(0, line)
                used += tokens
            if rendered_turns:
                rendered = "RECENT_TURNS:\n" + "\n".join(rendered_turns)
                built.history_tokens = approx_tokens(rendered)
                sections.append(rendered)

        # Tool results: compressed AI-facing views only, never raw upstream payloads.
        if tool_results:
            safe_results, tool_report = self._sanitizer.sanitize_payload(
                tool_results, purpose_fields=purpose_fields
            )
            rendered = _render_kv("AUTHORITATIVE_DATA", safe_results)
            rendered = _truncate_to_tokens(rendered, limits.max_tool_result_tokens)
            built.tool_result_tokens = approx_tokens(rendered)
            built.sanitization.removed_fields.extend(tool_report.removed_fields)
            sections.append(rendered)

        sections.append(f"QUESTION:\n{sanitized_question}")
        built.user_content = "\n\n".join(sections)
        built.total_input_tokens = built.system_prompt_tokens + approx_tokens(built.user_content)

        # Final structural check plus the hard context ceiling.
        self._sanitizer.assert_no_secrets(system_prompt + "\n" + built.user_content)
        ledger.check_context(built.total_input_tokens)

        ledger.retrieval_tokens += built.retrieval_tokens
        ledger.history_tokens += built.history_tokens
        ledger.system_prompt_tokens += built.system_prompt_tokens
        return built


def _render_kv(heading: str, payload: dict[str, Any]) -> str:
    lines = [f"{heading}:"]
    for key, value in payload.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{k}={v}" for k, v in value.items())
            lines.append(f"- {key}: {inner}")
        elif isinstance(value, list):
            lines.append(f"- {key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Schema-aware-ish truncation: keep whole lines, mark the cut explicitly."""
    if approx_tokens(text) <= max_tokens:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        tokens = approx_tokens(line)
        if used + tokens > max_tokens:
            kept.append("- [truncated to fit tool-result budget]")
            break
        kept.append(line)
        used += tokens
    return "\n".join(kept)
