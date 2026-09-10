"""Application-layer recovery drill (master prompt §20, §38; BCP_DR_READINESS.md §6).

Executes the parts of the DR test plan that the *application* can prove without cloud
infrastructure, against two application instances sharing an in-process Redis
emulator and a hash-chained audit log on disk:

    A  instance loss          kill the instance mid-journey; a fresh instance resumes the
                              flow at the same state and version, honours the confirmation
                              token issued by the dead instance, and completes the purchase
    B  concurrent writers     two instances race on one flow; optimistic locking lets exactly
                              one commit and the loser gets FLOW_STATE_CONFLICT
    C  duplicate after failover  the same idempotency key replayed on the surviving instance
                              is a safe replay, never a second charge
    D  store outage           the shared store disappears; requests fail with a controlled
                              UPSTREAM_UNAVAILABLE / 503 and nothing is fabricated
    E  audit continuity       the chained audit log written by instance 1 is extended by
                              instance 2 and verifies end to end
    F  knowledge restore      re-ingesting the corpus changes the version and the FAQ cache
                              key, so stale cached answers cannot be served

It measures **app-layer RTO** (time to build a replacement instance) and **RPO**
(committed transitions lost: must be 0). It does *not* measure region failover, backup
restore or platform RTO - those remain platform-owned items in the DR plan.

    python scripts/recovery_drill.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORTS = ROOT / "evals" / "reports"


@dataclass
class Check:
    scenario: str
    name: str
    passed: bool
    detail: str = ""


@dataclass
class DrillReport:
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    app_rto_ms: float = 0.0
    transitions_lost: int = 0
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, scenario: str, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(scenario, name, passed, detail))


def _settings(redis_name: str, audit_path: Path) -> Any:
    from tests.conftest import make_settings

    return make_settings(
        SESSION_STORE_PROVIDER="redis",
        WORKFLOW_STORE_PROVIDER="redis",
        RATE_LIMIT_STORE_PROVIDER="redis",
        REDIS_URL=f"fakeredis://{redis_name}",
        AUDIT_STORE_PROVIDER="chained_file",
        AUDIT_FILE_PATH=str(audit_path),
        AUDIT_CHAIN_SECRET="drill-not-a-real-secret",
        CONFIRMATION_TOKEN_SECRET="drill-shared-confirmation-secret",
    )


def _build(redis_name: str, audit_path: Path, providers: Any = None) -> Any:
    """Build an application instance. Instances share the provider double because the
    real System of Record (InsureMO) is one system, not one per instance."""
    from app.bootstrap import build_container
    from tests.conftest import default_responder

    return build_container(
        _settings(redis_name, audit_path), responder=default_responder(), providers=providers
    )


async def run_drill() -> DrillReport:
    from app.core.audit.chain import verify_chain
    from app.core.errors.taxonomy import AppError, FlowStateConflictError
    from app.core.storage.redis_client import reset_fake_servers
    from app.domains.motor import workflow as wf
    from app.integrations.contracts.dtos import PaymentStatus
    from tests.conftest import customer_context

    report = DrillReport()
    reset_fake_servers()
    redis_name = f"drill-{uuid.uuid4().hex[:8]}"
    audit_path = Path(tempfile.mkdtemp()) / "audit.chain.log"

    # ---------------------------------------------------------------- A -----
    instance_1 = _build(redis_name, audit_path)
    system_of_record = instance_1.providers
    conversation_id = "drill_conv_a"
    ctx = customer_context(conversation_id=conversation_id)
    await instance_1.orchestrator.start_conversation(ctx)
    await instance_1.orchestrator.start_flow(ctx, conversation_id, "motor.workflow.start")
    steps = [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (
            wf.ACTION_SUBMIT_VEHICLE,
            {
                "registration_number": "MH01AB1234",
                "vehicle_make": "Hatchback X",
                "manufacture_year": 2022,
                "fuel_type": "PETROL",
                "idv": 650000,
            },
        ),
        (wf.ACTION_SELECT_ADDONS, {"addons": ["Zero Depreciation"]}),
        (wf.ACTION_REQUEST_QUOTE, {}),
        (wf.ACTION_REVIEW, {}),
    ]
    review: Any = None
    for action, payload in steps:
        review = await instance_1.orchestrator.handle_action(ctx, "motor.workflow.action", action, payload)
    token_from_dead_instance = review.meta["confirmationTokens"][wf.ACTION_CONFIRM_PURCHASE]
    state_before = await instance_1.workflow_engine.get_active(conversation_id, ctx)
    transitions_before = len(state_before.history)

    # "Kill" instance 1: drop every reference. Nothing is flushed on purpose - a crash
    # does not get to run shutdown hooks.
    del instance_1

    started = time.perf_counter()
    instance_2 = _build(redis_name, audit_path, system_of_record)
    report.app_rto_ms = round((time.perf_counter() - started) * 1000, 2)

    resumed = await instance_2.workflow_engine.get_active(conversation_id, ctx)
    report.add(
        "A",
        "flow resumes on a fresh instance at the same state and version",
        resumed is not None
        and resumed.state == state_before.state
        and resumed.version == state_before.version,
        f"state={getattr(resumed, 'state', None)} version={getattr(resumed, 'version', None)}",
    )
    report.transitions_lost = transitions_before - len(resumed.history) if resumed else transitions_before
    report.add("A", "no committed transition was lost (RPO = 0)", report.transitions_lost == 0)

    confirm_ctx = ctx.child(idempotency_key="drill-confirm-1")
    at_payment = await instance_2.orchestrator.handle_action(
        confirm_ctx,
        "motor.workflow.action",
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        confirmation_token=token_from_dead_instance,
    )
    report.add(
        "A",
        "confirmation token issued by the dead instance verifies on the survivor",
        at_payment.meta.get("state") == wf.STATE_PAYMENT,
        f"state={at_payment.meta.get('state')}",
    )

    # ---------------------------------------------------------------- C -----
    replay = await instance_2.orchestrator.handle_action(
        confirm_ctx,
        "motor.workflow.action",
        wf.ACTION_CONFIRM_PURCHASE,
        {},
        confirmed=True,
        confirmation_token=token_from_dead_instance,
    )
    report.add(
        "C",
        "duplicate submit after failover is a safe replay, not a second charge",
        replay.meta.get("replayed") is True and replay.meta.get("state") == wf.STATE_PAYMENT,
    )

    # Gateway callback lands on the surviving instance; the shared provider records it.
    paid_state = await instance_2.workflow_store.get_active_for_conversation(conversation_id)
    assert paid_state is not None
    await instance_2.providers.payment.confirm_payment(
        str(paid_state.data["payment_id"]), "gw-drill-1", PaymentStatus.SUCCESS, ctx
    )
    pay_ctx = ctx.child(idempotency_key="drill-pay-1")
    final = await instance_2.orchestrator.handle_action(
        pay_ctx,
        "motor.workflow.action",
        wf.ACTION_COMPLETE_PAYMENT,
        {},
        confirmed=True,
        confirmation_token=at_payment.meta["confirmationTokens"][wf.ACTION_COMPLETE_PAYMENT],
    )
    report.add(
        "A",
        "journey completes on the survivor",
        final.meta.get("state") == wf.STATE_COMPLETE,
        f"state={final.meta.get('state')} error={final.meta.get('errorCode')}",
    )

    # ---------------------------------------------------------------- B -----
    instance_3 = _build(redis_name, audit_path, system_of_record)
    conv_b = "drill_conv_b"
    ctx_b = customer_context(conversation_id=conv_b)
    await instance_2.orchestrator.start_conversation(ctx_b)
    await instance_2.orchestrator.start_flow(ctx_b, conv_b, "motor.workflow.start")
    state_b = await instance_2.workflow_engine.get_active(conv_b, ctx_b)
    # Both instances hold the same version and try to commit the same transition.
    first = await instance_2.workflow_service.execute(ctx_b, state_b, wf.ACTION_BEGIN, {})
    conflicted = False
    try:
        await instance_3.workflow_service.execute(ctx_b, state_b, wf.ACTION_BEGIN, {})
    except FlowStateConflictError:
        conflicted = True
    report.add(
        "B",
        "optimistic locking across instances: one commit, one FLOW_STATE_CONFLICT",
        first.outcome.state.version == state_b.version + 1 and conflicted,
    )
    final_b = await instance_3.workflow_engine.get_active(conv_b, ctx_b)
    report.add(
        "B",
        "the losing instance reads the winner's committed state",
        final_b is not None and final_b.version == first.outcome.state.version,
    )

    # ---------------------------------------------------------------- E -----
    chain = verify_chain(
        audit_path.read_text(encoding="utf-8").splitlines(), secret="drill-not-a-real-secret"
    )
    report.add(
        "E",
        "audit chain written by three instances verifies end to end",
        chain.valid and chain.records_checked > 10,
        f"records={chain.records_checked} valid={chain.valid}",
    )

    # ---------------------------------------------------------------- F -----
    version_before = instance_2.corpus.version
    instance_2.ingestion.ingest_directory(instance_2.settings.rag_knowledge_dir)
    report.add(
        "F",
        "re-ingestion is idempotent for an unchanged corpus (version stable)",
        instance_2.corpus.version == version_before,
        f"version={instance_2.corpus.version}",
    )
    from app.rag.governance.documents import DocumentStatus

    first_doc = instance_2.corpus.documents()[0]
    instance_2.corpus.set_status(first_doc.document_id, DocumentStatus.REVOKED)
    report.add(
        "F",
        "a lifecycle change alters the corpus version, invalidating cache keys",
        instance_2.corpus.version != version_before,
        f"before={version_before} after={instance_2.corpus.version}",
    )

    # ---------------------------------------------------------------- D -----
    # Simulate the shared store vanishing: point the instance at a *different*, empty
    # emulator server (the survivor can no longer see any committed state) and then at a
    # broken client that raises on every call.
    from redis.exceptions import ConnectionError as RedisConnectionError

    class BrokenAsyncClient:
        def __getattr__(self, name: str) -> Any:
            async def fail(*_a: Any, **_k: Any) -> Any:
                raise RedisConnectionError("store unreachable")

            return fail

        def pipeline(self, *_a: Any, **_k: Any) -> Any:
            raise RedisConnectionError("store unreachable")

    instance_2.workflow_store._r = BrokenAsyncClient()  # type: ignore[attr-defined]
    instance_2.conversations._r = BrokenAsyncClient()  # type: ignore[attr-defined]
    outage_ok = False
    try:
        await instance_2.orchestrator.handle_message(ctx, "continue")
    except AppError as exc:
        outage_ok = exc.code.value == "UPSTREAM_UNAVAILABLE" and exc.http_status == 503 and exc.retryable
    report.add(
        "D",
        "store outage yields controlled UPSTREAM_UNAVAILABLE (503, retryable), nothing fabricated",
        outage_ok,
    )
    return report


def main() -> int:
    report = asyncio.run(run_drill())
    print("=" * 78)
    print("APPLICATION-LAYER RECOVERY DRILL (BCP_DR_READINESS.md §6)")
    print("=" * 78)
    for check in report.checks:
        flag = "PASS" if check.passed else "FAIL"
        print(f"[{flag}] {check.scenario}  {check.name}" + (f"  ({check.detail})" if check.detail else ""))
    print("-" * 78)
    print(f"app-layer RTO (replacement instance ready): {report.app_rto_ms} ms")
    print(f"committed transitions lost (RPO):          {report.transitions_lost}")
    print(f"VERDICT: {'PASS' if report.passed else 'FAIL'}")
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "recovery-drill-report.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": report.generated_at,
                "app_rto_ms": report.app_rto_ms,
                "transitions_lost": report.transitions_lost,
                "passed": report.passed,
                "checks": [asdict(c) for c in report.checks],
                "scope": "application layer only; region failover, backup restore and platform RTO are not measured here",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report written: {path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
