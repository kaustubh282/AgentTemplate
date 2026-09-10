"""DeepEval agent / LLM regression runner (master prompt §26.2, §26.6).

Four suites, matching §26.4's directory intent:

    faq           answer behaviour: relevance, grounding, refusal, hallucination
    agents        intent/extraction accuracy, tool permission, structured output
    conversation  multi-turn stability and interrupt -> FAQ -> resume behaviour
    safety        direct/indirect injection, extraction, escalation, denial-of-wallet

Metrics are project-owned and deterministic (see ``metrics.py``), so the suite runs in
CI with no judge model. Judge-dependent DeepEval metrics are listed as
``NOT_EVIDENCED`` with the exact reason rather than being fabricated (§26.12).

    python -m evals.deepeval.run
    python -m evals.deepeval.run faq safety --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase

from evals.deepeval.metrics import (
    DEFAULT_THRESHOLDS,
    JUDGE_METRIC_NAMES,
    agent_metrics,
    conversation_metrics,
    faq_metrics,
    safety_metrics,
)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def judge_available() -> tuple[bool, str]:
    if os.environ.get("DEEPEVAL_JUDGE_DISABLED", "").lower() in ("1", "true", "yes"):
        return False, "DEEPEVAL_JUDGE_DISABLED is set"
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPEVAL_JUDGE_API_KEY")):
        return False, (
            "no judge credentials found (set DEEPEVAL_JUDGE_API_KEY or OPENAI_API_KEY) - "
            "LLM-as-judge metrics are NOT_EVIDENCED"
        )
    return True, "judge configured"


def thresholds_from_env() -> dict[str, float]:
    resolved = dict(DEFAULT_THRESHOLDS)
    for key in resolved:
        env_key = f"DEEPEVAL_THRESHOLD_{key.upper()}"
        if env_key in os.environ:
            resolved[key] = float(os.environ[env_key])
    return resolved


@dataclass
class MetricOutcome:
    metric: str
    scores: list[float] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    threshold: float = 1.0

    @property
    def mean(self) -> float:
        return round(statistics.fmean(self.scores), 4) if self.scores else 0.0

    @property
    def passed(self) -> bool:
        return not self.failures and bool(self.scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "threshold": self.threshold,
            "cases": len(self.scores),
            "mean": self.mean,
            "passed": self.passed,
            "failures": [{"caseId": cid, "reason": reason} for cid, reason in self.failures],
        }


@dataclass
class SuiteOutcome:
    name: str
    cases: int = 0
    metrics: dict[str, MetricOutcome] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.metrics.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.name,
            "cases": self.cases,
            "verdict": "PASS" if self.passed else "FAIL",
            "metrics": [m.to_dict() for m in self.metrics.values()],
        }


@dataclass
class DeepEvalReport:
    framework_version: str
    dataset_version: str
    judge_status: str
    judge_reason: str
    thresholds: dict[str, float]
    suites: list[SuiteOutcome] = field(default_factory=list)
    judge_metrics: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    environment: str = "test"
    model_id: str = ""
    prompt_version: str = ""
    corpus_version: str = ""
    command: str = "python -m evals.deepeval.run"

    @property
    def verdict(self) -> str:
        return "PASS" if all(s.passed for s in self.suites) else "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": "deepeval",
            "frameworkVersion": self.framework_version,
            "generatedAt": self.generated_at,
            "command": self.command,
            "environment": self.environment,
            "datasetVersion": self.dataset_version,
            "modelId": self.model_id,
            "promptVersion": self.prompt_version,
            "corpusVersion": self.corpus_version,
            "judge": {"status": self.judge_status, "reason": self.judge_reason},
            "thresholds": self.thresholds,
            "suites": [s.to_dict() for s in self.suites],
            "judgeMetricsNotEvidenced": self.judge_metrics,
            "verdict": self.verdict,
        }


# ------------------------------------------------------------ case builders ---
async def build_faq_cases(container: Any) -> list[LLMTestCase]:
    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from evals.datasets.schema import load_jsonl
    from evals.native.suites import context_for

    cases: list[LLMTestCase] = []
    for case in load_jsonl("faq-golden.jsonl").cases:
        result = await container.harness.execute(
            context_for(case.actor, conversation_id=f"conv_de_{case.case_id}"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message=case.input,
                payload={"question": case.input},
            ),
        )
        record = result.record
        payload = result.payload or {}
        retrieval_context = _retrieval_context(container, case.input, case.actor)

        cases.append(
            LLMTestCase(
                name=case.case_id,
                input=case.input,
                actual_output=result.message,
                expected_output=case.reference_answer or "",
                retrieval_context=retrieval_context,
                token_cost=record.estimated_cost if record else 0.0,
                input_token_count=record.input_tokens if record else 0,
                output_token_count=record.output_tokens if record else 0,
                tools_called=[],
                metadata={
                    "outcome": result.outcome.value,
                    "expected_outcome": "ABSTAIN" if "abstention" in case.tags else "ALLOW",
                    "expected_sources": case.expected_sources,
                    "citations": [c["document_id"] for c in payload.get("citations", [])],
                    "forbidden_contains": case.forbidden_contains,
                    "model_calls": record.model_calls if record else 0,
                    "agent_steps": record.agent_steps if record else 0,
                    "agent_handoffs": record.agent_handoffs if record else 0,
                    "input_tokens": record.input_tokens if record else 0,
                    "output_tokens": record.output_tokens if record else 0,
                    "latency_ms": round(record.latency_ms, 2) if record else 0.0,
                    "max_model_calls": case.max_model_calls,
                    "max_agent_steps": case.max_agent_steps,
                    "max_input_tokens": case.max_input_tokens,
                    "latency_budget_ms": case.latency_budget_ms,
                },
            )
        )
    return cases


def _retrieval_context(container: Any, question: str, actor: str) -> list[str]:
    from app.rag.governance.documents import Audience
    from app.rag.retrieval.retriever import RetrievalFilter

    audience = (
        Audience.AGENT
        if actor.startswith("AGENT")
        else (Audience.CUSTOMER if actor.startswith("CUSTOMER") else Audience.PUBLIC)
    )
    result = container.retriever.retrieve(question, RetrievalFilter(audience=audience))
    return [c.chunk.text for c in result.chunks]


async def build_agent_cases(container: Any) -> list[LLMTestCase]:
    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_INTENT
    from evals.datasets.schema import load_jsonl
    from evals.native.suites import context_for

    cases: list[LLMTestCase] = []
    for case in load_jsonl("ambiguous-intent.jsonl").cases:
        if "fast-path" in case.tags:
            # These must never reach a model at all: proven by the router below.
            decision = container.router.route_message(case.input)
            cases.append(
                LLMTestCase(
                    name=case.case_id,
                    input=case.input,
                    actual_output=f"routed to {decision.capability_id or decision.action}",
                    tools_called=[],
                    metadata={
                        "outcome": "START_FLOW" if not decision.requires_model else "AI_PATH",
                        "expected_outcome": case.expected_outcome.value if case.expected_outcome else None,
                        "expected_intent": case.expected_intent,
                        "actual_intent": "START_PURCHASE" if decision.action == "BEGIN" else "OTHER",
                        "model_calls": 0,
                        "agent_steps": 0,
                        "agent_handoffs": 0,
                        "max_model_calls": case.max_model_calls,
                        "max_agent_steps": case.max_agent_steps,
                        "latency_budget_ms": case.latency_budget_ms,
                        "forbidden_tools": case.forbidden_tools,
                        "payload": {"capability": decision.capability_id},
                    },
                )
            )
            continue

        result = await container.harness.execute(
            context_for(case.actor, conversation_id=f"conv_de_{case.case_id}"),
            CapabilityRequest(
                capability_id=CAPABILITY_INTENT,
                user_message=case.input,
                payload={"message": case.input},
            ),
        )
        record = result.record
        payload = result.payload or {}
        cases.append(
            LLMTestCase(
                name=case.case_id,
                input=case.input,
                actual_output=result.message or json.dumps(payload),
                tools_called=[],
                input_token_count=record.input_tokens if record else 0,
                output_token_count=record.output_tokens if record else 0,
                metadata={
                    "outcome": result.outcome.value,
                    "expected_outcome": case.expected_outcome.value if case.expected_outcome else None,
                    "expected_intent": case.expected_intent,
                    "actual_intent": payload.get("intent"),
                    "model_calls": record.model_calls if record else 0,
                    "agent_steps": record.agent_steps if record else 0,
                    "agent_handoffs": record.agent_handoffs if record else 0,
                    "input_tokens": record.input_tokens if record else 0,
                    "latency_ms": round(record.latency_ms, 2) if record else 0.0,
                    "max_model_calls": case.max_model_calls,
                    "max_agent_steps": case.max_agent_steps,
                    "max_input_tokens": case.max_input_tokens,
                    "latency_budget_ms": case.latency_budget_ms,
                    "forbidden_tools": case.forbidden_tools,
                    "payload": payload,
                    "required_payload_keys": ["intent", "confidence", "entities"] if payload else [],
                },
            )
        )
    return cases


async def build_safety_cases(container: Any) -> list[LLMTestCase]:
    from app.ai.harness.service import CapabilityRequest
    from app.core.errors.taxonomy import ForbiddenError
    from app.integrations.mock import fixtures
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from evals.datasets.schema import load_jsonl
    from evals.native.suites import context_for

    cases: list[LLMTestCase] = []
    for case in load_jsonl("adversarial.jsonl").cases:
        ctx = context_for(case.actor, conversation_id=f"conv_de_{case.case_id}")

        if "authorization" in case.tags:
            requested = fixtures.CUSTOMER_A if case.actor == "AGENT_UNASSIGNED" else None
            try:
                await container.tools.get_policy_details(ctx, case.input, requested_customer_id=requested)
                outcome, output, tools = "ALLOW", "authoritative read succeeded", ["GetPolicyDetails"]
            except ForbiddenError as exc:
                outcome, output, tools = "BLOCK", exc.safe_message, []
            cases.append(
                LLMTestCase(
                    name=case.case_id,
                    input=case.input,
                    actual_output=output,
                    tools_called=tools,
                    metadata={
                        "outcome": outcome,
                        "expected_outcome": "BLOCK",
                        "forbidden_contains": case.forbidden_contains,
                        "forbidden_tools": case.forbidden_tools,
                        "model_calls": 0,
                        "agent_handoffs": 0,
                        "max_model_calls": case.max_model_calls,
                    },
                )
            )
            continue

        result = await container.harness.execute(
            ctx,
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message=case.input,
                payload={"question": case.input},
            ),
        )
        record = result.record
        cases.append(
            LLMTestCase(
                name=case.case_id,
                input=case.input,
                actual_output=result.message,
                tools_called=list(record.evidence_document_ids) if record else [],
                input_token_count=record.input_tokens if record else 0,
                metadata={
                    "outcome": result.outcome.value,
                    "expected_outcome": "BLOCK",
                    "forbidden_contains": case.forbidden_contains,
                    "forbidden_tools": case.forbidden_tools,
                    "model_calls": record.model_calls if record else 0,
                    "agent_steps": record.agent_steps if record else 0,
                    "agent_handoffs": record.agent_handoffs if record else 0,
                    "input_tokens": record.input_tokens if record else 0,
                    "max_model_calls": case.max_model_calls,
                },
            )
        )
    return cases


async def build_conversation_cases(container: Any) -> list[LLMTestCase]:
    """Multi-turn stability: interrupt -> FAQ -> resume must not disturb the flow."""
    from app.domains.motor import workflow as wf
    from evals.native.suites import context_for

    conversation_id = "conv_de_multiturn"
    ctx = context_for("CUSTOMER", conversation_id=conversation_id)

    cases: list[LLMTestCase] = []
    await container.orchestrator.handle_message(ctx, "I want to buy a policy")
    await container.orchestrator.handle_action(ctx, "motor.workflow.action", wf.ACTION_BEGIN, {})
    await container.orchestrator.handle_action(
        ctx, "motor.workflow.action", wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
    )

    state_before = await container.workflow_engine.get_active(conversation_id, ctx)

    turns = [
        "What is a deductible?",
        "What is a no claim bonus?",
        "What is IDV in motor insurance?",
        "continue",
    ]
    for index, turn in enumerate(turns, start=1):
        response = await container.orchestrator.handle_message(ctx, turn)
        state_after = await container.workflow_engine.get_active(conversation_id, ctx)
        unchanged = (
            state_before is not None
            and state_after is not None
            and state_after.state == state_before.state
            and state_after.version == state_before.version
        )
        cases.append(
            LLMTestCase(
                name=f"conv-{index:03d}",
                input=turn,
                actual_output=response.message,
                metadata={
                    "outcome": "ALLOW" if unchanged else "STATE_MUTATED",
                    "expected_outcome": "ALLOW",
                    "forbidden_contains": ["guaranteed", "definitely covered"],
                    "model_calls": response.meta.get("modelCalls", 0),
                    "agent_steps": response.meta.get("agentSteps", 0),
                    "agent_handoffs": response.meta.get("agentHandoffs", 0),
                    "input_tokens": response.meta.get("inputTokens", 0),
                    "max_model_calls": 1,
                    "max_agent_steps": 1,
                    "max_input_tokens": 2500,
                    "latency_budget_ms": 3000,
                },
            )
        )
    return cases


# ---------------------------------------------------------------- execution ---
def evaluate_suite(name: str, cases: list[LLMTestCase], metrics: list[BaseMetric]) -> SuiteOutcome:
    """Run each metric over each case using DeepEval's metric protocol."""
    outcome = SuiteOutcome(name=name, cases=len(cases))
    for metric in metrics:
        label = getattr(metric, "__name__", metric.__class__.__name__)
        record = MetricOutcome(metric=label, threshold=metric.threshold)
        for case in cases:
            metric.measure(case)
            record.scores.append(metric.score)
            if not metric.is_successful():
                record.failures.append((case.name or "unnamed", metric.reason))
        outcome.metrics[label] = record
    return outcome


async def run(selected: list[str]) -> DeepEvalReport:
    import importlib.metadata as md

    from app.bootstrap import build_container
    from tests.conftest import default_responder, make_settings

    settings = make_settings()
    thresholds = thresholds_from_env()
    available, reason = judge_available()

    report = DeepEvalReport(
        framework_version=md.version("deepeval"),
        dataset_version="1.0.0",
        judge_status="AVAILABLE" if available else "UNAVAILABLE",
        judge_reason=reason,
        thresholds=thresholds,
        judge_metrics=[] if available else list(JUDGE_METRIC_NAMES),
        environment=settings.app_env.value,
        model_id=settings.model_id,
    )

    builders = {
        "faq": (build_faq_cases, faq_metrics),
        "agents": (build_agent_cases, agent_metrics),
        "safety": (build_safety_cases, safety_metrics),
        "conversation": (build_conversation_cases, conversation_metrics),
    }
    names = selected or list(builders)

    for name in names:
        if name not in builders:
            raise KeyError(f"unknown deepeval suite: {name}")
        build_cases, metric_factory = builders[name]
        container = build_container(settings, responder=default_responder())
        report.prompt_version = container.prompts.combined_version()
        report.corpus_version = container.corpus.version
        cases = await build_cases(container)
        report.suites.append(evaluate_suite(name, cases, metric_factory(thresholds)))

    return report


def print_report(report: DeepEvalReport, *, verbose: bool = False) -> None:
    print("\n" + "=" * 78)
    print("DEEPEVAL AGENT / LLM EVALUATION (master prompt §26.2, §26.6)")
    print("=" * 78)
    print(f"deepeval        : {report.framework_version}")
    print(f"dataset version : {report.dataset_version}")
    print(f"environment     : {report.environment}")
    print(f"model           : {report.model_id}")
    print(f"prompt version  : {report.prompt_version}")
    print(f"corpus version  : {report.corpus_version}")
    print(f"judge           : {report.judge_status} - {report.judge_reason}")

    for suite in report.suites:
        print(f"\n[{'PASS' if suite.passed else 'FAIL'}] suite '{suite.name}' ({suite.cases} cases)")
        for metric in suite.metrics.values():
            status = "PASS" if metric.passed else "FAIL"
            print(
                f"    {status}  {metric.metric:<38} mean={metric.mean:.3f} "
                f"threshold={metric.threshold:.2f} n={len(metric.scores)}"
            )
            for case_id, why in metric.failures[: (None if verbose else 5)]:
                print(f"           - {case_id}: {why}")

    if report.judge_metrics:
        print(f"\nNOT_EVIDENCED (judge unavailable): {', '.join(report.judge_metrics)}")
        print(f"  reason: {report.judge_reason}")

    print(f"\nVERDICT: {report.verdict}")


def write_report(report: DeepEvalReport, filename: str = "deepeval-report.json") -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / filename
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the DeepEval agent regression suites.")
    parser.add_argument("suites", nargs="*", help="faq | agents | safety | conversation")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    report = asyncio.run(run(args.suites))
    print_report(report, verbose=args.verbose)
    path = write_report(report)
    print(f"report written: {path}")
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
