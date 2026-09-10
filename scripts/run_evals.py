"""Unified eval entry point (master prompt §26.9).

The conventional commands, implemented with this repository's tooling:

    python scripts/run_evals.py native        # eval:native
    python scripts/run_evals.py ragas         # eval:ragas
    python scripts/run_evals.py deepeval      # eval:deepeval
    python scripts/run_evals.py security      # eval:security
    python scripts/run_evals.py performance   # eval:performance
    python scripts/run_evals.py all           # eval:all

Exits non-zero when any selected gate fails, so CI and preprod promotion can gate on
it. A critical regression therefore blocks promotion rather than being advisory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "evals" / "reports"

#: name -> (description, command)
SUITES: dict[str, tuple[str, list[str]]] = {
    "native": (
        "Deterministic PASS/FAIL evals: workflow, auth, directives, PII, budgets, "
        "security, reliability, contracts.",
        [sys.executable, "-m", "evals.native.run"],
    ),
    "ragas": (
        "RAG quality plus the retrieval experiment comparison.",
        [sys.executable, "-m", "evals.ragas.runners.rag_eval"],
    ),
    "deepeval": (
        "Agent / LLM regression suites: faq, agents, conversation, safety.",
        [sys.executable, "-m", "evals.deepeval.run"],
    ),
    "security": (
        "Adversarial and authorization test suites.",
        [sys.executable, "-m", "pytest", "-q", "-m", "security", "tests/"],
    ),
    "performance": (
        "Latency, token and cost release gates.",
        [sys.executable, "-m", "pytest", "-q", "-s", "-m", "performance", "tests/"],
    ),
    "tests": (
        "The full pytest suite (unit, contract, integration, e2e, security, performance).",
        [sys.executable, "-m", "pytest", "-q"],
    ),
    "drill": (
        "Application-layer recovery drill: instance loss, concurrent writers, duplicate "
        "after failover, store outage, audit-chain continuity, knowledge restore.",
        [sys.executable, "scripts/recovery_drill.py"],
    ),
    "lint": (
        "Ruff lint and format checks.",
        [sys.executable, "-m", "ruff", "check", "."],
    ),
    "format": (
        "Ruff format verification.",
        [sys.executable, "-m", "ruff", "format", "--check", "."],
    ),
    "types": (
        "Static type checking.",
        [sys.executable, "-m", "mypy"],
    ),
}

#: What "all" runs, in order: cheap gates first so a failure surfaces quickly.
ALL_ORDER = ["lint", "format", "types", "tests", "drill", "native", "ragas", "deepeval"]


def run_suite(name: str) -> tuple[int, float]:
    description, command = SUITES[name]
    print("\n" + "=" * 78)
    print(f"RUNNING: {name}")
    print(f"  {description}")
    print(f"  $ {' '.join(command)}")
    print("=" * 78, flush=True)

    started = datetime.now(UTC)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    duration = (datetime.now(UTC) - started).total_seconds()
    print(f"\n-> {name}: exit={completed.returncode} in {duration:.1f}s")
    return completed.returncode, duration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation gates.")
    parser.add_argument(
        "suite",
        nargs="?",
        default="all",
        choices=[*SUITES, "all"],
        help="which gate to run (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="list the available gates")
    args = parser.parse_args(argv)

    if args.list:
        for name, (description, command) in SUITES.items():
            print(f"{name:<12} {description}\n{'':<12} $ {' '.join(command)}")
        return 0

    selected = ALL_ORDER if args.suite == "all" else [args.suite]
    results: dict[str, dict[str, float | int | str]] = {}
    failures: list[str] = []

    for name in selected:
        code, duration = run_suite(name)
        results[name] = {
            "exitCode": code,
            "durationSeconds": round(duration, 2),
            "verdict": "PASS" if code == 0 else "FAIL",
            "command": " ".join(SUITES[name][1]),
        }
        if code != 0:
            failures.append(name)

    print("\n" + "=" * 78)
    print("EVAL GATE SUMMARY")
    print("=" * 78)
    for name, outcome in results.items():
        print(f"  {outcome['verdict']:<5} {name:<12} {outcome['durationSeconds']}s")
    print(f"\nOVERALL: {'PASS' if not failures else 'FAIL'}")
    if failures:
        print(f"failed gates: {', '.join(failures)}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    summary = REPORTS / "gate-summary.json"
    summary.write_text(
        json.dumps(
            {
                "generatedAt": datetime.now(UTC).isoformat(),
                "selected": selected,
                "results": results,
                "verdict": "PASS" if not failures else "FAIL",
                "failedGates": failures,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"summary written: {summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
