"""Conversation/session store (master prompt §16, §41).

Kept structurally separate from workflow state and from authoritative business data.
Conversation memory is convenience context only: it is never the system of record, and
it is bounded and expiring by default rather than retained indefinitely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.ai.harness.context.builder import ConversationTurn
from app.core.errors.taxonomy import ConfigurationError
from app.core.storage.redis_client import RedisClients


@dataclass(slots=True)
class Conversation:
    conversation_id: str
    #: Authenticated owner. Every read and write of this conversation is checked
    #: against it by the orchestrator (§13.2, C-1). Set server-side, never from a payload.
    owner_subject_id: str
    tenant_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=1))
    turns: list[ConversationTurn] = field(default_factory=list)
    channel: str = "PUBLIC_WEB"

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


class ConversationStore(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None: ...
    async def create(self, conversation: Conversation) -> Conversation: ...
    async def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None: ...
    async def delete(self, conversation_id: str) -> None: ...


class InMemoryConversationStore:
    """Process-local store with a bounded turn window (§58.7)."""

    def __init__(
        self, *, max_turns_retained: int = 20, ttl_seconds: int = 3_600, max_entries: int = 10_000
    ) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Conversation] = {}
        self._max_turns = max_turns_retained
        self._ttl = ttl_seconds
        #: Hard bound (§19): expired conversations are evicted on write and, past the
        #: bound, the oldest are dropped so a long-lived process cannot grow unbounded.
        self._max_entries = max_entries

    async def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            conversation = self._items.get(conversation_id)
            if conversation is None or conversation.is_expired:
                return None
            return conversation

    async def create(self, conversation: Conversation) -> Conversation:
        with self._lock:
            conversation.expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl)
            self._evict_locked()
            self._items[conversation.conversation_id] = conversation
            return conversation

    def _evict_locked(self) -> None:
        if len(self._items) < self._max_entries:
            return
        now = datetime.now(UTC)
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            del self._items[key]
        while len(self._items) >= self._max_entries:
            oldest = min(self._items.values(), key=lambda c: c.updated_at)
            del self._items[oldest.conversation_id]

    async def append_turn(self, conversation_id: str, turn: ConversationTurn) -> None:
        with self._lock:
            conversation = self._items.get(conversation_id)
            if conversation is None:
                return
            turn.index = len(conversation.turns)
            conversation.turns.append(turn)
            # Old turns are dropped rather than retained indefinitely (§41).
            if len(conversation.turns) > self._max_turns:
                conversation.turns = conversation.turns[-self._max_turns :]
            conversation.updated_at = datetime.now(UTC)

    async def delete(self, conversation_id: str) -> None:
        """Supports the deletion / erasure workflow (§11)."""
        with self._lock:
            self._items.pop(conversation_id, None)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


def build_conversation_store(
    provider: str, ttl_seconds: int, clients: RedisClients | None = None
) -> ConversationStore:
    """Select the session store. ``redis`` is the multi-instance choice (§19)."""
    if provider == "inmemory":
        return InMemoryConversationStore(ttl_seconds=ttl_seconds)
    if provider == "redis":
        if clients is None:
            raise ConfigurationError("redis_clients_required_for_session_store")
        from app.orchestration.session_redis import RedisConversationStore

        return RedisConversationStore(clients, ttl_seconds=ttl_seconds)
    raise ConfigurationError(f"unsupported_session_store_provider:{provider}")
