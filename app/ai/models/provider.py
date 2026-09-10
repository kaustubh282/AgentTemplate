"""Model provider abstraction (master prompt §32, §20).

Business services never depend on a model vendor. They depend on
:class:`ModelInvoker`, which owns generation settings, timeouts, usage accounting and
a *controlled* fallback that never silently downgrades a high-risk decision.

The concrete adapters are Strands ``Model`` implementations, so the same objects can
be handed to a Strands ``Agent`` or invoked directly for a single-call task.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Message, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from app.ai.harness.context.budget import BudgetLedger, active_ledger
from app.core.errors.taxonomy import ForbiddenError, ModelTimeoutError, ModelUnavailableError
from app.core.logging.structured import get_logger
from app.core.observability.metrics import (
    FALLBACK_TOTAL,
    MODEL_CALLS_TOTAL,
    MODEL_COST,
    MODEL_FIRST_TOKEN_MS,
    MODEL_INPUT_TOKENS,
    MODEL_LATENCY_MS,
    MODEL_OUTPUT_TOKENS,
    MODEL_TOKENS_ESTIMATED_TOTAL,
    metrics,
)

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class ModelUsage:
    """Per-call usage accounting (§17, §58.12)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    latency_ms: float = 0.0
    first_token_ms: float | None = None
    model_id: str = ""
    provider: str = ""
    estimated_cost: float = 0.0
    fallback_used: bool = False
    #: True when the provider reported no usage and the counts were estimated locally.
    #: Telemetry consumers must never mistake an estimate for a provider figure (§58.12).
    tokens_estimated: bool = False


@dataclass(slots=True)
class ModelResult:
    text: str
    usage: ModelUsage
    structured: BaseModel | None = None
    stop_reason: str = "end_turn"


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """Conservative defaults for factual, regulated tasks (§32)."""

    temperature: float = 0.0
    max_output_tokens: int = 500
    timeout_ms: int = 12_000
    streaming: bool = False


def approx_tokens(text: str) -> int:
    """Conservative token estimate used when a provider reports no usage."""
    if not text:
        return 0
    return max(1, int(len(text.split()) * 1.35) + text.count("\n"))


class ScriptedResponder:
    """Rule-driven responder backing the deterministic test model.

    Rules are ordered ``(pattern, responder)`` pairs. This is a *test double*, and
    configuration refuses it in production.
    """

    def __init__(self) -> None:
        self._rules: list[tuple[re.Pattern[str], Callable[[str], str]]] = []
        self._default: Callable[[str], str] = lambda _prompt: "I do not have enough information."

    @staticmethod
    def _as_callable(responder: Callable[[str], str] | str) -> Callable[[str], str]:
        if callable(responder):
            return responder
        fixed = responder

        def constant(_prompt: str) -> str:
            return fixed

        return constant

    def add(self, pattern: str, responder: Callable[[str], str] | str) -> ScriptedResponder:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        self._rules.append((compiled, self._as_callable(responder)))
        return self

    def set_default(self, responder: Callable[[str], str] | str) -> ScriptedResponder:
        self._default = self._as_callable(responder)
        return self

    def respond(self, prompt: str) -> str:
        for pattern, responder in self._rules:
            if pattern.search(prompt):
                return responder(prompt)
        return self._default(prompt)


class EvidenceGroundedResponder(ScriptedResponder):
    """A test double that behaves like a *well-behaved grounded model*.

    Instead of needing a hand-written rule per question, it extracts the sentences of
    the prompt's EVIDENCE block that best match the QUESTION, and declines with the
    ``INSUFFICIENT_EVIDENCE`` marker when nothing matches well enough.

    This matters for evaluation honesty: with a rule-per-question script, a metric
    measures the script's coverage rather than the system's retrieval and grounding.
    An extractive double answers only from supplied evidence - exactly the behaviour
    the prompt demands - so the numbers reflect the platform.

    It ships with the template so downstream teams can run the full eval suite with
    no model credentials. Configuration refuses it in production.
    """

    _EVIDENCE = re.compile(r"EVIDENCE:\s*(.*?)(?:\n\s*QUESTION:|\Z)", re.DOTALL)
    #: The *last* QUESTION marker is the real question: a composed context can carry
    #: the marker too, and taking the first would swallow the evidence into the query.
    _QUESTION = re.compile(r"QUESTION:\s*(?!.*QUESTION:)(.*)", re.DOTALL)
    _SENTENCE = re.compile(r"(?<=[.!?])\s+")
    #: The untrusted-document delimiters and bracketed citation labels.
    _WRAPPER = re.compile(r"<<<[^>]*>>>|^\[[^\]]*\]$", re.MULTILINE)
    _WORD = re.compile(r"[a-z0-9]+")
    _STOP = frozenset(
        {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "can",
            "do",
            "does",
            "for",
            "from",
            "how",
            "i",
            "in",
            "is",
            "it",
            "my",
            "of",
            "on",
            "or",
            "the",
            "to",
            "what",
            "when",
            "which",
            "who",
            "why",
            "will",
            "with",
            "you",
            "your",
        }
    )

    def __init__(self, *, max_sentences: int = 2, min_overlap: float = 0.3) -> None:
        super().__init__()
        self._max_sentences = max_sentences
        self._min_overlap = min_overlap

    def _terms(self, text: str) -> set[str]:
        return {w for w in self._WORD.findall(text.lower()) if w not in self._STOP}

    def respond(self, prompt: str) -> str:
        # Explicit rules still win, so a caller can script a specific behaviour.
        for pattern, responder in self._rules:
            if pattern.search(prompt):
                return responder(prompt)

        evidence_match = self._EVIDENCE.search(prompt)
        question_match = self._QUESTION.search(prompt)
        if not evidence_match or not question_match:
            return "INSUFFICIENT_EVIDENCE"

        question_terms = self._terms(question_match.group(1))
        if not question_terms:
            return "INSUFFICIENT_EVIDENCE"

        # Strip the untrusted-document wrapper and citation labels *before* splitting:
        # they are delimiters, and treating them as sentence content would discard the
        # first real sentence of every chunk.
        evidence = self._WRAPPER.sub(" ", evidence_match.group(1))

        scored: list[tuple[float, str]] = []
        for raw in self._SENTENCE.split(evidence):
            sentence = " ".join(raw.split())
            if not sentence:
                continue
            terms = self._terms(sentence)
            if not terms:
                continue
            overlap = len(question_terms & terms) / len(question_terms)
            if overlap >= self._min_overlap:
                scored.append((overlap, sentence))

        if not scored:
            return "INSUFFICIENT_EVIDENCE"

        scored.sort(key=lambda item: item[0], reverse=True)
        return " ".join(sentence for _score, sentence in scored[: self._max_sentences])


class DeterministicModel(Model):
    """Offline Strands model used for tests, evals and local development.

    It produces reproducible output, reports usage, and can simulate latency and
    failure so resilience paths are executable without a provider account.
    """

    def __init__(
        self,
        model_id: str = "deterministic-test-model",
        *,
        responder: ScriptedResponder | None = None,
        latency_ms: int = 0,
        fail_with: Exception | None = None,
    ) -> None:
        self._config: dict[str, Any] = {"model_id": model_id}
        self.responder = responder or ScriptedResponder()
        self.latency_ms = latency_ms
        self.fail_with = fail_with
        self.call_count = 0

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    @staticmethod
    def _render(messages: list[Message], system_prompt: str | None) -> str:
        parts: list[str] = [system_prompt or ""]
        for message in messages:
            for block in message.get("content", []):
                text = block.get("text") if isinstance(block, dict) else None
                if text:
                    parts.append(str(text))
        return "\n".join(p for p in parts if p)

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: Any = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        cancel_signal: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Match the Strands ``Model.stream`` contract exactly.

        The extra keyword-only parameters are accepted and ignored: this double is
        deliberately non-streaming and tool-free, but it must remain substitutable for
        a real provider wherever the SDK passes them.
        """
        self.call_count += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000.0)
        if self.fail_with is not None:
            raise self.fail_with

        prompt = self._render(messages, system_prompt)
        text = self.responder.respond(prompt)

        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": approx_tokens(prompt),
                    "outputTokens": approx_tokens(text),
                    "totalTokens": approx_tokens(prompt) + approx_tokens(text),
                },
                "metrics": {"latencyMs": self.latency_ms},
            }
        }

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.call_count += 1
        rendered = self._render(prompt, system_prompt)
        raw = self.responder.respond(rendered)
        payload = _extract_json(raw)
        yield {"output": output_model.model_validate(payload)}


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response, tolerating code fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end > start else "{}"
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass
class ModelPricing:
    input_per_1k: float = 0.003
    output_per_1k: float = 0.015
    currency: str = "USD"

    def cost(self, usage: ModelUsage) -> float:
        return round(
            (usage.input_tokens / 1000.0) * self.input_per_1k
            + (usage.output_tokens / 1000.0) * self.output_per_1k,
            6,
        )


class ModelInvoker:
    """The only place the application calls a model.

    Owns timeouts, usage accounting, cost estimation, metrics and the controlled
    fallback policy. High-risk capabilities may forbid fallback entirely (§20).

    Budgets are enforced **here**, not by the caller. Every call requires a governing
    :class:`BudgetLedger` bound by the Harness (``app.ai.harness.context.budget.govern``).
    The ledger is checked before the provider is called and charged afterwards, so a
    handler cannot bypass the model-call budget or under-report its spend (§5.7.2).
    """

    def __init__(
        self,
        model: Model,
        *,
        settings: GenerationSettings,
        pricing: ModelPricing | None = None,
        provider_name: str = "deterministic",
        model_id: str = "deterministic-test-model",
        fallback_model: Model | None = None,
    ) -> None:
        self._model = model
        self._settings = settings
        self._pricing = pricing or ModelPricing()
        self._provider = provider_name
        self._model_id = model_id
        self._fallback = fallback_model
        self.usage_log: list[ModelUsage] = []
        #: Consecutive failed invocations; readiness reports the model as degraded
        #: once this crosses ``DEGRADED_AFTER_FAILURES`` (§30).
        self.consecutive_failures = 0

    DEGRADED_AFTER_FAILURES = 3

    @property
    def is_degraded(self) -> bool:
        return self.consecutive_failures >= self.DEGRADED_AFTER_FAILURES

    @staticmethod
    def _governing_ledger() -> BudgetLedger:
        """The ledger that authorizes this call. Absent means an ungoverned side path."""
        ledger = active_ledger()
        if ledger is None:
            raise ForbiddenError("BLOCK_UNGOVERNED_MODEL_CALL")
        ledger.check_model_call()
        return ledger

    @property
    def model(self) -> Model:
        return self._model

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def settings(self) -> GenerationSettings:
        return self._settings

    async def generate(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        capability_id: str = "unknown",
        allow_fallback: bool = False,
    ) -> ModelResult:
        """Single non-agentic model call with timeout, accounting and fallback policy."""
        self._governing_ledger()
        try:
            return await self._invoke(self._model, messages, system_prompt, capability_id, False)
        except (ModelTimeoutError, ModelUnavailableError):
            if not (allow_fallback and self._fallback is not None):
                raise
            metrics.increment(FALLBACK_TOTAL, labels={"kind": "model", "capability": capability_id})
            logger.warning("model_fallback_engaged", extra={"capabilityId": capability_id})
            return await self._invoke(self._fallback, messages, system_prompt, capability_id, True)

    async def _invoke(
        self,
        model: Model,
        messages: list[Message],
        system_prompt: str | None,
        capability_id: str,
        is_fallback: bool,
    ) -> ModelResult:
        started = time.perf_counter()
        first_token_ms: float | None = None
        chunks: list[str] = []
        usage = ModelUsage(model_id=self._model_id, provider=self._provider, fallback_used=is_fallback)
        stop_reason = "end_turn"

        async def run() -> None:
            nonlocal first_token_ms, stop_reason
            async for event in model.stream(messages, system_prompt=system_prompt):
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    text = delta.get("text")
                    if text:
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        chunks.append(text)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "end_turn")
                elif "metadata" in event:
                    reported = event["metadata"].get("usage", {})
                    usage.input_tokens = int(reported.get("inputTokens", 0))
                    usage.output_tokens = int(reported.get("outputTokens", 0))
                    usage.cached_input_tokens = int(reported.get("cacheReadInputTokens", 0) or 0)

        try:
            await asyncio.wait_for(run(), timeout=self._settings.timeout_ms / 1000.0)
        except TimeoutError as exc:
            self.consecutive_failures += 1
            metrics.increment(MODEL_CALLS_TOTAL, labels={"capability": capability_id, "outcome": "timeout"})
            raise ModelTimeoutError("model_timeout") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.consecutive_failures += 1
            metrics.increment(MODEL_CALLS_TOTAL, labels={"capability": capability_id, "outcome": "error"})
            logger.warning(
                "model_invocation_failed",
                extra={"capabilityId": capability_id, "errorType": type(exc).__name__},
            )
            raise ModelUnavailableError("model_invocation_failed") from exc

        text = "".join(chunks)
        usage.latency_ms = (time.perf_counter() - started) * 1000
        usage.first_token_ms = first_token_ms
        if usage.input_tokens == 0:
            usage.input_tokens = approx_tokens((system_prompt or "") + _messages_text(messages))
            usage.tokens_estimated = True
        if usage.output_tokens == 0:
            usage.output_tokens = approx_tokens(text)
            usage.tokens_estimated = True
        usage.estimated_cost = self._pricing.cost(usage)
        self._record(usage, capability_id)
        return ModelResult(text=text, usage=usage, stop_reason=stop_reason)

    async def generate_structured(
        self,
        output_model: type[T],
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        capability_id: str = "unknown",
    ) -> tuple[T, ModelUsage]:
        """Structured extraction with validation handled by the model adapter."""
        self._governing_ledger()
        started = time.perf_counter()
        usage = ModelUsage(model_id=self._model_id, provider=self._provider)
        result: T | None = None

        async def run() -> None:
            nonlocal result
            async for event in self._model.structured_output(
                output_model, messages, system_prompt=system_prompt
            ):
                if "output" in event:
                    result = event["output"]

        try:
            await asyncio.wait_for(run(), timeout=self._settings.timeout_ms / 1000.0)
        except TimeoutError as exc:
            self.consecutive_failures += 1
            raise ModelTimeoutError("model_timeout") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.consecutive_failures += 1
            raise ModelUnavailableError("structured_output_failed") from exc

        if result is None:
            self.consecutive_failures += 1
            raise ModelUnavailableError("structured_output_empty")

        usage.latency_ms = (time.perf_counter() - started) * 1000
        # The structured-output adapter reports no usage: these are estimates, flagged.
        usage.input_tokens = approx_tokens((system_prompt or "") + _messages_text(messages))
        usage.output_tokens = approx_tokens(result.model_dump_json())
        usage.tokens_estimated = True
        usage.estimated_cost = self._pricing.cost(usage)
        self._record(usage, capability_id)
        return result, usage

    def _record(self, usage: ModelUsage, capability_id: str) -> None:
        self.consecutive_failures = 0
        self.usage_log.append(usage)
        # Charge the governing ledger *here*, so recorded spend equals actual spend.
        ledger = active_ledger()
        if ledger is not None:
            ledger.record_model_call(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.estimated_cost,
                cached_input_tokens=usage.cached_input_tokens,
                tokens_estimated=usage.tokens_estimated,
            )
            ledger.record_agent_step()
        labels = {"capability": capability_id, "outcome": "success"}
        metrics.increment(MODEL_CALLS_TOTAL, labels=labels)
        if usage.tokens_estimated:
            metrics.increment(MODEL_TOKENS_ESTIMATED_TOTAL, labels={"capability": capability_id})
        metrics.observe(MODEL_LATENCY_MS, usage.latency_ms, labels={"capability": capability_id})
        metrics.observe(MODEL_INPUT_TOKENS, usage.input_tokens, labels={"capability": capability_id})
        metrics.observe(MODEL_OUTPUT_TOKENS, usage.output_tokens, labels={"capability": capability_id})
        metrics.observe(MODEL_COST, usage.estimated_cost, labels={"capability": capability_id})
        if usage.first_token_ms is not None:
            metrics.observe(MODEL_FIRST_TOKEN_MS, usage.first_token_ms)


def _messages_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts)


def user_message(text: str) -> Message:
    return {"role": "user", "content": [{"text": text}]}
