"""Conversation, chat and action endpoints (master prompt §29).

Handlers are thin: they resolve context, delegate to the orchestrator, and return the
validated response contract. No business logic and no JWT parsing lives here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_container, protected_request_context, public_request_context
from app.api.schemas.api_models import (
    ActionRequest,
    ChatRequest,
    CreateConversationRequest,
    CreateConversationResponse,
    StartFlowRequest,
)
from app.bootstrap import Container
from app.core.context.request_context import RequestContext, set_current_context
from app.ui_directives.schemas.directives import AssistantResponse

router = APIRouter(prefix="/api/v1", tags=["conversations"])


@router.post("/conversations", response_model=CreateConversationResponse, status_code=201)
async def create_conversation(
    _body: CreateConversationRequest,
    ctx: Annotated[RequestContext, Depends(public_request_context)],
    container: Annotated[Container, Depends(get_container)],
) -> CreateConversationResponse:
    set_current_context(ctx)
    conversation = await container.orchestrator.start_conversation(ctx)
    return CreateConversationResponse(
        conversation_id=conversation.conversation_id,
        created_at=conversation.created_at.isoformat(),
        expires_at=conversation.expires_at.isoformat(),
    )


@router.post("/chat", response_model=AssistantResponse)
async def chat(
    body: ChatRequest,
    ctx: Annotated[RequestContext, Depends(public_request_context)],
    container: Annotated[Container, Depends(get_container)],
) -> AssistantResponse:
    """Free-text entry point. The deterministic router runs before any model (§2.3)."""
    scoped = ctx.child(conversation_id=body.conversation_id or ctx.conversation_id)
    set_current_context(scoped)
    return await container.orchestrator.handle_message(scoped, body.message)


@router.post("/actions", response_model=AssistantResponse)
async def perform_action(
    body: ActionRequest,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
) -> AssistantResponse:
    """Structured action. Deterministic, allow-listed, zero model calls (§59.1)."""
    scoped = ctx.child(conversation_id=body.conversation_id)
    set_current_context(scoped)
    return await container.orchestrator.handle_action(
        scoped,
        body.capability_id,
        body.action,
        body.payload,
        confirmed=body.confirmed,
        confirmation_token=body.confirmation_token,
    )


@router.post("/flows/start", response_model=AssistantResponse)
async def start_flow(
    body: StartFlowRequest,
    ctx: Annotated[RequestContext, Depends(protected_request_context)],
    container: Annotated[Container, Depends(get_container)],
) -> AssistantResponse:
    scoped = ctx.child(conversation_id=body.conversation_id)
    set_current_context(scoped)
    return await container.orchestrator.start_flow(scoped, body.conversation_id, body.capability_id)
