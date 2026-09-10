"""PII, logging-redaction and model-boundary tests (master prompt §10, §59.5).

Two distinct boundaries are proven here:
  * PII must not reach logs, traces or audit records
  * unnecessary PII and all secrets must not cross the model boundary
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from app.core.audit.events import AuditAction
from app.core.errors.taxonomy import GuardrailBlockedError
from app.core.logging.structured import RedactingJsonFormatter
from app.core.observability.tracing import recorder
from app.core.privacy.classification import DataClass, classify_field, is_never_loggable
from app.core.privacy.masking import MaskingService
from app.integrations.mock import fixtures
from tests.conftest import customer_context

REAL_PII = {
    "mobile": "9876543210",
    "email": "asha.verma@example.com",
    "pan": "ABCDE1234F",
    "aadhaar": "2345 6789 0123",
    "card_number": "4111 1111 1111 1111",
    "registration_number": "MH01AB1234",
}

SECRETS = {
    "password": "hunter2",
    "otp": "483920",
    "access_token": "at_abcdefghijklmnop",
    "refresh_token": "rt_abcdefghijklmnop",
    "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.sig",
    "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
    "cvv": "123",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----abc",
}


# ------------------------------------------------------------ classification ---
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("password", DataClass.SECRET),
        ("access_token", DataClass.SECRET),
        ("aadhaar", DataClass.SENSITIVE_PII),
        ("mobile", DataClass.PII),
        ("email", DataClass.PII),
        ("pan", DataClass.PII),
        ("policy_number", DataClass.CONFIDENTIAL),
        ("product", DataClass.INTERNAL),
    ],
)
def test_field_classification(field, expected):
    assert classify_field(field) is expected


@pytest.mark.parametrize("field", list(SECRETS))
def test_secrets_are_never_loggable(field):
    assert is_never_loggable(field)


# --------------------------------------------------------------- masking ---
def test_masking_hides_values_but_keeps_a_usable_suffix():
    masker = MaskingService()
    assert masker.mask_value("mobile", "9876543210") == "******3210"
    assert masker.mask_value("email", "asha.verma@example.com") == "a***@example.com"
    assert masker.mask_value("password", "hunter2") == "[REDACTED]"
    assert masker.mask_value("aadhaar", "234567890123").endswith("0123")
    assert "234567890123" not in masker.mask_value("aadhaar", "234567890123")


def test_free_text_pii_is_masked():
    masker = MaskingService()
    text = "Call 9876543210 or mail asha.verma@example.com, PAN ABCDE1234F, Aadhaar 2345 6789 0123"
    masked = masker.mask_text(text)
    for raw in ("9876543210", "asha.verma@example.com", "ABCDE1234F", "2345 6789 0123"):
        assert raw not in masked


def test_nested_structures_are_redacted():
    masker = MaskingService()
    payload = {
        "customer": {"mobile": "9876543210", "nested": {"password": "hunter2"}},
        "policies": [{"policy_number": "PTC0000001234"}],
        "note": "reach me at asha.verma@example.com",
    }
    redacted = masker.redact(payload)
    serialized = json.dumps(redacted)
    for raw in ("9876543210", "hunter2", "PTC0000001234", "asha.verma@example.com"):
        assert raw not in serialized


# ------------------------------------------------------ logging redaction ---
def _capture_log(**extra) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter("svc", "test", "0.1.0"))
    logger = logging.getLogger(f"pii_probe_{len(extra)}_{id(extra)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.info("probe message with mobile 9876543210", extra=extra)
    return stream.getvalue()


@pytest.mark.parametrize(("field", "value"), list(REAL_PII.items()) + list(SECRETS.items()))
def test_no_pii_or_secret_reaches_the_log_stream(field, value):
    written = _capture_log(**{field: value})
    assert value not in written, f"{field} leaked into logs"


def test_log_message_body_is_masked_too():
    written = _capture_log(product="Motor")
    assert "9876543210" not in written
    assert "******3210" in written


def test_logs_are_valid_json_with_required_fields():
    written = _capture_log(product="Motor")
    record = json.loads(written)
    for field in ("timestamp", "severity", "service", "environment", "version", "message"):
        assert field in record


def test_exception_logging_omits_the_stack_trace():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter("svc", "test", "0.1.0"))
    logger = logging.getLogger("pii_probe_exception")
    logger.handlers = [handler]
    logger.propagate = False
    try:
        raise ValueError("failed for customer 9876543210")
    except ValueError:
        logger.exception("operation_failed")
    written = stream.getvalue()
    assert "9876543210" not in written
    assert "Traceback" not in written
    assert json.loads(written)["errorType"] == "ValueError"


# ------------------------------------------------------- model boundary ---
@pytest.mark.parametrize(("field", "value"), list(SECRETS.items()))
def test_secrets_never_cross_the_model_boundary(container, field, value):
    cleaned, report = container.sanitizer.sanitize_payload({field: value, "product": "Motor"})
    assert field not in cleaned
    assert field in report.removed_fields
    assert value not in json.dumps(cleaned)


@pytest.mark.parametrize("field", ["mobile", "email", "pan", "aadhaar", "customer_id"])
def test_unnecessary_pii_is_removed_from_model_context(container, field):
    """Data minimisation: a field not needed for the current purpose is dropped (§10.1)."""
    cleaned, report = container.sanitizer.sanitize_payload(
        {field: REAL_PII.get(field, "value"), "product": "Motor"}, purpose_fields=frozenset()
    )
    assert field not in cleaned
    assert field in report.removed_fields
    assert cleaned["product"] == "Motor"


def test_pii_needed_for_the_purpose_is_kept_but_masked(container):
    """When a field *is* required, it is kept in minimised form rather than raw."""
    cleaned, _ = container.sanitizer.sanitize_payload(
        {"mobile": "9876543210"}, purpose_fields=frozenset({"mobile"})
    )
    assert "mobile" in cleaned
    assert cleaned["mobile"] != "9876543210"


def test_structural_secret_check_blocks_a_rendered_context(container):
    with pytest.raises(GuardrailBlockedError):
        container.sanitizer.assert_no_secrets(
            "context with token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.signature"
        )
    with pytest.raises(GuardrailBlockedError):
        container.sanitizer.assert_no_secrets("card 4111 1111 1111 1111")


async def test_built_context_never_contains_raw_pii(container):
    """End-to-end: a message full of PII produces a clean model context."""
    ledger = container.budgets.ledger_for()
    built = container.context_builder.build(
        system_prompt="You are an assistant.",
        question="My mobile is 9876543210 and PAN ABCDE1234F, what is a deductible?",
        ledger=ledger,
        retrieval=container.retriever.retrieve("what is a deductible"),
    )
    for raw in ("9876543210", "ABCDE1234F"):
        assert raw not in built.user_content
        assert raw not in built.system_prompt


async def test_provider_payload_is_projected_not_forwarded(container):
    """The AI-facing DTO carries only declared fields (§10.4)."""
    from app.integrations.contracts.dtos import AiPolicySummary

    ctx = customer_context(fixtures.CUSTOMER_A)
    raw = await container.providers.policy.get_policy(fixtures.POLICY_A_MOTOR, ctx)
    view = AiPolicySummary.from_policy(raw)

    exposed = set(view.model_dump())
    assert exposed <= set(AiPolicySummary.ai_fields) | {"currency"}
    assert "customer_id" not in exposed
    assert raw.policy_number not in view.policy_number_masked


# ----------------------------------------------------------------- traces ---
async def test_traces_carry_no_prompt_or_document_text(container):
    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from tests.conftest import public_context

    recorder.reset()
    await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="What is a deductible? My mobile is 9876543210.",
            payload={"question": "What is a deductible?"},
        ),
    )
    for span in recorder.spans():
        serialized = json.dumps(span.attributes, default=str)
        assert "9876543210" not in serialized
        for banned in ("prompt", "system_prompt", "document_text", "chunk_text", "answer"):
            assert banned not in span.attributes


# ------------------------------------------------------------------ audit ---
async def test_audit_records_contain_no_unredacted_pii(container):
    from app.ai.harness.service import CapabilityRequest
    from app.orchestration.capabilities import CAPABILITY_FAQ
    from tests.conftest import public_context

    await container.harness.execute(
        public_context(),
        CapabilityRequest(
            capability_id=CAPABILITY_FAQ,
            user_message="My PAN is ABCDE1234F. What is a deductible?",
            payload={"question": "What is a deductible?"},
        ),
    )
    for event in container.audit_sink.events():
        serialized = event.model_dump_json()
        assert "ABCDE1234F" not in serialized
        assert "Bearer " not in serialized


async def test_audit_uses_pseudonymous_actor_and_masked_resource_refs(container):
    ctx = customer_context(fixtures.CUSTOMER_A)
    await container.tools.get_policy_details(ctx, fixtures.POLICY_A_MOTOR)
    events = container.audit_sink.events(AuditAction.PROVIDER_CALL_OUTCOME)
    assert events
    event = events[-1]
    assert event.actor_ref.startswith("sub_")
    assert fixtures.CUSTOMER_A not in event.model_dump_json()
    assert event.resource_ref is not None and event.resource_ref.startswith("*")


def test_request_context_log_fields_are_pseudonymous():
    ctx = customer_context(fixtures.CUSTOMER_A, conversation_id="conv_secret_id")
    fields = ctx.log_fields()
    assert fixtures.CUSTOMER_A not in json.dumps(fields)
    assert "conv_secret_id" not in json.dumps(fields)
    assert fields["actorRef"].startswith("sub_")
    assert fields["conversationRef"].startswith("conv_")
