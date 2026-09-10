"""Audit service and pluggable append-only sinks (master prompt §22, §41)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Protocol

from app.core.audit.events import (
    AuditAction,
    AuditEvent,
    AuditResult,
    DecisionRecord,
    VersionStamp,
)
from app.core.context.request_context import RequestContext
from app.core.errors.taxonomy import AppError, ErrorCode
from app.core.logging.structured import get_logger
from app.core.privacy.masking import MaskingService, masking_service

logger = get_logger(__name__)


class AuditSink(Protocol):
    """Append-only destination for audit events."""

    async def append(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    """Test/local sink retaining events in order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self, action: AuditAction | None = None) -> list[AuditEvent]:
        with self._lock:
            if action is None:
                return list(self._events)
            return [e for e in self._events if e.action is action]

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class FileAuditSink:
    """Append-only newline-delimited JSON sink.

    A real deployment substitutes a WORM/append-only store; the interface is identical.
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    async def append(self, event: AuditEvent) -> None:
        line = event.model_dump_json()
        await asyncio.to_thread(self._write, line)

    def _write(self, line: str) -> None:
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class AuditService:
    """Creates, redacts and persists audit events.

    ``fail_closed`` (default) turns a sink failure into an error so a required audit
    event is never silently dropped (§20.1 graceful-degradation matrix).
    """

    def __init__(
        self,
        sink: AuditSink,
        *,
        masker: MaskingService | None = None,
        fail_closed: bool = True,
        default_versions: VersionStamp | None = None,
    ) -> None:
        self._sink = sink
        self._masker = masker or masking_service
        self._fail_closed = fail_closed
        self._default_versions = default_versions or VersionStamp()

    async def record(
        self,
        ctx: RequestContext,
        action: AuditAction,
        result: AuditResult,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        source_system: str | None = None,
        reason_code: str | None = None,
        versions: VersionStamp | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            correlation_id=ctx.correlation_id,
            request_id=ctx.request_id,
            actor_ref=ctx.auth.subject_ref,
            actor_type=ctx.auth.actor_type.value,
            channel=ctx.channel.value,
            action=action,
            result=result,
            resource_type=resource_type,
            resource_ref=self._masker.mask_value("policy_number", resource_id) if resource_id else None,
            source_system=source_system,
            reason_code=reason_code,
            environment=ctx.environment,
            versions=versions or self._default_versions,
            attributes=self._masker.redact(attributes or {}),
        )
        await self._append(event)
        return event

    async def _append(self, event: AuditEvent) -> None:
        try:
            await self._sink.append(event)
        except Exception as exc:
            logger.error("audit_sink_failure", extra={"auditAction": event.action.value})
            if self._fail_closed:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR, reason="audit_sink_unavailable", retryable=True
                ) from exc

    async def record_decision(self, ctx: RequestContext, record: DecisionRecord) -> AuditEvent:
        """Persist an explainability record *in the audit trail* (§23).

        The record is appended to the same append-only sink as every other audit event,
        so retrieved source ids, guardrail and grounding outcomes and the version stamp
        survive a restart and can be pulled by an auditor. It is also logged for
        operators. It never contains prompt text or chain-of-thought.
        """
        logger.info(
            "decision_record",
            extra={
                "capabilityId": record.capability_id,
                "intent": record.intent_selected,
                "sources": record.retrieved_source_ids,
                "tools": record.tools_selected,
                "guardrail": record.guardrail_result,
                "grounding": record.grounding_result,
                "finalAction": record.final_action_category,
                "decisionRecordVersion": record.schema_version,
            },
        )
        event = AuditEvent(
            correlation_id=record.correlation_id,
            request_id=record.request_id,
            actor_ref=ctx.auth.subject_ref,
            actor_type=ctx.auth.actor_type.value,
            channel=ctx.channel.value,
            action=AuditAction.AI_DECISION_RECORDED,
            result=AuditResult.SUCCESS,
            resource_type="AI_DECISION",
            reason_code=record.final_action_category,
            environment=ctx.environment,
            versions=record.versions,
            attributes={
                "capabilityId": record.capability_id,
                "intentSelected": record.intent_selected,
                "rulesEvaluated": list(record.rules_evaluated),
                "retrievedSourceIds": list(record.retrieved_source_ids),
                "toolsSelected": list(record.tools_selected),
                "toolResultCategories": list(record.tool_result_categories),
                "guardrailResult": record.guardrail_result,
                "groundingResult": record.grounding_result,
                "decisionRecordVersion": record.schema_version,
            },
        )
        await self._append(event)
        return event


def build_audit_sink(provider: str, file_path: str, *, chain_secret: str | None = None) -> AuditSink:
    """Select the audit sink. ``chained_file`` is the tamper-evident one (§22, B2)."""
    if provider == "file":
        return FileAuditSink(file_path)
    if provider in ("chained_file", "chained_memory"):
        from app.core.audit.chain import FileAppendOnlyLog, HashChainedAuditSink, MemoryAppendOnlyLog

        if not chain_secret:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, reason="audit_chain_secret_required")
        log = FileAppendOnlyLog(file_path) if provider == "chained_file" else MemoryAppendOnlyLog()
        return HashChainedAuditSink(log, secret=chain_secret)
    return InMemoryAuditSink()
