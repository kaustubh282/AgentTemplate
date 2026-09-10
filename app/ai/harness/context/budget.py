"""Token / model-call / agent-step budget enforcement (master prompt §17, §58.3).

Budgets are checked *before* a model call, and the context builder compresses or
rejects inputs before the request budget is exceeded. Exceeding a budget is a
first-class, observable outcome - not a silent truncation.

Enforcement is **structural, not cooperative**. The Harness binds the request's
ledger with :func:`govern`, and :class:`~app.ai.models.provider.ModelInvoker` reads
that binding for every call it makes: it refuses to run without one, checks the budget
before invoking and records usage afterwards. A handler cannot skip the ledger by
calling the invoker directly, and the recorded ``model_calls`` therefore always equals
the calls that actually happened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.errors.taxonomy import BudgetExceededError
from app.core.observability.metrics import (
    AGENT_HANDOFFS_PER_REQUEST,
    AGENT_STEPS_PER_REQUEST,
    TOOL_CALLS_PER_REQUEST,
    metrics,
)


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Configured ceilings for one request (§58.3)."""

    max_context_tokens: int = 3_500
    max_history_tokens: int = 600
    max_history_turns: int = 4
    max_retrieval_tokens: int = 1_400
    max_tool_result_tokens: int = 400
    max_output_tokens: int = 500
    max_agent_steps: int = 3
    max_model_calls: int = 2
    max_tool_calls: int = 4

    def narrowed(
        self,
        *,
        model_calls: int | None = None,
        tokens: int | None = None,
        agent_steps: int | None = None,
        tool_calls: int | None = None,
    ) -> BudgetLimits:
        """Apply a capability's tighter budget on top of the global ceiling."""
        return BudgetLimits(
            max_context_tokens=min(self.max_context_tokens, tokens) if tokens else self.max_context_tokens,
            max_history_tokens=self.max_history_tokens,
            max_history_turns=self.max_history_turns,
            max_retrieval_tokens=self.max_retrieval_tokens,
            max_tool_result_tokens=self.max_tool_result_tokens,
            max_output_tokens=self.max_output_tokens,
            max_agent_steps=min(self.max_agent_steps, agent_steps) if agent_steps else self.max_agent_steps,
            max_model_calls=min(self.max_model_calls, model_calls)
            if model_calls is not None
            else self.max_model_calls,
            max_tool_calls=min(self.max_tool_calls, tool_calls) if tool_calls else self.max_tool_calls,
        )


@dataclass(slots=True)
class BudgetLedger:
    """Running consumption for one request."""

    limits: BudgetLimits
    model_calls: int = 0
    agent_steps: int = 0
    agent_handoffs: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    retrieval_tokens: int = 0
    history_tokens: int = 0
    system_prompt_tokens: int = 0
    tool_result_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost: float = 0.0
    #: Calls whose token counts were *estimated* because the provider reported none.
    estimated_token_calls: int = 0
    breaches: list[str] = field(default_factory=list)

    def check_model_call(self) -> None:
        if self.model_calls + 1 > self.limits.max_model_calls:
            self.breaches.append("BLOCK_MODEL_CALL_BUDGET")
            raise BudgetExceededError(
                "BLOCK_TOKEN_BUDGET",
                details={"limit": self.limits.max_model_calls, "kind": "model_calls"},
            )

    def check_context(self, tokens: int) -> None:
        if tokens > self.limits.max_context_tokens:
            self.breaches.append("BLOCK_CONTEXT_TOKEN_BUDGET")
            raise BudgetExceededError(
                "BLOCK_TOKEN_BUDGET",
                details={"limit": self.limits.max_context_tokens, "requested": tokens},
            )

    def check_agent_step(self) -> None:
        if self.agent_steps + 1 > self.limits.max_agent_steps:
            self.breaches.append("BLOCK_AGENT_STEP_BUDGET")
            raise BudgetExceededError(
                "BLOCK_AGENT_STEP_BUDGET", details={"limit": self.limits.max_agent_steps}
            )

    def check_tool_call(self) -> None:
        if self.tool_calls + 1 > self.limits.max_tool_calls:
            self.breaches.append("BLOCK_TOOL_CALL_BUDGET")
            raise BudgetExceededError("BLOCK_TOOL_CALL_BUDGET", details={"limit": self.limits.max_tool_calls})

    def record_model_call(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        cached_input_tokens: int = 0,
        tokens_estimated: bool = False,
    ) -> None:
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_input_tokens
        self.estimated_cost = round(self.estimated_cost + cost, 6)
        if tokens_estimated:
            self.estimated_token_calls += 1

    def record_agent_step(self, *, handoff: bool = False) -> None:
        self.agent_steps += 1
        if handoff:
            self.agent_handoffs += 1

    def record_tool_call(self, tokens: int = 0) -> None:
        self.tool_calls += 1
        self.tool_result_tokens += tokens

    def publish(self, capability_id: str) -> None:
        metrics.observe(AGENT_STEPS_PER_REQUEST, self.agent_steps, labels={"capability": capability_id})
        metrics.observe(AGENT_HANDOFFS_PER_REQUEST, self.agent_handoffs, labels={"capability": capability_id})
        metrics.observe(TOOL_CALLS_PER_REQUEST, self.tool_calls, labels={"capability": capability_id})


class TokenBudgetService:
    """Creates ledgers and answers 'may this consume more?'."""

    def __init__(self, limits: BudgetLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def ledger_for(
        self,
        *,
        model_call_budget: int | None = None,
        token_budget: int | None = None,
        agent_step_budget: int | None = None,
        tool_call_budget: int | None = None,
    ) -> BudgetLedger:
        return BudgetLedger(
            limits=self._limits.narrowed(
                model_calls=model_call_budget,
                tokens=token_budget or None,
                agent_steps=agent_step_budget or None,
                tool_calls=tool_call_budget or None,
            )
        )


# ------------------------------------------------------------ governance ---
_ACTIVE_LEDGER: ContextVar[BudgetLedger | None] = ContextVar("protec_active_budget_ledger", default=None)


@contextmanager
def govern(ledger: BudgetLedger) -> Iterator[BudgetLedger]:
    """Bind ``ledger`` as the budget authority for every model call in this context.

    The Harness wraps each handler invocation in this. Because the binding is a
    ``ContextVar``, concurrent requests never see each other's ledgers, and a handler
    that reaches the shared ``ModelInvoker`` by any path is still governed.
    """
    token = _ACTIVE_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_LEDGER.reset(token)


def active_ledger() -> BudgetLedger | None:
    """The ledger governing the current execution context, if any."""
    return _ACTIVE_LEDGER.get()
