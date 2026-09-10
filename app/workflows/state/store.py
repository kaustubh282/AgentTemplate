"""Workflow state persistence behind an interface (master prompt §16, §19, §41).

The in-memory implementation is correct for a single process and for tests. A shared
implementation (Redis/SQL/DynamoDB) satisfies the same Protocol, which is what keeps
API processes stateless and horizontally scalable.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Protocol

from app.core.errors.taxonomy import ConfigurationError, FlowStateConflictError, ResourceNotFoundError
from app.core.storage.redis_client import RedisClients
from app.workflows.state.models import FlowStatus, WorkflowState


class WorkflowStateStore(Protocol):
    async def get(self, flow_id: str) -> WorkflowState | None: ...
    async def get_active_for_conversation(self, conversation_id: str) -> WorkflowState | None: ...
    async def create(self, state: WorkflowState) -> WorkflowState: ...
    async def update(self, state: WorkflowState, expected_version: int) -> WorkflowState: ...
    async def delete(self, flow_id: str) -> None: ...


class InMemoryWorkflowStateStore:
    """Process-local store with optimistic locking semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_flow: dict[str, WorkflowState] = {}

    async def get(self, flow_id: str) -> WorkflowState | None:
        with self._lock:
            state = self._by_flow.get(flow_id)
            return state.model_copy(deep=True) if state else None

    async def get_active_for_conversation(self, conversation_id: str) -> WorkflowState | None:
        with self._lock:
            for state in self._by_flow.values():
                if state.conversation_id == conversation_id and state.status is FlowStatus.ACTIVE:
                    return state.model_copy(deep=True)
            return None

    async def create(self, state: WorkflowState) -> WorkflowState:
        with self._lock:
            if state.flow_id in self._by_flow:
                raise FlowStateConflictError("flow_already_exists")
            stored = state.model_copy(deep=True)
            self._by_flow[state.flow_id] = stored
            return stored.model_copy(deep=True)

    async def update(self, state: WorkflowState, expected_version: int) -> WorkflowState:
        with self._lock:
            current = self._by_flow.get(state.flow_id)
            if current is None:
                raise ResourceNotFoundError("flow_not_found")
            if current.version != expected_version:
                raise FlowStateConflictError(
                    "optimistic_lock_conflict",
                    details={"expected": expected_version, "actual": current.version},
                )
            updated = state.model_copy(deep=True)
            updated.version = expected_version + 1
            updated.updated_at = datetime.now(UTC)
            self._by_flow[state.flow_id] = updated
            return updated.model_copy(deep=True)

    async def delete(self, flow_id: str) -> None:
        with self._lock:
            self._by_flow.pop(flow_id, None)

    def reset(self) -> None:
        with self._lock:
            self._by_flow.clear()

    def count(self) -> int:
        with self._lock:
            return len(self._by_flow)


def build_workflow_store(provider: str, clients: RedisClients | None = None) -> WorkflowStateStore:
    """Select the workflow store. ``redis`` is the multi-instance choice (§19)."""
    if provider == "inmemory":
        return InMemoryWorkflowStateStore()
    if provider == "redis":
        if clients is None:
            raise ConfigurationError("redis_clients_required_for_workflow_store")
        from app.workflows.state.redis_store import RedisWorkflowStateStore

        return RedisWorkflowStateStore(clients)
    raise ConfigurationError(f"unsupported_workflow_store_provider:{provider}")
