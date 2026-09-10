"""Native deterministic eval entry point (master prompt §26.9).

    python -m evals.native.run              # every suite
    python -m evals.native.run workflow pii # selected suites
    python -m evals.native.run --verbose

Exits non-zero on any failure so CI/preprod promotion can gate on it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from evals.native.harness import NativeEvalRunner, exit_code, print_report, write_report
from evals.native.suites import register_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the native deterministic evals.")
    parser.add_argument("suites", nargs="*", help="suite names (default: all)")
    parser.add_argument("--verbose", "-v", action="store_true", help="show every check")
    parser.add_argument("--list", action="store_true", help="list the available suites")
    parser.add_argument(
        "--report", default="native-eval-report.json", help="report filename under evals/reports"
    )
    args = parser.parse_args(argv)

    runner = NativeEvalRunner()
    register_all(runner)

    if args.list:
        for name in runner.names():
            print(name)
        return 0

    results = asyncio.run(runner.run(*args.suites))
    print_report(results, verbose=args.verbose)
    path = write_report(results, filename=args.report)
    print(f"\nreport written: {path}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
