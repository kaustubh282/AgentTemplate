"""Native deterministic eval suites (master prompt §26.3).

Each suite drives the real application container against the shared golden datasets
and asserts exact values. The suites cover every item §26.3 lists: workflow
transitions, UI directive validity, API contract behaviour, authorization and
ownership, idempotency, prod-with-mock rejection, PII leakage (logs and model
boundary), secret leakage, model-call and agent-step counts, token budgets, latency
SLOs, rate limits, timeout/retry/circuit behaviour, audit generation, correlation
ids, health behaviour, fallback behaviour and provider contract substitution.
"""

from __future__ import annotations

import io
import json
import logging
import time
from typing import Any

from app.ai.harness.service import CapabilityRequest
from app.bootstrap import Container, build_container
from app.core.audit.events import AuditAction
from app.core.auth.auth_context import ActorType, AuthContext, Role
from app.core.config.settings import Settings
from app.core.context.request_context import Channel, RequestContext
from app.core.errors.taxonomy import AppError, ForbiddenError
from app.core.logging.structured import RedactingJsonFormatter
from app.core.observability.tracing import recorder
from app.domains.motor import workflow as wf
from app.integrations.mock import fixtures
from app.orchestration.capabilities import CAPABILITY_FAQ, CAPABILITY_INTENT
from evals.datasets.schema import Category, EvalCase, load_jsonl
from evals.native.harness import CaseRecorder, SuiteResult, Verdict

VALID_VEHICLE = {
    "registration_number": "MH01AB1234",
    "vehicle_make": "Hatchback X",
    "manufacture_year": 2022,
    "fuel_type": "PETROL",
    "idv": 650000,
}

#: The payload each motor action expects. A legal transition must be driven with its
#: real payload, otherwise validation - correctly - rejects it and the case would
#: measure the validator rather than the transition.
_ACTION_PAYLOADS: dict[str, dict[str, Any]] = {
    wf.ACTION_BEGIN: {},
    wf.ACTION_IDENTIFY: {"product_code": "MTR-PVT-CAR"},
    wf.ACTION_SUBMIT_VEHICLE: dict(VALID_VEHICLE),
    wf.ACTION_SELECT_ADDONS: {"addons": ["Zero Depreciation"]},
    wf.ACTION_REQUEST_QUOTE: {},
    wf.ACTION_RETRY_QUOTE: {},
    wf.ACTION_REVIEW: {},
    wf.ACTION_GO_BACK: {},
    wf.ACTION_EDIT: {},
    wf.ACTION_CONFIRM_PURCHASE: {},
    wf.ACTION_COMPLETE_PAYMENT: {},
}

_SEQUENCE_TO_STATE: dict[str, list[tuple[str, dict[str, Any]]]] = {
    wf.STATE_ENTRY: [],
    wf.STATE_IDENTIFY_CUSTOMER: [(wf.ACTION_BEGIN, {})],
    wf.STATE_COLLECT_DATA: [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
    ],
    wf.STATE_VALIDATE: [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
    ],
    wf.STATE_QUOTE: [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
        (wf.ACTION_REQUEST_QUOTE, {}),
    ],
    wf.STATE_REVIEW: [
        (wf.ACTION_BEGIN, {}),
        (wf.ACTION_IDENTIFY, {"product_code": "MTR-PVT-CAR"}),
        (wf.ACTION_SUBMIT_VEHICLE, dict(VALID_VEHICLE)),
        (wf.ACTION_REQUEST_QUOTE, {}),
        (wf.ACTION_REVIEW, {}),
    ],
}


# --------------------------------------------------------------- context helpers ---
def context_for(actor: str, *, conversation_id: str, idempotency_key: str | None = None) -> RequestContext:
    """Build the trusted context the case's actor implies."""
    if actor == "PUBLIC":
        from app.core.auth.auth_context import anonymous_context

        return RequestContext(
            conversation_id=conversation_id,
            channel=Channel.PUBLIC_WEB,
            environment="test",
            auth=anonymous_context(),
        )
    if actor in ("AGENT", "AGENT_UNASSIGNED"):
        assigned = (fixtures.AGENT_ASSIGNED_CUSTOMER,) if actor == "AGENT" else ()
        return RequestContext(
            conversation_id=conversation_id,
            channel=Channel.WEB_AGENT,
            environment="test",
            idempotency_key=idempotency_key,
            auth=AuthContext(
                subject_id="AGT-9001" if actor == "AGENT" else "AGT-9002",
                actor_type=ActorType.AGENT,
                roles=(Role.AGENT,),
                assigned_customer_ids=assigned,
                tenant_id="TENANT-IN",
            ),
        )
    subject = fixtures.CUSTOMER_B if actor == "CUSTOMER_B" else fixtures.CUSTOMER_A
    return RequestContext(
        conversation_id=conversation_id,
        channel=Channel.WEB_CUSTOMER,
        environment="test",
        idempotency_key=idempotency_key,
        auth=AuthContext(
            subject_id=subject,
            actor_type=ActorType.CUSTOMER,
            roles=(Role.CUSTOMER,),
            tenant_id="TENANT-IN",
        ),
    )


async def drive_to(container: Container, ctx: RequestContext, target_state: str) -> Any:
    """Walk the motor flow to a state using only allow-listed transitions."""
    outcome = await container.workflow_engine.start(
        ctx, wf.WORKFLOW_ID, conversation_id=ctx.conversation_id or "conv"
    )
    state = outcome.state
    for action, payload in _SEQUENCE_TO_STATE[target_state]:
        state = (await container.workflow_service.execute(ctx, state, action, payload)).outcome.state
    return state


# ============================== workflow suite ==============================
async def workflow_suite(build: Any) -> SuiteResult:
    """§26.3: workflow state transitions and illegal/out-of-order rejection."""
    suite = SuiteResult(name="workflow")
    dataset = load_jsonl("workflow.jsonl")

    for case in dataset.cases:
        container = build()
        recorder_ = CaseRecorder(case.case_id, case.category.value)
        conversation_id = f"conv_{case.case_id}"
        ctx = context_for(case.actor, conversation_id=conversation_id, idempotency_key="idem-native")

        from_state = next((t for t in case.tags if t in _SEQUENCE_TO_STATE), None)
        if from_state is None:
            recorder_.skip("resolve_from_state", "case does not name a start state tag")
            suite.cases.append(recorder_.finish())
            continue

        try:
            state = await drive_to(container, ctx.child(idempotency_key=None), from_state)
            recorder_.equals("start_state", from_state, state.state)

            model_calls_before = getattr(container.invoker.model, "call_count", 0)
            started = time.perf_counter()
            try:
                result = await container.workflow_service.execute(
                    ctx,
                    state,
                    case.input,
                    dict(_ACTION_PAYLOADS.get(case.input, {})),
                    confirmed=True,
                )
                final_state = result.outcome.state.state
                rejected = False
            except AppError:
                final_state = (await container.workflow_engine.load(state.flow_id, ctx)).state
                rejected = True
            latency_ms = (time.perf_counter() - started) * 1000

            if case.expected_outcome and case.expected_outcome.value == "REJECT_TRANSITION":
                recorder_.is_true("transition_rejected", rejected)
                recorder_.equals("state_unchanged", case.expected_workflow_state, final_state)
            else:
                recorder_.is_true("transition_accepted", not rejected)
                recorder_.equals("resulting_state", case.expected_workflow_state, final_state)

            recorder_.equals(
                "model_calls",
                case.max_model_calls,
                getattr(container.invoker.model, "call_count", 0) - model_calls_before,
            )
            recorder_.at_most("latency_ms", case.latency_budget_ms, round(latency_ms, 2))
        except Exception as exc:
            recorder_.error("suite_execution", exc)

        suite.cases.append(recorder_.finish())

    return suite


# ================================ auth suite ================================
async def auth_suite(build: Any) -> SuiteResult:
    """§26.3: authorization / RBAC / resource ownership, exactly."""
    suite = SuiteResult(name="auth")

    scenarios = [
        ("auth-001", "CUSTOMER", fixtures.POLICY_A_MOTOR, True, "own policy is readable"),
        ("auth-002", "CUSTOMER", fixtures.POLICY_B_MOTOR, False, "another customer policy is forbidden"),
        ("auth-003", "CUSTOMER_B", fixtures.POLICY_B_MOTOR, True, "own policy is readable"),
        ("auth-004", "CUSTOMER_B", fixtures.POLICY_A_MOTOR, False, "cross-customer read is forbidden"),
        ("auth-005", "AGENT", fixtures.POLICY_A_MOTOR, True, "assigned agent may read"),
        ("auth-006", "AGENT_UNASSIGNED", fixtures.POLICY_A_MOTOR, False, "unassigned agent is forbidden"),
        ("auth-007", "CUSTOMER", "POL-DOES-NOT-EXIST", False, "unknown resource is forbidden, not 404"),
    ]

    for case_id, actor, policy_id, should_allow, description in scenarios:
        container = build()
        rec = CaseRecorder(case_id, "AUTH")
        ctx = context_for(actor, conversation_id=f"conv_{case_id}")
        requested_customer = fixtures.CUSTOMER_A if actor in ("AGENT", "AGENT_UNASSIGNED") else None
        try:
            result = await container.tools.get_policy_details(
                ctx, policy_id, requested_customer_id=requested_customer
            )
            rec.is_true("access_allowed", should_allow, description)
            if should_allow:
                rec.is_true("payload_present", result.available)
        except ForbiddenError as exc:
            rec.is_true("access_denied", not should_allow, description)
            # The *user-facing* denial must be uniform so existence is not disclosed.
            # The internal reason code is deliberately specific for audit.
            rec.equals("http_status", 403, exc.http_status)
            rec.equals(
                "user_message_is_uniform",
                "You do not have access to this resource.",
                exc.safe_message,
            )
        except Exception as exc:
            rec.error("auth_scenario", exc)
        suite.cases.append(rec.finish())

    # Denial must be audited.
    container = build()
    rec = CaseRecorder("auth-100", "AUTH")
    ctx = context_for("CUSTOMER", conversation_id="conv_auth_audit").child(channel=Channel.PUBLIC_WEB)
    try:
        await container.pep.authorize(ctx, "motor.policy.details", {"policy_id": fixtures.POLICY_A_MOTOR})
        rec.is_true("denied", False, "the PEP allowed a disallowed channel")
    except ForbiddenError:
        events = container.audit_sink.events(AuditAction.AUTHORIZATION_DENIED)
        rec.at_least("audit_events", 1, len(events))
        if events:
            rec.equals("audit_result", "DENIED", events[-1].result.value)
            rec.is_true("actor_is_pseudonymous", events[-1].actor_ref.startswith(("sub_", "anonymous")))
    suite.cases.append(rec.finish())

    return suite


# ========================== ui directives suite ==========================
async def ui_directives_suite(build: Any) -> SuiteResult:
    """§26.3: UI directive schema validity and fail-closed behaviour."""
    from app.core.errors.taxonomy import ValidationError
    from app.ui_directives.registry.registry import PAYLOAD_BY_TYPE
    from app.ui_directives.schemas.directives import DirectiveType

    suite = SuiteResult(name="ui_directives")
    container = build()

    rec = CaseRecorder("ui-001", "UI_DIRECTIVE")
    rec.equals(
        "every_directive_type_is_registered",
        sorted(t.value for t in DirectiveType),
        sorted(t.value for t in PAYLOAD_BY_TYPE),
    )
    suite.cases.append(rec.finish())

    # Unknown directive types must fail closed.
    for index, unknown in enumerate(["EXECUTE_JS", "SHOW_ANYTHING", "", "show_message"], start=2):
        rec = CaseRecorder(f"ui-{index:03d}", "UI_DIRECTIVE")
        try:
            container.directive_registry.validate_untrusted({"type": unknown, "payload": {}})
            rec.is_true("unknown_type_rejected", False, f"accepted {unknown!r}")
        except ValidationError:
            rec.is_true("unknown_type_rejected", True)
        suite.cases.append(rec.finish())

    # Markup and script must be refused inside any payload string.
    for index, unsafe in enumerate(
        ["<script>x</script>", "javascript:alert(1)", "<iframe src=x>", "onerror=steal()"], start=10
    ):
        rec = CaseRecorder(f"ui-{index:03d}", "UI_DIRECTIVE")
        try:
            container.directive_registry.build(
                DirectiveType.SHOW_MESSAGE,
                {"body": unsafe, "accessibility": {"aria_label": "a"}},
            )
            rec.is_true("markup_rejected", False, f"accepted {unsafe!r}")
        except ValidationError:
            rec.is_true("markup_rejected", True)
        suite.cases.append(rec.finish())

    # Accessibility metadata is mandatory.
    rec = CaseRecorder("ui-020", "UI_DIRECTIVE")
    try:
        container.directive_registry.build(DirectiveType.SHOW_MESSAGE, {"body": "hello"})
        rec.is_true("accessibility_required", False, "a directive without a11y was accepted")
    except ValidationError:
        rec.is_true("accessibility_required", True)
    suite.cases.append(rec.finish())

    # Every state of every workflow must yield a valid directive.
    for domain, binding in container.orchestrator._bindings.items():
        definition = container.workflow_registry.get(binding.workflow_id)
        for state in definition.states:
            rec = CaseRecorder(f"ui-state-{domain}-{state}", "UI_DIRECTIVE")
            try:
                directive = binding.directive_builder.for_state(state, _sample_flow_data())
                rec.is_true("directive_built", directive is not None)
                rec.is_true(
                    "type_registered",
                    container.directive_registry.is_registered(directive.type.value),
                )
                rec.is_true(
                    "accessibility_present",
                    bool(directive.payload.get("accessibility", {}).get("aria_label")),
                )
            except Exception as exc:
                rec.error("directive_for_state", exc)
            suite.cases.append(rec.finish())

    return suite


def _sample_flow_data() -> dict[str, Any]:
    return {
        "product_code": "MTR-PVT-CAR",
        "registration_number": "MH01AB1234",
        "vehicle_make": "Hatchback X",
        "manufacture_year": 2022,
        "fuel_type": "PETROL",
        "idv": 650000.0,
        "addons": ["Zero Depreciation"],
        "quote_reference": "QREFTEST01",
        "quote_total": 21000.0,
        "quote_currency": "INR",
        "quote_valid_until": "2027-01-01",
        "quote_source_system": "MOCK",
        "quote_lines": [{"label": "Base premium", "amount": 17796.61, "note": None}],
        "payment_reference": "PAYREFTEST01",
        "payment_handoff_token": "hst_test",
        "payment_expires_in_seconds": 900,
        "policy_number": "PTC0000001234",
        "region": "SCHENGEN",
        "trip_days": 14,
        "traveller_age": 34,
    }


# ================================ pii suite ================================
async def pii_suite(build: Any) -> SuiteResult:
    """§26.3: PII into logs, PII across the model boundary, secret leakage."""
    suite = SuiteResult(name="pii")
    dataset = load_jsonl("pii-security.jsonl")

    for case in dataset.cases:
        container = build()
        rec = CaseRecorder(case.case_id, case.category.value)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingJsonFormatter("eval", "test", "0.1.0"))
        root = logging.getLogger()
        previous = root.handlers
        root.handlers = [handler]
        recorder.reset()

        try:
            result = await container.harness.execute(
                context_for(case.actor, conversation_id=f"conv_{case.case_id}"),
                CapabilityRequest(
                    capability_id=CAPABILITY_FAQ,
                    user_message=case.input,
                    payload={"question": case.input},
                ),
            )
            logs = stream.getvalue()
            spans = json.dumps([s.attributes for s in recorder.spans()], default=str)
            audit = json.dumps([e.model_dump(mode="json") for e in container.audit_sink.events()])
            record_json = result.record.model_dump_json() if result.record else "{}"

            rec.contains_none("no_pii_in_logs", case.forbidden_contains, logs)
            rec.contains_none("no_pii_in_traces", case.forbidden_contains, spans)
            rec.contains_none("no_pii_in_audit", case.forbidden_contains, audit)
            rec.contains_none("no_pii_in_execution_record", case.forbidden_contains, record_json)
            rec.contains_none("no_pii_in_response", case.forbidden_contains, result.message)

            if case.expected_outcome and case.expected_outcome.value == "BLOCK":
                rec.equals("outcome", "BLOCK", result.outcome.value)
        except Exception as exc:
            rec.error("pii_case", exc)
        finally:
            root.handlers = previous

        suite.cases.append(rec.finish())

    # Structural model-boundary checks.
    container = build()
    for index, (field, value) in enumerate(
        [
            ("password", "hunter2"),
            ("access_token", "at_abc"),
            ("refresh_token", "rt_abc"),
            ("authorization", "Bearer abc.def.ghi"),
            ("api_key", "sk-abcdefghijklmnop"),
            ("aadhaar", "234567890123"),
            ("mobile", "9876543210"),
            ("customer_id", "CUST-1001"),
        ],
        start=200,
    ):
        rec = CaseRecorder(f"pii-{index}", "PII_SECURITY")
        cleaned, report = container.sanitizer.sanitize_payload({field: value, "product": "Motor"})
        rec.is_true("field_removed_from_model_context", field not in cleaned)
        rec.contains_none("value_not_present", [value], json.dumps(cleaned))
        rec.is_true("removal_is_recorded", field in report.removed_fields)
        suite.cases.append(rec.finish())

    return suite


# ============================ token budget suite ============================
async def token_budget_suite(build: Any) -> SuiteResult:
    """§26.3 / §58.13: model-call counts, agent steps and token budgets."""
    suite = SuiteResult(name="token_budget")

    # Deterministic actions must use zero model calls.
    container = build()
    rec = CaseRecorder("tok-001", "PERFORMANCE")
    ctx = context_for("CUSTOMER", conversation_id="conv_tok_fastpath")
    before = getattr(container.invoker.model, "call_count", 0)
    harness_before = len(container.harness.execution_records)
    try:
        state = await drive_to(container, ctx, wf.STATE_REVIEW)
        rec.equals("final_state", wf.STATE_REVIEW, state.state)
        rec.equals("model_calls", 0, getattr(container.invoker.model, "call_count", 0) - before)
        rec.equals("harness_executions", 0, len(container.harness.execution_records) - harness_before)
        rec.equals("llm_input_tokens", 0, sum(u.input_tokens for u in container.invoker.usage_log))
        rec.equals("llm_output_tokens", 0, sum(u.output_tokens for u in container.invoker.usage_log))
    except Exception as exc:
        rec.error("fast_path", exc)
    suite.cases.append(rec.finish())

    # FAQ cases: at most one model call and within the token budget.
    container = build()
    for case in load_jsonl("faq-golden.jsonl").cases:
        rec = CaseRecorder(f"tok-{case.case_id}", case.category.value)
        try:
            result = await container.harness.execute(
                context_for(case.actor, conversation_id=f"conv_{case.case_id}"),
                CapabilityRequest(
                    capability_id=CAPABILITY_FAQ,
                    user_message=case.input,
                    payload={"question": case.input},
                ),
            )
            record = result.record
            if record is None:
                rec.error("execution_record", RuntimeError("no execution record produced"))
            else:
                rec.at_most("model_calls", case.max_model_calls, record.model_calls)
                rec.at_most("agent_steps", case.max_agent_steps, record.agent_steps)
                rec.equals("agent_handoffs", 0, record.agent_handoffs)
                rec.at_most("input_tokens", case.max_input_tokens, record.input_tokens)
                rec.at_most("output_tokens", case.max_output_tokens, record.output_tokens)
                rec.at_most("latency_ms", case.latency_budget_ms, round(record.latency_ms, 2))
        except Exception as exc:
            rec.error("faq_budget", exc)
        suite.cases.append(rec.finish())

    # Routing cases.
    container = build()
    for case in load_jsonl("ambiguous-intent.jsonl").cases:
        if "fast-path" in case.tags:
            continue
        rec = CaseRecorder(f"tok-{case.case_id}", case.category.value)
        try:
            result = await container.harness.execute(
                context_for(case.actor, conversation_id=f"conv_{case.case_id}"),
                CapabilityRequest(
                    capability_id=CAPABILITY_INTENT,
                    user_message=case.input,
                    payload={"message": case.input},
                ),
            )
            record = result.record
            if record is None:
                rec.error("execution_record", RuntimeError("no execution record produced"))
            else:
                rec.at_most("model_calls", case.max_model_calls, record.model_calls)
                rec.at_most("input_tokens", case.max_input_tokens, record.input_tokens)
                rec.equals("agent_handoffs", 0, record.agent_handoffs)
        except Exception as exc:
            rec.error("routing_budget", exc)
        suite.cases.append(rec.finish())

    # Interrupt / resume must not grow context with conversation length.
    container = build()
    rec = CaseRecorder("tok-500", "PERFORMANCE")
    try:
        ctx = context_for("CUSTOMER", conversation_id="conv_tok_resume")
        await container.orchestrator.handle_message(ctx, "I want to buy a policy")
        for index in range(10):
            await container.orchestrator.handle_message(ctx, f"What is a deductible? ({index})")
        model_records = [r for r in container.harness.execution_records if r.model_calls]
        rec.at_least("model_requests_observed", 4, len(model_records))
        if len(model_records) >= 4:
            first = sum(r.input_tokens for r in model_records[:2]) / 2
            last = sum(r.input_tokens for r in model_records[-2:]) / 2
            growth = (last - first) / first if first else 0.0
            rec.at_most("context_growth_ratio", 0.25, round(growth, 4))
        rec.is_true(
            "history_within_window",
            all(r.history_tokens <= container.settings.max_history_tokens for r in model_records),
        )
    except Exception as exc:
        rec.error("interrupt_resume", exc)
    suite.cases.append(rec.finish())

    return suite


# ============================== security suite ==============================
async def security_suite(build: Any) -> SuiteResult:
    """§26.3: adversarial behaviour, unauthorized tools, prod-with-mock rejection."""
    suite = SuiteResult(name="security")
    dataset = load_jsonl("adversarial.jsonl")

    for case in dataset.cases:
        container = build()
        rec = CaseRecorder(case.case_id, case.category.value)
        ctx = context_for(case.actor, conversation_id=f"conv_{case.case_id}")

        if "authorization" in case.tags:
            requested = fixtures.CUSTOMER_A if case.actor == "AGENT_UNASSIGNED" else None
            try:
                await container.tools.get_policy_details(ctx, case.input, requested_customer_id=requested)
                rec.is_true("forbidden", False, "unauthorized read was allowed")
            except ForbiddenError:
                rec.is_true("forbidden", True)
            except Exception as exc:
                rec.error("authorization_case", exc)
            suite.cases.append(rec.finish())
            continue

        try:
            result = await container.harness.execute(
                ctx,
                CapabilityRequest(
                    capability_id=CAPABILITY_FAQ,
                    user_message=case.input,
                    payload={"question": case.input},
                ),
            )
            rec.equals("outcome", "BLOCK", result.outcome.value)
            rec.contains_none("no_leak_in_response", case.forbidden_contains, result.message)
            if result.record:
                rec.at_most("model_calls", case.max_model_calls, result.record.model_calls)
                rec.equals("estimated_cost", 0.0, result.record.estimated_cost)
        except Exception as exc:
            rec.error("adversarial_case", exc)
        suite.cases.append(rec.finish())

    # Unauthorized tool execution.
    container = build()
    rec = CaseRecorder("sec-200", "ADVERSARIAL")
    capability = container.capability_registry.get("motor.policy.details")
    for tool in ["IssuePolicy", "InitiatePayment", "DeleteAllPolicies", ""]:
        try:
            container.pep.authorize_tool_request(capability, tool)
            rec.is_true(f"tool_blocked_{tool or 'empty'}", False, f"{tool!r} was allowed")
        except ForbiddenError:
            rec.is_true(f"tool_blocked_{tool or 'empty'}", True)
    rec.is_true(
        "declared_tool_allowed",
        _tool_allowed(container, capability, "GetPolicyDetails"),
    )
    suite.cases.append(rec.finish())

    # Production must refuse mock providers and the deterministic model.
    rec = CaseRecorder("sec-300", "ADVERSARIAL")
    try:
        Settings(
            APP_ENV="prod",
            MODEL_PROVIDER="openai",
            JWT_JWKS_URI="https://idp.example/jwks.json",
            JWT_ALLOWED_ALGORITHMS="RS256",
        )
        rec.is_true("prod_refuses_mock_providers", False, "prod accepted mock providers")
    except Exception:
        rec.is_true("prod_refuses_mock_providers", True)

    try:
        Settings(
            APP_ENV="prod",
            MODEL_PROVIDER="deterministic",
            JWT_JWKS_URI="https://idp.example/jwks.json",
            JWT_ALLOWED_ALGORITHMS="RS256",
            CUSTOMER_PROVIDER="insuremo",
            POLICY_PROVIDER="insuremo",
            QUOTE_PROVIDER="insuremo",
            CLAIMS_PROVIDER="insuremo",
            PAYMENT_PROVIDER="insuremo",
        )
        rec.is_true("prod_refuses_test_model", False, "prod accepted the deterministic model")
    except Exception:
        rec.is_true("prod_refuses_test_model", True)
    suite.cases.append(rec.finish())

    # Rate limiting on the expensive tier.
    rec = CaseRecorder("sec-400", "ADVERSARIAL")
    from app.core.security.rate_limit import InMemoryRateLimitStore, RateLimitClass, RateLimiter

    limiter = RateLimiter(
        InMemoryRateLimitStore(), enabled=True, ai_per_minute=3, deterministic_per_minute=100
    )
    allowed = sum(1 for _ in range(3) if limiter.check(RateLimitClass.AI, subject_ref="s").allowed)
    rec.equals("ai_requests_allowed_before_limit", 3, allowed)
    rec.is_true(
        "ai_request_blocked_after_limit",
        not limiter.check(RateLimitClass.AI, subject_ref="s").allowed,
    )
    rec.is_true(
        "cheap_tier_unaffected",
        limiter.check(RateLimitClass.DETERMINISTIC, subject_ref="s").allowed,
    )
    suite.cases.append(rec.finish())

    return suite


def _tool_allowed(container: Container, capability: Any, tool: str) -> bool:
    try:
        container.pep.authorize_tool_request(capability, tool)
    except ForbiddenError:
        return False
    return True


# ============================= reliability suite =============================
async def reliability_suite(build: Any) -> SuiteResult:
    """§26.3 / §26.7: timeout, retry, circuit breaker, fallback, degraded modes."""
    suite = SuiteResult(name="reliability")

    # Provider timeout must not fabricate data.
    container = build()
    rec = CaseRecorder("rel-001", "RELIABILITY")
    container.faults.timeout_operations.add("get_policy")
    try:
        result = await container.tools.get_policy_details(
            context_for("CUSTOMER", conversation_id="conv_rel_1"), fixtures.POLICY_A_MOTOR
        )
        rec.equals("available", False, result.available)
        rec.equals("data", None, result.data)
        rec.is_true("reason_recorded", bool(result.reason_code))
    except Exception as exc:
        rec.error("provider_timeout", exc)
    suite.cases.append(rec.finish())

    # Provider outage must preserve workflow state.
    container = build()
    rec = CaseRecorder("rel-005", "RELIABILITY")
    try:
        ctx = context_for("CUSTOMER", conversation_id="conv_rel_5")
        state = await drive_to(container, ctx, wf.STATE_VALIDATE)
        version_before = state.version
        container.faults.unavailable_operations.add("create_quote")
        result = await container.workflow_service.execute(ctx, state, wf.ACTION_REQUEST_QUOTE)
        rec.is_true("service_failed", result.service_failed)
        rec.equals("state_preserved", wf.STATE_VALIDATE, result.outcome.state.state)
        rec.is_true("no_quote_stored", "quote_id" not in result.outcome.state.data)
        rec.equals("business_data_unchanged", state.data, result.outcome.state.data)
        rec.equals("no_transition_recorded", len(state.history), len(result.outcome.state.history))
        # The transition is reserved before the provider call and released when it fails
        # (§8), so the optimistic-locking version moves while no business change is
        # committed. The reservation must not be left behind.
        rec.is_true("reservation_released", result.outcome.state.pending_action is None)
        rec.is_true("version_advanced_by_reservation", result.outcome.state.version > version_before)
    except Exception as exc:
        rec.error("provider_outage", exc)
    suite.cases.append(rec.finish())

    # Model outage becomes a controlled fallback.
    container = build()
    rec = CaseRecorder("rel-002", "RELIABILITY")
    try:
        from app.ai.models.provider import DeterministicModel, GenerationSettings, ModelInvoker

        container.harness._invoker = ModelInvoker(
            DeterministicModel("broken", fail_with=RuntimeError("down")),
            settings=GenerationSettings(),
        )
        result = await container.harness.execute(
            context_for("PUBLIC", conversation_id="conv_rel_2"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
        rec.equals("outcome", "FALLBACK", result.outcome.value)
        rec.equals("payload_empty", {}, result.payload)
        rec.contains_none("no_invented_answer", ["deductible is"], result.message)
    except Exception as exc:
        rec.error("model_outage", exc)
    suite.cases.append(rec.finish())

    # Empty corpus abstains.
    container = build()
    rec = CaseRecorder("rel-007", "RELIABILITY")
    try:
        for document in container.corpus.documents():
            container.ingestion.revoke(document.document_id)
        result = await container.harness.execute(
            context_for("PUBLIC", conversation_id="conv_rel_7"),
            CapabilityRequest(
                capability_id=CAPABILITY_FAQ,
                user_message="What is a deductible?",
                payload={"question": "What is a deductible?"},
            ),
        )
        rec.equals("outcome", "ABSTAIN", result.outcome.value)
        rec.equals("model_calls", 0, result.record.model_calls if result.record else -1)
    except Exception as exc:
        rec.error("empty_corpus", exc)
    suite.cases.append(rec.finish())

    # Circuit breaker opens and recovers.
    rec = CaseRecorder("rel-008", "RELIABILITY")
    try:
        from app.core.errors.taxonomy import UpstreamUnavailableError
        from app.core.resilience.policies import (
            NO_RETRY,
            CircuitBreaker,
            CircuitState,
            ResiliencePolicy,
        )

        breaker = CircuitBreaker("eval", failure_threshold=2, reset_seconds=0.05)
        policy = ResiliencePolicy("eval", timeout_ms=50, retry=NO_RETRY, breaker=breaker)

        async def failing() -> None:
            raise UpstreamUnavailableError("down")

        import contextlib

        for _ in range(2):
            # The failures are the point; the breaker state afterwards is the assertion.
            with contextlib.suppress(UpstreamUnavailableError):
                await policy.execute(failing)
        rec.equals("circuit_state", CircuitState.OPEN.value, breaker.state.value)

        import asyncio

        await asyncio.sleep(0.06)
        rec.equals("circuit_half_open", CircuitState.HALF_OPEN.value, breaker.state.value)

        async def succeeding() -> str:
            return "ok"

        rec.equals("recovered", "ok", await policy.execute(succeeding))
        rec.equals("circuit_closed", CircuitState.CLOSED.value, breaker.state.value)
    except Exception as exc:
        rec.error("circuit_breaker", exc)
    suite.cases.append(rec.finish())

    # Irreversible actions are never retried.
    rec = CaseRecorder("rel-100", "RELIABILITY")
    from app.core.resilience.policies import SideEffectClass, retry_allowed

    rec.equals("read_only_retryable", True, retry_allowed(SideEffectClass.READ_ONLY, None))
    rec.equals("write_without_key", False, retry_allowed(SideEffectClass.LOW_RISK_WRITE, None))
    rec.equals("write_with_key", True, retry_allowed(SideEffectClass.LOW_RISK_WRITE, "k"))
    rec.equals("irreversible_never", False, retry_allowed(SideEffectClass.IRREVERSIBLE, "k"))
    suite.cases.append(rec.finish())

    # A failing audit sink fails closed.
    rec = CaseRecorder("rel-101", "RELIABILITY")
    try:
        from app.core.audit.events import AuditResult
        from app.core.audit.service import AuditService

        class BrokenSink:
            async def append(self, event: Any) -> None:
                raise OSError("audit store unreachable")

        strict = AuditService(BrokenSink(), fail_closed=True)
        try:
            await strict.record(
                context_for("CUSTOMER", conversation_id="conv_rel_101"),
                AuditAction.FLOW_STARTED,
                AuditResult.SUCCESS,
            )
            rec.is_true("audit_fails_closed", False, "a dropped audit event was tolerated")
        except AppError:
            rec.is_true("audit_fails_closed", True)
    except Exception as exc:
        rec.error("audit_fail_closed", exc)
    suite.cases.append(rec.finish())

    return suite


# =============================== contract suite ===============================
async def contract_suite(build: Any) -> SuiteResult:
    """§26.3: API contract behaviour, correlation ids, health, provider substitution."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import default_responder, make_settings

    suite = SuiteResult(name="contract")
    app = create_app(make_settings(), responder=default_responder())

    with TestClient(app, raise_server_exceptions=False) as client:
        rec = CaseRecorder("api-001", "CONTRACT")
        live = client.get("/api/v1/health/live")
        rec.equals("liveness_status", 200, live.status_code)
        rec.equals("liveness_body", "alive", live.json()["status"])
        ready = client.get("/api/v1/health/ready")
        rec.equals("readiness_status", 200, ready.status_code)
        rec.equals("readiness_body", "ready", ready.json()["status"])
        rec.contains_none(
            "health_leaks_nothing",
            ["password", "secret", "jwks", "localhost", "insuremo"],
            ready.text,
        )
        suite.cases.append(rec.finish())

        rec = CaseRecorder("api-002", "CONTRACT")
        response = client.get("/api/v1/health/live", headers={"X-Correlation-Id": "corr-eval-1"})
        rec.equals("correlation_echoed", "corr-eval-1", response.headers.get("X-Correlation-Id"))
        rec.is_true("request_id_present", bool(response.headers.get("X-Request-Id")))
        for header in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy"):
            rec.is_true(f"header_{header}", header in response.headers)
        suite.cases.append(rec.finish())

        rec = CaseRecorder("api-003", "CONTRACT")
        openapi = client.get("/api/v1/openapi.json")
        rec.equals("openapi_status", 200, openapi.status_code)
        schema = openapi.json()
        rec.is_true("has_paths", bool(schema.get("paths")))
        rec.is_true(
            "has_bearer_scheme",
            "bearerAuth" in schema.get("components", {}).get("securitySchemes", {}),
        )
        rec.is_true(
            "has_error_schema",
            "ErrorResponse" in schema.get("components", {}).get("schemas", {}),
        )
        suite.cases.append(rec.finish())

        rec = CaseRecorder("api-004", "CONTRACT")
        unauth = client.post(
            "/api/v1/actions",
            json={
                "conversation_id": "c",
                "capability_id": "motor.workflow.action",
                "action": "BEGIN",
                "payload": {},
            },
        )
        rec.equals("unauthenticated_status", 401, unauth.status_code)
        body = unauth.json()
        rec.equals("error_code", "AUTHENTICATION_REQUIRED", body["error"]["code"])
        for required in ("code", "message", "retryable", "requestId", "correlationId"):
            rec.is_true(f"error_field_{required}", required in body["error"])
        rec.contains_none(
            "no_internals_leaked",
            ["traceback", 'file "', "sqlalchemy", "jwt_dev", "hunter2"],
            unauth.text,
        )
        suite.cases.append(rec.finish())

        rec = CaseRecorder("api-005", "CONTRACT")
        for payload in [{}, {"message": ""}, {"message": "x" * 5000}, {"unexpected": 1}]:
            invalid = client.post("/api/v1/chat", json=payload)
            rec.is_true(f"rejected_{len(json.dumps(payload))}", invalid.status_code in (400, 422))
        suite.cases.append(rec.finish())

    # Provider substitution: both implementations satisfy the same Protocols.
    rec = CaseRecorder("api-006", "CONTRACT")
    try:
        from app.integrations.contracts.providers import PolicyProvider, QuoteProvider
        from app.integrations.factory import build_provider_bundle
        from app.integrations.insuremo.providers import InsureMoPolicyProvider

        mock_bundle = build_provider_bundle(Settings(APP_ENV="test"))
        rec.is_true("mock_satisfies_policy_protocol", isinstance(mock_bundle.policy, PolicyProvider))
        rec.is_true("mock_satisfies_quote_protocol", isinstance(mock_bundle.quote, QuoteProvider))
        rec.equals("mock_flagged", True, mock_bundle.is_mock)

        real_bundle = build_provider_bundle(
            Settings(
                APP_ENV="test",
                CUSTOMER_PROVIDER="insuremo",
                POLICY_PROVIDER="insuremo",
                QUOTE_PROVIDER="insuremo",
                CLAIMS_PROVIDER="insuremo",
                PAYMENT_PROVIDER="insuremo",
                INSUREMO_BASE_URL="https://api.example",
                INSUREMO_API_KEY="k",
            )
        )
        rec.is_true("insuremo_selected", isinstance(real_bundle.policy, InsureMoPolicyProvider))
        rec.is_true("real_satisfies_policy_protocol", isinstance(real_bundle.policy, PolicyProvider))
        rec.equals("real_not_flagged_mock", False, real_bundle.is_mock)
    except Exception as exc:
        rec.error("provider_substitution", exc)
    suite.cases.append(rec.finish())

    return suite


# ============================== registration ==============================
def build_container_factory() -> Any:
    """Return a callable producing a fresh container per case."""
    from tests.conftest import default_responder, make_settings

    def factory() -> Container:
        from app.core.observability.metrics import metrics

        metrics.reset()
        return build_container(make_settings(), responder=default_responder())

    return factory


def register_all(runner: Any) -> None:
    build = build_container_factory()
    runner.register("workflow", lambda: workflow_suite(build))
    runner.register("auth", lambda: auth_suite(build))
    runner.register("ui_directives", lambda: ui_directives_suite(build))
    runner.register("pii", lambda: pii_suite(build))
    runner.register("token_budget", lambda: token_budget_suite(build))
    runner.register("security", lambda: security_suite(build))
    runner.register("reliability", lambda: reliability_suite(build))
    runner.register("contract", lambda: contract_suite(build))


__all__ = ["Category", "EvalCase", "Verdict", "register_all"]
