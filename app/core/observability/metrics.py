"""In-process metrics registry (master prompt §21.2).

Deliberately dependency-free: counters, gauges and histograms are held in memory and
exported either as JSON (for the internal metrics endpoint and benchmark harness) or
handed to an OpenTelemetry meter when one is configured. This keeps the template
runnable without a collector while preserving an OTEL-compatible shape.
"""

from __future__ import annotations

import math
import random
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

LabelSet = tuple[tuple[str, str], ...]


def _labels(labels: dict[str, str] | None) -> LabelSet:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


@dataclass
class Histogram:
    """Bounded histogram: ``count``, ``sum`` and ``max`` are exact running totals;
    quantiles are computed from a fixed-size uniform reservoir (Vitter's algorithm R),
    so memory is constant however long the process lives (M-8)."""

    values: list[float] = field(default_factory=list)
    max_samples: int = 10_000
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0
    _rng: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        if self.count == 1 or value > self.maximum:
            self.maximum = value
        if len(self.values) < self.max_samples:
            self.values.append(value)
            return
        # Replace a random slot with probability max_samples / count; not a security use.
        slot = self._rng.randrange(self.count)
        if slot < self.max_samples:
            self.values[slot] = value

    def percentile(self, p: float) -> float:
        if not self.values:
            return 0.0
        ordered = sorted(self.values)
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * (p / 100.0)
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return ordered[int(rank)]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)

    def summary(self) -> dict[str, float]:
        return {
            "count": float(self.count),
            "sum": float(self.total),
            "avg": float(self.total / self.count) if self.count else 0.0,
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "p99": self.percentile(99),
            "max": float(self.maximum) if self.count else 0.0,
        }


class MetricsRegistry:
    """Thread-safe metric store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, LabelSet], float] = defaultdict(float)
        self._gauges: dict[tuple[str, LabelSet], float] = {}
        self._histograms: dict[tuple[str, LabelSet], Histogram] = defaultdict(Histogram)

    def increment(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._histograms[(name, _labels(labels))].observe(value)

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get((name, _labels(labels)), 0.0)

    def histogram(self, name: str, labels: dict[str, str] | None = None) -> Histogram:
        with self._lock:
            return self._histograms[(name, _labels(labels))]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for (name, labels), value in sorted(self._counters.items())
                ],
                "gauges": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for (name, labels), value in sorted(self._gauges.items())
                ],
                "histograms": [
                    {"name": name, "labels": dict(labels), **hist.summary()}
                    for (name, labels), hist in sorted(self._histograms.items())
                ],
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# ------------------------------------------------------------------ names ---
# Platform
REQUESTS_TOTAL = "protec_requests_total"
REQUEST_ERRORS_TOTAL = "protec_request_errors_total"
REQUEST_LATENCY_MS = "protec_request_latency_ms"
RATE_LIMIT_REJECTS_TOTAL = "protec_rate_limit_rejects_total"
CONCURRENCY_GAUGE = "protec_inflight_requests"

# AI
MODEL_CALLS_TOTAL = "protec_model_calls_total"
MODEL_LATENCY_MS = "protec_model_latency_ms"
MODEL_FIRST_TOKEN_MS = "protec_model_first_token_ms"
MODEL_INPUT_TOKENS = "protec_model_input_tokens"
MODEL_OUTPUT_TOKENS = "protec_model_output_tokens"
MODEL_COST = "protec_model_estimated_cost"
#: Calls whose token counts were estimated locally because the provider reported none.
MODEL_TOKENS_ESTIMATED_TOTAL = "protec_model_tokens_estimated_total"
TOOL_CALLS_PER_REQUEST = "protec_tool_calls_per_request"
AGENT_STEPS_PER_REQUEST = "protec_agent_steps_per_request"
AGENT_HANDOFFS_PER_REQUEST = "protec_agent_handoffs_per_request"
GUARDRAIL_INTERVENTIONS_TOTAL = "protec_guardrail_interventions_total"
SCHEMA_VALIDATION_FAILURES_TOTAL = "protec_schema_validation_failures_total"
FALLBACK_TOTAL = "protec_fallback_total"
ABSTENTION_TOTAL = "protec_abstention_total"
ZERO_MODEL_CALL_REQUESTS_TOTAL = "protec_zero_model_call_requests_total"

# RAG
RETRIEVAL_LATENCY_MS = "protec_retrieval_latency_ms"
RETRIEVAL_DOCS = "protec_retrieval_documents"
RETRIEVAL_ZERO_RESULT_TOTAL = "protec_retrieval_zero_result_total"
GROUNDING_FAILURE_TOTAL = "protec_grounding_failure_total"
STALE_DOCUMENT_HITS_TOTAL = "protec_stale_document_hits_total"
CONFLICTING_SOURCE_TOTAL = "protec_conflicting_source_total"
RETRIEVAL_TOKENS = "protec_retrieval_tokens"
RERANKER_LATENCY_MS = "protec_reranker_latency_ms"

# Cache
CACHE_HITS_TOTAL = "protec_cache_hits_total"
CACHE_MISSES_TOTAL = "protec_cache_misses_total"
CACHE_STALE_PREVENTED_TOTAL = "protec_cache_stale_prevented_total"

# Business flow
FLOW_STARTS_TOTAL = "protec_flow_starts_total"
FLOW_STEP_COMPLETIONS_TOTAL = "protec_flow_step_completions_total"
FLOW_ABANDONMENT_TOTAL = "protec_flow_abandonment_total"
FLOW_VALIDATION_FAILURES_TOTAL = "protec_flow_validation_failures_total"
FLOW_API_FAILURES_TOTAL = "protec_flow_api_failures_total"
FLOW_COMPLETIONS_TOTAL = "protec_flow_completions_total"

# Provider
PROVIDER_CALLS_TOTAL = "protec_provider_calls_total"
PROVIDER_LATENCY_MS = "protec_provider_latency_ms"
PROVIDER_FAILURES_TOTAL = "protec_provider_failures_total"
CIRCUIT_OPEN_TOTAL = "protec_circuit_open_total"

# Auth
AUTH_FAILURES_TOTAL = "protec_auth_failures_total"
AUTHZ_DENIALS_TOTAL = "protec_authorization_denials_total"


metrics = MetricsRegistry()
