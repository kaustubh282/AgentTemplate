"""Cost and token report utility (master prompt §35).

Reads the machine-readable evidence in ``evals/reports`` and prints a FinOps summary
by environment, capability, model and outcome, with the configured ceilings applied.

    python scripts/cost_report.py
    python scripts/cost_report.py --live      # run a fresh benchmark first
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REPORTS = ROOT / "evals" / "reports"


def load(name: str) -> dict | None:
    path = REPORTS / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the FinOps cost report.")
    parser.add_argument("--live", action="store_true", help="run a fresh benchmark first")
    args = parser.parse_args(argv)

    if args.live:
        subprocess.run([sys.executable, "scripts/benchmark.py"], cwd=ROOT, check=False)

    benchmark = load("benchmark-report.json")
    if benchmark is None:
        print("No benchmark report found. Run: python scripts/benchmark.py")
        return 1

    from app.core.config.settings import Settings

    settings = Settings(APP_ENV="test")
    metrics = benchmark["metrics"]

    print("=" * 74)
    print("FINOPS COST REPORT (master prompt §35, §35.1)")
    print("=" * 74)
    print(f"generated       : {benchmark['generatedAt']}")
    print(f"environment     : {benchmark['environment']}")
    print(f"model           : {benchmark['modelId']}")
    print(f"corpus version  : {benchmark['corpusVersion']}")
    print(f"currency        : {settings.model_currency}")
    print()
    print("PRICING ASSUMPTIONS (REQUIRES_VERIFICATION)")
    print(f"  input  per 1k tokens : {settings.model_input_cost_per_1k}")
    print(f"  output per 1k tokens : {settings.model_output_cost_per_1k}")
    print()
    print("USAGE")
    print(f"  total requests            : {metrics['totalRequests']}")
    print(f"  model calls / request     : {metrics['modelCallsPerRequest']}")
    print(f"  zero-model-call rate      : {metrics['zeroModelCallRequestRate'] * 100:.1f}%")
    print(f"  avg input tokens          : {metrics['avgInputTokens']}")
    print(f"  median input tokens       : {metrics['medianInputTokens']}")
    print(f"  p95 input tokens          : {metrics['p95InputTokens']}")
    print(f"  avg output tokens         : {metrics['avgOutputTokens']}")
    print()
    print("COST")
    print(f"  per successful FAQ        : {metrics['costPerSuccessfulFaq']}")
    print(f"  per 1000 FAQ              : {metrics['costPer1000Faq']}")
    print(f"  per 1000 mixed requests   : {metrics['costPer1000Requests']}")
    print()
    print("CEILINGS (configurable; CI fails when exceeded)")
    checks = [
        ("MAX_COST_PER_1000_FAQ", settings.max_cost_per_1000_faq, metrics["costPer1000Faq"]),
        (
            "MAX_COST_PER_1000_MIXED_REQUESTS",
            settings.max_cost_per_1000_mixed_requests,
            metrics["costPer1000Requests"],
        ),
    ]
    breached = False
    for name, ceiling, actual in checks:
        ok = actual <= ceiling
        breached = breached or not ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<34} {actual} <= {ceiling}")

    for name, report_file in (
        ("Ragas", "ragas-report.json"),
        ("DeepEval", "deepeval-report.json"),
    ):
        report = load(report_file)
        if report:
            print(f"\n{name} judge status: {report['judge']['status']}")

    print("\nVERDICT:", "FAIL" if breached else "PASS")
    return 1 if breached else 0


if __name__ == "__main__":
    sys.exit(main())
