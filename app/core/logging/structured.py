"""Structured JSON logging with mandatory redaction (master prompt §21.1, §10.2).

Every record is emitted as a single JSON object. The formatter routes *all* message
text and extra fields through :class:`MaskingService`, so a caller cannot log raw PII
even by mistake. Request/correlation identifiers are attached automatically from the
request context.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context.request_context import get_current_context
from app.core.privacy.masking import MaskingService, masking_service

#: Attributes present on every LogRecord that must not be copied into the payload.
_STANDARD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class RedactingJsonFormatter(logging.Formatter):
    """Formats records as redacted JSON."""

    def __init__(
        self,
        service: str,
        environment: str,
        version: str,
        masker: MaskingService | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._version = version
        self._masker = masker or masking_service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "version": self._version,
            "logger": record.name,
            "message": self._masker.mask_text(record.getMessage()),
        }

        ctx = get_current_context()
        if ctx is not None:
            payload.update(ctx.log_fields())

        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        if extras:
            payload.update(self._masker.redact(extras))

        if record.exc_info:
            # Type and redacted message only: no stack trace in the log stream (§44).
            exc_type, exc_value, _tb = record.exc_info
            payload["errorType"] = getattr(exc_type, "__name__", "Exception")
            payload["errorMessage"] = self._masker.mask_text(str(exc_value))

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    *,
    service: str,
    environment: str,
    version: str,
    level: str = "INFO",
    stream: Any = None,
) -> None:
    """Install the redacting JSON formatter as the single root handler."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(RedactingJsonFormatter(service, environment, version))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn duplicates access logs in a non-JSON format; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
