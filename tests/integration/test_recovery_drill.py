"""The application-layer recovery drill is part of the gate, not a one-off script."""

from __future__ import annotations

import pytest

from scripts.recovery_drill import run_drill

pytestmark = pytest.mark.e2e


async def test_recovery_drill_passes_every_scenario():
    report = await run_drill()
    failed = [f"{c.scenario}: {c.name} ({c.detail})" for c in report.checks if not c.passed]
    assert not failed, failed
    assert report.transitions_lost == 0, "RPO for committed transitions must be zero"
    assert report.app_rto_ms < 5_000, "a replacement instance must be ready in well under 5 s"
    scenarios = {c.scenario for c in report.checks}
    assert scenarios == {"A", "B", "C", "D", "E", "F"}
