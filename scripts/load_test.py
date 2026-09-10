"""Executable load-test starter (master prompt §19, §28).

Four profiles, matching §19's list:

    faq         FAQ-only load
    mixed       FAQ + deterministic transaction traffic
    degraded    the same mix while a provider is failing
    burst       a short high-concurrency spike

It drives the ASGI application in-process, so it measures *application* behaviour
(concurrency, backpressure, per-request cost) without needing a deployed environment.
A production load test should additionally run against a deployed preprod through the
real gateway; this script is the starting point, not a substitute for that.

    python scripts/load_test.py mixed --concurrency 32 --requests 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "evals" / "reports"

FAQ_QUESTIONS = [
    "What is a deductible?",
    "What is a premium?",
    "What is sum insured?",
    "What is a no claim bonus?",
    "What is IDV in motor insurance?",
    "What is zero depreciation cover?",
    "Does travel insurance cover adventure sports?",
    "How do I raise a grievance?",
]

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}


@dataclass
class Sample:
    kind: str
    ok: bool
    latency_ms: float
    status: int = 200
    model_calls: int = 0


@dataclass
class LoadResult:
    profile: str
    concurrency: int
    requests: int
    samples: list[Sample] = field(default_factory=list)
    wall_seconds: float = 0.0
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def summary(self) -> dict[str, Any]:
        latencies = [s.latency_ms for s in self.samples]
        ordered = sorted(latencies)

        def pct(p: float) -> float:
            if not ordered:
                return 0.0
            rank = (len(ordered) - 1) * (p / 100.0)
            low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
            return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 2)

        by_kind: dict[str, dict[str, float]] = {}
        for kind in sorted({s.kind for s in self.samples}):
            subset = [s for s in self.samples if s.kind == kind]
            kind_latencies = sorted(s.latency_ms for s in subset)
            by_kind[kind] = {
                "count": len(subset),
                "successRate": round(sum(1 for s in subset if s.ok) / len(subset), 4),
                "p50": round(statistics.median(kind_latencies), 2),
                "p95": round(
                    kind_latencies[min(int(len(kind_latencies) * 0.95), len(kind_latencies) - 1)], 2
                ),
                "modelCallsPerRequest": round(sum(s.model_calls for s in subset) / len(subset), 3),
            }

        return {
            "profile": self.profile,
            "generatedAt": self.generated_at,
            "concurrency": self.concurrency,
            "requests": len(self.samples),
            "wallSeconds": round(self.wall_seconds, 2),
            "throughputRps": round(len(self.samples) / self.wall_seconds, 2) if self.wall_seconds else 0.0,
            "successRate": round(sum(1 for s in self.samples if s.ok) / len(self.samples), 4)
            if self.samples
            else 0.0,
            "latency": {
                "avg": round(statistics.fmean(latencies), 2) if latencies else 0.0,
                "p50": pct(50),
                "p95": pct(95),
                "p99": pct(99),
                "max": round(max(latencies), 2) if latencies else 0.0,
            },
            "statusCounts": _counts(str(s.status) for s in self.samples),
            "byKind": by_kind,
        }


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ================================== drivers ==================================
async def mint_conversation(client: Any, headers: dict[str, str] | None = None) -> str:
    """Conversation ids are server-minted (§16); a load client obtains one like any client."""
    created = await client.post("/api/v1/conversations", json={}, headers=headers or {})
    if created.status_code != 201:
        raise RuntimeError(f"could not mint a conversation: {created.status_code} {created.text}")
    conversation_id: str = created.json()["conversation_id"]
    return conversation_id


async def faq_request(client: Any, conversation_id: str, question: str) -> Sample:
    started = time.perf_counter()
    response = await client.post(
        "/api/v1/chat", json={"conversation_id": conversation_id, "message": question}
    )
    latency = (time.perf_counter() - started) * 1000
    body = response.json() if response.status_code == 200 else {}
    return Sample(
        kind="faq",
        ok=response.status_code == 200,
        latency_ms=latency,
        status=response.status_code,
        model_calls=(body.get("meta") or {}).get("modelCalls", 0),
    )


async def transaction_requests(client: Any, token: str, index: int) -> list[Sample]:
    from app.domains.motor import workflow as wf

    samples: list[Sample] = []
    headers = {"Authorization": f"Bearer {token}"}

    started = time.perf_counter()
    created = await client.post("/api/v1/conversations", json={}, headers=headers)
    samples.append(
        Sample(
            "conversation",
            created.status_code == 201,
            (time.perf_counter() - started) * 1000,
            created.status_code,
        )
    )
    if created.status_code != 201:
        return samples
    conversation_id = created.json()["conversation_id"]
    scoped = {**headers, "X-Conversation-Id": conversation_id}

    started = time.perf_counter()
    flow = await client.post(
        "/api/v1/flows/start",
        headers=scoped,
        json={"conversation_id": conversation_id, "capability_id": "motor.workflow.start"},
    )
    samples.append(
        Sample(
            "flow_start", flow.status_code == 200, (time.perf_counter() - started) * 1000, flow.status_code
        )
    )

    for action, payload in [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
        (wf.ACTION_REQUEST_QUOTE, {}),
    ]:
        started = time.perf_counter()
        response = await client.post(
            "/api/v1/actions",
            headers=scoped,
            json={
                "conversation_id": conversation_id,
                "capability_id": "motor.workflow.action",
                "action": action,
                "payload": payload,
                "confirmed": False,
            },
        )
        body = response.json() if response.status_code == 200 else {}
        samples.append(
            Sample(
                kind="deterministic_action",
                ok=response.status_code == 200,
                latency_ms=(time.perf_counter() - started) * 1000,
                status=response.status_code,
                model_calls=(body.get("meta") or {}).get("modelCalls", 0),
            )
        )
    return samples


async def run_profile(profile: str, concurrency: int, requests: int) -> LoadResult:
    import httpx

    from app.main import create_app
    from tests.conftest import default_responder, make_settings, make_token

    settings = make_settings(RATE_LIMIT_ENABLED="false")
    app = create_app(settings, responder=default_responder())
    result = LoadResult(profile=profile, concurrency=concurrency, requests=requests)
    token = make_token()
    random.seed(1234)

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://loadtest") as client,
        # The lifespan is entered explicitly so the container exists.
        app.router.lifespan_context(app),
    ):
        # (single block so the client and lifespan share one scope)
        container = app.state.container
        if profile == "degraded":
            container.faults.unavailable_operations.add("create_quote")

        semaphore = asyncio.Semaphore(concurrency)

        async def one(index: int) -> list[Sample]:
            async with semaphore:
                if profile == "faq":
                    conversation_id = await mint_conversation(client)
                    return [await faq_request(client, conversation_id, random.choice(FAQ_QUESTIONS))]
                if profile in ("mixed", "degraded", "burst"):
                    if index % 3 == 0:
                        return await transaction_requests(client, token, index)
                    conversation_id = await mint_conversation(client)
                    return [await faq_request(client, conversation_id, random.choice(FAQ_QUESTIONS))]
                raise ValueError(f"unknown profile: {profile}")

        started = time.perf_counter()
        batches = await asyncio.gather(*(one(i) for i in range(requests)))
        result.wall_seconds = time.perf_counter() - started

    for batch in batches:
        result.samples.extend(batch)
    return result


PROFILE_GATES = {
    "faq": {"successRate": 0.99, "p95": 3000.0},
    "mixed": {"successRate": 0.99, "p95": 3000.0},
    "burst": {"successRate": 0.95, "p95": 5000.0},
    # A degraded provider is expected to produce controlled retry directives, which
    # are HTTP 200 with a retry directive, so success stays high by design.
    "degraded": {"successRate": 0.95, "p95": 5000.0},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an application load-test profile.")
    parser.add_argument("profile", choices=["faq", "mixed", "degraded", "burst"], default="mixed", nargs="?")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--requests", type=int, default=200)
    args = parser.parse_args(argv)

    concurrency = args.concurrency if args.profile != "burst" else max(args.concurrency, 64)
    result = asyncio.run(run_profile(args.profile, concurrency, args.requests))
    summary = result.summary()

    print("\n" + "=" * 70)
    print(f"LOAD TEST: {args.profile} (concurrency={concurrency}, iterations={args.requests})")
    print("=" * 70)
    print(f"requests      : {summary['requests']}")
    print(f"wall seconds  : {summary['wallSeconds']}")
    print(f"throughput    : {summary['throughputRps']} req/s")
    print(f"success rate  : {summary['successRate']}")
    print(
        f"latency ms    : avg={summary['latency']['avg']} p50={summary['latency']['p50']} "
        f"p95={summary['latency']['p95']} p99={summary['latency']['p99']} max={summary['latency']['max']}"
    )
    print(f"status counts : {summary['statusCounts']}")
    print("\nby request kind:")
    for kind, stats in summary["byKind"].items():
        print(
            f"  {kind:<22} n={stats['count']:<5} success={stats['successRate']:<7} "
            f"p50={stats['p50']:<8} p95={stats['p95']:<8} modelCalls/req={stats['modelCallsPerRequest']}"
        )

    gates = PROFILE_GATES[args.profile]
    failures = []
    if summary["successRate"] < gates["successRate"]:
        failures.append(f"success rate {summary['successRate']} < {gates['successRate']}")
    if summary["latency"]["p95"] > gates["p95"]:
        failures.append(f"p95 {summary['latency']['p95']}ms > {gates['p95']}ms")

    summary["gates"] = gates
    summary["failures"] = failures
    summary["verdict"] = "PASS" if not failures else "FAIL"

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"load-test-{args.profile}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nGATES: {gates}")
    for failure in failures:
        print(f"  FAIL  {failure}")
    print(f"report written: {path}")
    print(f"VERDICT: {summary['verdict']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
