"""Reliability and graceful-degradation tests (master prompt §20, §20.1, §26.7).

Each row of the §20.1 degradation matrix is executed: what stays available when a
dependency fails, and - more importantly - what must *never* happen (invented data,
a timeout converted into success, a stale cache hit, a dropped audit event).
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai.harness.context.budget import BudgetLedger, BudgetLimits, govern
from app.ai.harness.decisions import HarnessOutcome, ReasonCode
from app.ai.harness.service import CapabilityRequest
from app.ai.models.provider import (
    DeterministicModel,
    GenerationSettings,
    ModelInvoker,
    ScriptedResponder,
    user_message,
)
from app.core.errors.taxonomy import (
    AppError,
    ErrorCode,
    ForbiddenError,
    ModelUnavailableError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.core.resilience.policies import (
    NO_RETRY,
    CircuitBreaker,
    CircuitState,
    ResiliencePolicy,
    RetryPolicy,
    SideEffectClass,
    retry_allowed,
)
from app.domains.motor import workflow as wf
from app.integrations.mock import fixtures
from app.orchestration.capabilities import CAPABILITY_FAQ
from tests.conftest import customer_context, public_context

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}


# ============================ model unavailable ============================
async def test_model_timeout_becomes_a_controlled_fallback_not_an_answer(container):
    """§20.1: a model failure must never produce invented AI output."""
    slow_model = DeterministicModel("slow", responder=ScriptedResponder(), latency_ms=3_000)
    container.harness._invoker = ModelInvoker(slow_model, settings=GenerationSettings(timeout_ms=50))
    result = await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="What is a deductible?",
            payload={"question": "What is a deductible?"},
        ),
    )
    assert result.outcome is HarnessOutcome.FALLBACK
    assert result.reason_code is ReasonCode.FALLBACK_MODEL_UNAVAILABLE
    assert "temporarily unavailable" in result.message.lower()
    assert result.payload == {}, "no fabricated answer payload"


async def test_model_outage_becomes_a_controlled_fallback(container):
    failing = DeterministicModel("broken", fail_with=RuntimeError("provider down"))
    container.harness._invoker = ModelInvoker(failing, settings=GenerationSettings())
    result = await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="What is a premium?",
            payload={"question": "What is a premium?"},
        ),
    )
    assert result.outcome is HarnessOutcome.FALLBACK
    assert result.record is not None
    assert result.record.fallback_decision == ReasonCode.FALLBACK_MODEL_UNAVAILABLE.value


async def test_deterministic_workflow_still_works_while_the_model_is_down(container):
    """§20.1: deterministic actions continue when the model is unavailable."""
    failing = DeterministicModel("broken", fail_with=RuntimeError("provider down"))
    container.harness._invoker = ModelInvoker(failing, settings=GenerationSettings())

    ctx = customer_context(conversation_id="conv_model_down")
    outcome = await container.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id="conv_model_down")
    state = (await container.workflow_service.execute(ctx, outcome.state, wf.ACTION_BEGIN)).outcome.state
    state = (
        await container.workflow_service.execute(
            ctx, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
        )
    ).outcome.state
    assert state.state == wf.STATE_COLLECT_DATA


async def test_model_fallback_is_refused_for_high_risk_capabilities(container):
    """§20: never silently switch to a weaker model for a high-risk decision."""
    from app.core.registry.capability import RiskLevel

    for capability in container.capability_registry.all():
        allowed = container.harness.allows_fallback(capability)
        if capability.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            assert not allowed, f"{capability.id} must not permit model fallback"
        else:
            assert allowed


async def test_model_invoker_does_not_fall_back_unless_asked(container):
    failing = DeterministicModel("broken", fail_with=RuntimeError("down"))
    invoker = ModelInvoker(
        failing,
        settings=GenerationSettings(),
        fallback_model=DeterministicModel("backup", responder=ScriptedResponder().set_default("ok")),
    )
    # A model call is only legal under a governing budget ledger (H-2).
    with pytest.raises(ForbiddenError, match="BLOCK_UNGOVERNED_MODEL_CALL"):
        await invoker.generate([user_message("hi")], allow_fallback=True)

    with govern(BudgetLedger(BudgetLimits())):
        with pytest.raises(ModelUnavailableError):
            await invoker.generate([user_message("hi")], allow_fallback=False)

        result = await invoker.generate([user_message("hi")], allow_fallback=True)
    assert result.usage.fallback_used is True


# ============================== RAG unavailable ==============================
async def test_rag_failure_returns_controlled_unavailability_not_model_memory(container):
    """§20.1: never fall back to model memory for policy-specific facts."""

    def exploding_retrieve(*_args, **_kwargs):
        raise UpstreamUnavailableError("vector_store_down")

    container.retriever.retrieve = exploding_retrieve  # type: ignore[method-assign]

    with pytest.raises(AppError) as exc:
        await container.harness.execute(
            public_context(),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
    assert exc.value.code is ErrorCode.UPSTREAM_UNAVAILABLE


async def test_empty_corpus_abstains_rather_than_guessing(container):
    """If nothing is retrievable, the answer is 'I cannot verify', not a guess."""
    for document in container.corpus.documents():
        container.ingestion.revoke(document.document_id)

    result = await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="What is a deductible?",
            payload={"question": "What is a deductible?"},
        ),
    )
    assert result.outcome is HarnessOutcome.ABSTAIN
    assert result.record is not None
    assert result.record.model_calls == 0


# =========================== provider unavailable ===========================
async def test_provider_timeout_is_not_converted_into_success(container):
    container.faults.timeout_operations.add("get_policy")
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is False
    assert result.data is None


async def test_provider_failure_preserves_transaction_state(container):
    """§20: surface the error, preserve the journey - do not fabricate a quote."""
    ctx = customer_context(conversation_id="conv_provider_fail")
    outcome = await container.workflow_engine.start(ctx, wf.WORKFLOW_ID, conversation_id="conv_provider_fail")
    state = (await container.workflow_service.execute(ctx, outcome.state, wf.ACTION_BEGIN)).outcome.state
    state = (
        await container.workflow_service.execute(
            ctx, state, wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}
        )
    ).outcome.state
    state = (
        await container.workflow_service.execute(ctx, state, wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE))
    ).outcome.state
    assert state.state == wf.STATE_VALIDATE
    version_before = state.version

    container.faults.unavailable_operations.add("create_quote")
    result = await container.workflow_service.execute(ctx, state, wf.ACTION_REQUEST_QUOTE)

    assert result.service_failed is True
    assert result.error is not None
    failed_state = result.outcome.state
    assert failed_state.state == wf.STATE_VALIDATE, "state must not advance"
    assert "quote_id" not in failed_state.data, "no fabricated quote was stored"
    assert failed_state.data == state.data, "no business field was written"
    assert len(failed_state.history) == len(state.history), "no transition was recorded"
    # The side-effecting transition is *reserved* before the provider is called and the
    # reservation is released when it fails (§8, H-1), so the optimistic-locking version
    # moves even though no business transition was committed. That is the point: a
    # concurrent submit that read the old version can no longer commit either.
    assert failed_state.version > version_before
    assert failed_state.pending_action is None, "the reservation must be released"

    # Recovery: the same action succeeds once the provider returns. The caller must work
    # from the current state, exactly as the orchestrator does on every request.
    container.faults.unavailable_operations.discard("create_quote")
    recovered = await container.workflow_service.execute(ctx, failed_state, wf.ACTION_REQUEST_QUOTE)
    assert recovered.service_failed is False
    assert recovered.outcome.state.state == wf.STATE_QUOTE
    assert recovered.outcome.state.data["quote_id"]


async def test_malformed_upstream_response_is_rejected_not_parsed_optimistically(container):
    container.faults.invalid_response_operations.add("get_policy")
    ctx = customer_context(fixtures.CUSTOMER_A)
    result = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert result.available is False
    assert result.reason_code == ErrorCode.UPSTREAM_INVALID_RESPONSE.value


async def test_partial_dependency_outage_leaves_other_capabilities_working(container):
    """§20.1: an unaffected read-only capability continues during a partial outage."""
    container.faults.unavailable_operations.add("get_claim")
    ctx = customer_context(fixtures.CUSTOMER_A)

    claim = await container.tools.get_claim_status(ctx, fixtures.CLAIM_A)
    assert claim.available is False

    policy = await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    assert policy.available is True, "an unrelated dependency must stay available"


# ============================= circuit breaker =============================
async def test_circuit_breaker_opens_after_repeated_failures():
    breaker = CircuitBreaker("test", failure_threshold=3, reset_seconds=0.05)
    policy = ResiliencePolicy("test", timeout_ms=50, retry=NO_RETRY, breaker=breaker)

    async def failing():
        raise UpstreamUnavailableError("down")

    for _ in range(3):
        with pytest.raises(UpstreamUnavailableError):
            await policy.execute(failing)

    assert breaker.state is CircuitState.OPEN
    # Further calls are refused immediately rather than hammering the upstream.
    with pytest.raises(UpstreamUnavailableError) as exc:
        await policy.execute(failing)
    assert exc.value.reason == "circuit_open"


async def test_circuit_breaker_half_opens_and_recovers():
    breaker = CircuitBreaker("test", failure_threshold=1, reset_seconds=0.05)
    policy = ResiliencePolicy("test", timeout_ms=200, retry=NO_RETRY, breaker=breaker)

    async def failing():
        raise UpstreamUnavailableError("down")

    async def succeeding():
        return "ok"

    with pytest.raises(UpstreamUnavailableError):
        await policy.execute(failing)
    assert breaker.state is CircuitState.OPEN

    await asyncio.sleep(0.06)
    assert breaker.state is CircuitState.HALF_OPEN
    assert await policy.execute(succeeding) == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_circuit_open_is_counted_in_metrics():
    from app.core.observability.metrics import CIRCUIT_OPEN_TOTAL, metrics

    breaker = CircuitBreaker("metered", failure_threshold=1, reset_seconds=10)
    policy = ResiliencePolicy("metered", timeout_ms=50, retry=NO_RETRY, breaker=breaker)

    async def failing():
        raise UpstreamUnavailableError("down")

    with pytest.raises(UpstreamUnavailableError):
        await policy.execute(failing)
    with pytest.raises(UpstreamUnavailableError):
        await policy.execute(failing)
    assert metrics.counter_value(CIRCUIT_OPEN_TOTAL, {"dependency": "metered"}) >= 1


# ================================ retries ================================
@pytest.mark.parametrize(
    ("side_effect", "key", "expected"),
    [
        (SideEffectClass.READ_ONLY, None, True),
        (SideEffectClass.READ_ONLY, "k", True),
        (SideEffectClass.LOW_RISK_WRITE, None, False),
        (SideEffectClass.LOW_RISK_WRITE, "k", True),
        (SideEffectClass.HIGH_RISK_WRITE, None, False),
        (SideEffectClass.HIGH_RISK_WRITE, "k", True),
        (SideEffectClass.IRREVERSIBLE, None, False),
        (SideEffectClass.IRREVERSIBLE, "k", False),
    ],
)
def test_retry_is_only_allowed_when_duplication_is_prevented(side_effect, key, expected):
    """§8, §20: never retry blindly; an irreversible action is never auto-retried."""
    assert retry_allowed(side_effect, key) is expected


async def test_read_only_calls_are_retried_and_can_succeed():
    attempts = {"count": 0}

    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise UpstreamUnavailableError("transient")
        return "ok"

    policy = ResiliencePolicy(
        "flaky",
        timeout_ms=500,
        retry=RetryPolicy(max_attempts=3, base_delay_ms=1, jitter=False),
        breaker=CircuitBreaker("flaky", failure_threshold=10),
    )
    assert await policy.execute(flaky) == "ok"
    assert attempts["count"] == 3


async def test_an_irreversible_action_is_attempted_exactly_once():
    attempts = {"count": 0}

    async def failing():
        attempts["count"] += 1
        raise UpstreamUnavailableError("down")

    policy = ResiliencePolicy(
        "irreversible",
        timeout_ms=500,
        retry=RetryPolicy(max_attempts=5, base_delay_ms=1, jitter=False),
        breaker=CircuitBreaker("irreversible", failure_threshold=10),
    )
    with pytest.raises(UpstreamUnavailableError):
        await policy.execute(failing, side_effect=SideEffectClass.IRREVERSIBLE)
    assert attempts["count"] == 1, "an irreversible action must never be retried"


async def test_non_retryable_errors_are_not_retried():
    from app.core.errors.taxonomy import ValidationError

    attempts = {"count": 0}

    async def invalid():
        attempts["count"] += 1
        raise ValidationError("bad_input")

    policy = ResiliencePolicy(
        "validating",
        timeout_ms=500,
        retry=RetryPolicy(max_attempts=4, base_delay_ms=1, jitter=False),
        breaker=CircuitBreaker("validating", failure_threshold=10),
    )
    with pytest.raises(ValidationError):
        await policy.execute(invalid)
    assert attempts["count"] == 1


async def test_timeout_produces_a_timeout_error_not_a_generic_failure():
    async def slow():
        await asyncio.sleep(1)

    policy = ResiliencePolicy(
        "slow", timeout_ms=20, retry=NO_RETRY, breaker=CircuitBreaker("slow", failure_threshold=99)
    )
    with pytest.raises(UpstreamTimeoutError):
        await policy.execute(slow)


# ================================= cache =================================
def test_cache_expiry_prevents_a_stale_hit(container):
    from app.core.security.cache import TtlLruCache

    cache: TtlLruCache[dict] = TtlLruCache(max_entries=4, ttl_seconds=0)
    cache.set("k", {"answer": "old"})
    assert cache.get("k") is None, "an expired entry must not be served"


def test_cache_is_bounded_and_evicts_oldest():
    from app.core.security.cache import TtlLruCache

    cache: TtlLruCache[int] = TtlLruCache(max_entries=2, ttl_seconds=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert len(cache) == 2
    assert cache.get("a") is None


def test_cache_key_changes_when_the_corpus_changes(container):
    from app.core.security.cache import FaqCacheKey

    def key(corpus_version: str) -> str:
        return FaqCacheKey(
            normalized_query="what is a deductible",
            domain="common",
            corpus_version=corpus_version,
            prompt_version="1.0.0",
            guardrail_policy_version="1.0.0",
            language="en",
        ).render()

    assert key("v1") != key("v2"), "a knowledge change must invalidate the cached answer"


def test_customer_specific_responses_are_never_shared_cached():
    from app.core.security.cache import is_cacheable

    assert is_cacheable(contains_customer_data=False, is_authenticated_scope=False) is True
    assert is_cacheable(contains_customer_data=True, is_authenticated_scope=False) is False
    assert is_cacheable(contains_customer_data=False, is_authenticated_scope=True) is False


async def test_cache_failure_falls_through_to_the_authoritative_path(container):
    """§20.1: bypass the cache, do not serve something unsafe."""
    from app.ai.agents.faq_agent import FaqAgent
    from app.core.security.cache import NoOpCache

    agent = FaqAgent(container.retriever, container.prompts, cache=NoOpCache(), cache_enabled=True)
    container.harness._handlers[CAPABILITY_FAQ] = agent.handle

    result = await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="What is a deductible?",
            payload={"question": "What is a deductible?"},
        ),
    )
    assert result.outcome is HarnessOutcome.ALLOW
    assert result.record is not None and result.record.model_calls == 1


# ============================ audit / telemetry ============================
async def test_a_failing_audit_sink_fails_closed(container):
    """§20.1: a required audit event is never silently dropped."""

    class BrokenSink:
        async def append(self, event):
            raise OSError("audit store unreachable")

    from app.core.audit.service import AuditService

    strict = AuditService(BrokenSink(), fail_closed=True)
    with pytest.raises(AppError) as exc:
        await strict.record(
            customer_context(),
            __import__("app.core.audit.events", fromlist=["AuditAction"]).AuditAction.FLOW_STARTED,
            __import__("app.core.audit.events", fromlist=["AuditResult"]).AuditResult.SUCCESS,
        )
    assert exc.value.code is ErrorCode.INTERNAL_ERROR
    assert exc.value.retryable is True


async def test_audit_can_be_configured_to_fail_open_explicitly(container):
    """The policy is a decision, not an accident: fail-open must be opted into."""

    class BrokenSink:
        async def append(self, event):
            raise OSError("audit store unreachable")

    from app.core.audit.events import AuditAction, AuditResult
    from app.core.audit.service import AuditService

    lenient = AuditService(BrokenSink(), fail_closed=False)
    event = await lenient.record(customer_context(), AuditAction.FLOW_STARTED, AuditResult.SUCCESS)
    assert event is not None


def test_telemetry_backend_failure_does_not_break_the_request():
    """§20.1: observability is fail-open by configured policy."""
    from app.core.observability.metrics import MetricsRegistry

    registry = MetricsRegistry()
    registry.increment("x")
    assert registry.counter_value("x") == 1.0
    # A tracing span that raises still lets the exception through without masking it.
    from app.core.observability.tracing import Tracer

    tracer = Tracer()
    with pytest.raises(ValueError), tracer.span("failing"):
        raise ValueError("inner")


# =============================== escalation ===============================
async def test_unsupported_sensitive_case_escalates_to_a_human_path(container):
    """§20: an application-level route exists for unsupported requests."""
    responder = ScriptedResponder().set_default(
        '{"capability": "UNSUPPORTED", "reason_category": "sensitive_dispute", '
        '"needs_clarification": false, "clarifying_question": ""}'
    )
    container.harness._invoker = ModelInvoker(
        DeterministicModel("scripted", responder=responder), settings=GenerationSettings()
    )
    from app.orchestration.capabilities import CAPABILITY_SUPERVISOR

    result = await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_SUPERVISOR,
            user_message="I want to formally dispute how my claim was handled and sue you",
            payload={"message": "dispute"},
        ),
    )
    assert result.outcome is HarnessOutcome.ESCALATE
    assert result.reason_code is ReasonCode.ESCALATE_UNSUPPORTED_SENSITIVE_CASE


def test_degradation_matrix_is_documented_and_matches_the_tests():
    """§20.1 requires the matrix to be documented; keep doc and code in step."""
    from pathlib import Path

    doc = Path("docs/operations/INCIDENT_RUNBOOK.md")
    if not doc.exists():
        pytest.skip("runbook not yet written; covered by the documentation task")
    text = doc.read_text(encoding="utf-8").lower()
    for dependency in ("model", "rag", "provider", "cache", "observability"):
        assert dependency in text
