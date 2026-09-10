"""Shared conversation store on Redis (master prompt §16, §19, §41).

Implements :class:`~app.orchestration.session.ConversationStore` for a multi-instance
deployment. Conversation memory is convenience context, not the system of record, and
this store keeps the same bounds the in-memory one enforces: a capped turn window and
a TTL after which the conversation simply disappears (§58.7, §41).

Key layout (under the configured prefix):

    conv:{id}        -> hash of owner_subject_id, tenant_id, channel, created_at, expires_at
    conv:{id}:turns  -> list of turn JSON, trimmed to the retained window

Turn appends use ``RPUSH`` + ``LTRIM`` in one transaction, so two instances appending
concurrently never lose or duplicate a turn and the window bound always holds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.exceptions import RedisError

from app.ai.harness.context.builder import ConversationTurn
from app.core.storage.redis_client import RedisClients, store_unavailable
from app.orchestration.session import Conversation


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class RedisConversationStore:
    """Multi-instance safe conversation store."""

    def __init__(
        self, clients: RedisClients, *, max_turns_retained: int = 20, ttl_seconds: int = 3_600
    ) -> None:
        self._clients = clients
        self._r = clients.asyncio
        self._max_turns = max_turns_retained
        self._ttl = ttl_seconds

    def _meta_key(self, conversation_id: str) -> str:
        return self._clients.key("conv", conversation_id)

    def _turns_key(self, conversation_id: str) -> str:
        return self._clients.key("conv", conversation_id, "turns")

    async def get(self, conversation_id: str) -> Conversation | None:
        try:
            raw_meta = await self._r.hgetall(self._meta_key(conversation_id))
            if not raw_meta:
                return None
            raw_turns = await self._r.lrange(self._turns_key(conversation_id), 0, -1)
        except RedisError as exc:
            raise store_unavailable("conversation.get", exc) from exc

        meta: dict[str, str] = {_text(k): _text(v) for k, v in raw_meta.items()}
        conversation = Conversation(
            conversation_id=conversation_id,
            owner_subject_id=meta["owner_subject_id"],
            tenant_id=meta.get("tenant_id") or None,
            channel=meta.get("channel", "PUBLIC_WEB"),
            created_at=datetime.fromisoformat(meta["created_at"]),
            updated_at=datetime.fromisoformat(meta.get("updated_at") or meta["created_at"]),
            expires_at=datetime.fromisoformat(meta["expires_at"]),
        )
        if conversation.is_expired:
            return None
        conversation.turns = [
            ConversationTurn(role=item["role"], text=item["text"], index=int(item.get("index", i)))
            for i, item in enumerate(json.loads(_text(raw)) for raw in raw_turns)
        ]
        return conversation

    async def create(self, conversation: Conversation) -> Conversation:
        conversation.expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl)
        meta: dict[Any, Any] = {
            "owner_subject_id": conversation.owner_subject_id,
            "tenant_id": conversation.tenant_id or "",
            "channel": conversation.channel,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
            "expires_at": conversation.expires_at.isoformat(),
        }
        try:
            async with self._r.pipeline(transaction=True) as pipe:
                pipe.hset(self._meta_key(conversation.conversation_id), mapping=meta)
                pipe.expire(self._meta_key(conversation.conversation_id), self._ttl)
                await pipe.execute()
        except RedisError as exc:
            raise store_unavailable("conversation.create", exc) from exc
        return conversation

    async def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        meta_key = self._meta_key(conversation_id)
        turns_key = self._turns_key(conversation_id)
        try:
            if not await self._r.exists(meta_key):
                return  # same contract as the in-memory store: unknown conversation, no-op
            turn.index = await self._r.llen(turns_key)
            payload = json.dumps({"role": turn.role, "text": turn.text, "index": turn.index})
            async with self._r.pipeline(transaction=True) as pipe:
                pipe.rpush(turns_key, payload)
                # Old turns are dropped rather than retained indefinitely (§41).
                pipe.ltrim(turns_key, -self._max_turns, -1)
                pipe.expire(turns_key, self._ttl)
                pipe.hset(meta_key, "updated_at", datetime.now(UTC).isoformat())
                await pipe.execute()
        except RedisError as exc:
            raise store_unavailable("conversation.append_turn", exc) from exc

    async def delete(self, conversation_id: str) -> None:
        """Supports the deletion / erasure workflow (§11)."""
        try:
            await self._r.delete(self._meta_key(conversation_id), self._turns_key(conversation_id))
        except RedisError as exc:
            raise store_unavailable("conversation.delete", exc) from exc

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except RedisError:
            return False
