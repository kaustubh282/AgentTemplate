"""Channel adapter contract and the two shipped adapters (master prompt §1, §45).

The platform speaks one internal contract - :class:`AssistantResponse` with a registered
UI directive - regardless of where a message came from. A *channel adapter* owns the two
translations at the edge:

* **inbound**: a channel's raw envelope -> the normalised text and identifiers the
  orchestrator needs (nothing else; channel payloads never reach the model or state)
* **outbound**: an ``AssistantResponse`` -> what that channel can actually render

The web channel renders directives as-is (a rich client draws the components). A text
channel such as WhatsApp cannot render components, so its adapter turns each directive
into plain text plus a numbered list of the allowed actions, and it *never* emits
markup. Every adapter reuses the same auth, router, Harness, workflow, guardrail, audit
and provider infrastructure; a new channel is one adapter class and one route, which is
the same shape of extension the travel domain proves for products.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.context.request_context import Channel
from app.core.errors.taxonomy import ValidationError
from app.ui_directives.schemas.directives import AssistantResponse, DirectiveType


class InboundMessage(BaseModel):
    """What every channel must be reduced to before the orchestrator sees it."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4_000)
    conversation_id: str | None = None
    #: Channel-native sender reference (e.g. an E.164 number hash). Never an identity:
    #: identity comes from the validated AuthContext, not from the channel envelope.
    sender_ref: str | None = None
    locale: str = "en-IN"


@dataclass(slots=True)
class RenderedMessage:
    """Channel-ready output. ``text`` is always plain text; ``payload`` is channel-native."""

    channel: Channel
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    quick_replies: list[str] = field(default_factory=list)


class ChannelAdapter(Protocol):
    channel: Channel

    def normalize_inbound(self, raw: dict[str, Any]) -> InboundMessage: ...

    def render(self, response: AssistantResponse) -> RenderedMessage: ...


class WebChannelAdapter:
    """Rich web/mobile clients render the directive contract themselves."""

    channel = Channel.WEB_CUSTOMER

    def normalize_inbound(self, raw: dict[str, Any]) -> InboundMessage:
        return InboundMessage(
            text=str(raw.get("message", "")),
            conversation_id=raw.get("conversation_id"),
            locale=str(raw.get("locale") or "en-IN"),
        )

    def render(self, response: AssistantResponse) -> RenderedMessage:
        return RenderedMessage(
            channel=self.channel,
            text=response.message,
            payload=response.model_dump(mode="json"),
            quick_replies=list(response.allowed_actions),
        )


_MARKUP = re.compile(r"<[^>]{1,200}>")


class TextChannelAdapter:
    """Plain-text rendering for messaging channels (WhatsApp-style).

    Directives are flattened into text a person can read on a phone; allowed actions
    become a numbered menu so the deterministic router can map a reply of ``2`` back to
    an action with zero model calls. Accessibility hints and client-only payload fields
    are dropped; no markup is ever emitted.
    """

    def __init__(self, channel: Channel = Channel.WHATSAPP, *, max_chars: int = 1_500) -> None:
        self.channel = channel
        self._max_chars = max_chars

    def normalize_inbound(self, raw: dict[str, Any]) -> InboundMessage:
        text = str(raw.get("text") or raw.get("body") or "").strip()
        if not text:
            raise ValidationError("empty_channel_message")
        return InboundMessage(
            text=text,
            conversation_id=raw.get("conversation_id"),
            sender_ref=str(raw["from"]) if raw.get("from") else None,
            locale=str(raw.get("locale") or "en-IN"),
        )

    def render(self, response: AssistantResponse) -> RenderedMessage:
        lines: list[str] = []
        directive = response.directive
        if directive is None:
            lines.append(response.message)
        else:
            lines.extend(self._render_directive(directive.type, directive.payload, response.message))
        if response.allowed_actions:
            lines.append("")
            lines.append("Reply with a number:")
            for index, action in enumerate(response.allowed_actions, start=1):
                lines.append(f"{index}. {_humanise(action)}")
        text = _MARKUP.sub("", "\n".join(line for line in lines if line is not None))
        if len(text) > self._max_chars:
            text = text[: self._max_chars - 1].rstrip() + "…"
        return RenderedMessage(
            channel=self.channel,
            text=text,
            payload={"type": "text", "text": text},
            quick_replies=list(response.allowed_actions),
        )

    @staticmethod
    def _render_directive(kind: DirectiveType, payload: dict[str, Any], message: str) -> list[str]:
        if kind is DirectiveType.SHOW_FAQ_ANSWER:
            out = [str(payload.get("answer") or message)]
            citations = payload.get("citations") or []
            if citations:
                names = sorted({f"{c.get('document_name')} v{c.get('version')}" for c in citations})
                out.append("Source: " + "; ".join(names))
            return out
        if kind is DirectiveType.SHOW_FORM:
            out = [message]
            for item in payload.get("fields", []):
                label = item.get("label") or item.get("name")
                out.append(f"- {label}")
            return out
        if kind is DirectiveType.SHOW_OPTIONS:
            out = [message]
            for item in payload.get("options", []):
                out.append(f"- {item.get('label') or item.get('value')}")
            return out
        if kind in (DirectiveType.SHOW_QUOTE_SUMMARY, DirectiveType.SHOW_REVIEW):
            out = [message]
            for line in payload.get("lines", []) or payload.get("line_items", []):
                out.append(f"- {line.get('label')}: {line.get('amount_display') or line.get('amount')}")
            total = payload.get("total_display") or payload.get("total")
            if total is not None:
                out.append(f"Total: {total}")
            return out
        if kind is DirectiveType.SHOW_RETRY:
            return [str(payload.get("body") or message)]
        if kind is DirectiveType.SHOW_ERROR:
            return [str(payload.get("body") or message)]
        if kind is DirectiveType.SHOW_HUMAN_HANDOFF:
            return [str(payload.get("body") or message), "Our support team can help you from here."]
        if kind is DirectiveType.SHOW_COMPLETION:
            return [message, f"Reference: {payload.get('reference_masked', '')}".rstrip()]
        body = payload.get("body")
        return [str(body) if body else message]


def _humanise(action: str) -> str:
    return action.replace("_", " ").title()


class ChannelRegistry:
    """Adapters registered at startup; unknown channels fail closed."""

    def __init__(self) -> None:
        self._adapters: dict[Channel, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        if adapter.channel in self._adapters:
            raise ValueError(f"duplicate channel adapter: {adapter.channel}")
        self._adapters[adapter.channel] = adapter

    def get(self, channel: Channel) -> ChannelAdapter:
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise ValidationError("unsupported_channel", details={"channel": channel.value})
        return adapter

    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._adapters)
