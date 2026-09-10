"""Channel endpoints (master prompt §45): one route per messaging channel.

A messaging channel cannot render UI directives. Its route accepts the channel's
inbound envelope, normalises it through the registered adapter, runs the *same*
orchestrator every other channel uses (auth, router, Harness, workflow, guardrails,
audit unchanged) and renders the result back into plain text for that channel.

Text replies that are a bare menu number are mapped to the allowed action shown in the
previous turn - a deterministic UI action with zero model calls.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_container, protected_request_context
from app.api.schemas.api_models import SERVER_MINTED_ID_PATTERN
from app.bootstrap import Container
from app.core.context.request_context import Channel, RequestContext, set_current_context

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


class WhatsAppInbound(BaseModel):
    """Minimal, provider-neutral inbound envelope. The gateway integration maps its
    vendor payload to this shape; vendor fields never reach the platform."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4_000)
    conversation_id: str | None = Field(default=None, pattern=SERVER_MINTED_ID_PATTERN)
    #: Pseudonymous sender reference supplied by the gateway; never an identity.
    sender_ref: str | None = Field(default=None, max_length=64)


class ChannelReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    request_id: str
    text: str
    quick_replies: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post("/whatsapp/messages", response_model=ChannelReply)
async def whatsapp_message(
    body: WhatsAppInbound,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
) -> ChannelReply:
    """Authenticated (the gateway exchanges the sender's verified identity for a JWT
    upstream); the channel is fixed by the route, not by a header."""
    adapter = container.channels.get(Channel.WHATSAPP)
    inbound = adapter.normalize_inbound(body.model_dump())
    scoped = ctx.child(
        channel=Channel.WHATSAPP, conversation_id=inbound.conversation_id or ctx.conversation_id
    )
    set_current_context(scoped)

    message = inbound.text
    if scoped.conversation_id and message.strip().isdigit():
        # A menu number replies to the previous turn's allowed actions: deterministic.
        active = await container.workflow_engine.get_active(scoped.conversation_id, scoped)
        if active is not None:
            actions = container.workflow_engine.allowed_actions(active)
            index = int(message.strip()) - 1
            if 0 <= index < len(actions):
                binding = container.orchestrator.binding_for_workflow(active.workflow_id)
                response = await container.orchestrator.handle_action(
                    scoped, binding.action_capability_id, actions[index], {}
                )
                rendered = adapter.render(response)
                return ChannelReply(
                    conversation_id=response.conversation_id,
                    request_id=response.request_id,
                    text=rendered.text,
                    quick_replies=rendered.quick_replies,
                    meta={
                        k: v for k, v in response.meta.items() if k in ("modelCalls", "state", "routePath")
                    },
                )

    response = await container.orchestrator.handle_message(scoped, message)
    rendered = adapter.render(response)
    return ChannelReply(
        conversation_id=response.conversation_id,
        request_id=response.request_id,
        text=rendered.text,
        quick_replies=rendered.quick_replies,
        meta={k: v for k, v in response.meta.items() if k in ("modelCalls", "state", "routePath", "outcome")},
    )
