"""FinOps: token and cost reporting with configurable ceilings (§35, §35.1, §58.12).

Built entirely from Harness execution records, so every figure is traceable to a real
request rather than estimated after the fact.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from app.ai.harness.decisions import HarnessExecutionRecord


def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (p / 100.0)
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def build_cost_report(
    records: Sequence[HarnessExecutionRecord],
    *,
    environment: str,
    currency: str = "USD",
    ceilings: dict[str, float] | None = None,
    deterministic_request_count: int = 0,
) -> dict[str, Any]:
    """Aggregate cost and token usage by environment, capability, model and outcome.

    ``deterministic_request_count`` lets the caller include requests that never
    reached the Harness at all, so the zero-model-call rate reflects real traffic.
    """
    ceilings = ceilings or {}
    ai_requests = len(records)
    total_requests = ai_requests + deterministic_request_count

    input_tokens = [float(r.input_tokens) for r in records]
    output_tokens = [float(r.output_tokens) for r in records]
    costs = [r.estimated_cost for r in records]
    latencies = [r.latency_ms for r in records]
    model_calls = [float(r.model_calls) for r in records]

    zero_model_ai = sum(1 for r in records if r.model_calls == 0)
    zero_model_total = zero_model_ai + deterministic_request_count

    by_capability: dict[str, dict[str, float]] = defaultdict(
        lambda: {"requests": 0.0, "inputTokens": 0.0, "outputTokens": 0.0, "cost": 0.0, "modelCalls": 0.0}
    )
    by_model: dict[str, float] = defaultdict(float)
    by_outcome: dict[str, int] = defaultdict(int)

    for record in records:
        bucket = by_capability[record.capability_id]
        bucket["requests"] += 1
        bucket["inputTokens"] += record.input_tokens
        bucket["outputTokens"] += record.output_tokens
        bucket["cost"] = round(bucket["cost"] + record.estimated_cost, 6)
        bucket["modelCalls"] += record.model_calls
        by_model[record.model_id or "unknown"] += record.estimated_cost
        by_outcome[record.outcome.value] += 1

    faq_records = [r for r in records if "faq" in r.capability_id.lower()]
    faq_success = [r for r in faq_records if r.outcome.value == "ALLOW"]
    cost_per_1000_faq = (
        round(sum(r.estimated_cost for r in faq_success) / len(faq_success) * 1000, 4) if faq_success else 0.0
    )
    cost_per_1000_mixed = round(sum(costs) / total_requests * 1000, 4) if total_requests else 0.0

    breaches: list[str] = []
    if "maxCostPer1000Faq" in ceilings and cost_per_1000_faq > ceilings["maxCostPer1000Faq"]:
        breaches.append("MAX_COST_PER_1000_FAQ")
    if "maxCostPer1000Mixed" in ceilings and cost_per_1000_mixed > ceilings["maxCostPer1000Mixed"]:
        breaches.append("MAX_COST_PER_1000_MIXED_REQUESTS")

    return {
        "environment": environment,
        "currency": currency,
        "totals": {
            "aiRequests": ai_requests,
            "deterministicRequests": deterministic_request_count,
            "totalRequests": total_requests,
            "modelCalls": int(sum(model_calls)),
            "inputTokens": int(sum(input_tokens)),
            "outputTokens": int(sum(output_tokens)),
            "estimatedCost": round(sum(costs), 6),
        },
        "tokens": {
            "avgInput": round(statistics.fmean(input_tokens), 2) if input_tokens else 0.0,
            "medianInput": round(statistics.median(input_tokens), 2) if input_tokens else 0.0,
            "p95Input": round(_percentile(input_tokens, 95), 2),
            "p99Input": round(_percentile(input_tokens, 99), 2),
            "avgOutput": round(statistics.fmean(output_tokens), 2) if output_tokens else 0.0,
            "medianOutput": round(statistics.median(output_tokens), 2) if output_tokens else 0.0,
            "p95Output": round(_percentile(output_tokens, 95), 2),
        },
        "latency": {
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "p99": round(_percentile(latencies, 99), 2),
        },
        "efficiency": {
            "modelCallsPerRequest": round(sum(model_calls) / ai_requests, 3) if ai_requests else 0.0,
            "agentStepsPerRequest": round(sum(r.agent_steps for r in records) / ai_requests, 3)
            if ai_requests
            else 0.0,
            "agentHandoffsPerRequest": round(sum(r.agent_handoffs for r in records) / ai_requests, 3)
            if ai_requests
            else 0.0,
            "zeroModelCallRequestRate": round(zero_model_total / total_requests, 4)
            if total_requests
            else 0.0,
        },
        "cost": {
            "perSuccessfulFaq": round(sum(r.estimated_cost for r in faq_success) / len(faq_success), 6)
            if faq_success
            else 0.0,
            "per1000Faq": cost_per_1000_faq,
            "per1000MixedRequests": cost_per_1000_mixed,
        },
        "byCapability": {k: dict(v) for k, v in sorted(by_capability.items())},
        "byModel": {k: round(v, 6) for k, v in sorted(by_model.items())},
        "byOutcome": dict(sorted(by_outcome.items())),
        "ceilings": ceilings,
        "breaches": breaches,
        "withinBudget": not breaches,
    }
