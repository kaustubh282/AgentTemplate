"""Shared (Redis) store adapters, proven offline with an in-process Redis emulator.

Two application instances are built against the *same* emulated server, so every
assertion here is about multi-instance behaviour (§19): state written by one instance
is read by another, optimistic locking holds across instances, rate limits are one
budget rather than one per instance, and a store outage is a controlled 503.

`fakeredis` emulates the Redis wire semantics (WATCH/MULTI, INCR/EXPIRE, RPUSH/LTRIM);
confirming against a real Redis in preprod remains a recorded next action.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.ai.harness.context.builder import ConversationTurn
from app.bootstrap import build_container
from app.core.errors.taxonomy import FlowStateConflictError, RateLimitedError, UpstreamUnavailableError
from app.core.security.rate_limit import RateLimitClass
from app.core.storage.redis_client import build_redis_clients, reset_fake_servers
from app.domains.motor import workflow as wf
from app.integrations.mock import fixtures
from app.orchestration.session import Conversation
from app.workflows.state.models import WorkflowState
from tests.conftest import auth_headers, customer_context, default_responder, make_settings, make_token

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}


def shared_settings(name: str, **overrides):
    return make_settings(
        SESSION_STORE_PROVIDER="redis",
        WORKFLOW_STORE_PROVIDER="redis",
        RATE_LIMIT_STORE_PROVIDER="redis",
        REDIS_URL=f"fakeredis://{name}",
        **overrides,
    )


@pytest.fixture
def store_name():
    reset_fake_servers()
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def two_instances(store_name):
    a = build_container(shared_settings(store_name), responder=default_responder())
    b = build_container(shared_settings(store_name), responder=default_responder())
    return a, b


# ------------------------------------------------------------- workflow store ---
async def test_state_written_by_one_instance_is_read_by_another(two_instances):
    a, b = two_instances
    ctx = customer_context(conversation_id="conv_shared_1")
    started = await a.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id="conv_shared_1")
    seen = await b.workflow_engine.get_active("conv_shared_1", ctx)
    assert seen is not None
    assert seen.flow_id == started.state.flow_id
    assert seen.version == started.state.version


async def test_optimistic_locking_holds_across_instances(two_instances):
    a, b = two_instances
    ctx = customer_context(conversation_id="conv_shared_lock")
    state = (await a.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id="conv_shared_lock")).state

    winner = await a.workflow_service.execute(ctx, state, wf.ACTION_BEGIN, {})
    assert winner.outcome.state.version == state.version + 1
    with pytest.raises(FlowStateConflictError, match="optimistic_lock_conflict"):
        await b.workflow_service.execute(ctx, state, wf.ACTION_BEGIN, {})

    current = await b.workflow_engine.get_active("conv_shared_lock", ctx)
    assert current is not None and current.version == winner.outcome.state.version


async def test_concurrent_writers_commit_exactly_once(two_instances):
    """Twenty racing updates from two instances: every version increments by exactly one."""
    a, b = two_instances
    ctx = customer_context(conversation_id="conv_shared_race")
    state = (await a.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id="conv_shared_race")).state

    async def attempt(container):
        try:
            return await container.workflow_service.execute(ctx, state, wf.ACTION_BEGIN, {})
        except FlowStateConflictError:
            return None

    results = await asyncio.gather(*(attempt(a if i % 2 else b) for i in range(20)))
    committed = [r for r in results if r is not None]
    assert len(committed) == 1, "exactly one writer may commit a given version"
    final = await a.workflow_engine.get_active("conv_shared_race", ctx)
    assert final is not None and final.version == state.version + 1


async def test_ownership_is_enforced_on_the_shared_store_too(two_instances):
    a, b = two_instances
    owner = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_shared_own")
    await a.workflow_engine.start(owner, wf.WORKFLOW_ID, conversation_id="conv_shared_own")
    attacker = customer_context(fixtures.CUSTOMER_B, conversation_id="conv_shared_own")
    from app.core.errors.taxonomy import ForbiddenError

    with pytest.raises(ForbiddenError):
        await b.workflow_engine.get_active("conv_shared_own", attacker)


async def test_completed_flow_releases_the_conversation_pointer(store_name):
    clients = build_redis_clients(f"fakeredis://{store_name}", key_prefix="t")
    from app.workflows.state.models import FlowStatus
    from app.workflows.state.redis_store import RedisWorkflowStateStore

    store = RedisWorkflowStateStore(clients)
    state = WorkflowState(
        workflow_id="w", workflow_version="1", conversation_id="c1", owner_subject_id="s", state="ENTRY"
    )
    await store.create(state)
    assert (await store.get_active_for_conversation("c1")) is not None
    done = state.model_copy(update={"status": FlowStatus.COMPLETED})
    await store.update(done, expected_version=0)
    assert (await store.get_active_for_conversation("c1")) is None
    assert (await store.get(state.flow_id)) is not None, "history remains readable by flow id"


async def test_create_is_conflict_safe_and_delete_removes_pointer(store_name):
    clients = build_redis_clients(f"fakeredis://{store_name}", key_prefix="t")
    from app.workflows.state.redis_store import RedisWorkflowStateStore

    store = RedisWorkflowStateStore(clients)
    state = WorkflowState(
        workflow_id="w", workflow_version="1", conversation_id="c2", owner_subject_id="s", state="ENTRY"
    )
    await store.create(state)
    with pytest.raises(FlowStateConflictError, match="flow_already_exists"):
        await store.create(state)
    await store.delete(state.flow_id)
    assert await store.get(state.flow_id) is None
    assert await store.get_active_for_conversation("c2") is None


# --------------------------------------------------------- conversation store ---
async def test_conversation_turns_are_shared_and_bounded(store_name):
    clients = build_redis_clients(f"fakeredis://{store_name}", key_prefix="t")
    from app.orchestration.session_redis import RedisConversationStore

    writer = RedisConversationStore(clients, max_turns_retained=5, ttl_seconds=60)
    reader = RedisConversationStore(clients, max_turns_retained=5, ttl_seconds=60)
    await writer.create(Conversation(conversation_id="cv1", owner_subject_id="CUST-1", tenant_id="TENANT-IN"))
    for i in range(8):
        await writer.append_turn("cv1", ConversationTurn(role="user", text=f"turn {i}"))

    seen = await reader.get("cv1")
    assert seen is not None
    assert seen.owner_subject_id == "CUST-1" and seen.tenant_id == "TENANT-IN"
    assert [t.text for t in seen.turns] == [f"turn {i}" for i in range(3, 8)], "window bound holds"
    await writer.delete("cv1")
    assert await reader.get("cv1") is None


async def test_conversation_ownership_gate_works_across_instances(two_instances):
    a, b = two_instances
    from app.core.errors.taxonomy import ForbiddenError

    owner = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_gate")
    await a.orchestrator.start_conversation(owner)
    conversation = await a.orchestrator._open_conversation(owner, "conv_gate")
    attacker = customer_context(fixtures.CUSTOMER_B, conversation_id="conv_gate")
    with pytest.raises(ForbiddenError, match="conversation_owner_mismatch"):
        await b.orchestrator._open_conversation(attacker, conversation.conversation_id)


# ------------------------------------------------------------ rate limiting ---
def test_rate_limit_is_one_budget_across_instances(store_name):
    clients = build_redis_clients(f"fakeredis://{store_name}", key_prefix="t")
    from app.core.security.rate_limit import RateLimiter
    from app.core.security.rate_limit_redis import RedisRateLimitStore

    limiter_a = RateLimiter(RedisRateLimitStore(clients), public_per_minute=5)
    limiter_b = RateLimiter(RedisRateLimitStore(clients), public_per_minute=5)
    allowed = 0
    for i in range(10):
        limiter = limiter_a if i % 2 == 0 else limiter_b
        if limiter.check(RateLimitClass.PUBLIC, subject_ref="anon").allowed:
            allowed += 1
    assert allowed == 5, "the limit is shared, not multiplied by the instance count"


def test_rate_limit_store_outage_fails_closed(store_name):
    from app.core.security.rate_limit_redis import RedisRateLimitStore

    clients = build_redis_clients(f"fakeredis://{store_name}", key_prefix="t")

    class Broken:
        def pipeline(self, *_a, **_k):
            raise RedisConnectionError("down")

    store = RedisRateLimitStore(clients)
    store._r = Broken()  # type: ignore[assignment]
    with pytest.raises(RateLimitedError, match="STORE_UNAVAILABLE"):
        store.hit("k", 10, 60)


# ----------------------------------------------------------------- outage ---
async def test_store_outage_is_a_controlled_upstream_unavailable(two_instances):
    a, _ = two_instances
    ctx = customer_context(conversation_id="conv_outage")

    class BrokenAsync:
        def __getattr__(self, name):
            async def fail(*_a, **_k):
                raise RedisConnectionError("down")

            return fail

    a.workflow_store._r = BrokenAsync()
    with pytest.raises(UpstreamUnavailableError, match="state_store_unavailable") as exc:
        await a.workflow_engine.get_active("conv_outage", ctx)
    assert exc.value.http_status == 503 and exc.value.retryable


# --------------------------------------------------------------- over HTTP ---
def test_two_http_instances_share_one_journey(store_name):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = shared_settings(store_name)
    app_a = create_app(settings, responder=default_responder())
    app_b = create_app(settings, responder=default_responder())
    token = make_token()
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        created = client_a.post("/api/v1/conversations", json={}, headers=auth_headers(token))
        conversation_id = created.json()["conversation_id"]
        started = client_a.post(
            "/api/v1/flows/start",
            headers=auth_headers(token, conversation_id),
            json={"conversation_id": conversation_id, "capability_id": "motor.workflow.start"},
        )
        assert started.status_code == 200

        # Instance B continues the journey instance A started.
        begun = client_b.post(
            "/api/v1/actions",
            headers=auth_headers(token, conversation_id),
            json={
                "conversation_id": conversation_id,
                "capability_id": "motor.workflow.action",
                "action": wf.ACTION_BEGIN,
                "payload": {},
                "confirmed": False,
            },
        )
        assert begun.status_code == 200, begun.text
        assert begun.json()["meta"]["state"] == wf.STATE_IDENTIFY_CUSTOMER

        # And instance A sees B's commit.
        resumed = client_a.post(
            "/api/v1/chat",
            headers=auth_headers(token, conversation_id),
            json={"conversation_id": conversation_id, "message": "continue"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["meta"]["state"] == wf.STATE_IDENTIFY_CUSTOMER

        ready = client_b.get("/api/v1/health/ready").json()
        assert {d["name"]: d["status"] for d in ready["dependencies"]}["state_store"] == "up"
