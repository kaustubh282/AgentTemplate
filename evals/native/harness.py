"""Native deterministic eval harness (master prompt §26.3).

Facts that must not depend on an LLM judge are asserted here with exact PASS/FAIL:

    Expected model calls = 0
    Actual model calls   = 1
    Result               = FAIL

...not an opinion score. The harness drives the *real* application container, so a
pass is evidence about the shipped system rather than about a stub.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass(slots=True)
class CheckResult:
    """One assertion, with the expected and actual values recorded verbatim."""

    name: str
    verdict: Verdict
    expected: Any = None
    actual: Any = None
    detail: str = ""

    def render(self) -> str:
        if self.verdict is Verdict.PASS:
            return f"  PASS  {self.name}"
        return (
            f"  {self.verdict.value:5s} {self.name}\n"
            f"        expected = {self.expected!r}\n"
            f"        actual   = {self.actual!r}"
            + (f"\n        detail   = {self.detail}" if self.detail else "")
        )


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    checks: list[CheckResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def verdict(self) -> Verdict:
        if any(c.verdict is Verdict.ERROR for c in self.checks):
            return Verdict.ERROR
        if any(c.verdict is Verdict.FAIL for c in self.checks):
            return Verdict.FAIL
        if self.checks and all(c.verdict is Verdict.SKIP for c in self.checks):
            return Verdict.SKIP
        return Verdict.PASS

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.verdict in (Verdict.FAIL, Verdict.ERROR)]


@dataclass(slots=True)
class SuiteResult:
    """One named suite (workflow, auth, pii, ...)."""

    name: str
    cases: list[CaseResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_ms: float = 0.0
    #: Set when a suite could not run at all (e.g. a missing dependency).
    skipped_reason: str | None = None

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.verdict is Verdict.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.verdict in (Verdict.FAIL, Verdict.ERROR))

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.verdict is Verdict.SKIP)

    @property
    def verdict(self) -> Verdict:
        if self.skipped_reason:
            return Verdict.SKIP
        return Verdict.FAIL if self.failed else Verdict.PASS

    @property
    def pass_rate(self) -> float:
        scored = self.total - self.skipped
        return round(self.passed / scored, 4) if scored else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.name,
            "verdict": self.verdict.value,
            "startedAt": self.started_at,
            "durationMs": round(self.duration_ms, 2),
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "passRate": self.pass_rate,
            "skippedReason": self.skipped_reason,
            "failures": [
                {
                    "caseId": case.case_id,
                    "checks": [
                        {
                            "name": check.name,
                            "verdict": check.verdict.value,
                            "expected": check.expected,
                            "actual": check.actual,
                            "detail": check.detail,
                        }
                        for check in case.failures
                    ],
                }
                for case in self.cases
                if case.failures
            ],
        }


class CaseRecorder:
    """Collects checks for one case. Every assertion is explicit and comparable."""

    def __init__(self, case_id: str, category: str) -> None:
        self.result = CaseResult(case_id=case_id, category=category)
        self._started = time.perf_counter()

    # -- assertions ---------------------------------------------------------
    def equals(self, name: str, expected: Any, actual: Any, detail: str = "") -> bool:
        ok = expected == actual
        self.result.checks.append(
            CheckResult(name, Verdict.PASS if ok else Verdict.FAIL, expected, actual, detail)
        )
        return ok

    def at_most(self, name: str, limit: Any, actual: Any, detail: str = "") -> bool:
        if limit is None:
            self.result.checks.append(CheckResult(name, Verdict.SKIP, limit, actual, "no limit set"))
            return True
        ok = actual <= limit
        self.result.checks.append(
            CheckResult(name, Verdict.PASS if ok else Verdict.FAIL, f"<= {limit}", actual, detail)
        )
        return ok

    def at_least(self, name: str, floor: Any, actual: Any, detail: str = "") -> bool:
        ok = actual >= floor
        self.result.checks.append(
            CheckResult(name, Verdict.PASS if ok else Verdict.FAIL, f">= {floor}", actual, detail)
        )
        return ok

    def is_true(self, name: str, actual: bool, detail: str = "") -> bool:
        self.result.checks.append(
            CheckResult(name, Verdict.PASS if actual else Verdict.FAIL, True, actual, detail)
        )
        return bool(actual)

    def contains_all(self, name: str, required: list[str], haystack: str) -> bool:
        lowered = haystack.lower()
        missing = [r for r in required if r.lower() not in lowered]
        ok = not missing
        self.result.checks.append(
            CheckResult(
                name,
                Verdict.PASS if ok else Verdict.FAIL,
                f"contains {required}",
                f"missing {missing}",
            )
        )
        return ok

    def contains_none(self, name: str, forbidden: list[str], haystack: str) -> bool:
        lowered = haystack.lower()
        present = [f for f in forbidden if f.lower() in lowered]
        ok = not present
        self.result.checks.append(
            CheckResult(
                name,
                Verdict.PASS if ok else Verdict.FAIL,
                f"contains none of {forbidden}",
                f"present {present}",
            )
        )
        return ok

    def error(self, name: str, exc: BaseException) -> None:
        self.result.checks.append(
            CheckResult(name, Verdict.ERROR, "no exception", type(exc).__name__, str(exc)[:200])
        )

    def skip(self, name: str, reason: str) -> None:
        self.result.checks.append(CheckResult(name, Verdict.SKIP, None, None, reason))

    def finish(self) -> CaseResult:
        self.result.duration_ms = (time.perf_counter() - self._started) * 1000
        return self.result


SuiteFn = Callable[[], Awaitable[SuiteResult]]


class NativeEvalRunner:
    """Registers and runs the native suites, then writes a machine-readable report."""

    def __init__(self) -> None:
        self._suites: dict[str, SuiteFn] = {}

    def register(self, name: str, fn: SuiteFn) -> None:
        self._suites[name] = fn

    def names(self) -> tuple[str, ...]:
        return tuple(self._suites)

    async def run(self, *names: str) -> list[SuiteResult]:
        selected = names or tuple(self._suites)
        results: list[SuiteResult] = []
        for name in selected:
            fn = self._suites.get(name)
            if fn is None:
                raise KeyError(f"unknown native suite: {name}")
            started = time.perf_counter()
            suite = await fn()
            suite.duration_ms = (time.perf_counter() - started) * 1000
            results.append(suite)
        return results


def print_report(results: list[SuiteResult], *, verbose: bool = False) -> None:
    print("\n" + "=" * 78)
    print("NATIVE DETERMINISTIC EVALS (master prompt §26.3)")
    print("=" * 78)
    for suite in results:
        status = suite.verdict.value
        if suite.skipped_reason:
            print(f"\n[{status}] {suite.name}: {suite.skipped_reason}")
            continue
        print(
            f"\n[{status}] {suite.name}: {suite.passed}/{suite.total} passed"
            f" ({suite.pass_rate * 100:.1f}%), {suite.failed} failed,"
            f" {suite.skipped} skipped, {suite.duration_ms:.0f}ms"
        )
        for case in suite.cases:
            if case.failures or verbose:
                print(f"  case {case.case_id} [{case.verdict.value}]")
                for check in case.checks:
                    if check.verdict is not Verdict.PASS or verbose:
                        print(check.render())

    total = sum(s.total for s in results)
    passed = sum(s.passed for s in results)
    failed = sum(s.failed for s in results)
    print("\n" + "-" * 78)
    print(f"TOTAL: {passed}/{total} passed, {failed} failed")
    print("OVERALL:", "PASS" if failed == 0 else "FAIL")
    print("-" * 78)


def write_report(results: list[SuiteResult], *, filename: str = "native-eval-report.json") -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "framework": "native-deterministic",
        "generatedAt": datetime.now(UTC).isoformat(),
        "suites": [s.to_dict() for s in results],
        "totals": {
            "total": sum(s.total for s in results),
            "passed": sum(s.passed for s in results),
            "failed": sum(s.failed for s in results),
            "skipped": sum(s.skipped for s in results),
        },
        "verdict": "PASS" if not any(s.failed for s in results) else "FAIL",
    }
    path = REPORTS_DIR / filename
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def exit_code(results: list[SuiteResult]) -> int:
    """Non-zero when any suite failed, so CI can gate on it (§26.9)."""
    return 1 if any(s.failed for s in results) else 0
