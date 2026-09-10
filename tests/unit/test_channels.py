"""Channel extensibility proof (master prompt §1, §45): a text-only channel reuses the
same orchestrator, auth, workflow, guardrails and audit, and renders directives as
plain text with a numbered action menu. No markup ever leaves a text channel."""

from __future__ import annotations

import pytest

from app.channels.adapters import ChannelRegistry, TextChannelAdapter, WebChannelAdapter
from app.core.context.request_context import Channel
from app.core.errors.taxonomy import ValidationError
from app.ui_directives.schemas.directives import AssistantResponse, Directive, DirectiveType, ResponseType
from tests.conftest import auth_headers


def _faq_response() -> AssistantResponse:
    return AssistantResponse(
        conversation_id="conv_1",
        request_id="req_1",
        response_type=ResponseType.UI_DIRECTIVE,
        message="A deductible is the amount you pay first.",
        directive=Directive(
            type=DirectiveType.SHOW_FAQ_ANSWER,
            payload={
                "answer": "A deductible is <b>the amount</b> you pay first.",
                "verification": "VERIFIED",
                "citations": [{"document_id": "KB-1", "document_name": "Glossary", "version": "1.2"}],
                "accessibility": {"aria_label": "x", "role": "region"},
            },
        ),
        allowed_actions=["CONTINUE", "GO_BACK"],
    )


def test_text_adapter_renders_plain_text_menu_without_markup():
    rendered = TextChannelAdapter().render(_faq_response())
    assert rendered.channel is Channel.WHATSAPP
    assert "<b>" not in rendered.text and "</b>" not in rendered.text
    assert "Source: Glossary v1.2" in rendered.text
    assert "1. Continue" in rendered.text and "2. Go Back" in rendered.text
    assert rendered.quick_replies == ["CONTINUE", "GO_BACK"]
    assert "aria_label" not in rendered.text


def test_text_adapter_truncates_and_never_leaks_payload_fields():
    adapter = TextChannelAdapter(max_chars=60)
    rendered = adapter.render(_faq_response())
    assert len(rendered.text) <= 60
    assert rendered.payload == {"type": "text", "text": rendered.text}


def test_registry_fails_closed_for_unknown_channel():
    registry = ChannelRegistry()
    registry.register(WebChannelAdapter())
    with pytest.raises(ValidationError, match="unsupported_channel"):
        registry.get(Channel.WHATSAPP)
    with pytest.raises(ValueError):
        registry.register(WebChannelAdapter())


def test_web_adapter_passes_the_directive_contract_through():
    rendered = WebChannelAdapter().render(_faq_response())
    assert rendered.payload["directive"]["type"] == "SHOW_FAQ_ANSWER"


def test_whatsapp_route_runs_the_same_flow_with_zero_model_calls(client, customer_token):
    headers = auth_headers(customer_token)
    conv = client.post("/api/v1/conversations", json={}, headers=headers).json()["conversation_id"]
    r = client.post(
        "/api/v1/channels/whatsapp/messages",
        json={"text": "I want to buy a policy", "conversation_id": conv, "sender_ref": "wa-hash-1"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["modelCalls"] == 0 and body["meta"]["state"] == "ENTRY"
    assert "Reply with a number:" in body["text"] and "1. Begin" in body["text"]
    assert "<" not in body["text"]

    # A bare menu number is a deterministic UI action.
    r = client.post(
        "/api/v1/channels/whatsapp/messages", json={"text": "1", "conversation_id": conv}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["meta"]["state"] == "IDENTIFY_CUSTOMER" and r.json()["meta"]["modelCalls"] == 0

    # FAQ over the same channel is grounded and rendered as text with a source line.
    r = client.post(
        "/api/v1/channels/whatsapp/messages",
        json={"text": "What is a deductible?", "conversation_id": conv},
        headers=headers,
    )
    assert r.status_code == 200
    faq = r.json()
    assert "Source:" in faq["text"], faq["text"]
    assert faq["meta"]["routePath"] == "FAQ_RAG"
    assert faq["meta"]["outcome"] == "ALLOW"
    # An FAQ turn does not touch the transaction, so it reports no workflow state.
    assert "state" not in faq["meta"]

    # The channel is fixed by the route and recorded in the audit trail.
    events = client.app.state.container.audit_sink.events()
    assert any(e.channel == "WHATSAPP" for e in events)


def test_whatsapp_route_requires_authentication_and_rejects_unknown_fields(client):
    assert client.post("/api/v1/channels/whatsapp/messages", json={"text": "hi"}).status_code == 401
    from tests.conftest import make_token

    tok = make_token()
    r = client.post(
        "/api/v1/channels/whatsapp/messages",
        json={"text": "hi", "vendor_blob": {"x": 1}},
        headers=auth_headers(tok),
    )
    assert r.status_code == 400


def test_whatsapp_channel_can_be_disabled_by_feature_flag():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import default_responder, make_settings, make_token

    app = create_app(make_settings(FEATURE_WHATSAPP_CHANNEL_ENABLED="false"), responder=default_responder())
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(
            "/api/v1/channels/whatsapp/messages", json={"text": "hi"}, headers=auth_headers(make_token())
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
