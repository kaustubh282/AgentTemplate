"""Shared workflow-state store on Redis (master prompt §16, §19, §41).

Implements :class:`~app.workflows.state.store.WorkflowStateStore` with the *same*
semantics the in-memory store guarantees and the engine relies on:

* one active flow per conversation (pointer key), resolved without scanning
* **optimistic locking**: ``update`` succeeds only if the stored version equals
  ``expected_version``; a stale writer receives ``FLOW_STATE_CONFLICT``. Implemented
  with ``WATCH`` / ``MULTI`` so the check-and-set is atomic against every other
  instance sharing the store, not just other coroutines in this process
* keys expire with the flow, so abandoned journeys are not retained indefinitely (§41)

Key layout (all under the configured prefix):

    wf:flow:{flow_id}                -> WorkflowState JSON
    wf:conv:{conversation_id}:active -> flow_id of the ACTIVE flow

A transport failure is reported as ``UPSTREAM_UNAVAILABLE`` (retryable), never masked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.exceptions import RedisError, WatchError

from app.core.errors.taxonomy import FlowStateConflictError, ResourceNotFoundError
from app.core.storage.redis_client import RedisClients, store_unavailable
from app.workflows.state.models import FlowStatus, WorkflowState

#: Grace kept after logical expiry so an expired-but-not-yet-purged flow still reads as
#: expired (the engine checks ``is_expired``) rather than vanishing mid-request.
EXPIRY_GRACE_SECONDS = 300
#: Optimistic-lock retries are *not* performed: a conflict is the caller's signal.
_STATUS_ACTIVE = FlowStatus.ACTIVE


class RedisWorkflowStateStore:
    """Multi-instance safe workflow store."""

    def __init__(self, clients: RedisClients) -> None:
        self._clients = clients
        self._r = clients.asyncio

    # ------------------------------------------------------------------ keys ---
    def _flow_key(self, flow_id: str) -> str:
        return self._clients.key("wf", "flow", flow_id)

    def _pointer_key(self, conversation_id: str) -> str:
        return self._clients.key("wf", "conv", conversation_id, "active")

    @staticmethod
    def _ttl_seconds(state: WorkflowState) -> int:
        remaining = (state.expires_at - datetime.now(UTC)).total_seconds()
        return max(int(remaining), 0) + EXPIRY_GRACE_SECONDS

    # ------------------------------------------------------------------ read ---
    async def get(self, flow_id: str) -> WorkflowState | None:
        try:
            raw = await self._r.get(self._flow_key(flow_id))
        except RedisError as exc:
            raise store_unavailable("workflow.get", exc) from exc
        return WorkflowState.model_validate_json(raw) if raw else None

    async def get_active_for_conversation(self, conversation_id: str) -> WorkflowState | None:
        try:
            flow_id = await self._r.get(self._pointer_key(conversation_id))
        except RedisError as exc:
            raise store_unavailable("workflow.get_active", exc) from exc
        if not flow_id:
            return None
        state = await self.get(flow_id.decode("utf-8") if isinstance(flow_id, bytes) else str(flow_id))
        if state is None or state.status is not _STATUS_ACTIVE:
            return None
        return state

    # ----------------------------------------------------------------- write ---
    async def create(self, state: WorkflowState) -> WorkflowState:
        key = self._flow_key(state.flow_id)
        ttl = self._ttl_seconds(state)
        try:
            created = await self._r.set(key, state.model_dump_json(), ex=ttl, nx=True)
            if not created:
                raise FlowStateConflictError("flow_already_exists")
            if state.status is _STATUS_ACTIVE:
                await self._r.set(self._pointer_key(state.conversation_id), state.flow_id, ex=ttl)
        except RedisError as exc:
            raise store_unavailable("workflow.create", exc) from exc
        return state.model_copy(deep=True)

    async def update(self, state: WorkflowState, expected_version: int) -> WorkflowState:
        """Atomic compare-and-set on the stored version across all instances."""
        key = self._flow_key(state.flow_id)
        pointer = self._pointer_key(state.conversation_id)
        updated = state.model_copy(deep=True)
        updated.version = expected_version + 1
        updated.updated_at = datetime.now(UTC)
        ttl = self._ttl_seconds(updated)

        try:
            async with self._r.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                current_raw = await pipe.get(key)
                if current_raw is None:
                    await pipe.unwatch()
                    raise ResourceNotFoundError("flow_not_found")
                current = WorkflowState.model_validate_json(current_raw)
                if current.version != expected_version:
                    await pipe.unwatch()
                    raise FlowStateConflictError(
                        "optimistic_lock_conflict",
                        details={"expected": expected_version, "actual": current.version},
                    )
                pipe.multi()
                pipe.set(key, updated.model_dump_json(), ex=ttl)
                if updated.status is _STATUS_ACTIVE:
                    pipe.set(pointer, updated.flow_id, ex=ttl)
                else:
                    # A completed/abandoned flow no longer blocks a new journey.
                    pipe.delete(pointer)
                await pipe.execute()
        except WatchError as exc:
            # Another instance committed between our read and our write.
            raise FlowStateConflictError(
                "optimistic_lock_conflict", details={"expected": expected_version, "actual": "concurrent"}
            ) from exc
        except RedisError as exc:
            raise store_unavailable("workflow.update", exc) from exc
        return updated.model_copy(deep=True)

    async def delete(self, flow_id: str) -> None:
        try:
            raw = await self._r.get(self._flow_key(flow_id))
            if raw:
                state = WorkflowState.model_validate_json(raw)
                await self._r.delete(self._pointer_key(state.conversation_id))
            await self._r.delete(self._flow_key(flow_id))
        except RedisError as exc:
            raise store_unavailable("workflow.delete", exc) from exc

    # --------------------------------------------------------------- probes ---
    async def ping(self) -> bool:
        """Readiness probe: can this instance reach the shared store?"""
        try:
            return bool(await self._r.ping())
        except RedisError:
            return False
