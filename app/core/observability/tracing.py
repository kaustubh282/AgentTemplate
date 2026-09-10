"""OpenTelemetry tracing with an in-process recorder (master prompt §21.3).

:func:`configure_tracing` builds a real ``TracerProvider`` (resource, ratio sampler and,
when an endpoint is configured, an OTLP/HTTP ``BatchSpanProcessor``). Every span the
application opens through :data:`tracer` is emitted through that provider *and* copied
into the in-process :data:`recorder`, so traces stay inspectable in tests and local runs
without a collector.

Raw prompt content, retrieved document text and any sensitive attribute are never
placed on a span; only identifiers, categories and measurements are. Exceptions are
recorded as ``error.type`` only: the message is not attached because it may carry PII.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from app.core.context.request_context import get_current_context
from app.core.errors.taxonomy import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

#: Attribute keys that must never carry free text into a trace backend.
_FORBIDDEN_SPAN_KEYS = frozenset(
    {"prompt", "system_prompt", "messages", "document_text", "chunk_text", "answer", "user_message"}
)

#: OTLP/HTTP traces are posted to this path; the env-var convention is to configure the
#: collector base URL, so the path is appended when absent.
_OTLP_TRACES_PATH = "/v1/traces"


@dataclass
class SpanRecord:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0
    status: str = "OK"
    events: list[dict[str, Any]] = field(default_factory=list)
    #: True when the OpenTelemetry span backing this record was sampled and recording.
    recording: bool = False


class TraceRecorder:
    """In-process span sink; always populated, whether or not an exporter is configured."""

    def __init__(self, max_spans: int = 5_000) -> None:
        self._lock = threading.Lock()
        self._spans: list[SpanRecord] = []
        self._max = max_spans

    def record(self, span: SpanRecord) -> None:
        with self._lock:
            if len(self._spans) < self._max:
                self._spans.append(span)

    def spans(self, name: str | None = None) -> list[SpanRecord]:
        with self._lock:
            if name is None:
                return list(self._spans)
            return [s for s in self._spans if s.name == name]

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()


recorder = TraceRecorder()


def _sanitize(attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not attributes:
        return {}
    return {k: v for k, v in attributes.items() if k.lower() not in _FORBIDDEN_SPAN_KEYS}


def _otel_attributes(attributes: dict[str, Any]) -> dict[str, str | bool | int | float]:
    """OTEL accepts only primitive attribute values; anything else is stringified."""
    converted: dict[str, str | bool | int | float] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        converted[key] = value if isinstance(value, str | bool | int | float) else str(value)
    return converted


class Tracer:
    """Thin span factory used across the application."""

    def __init__(self, otel_tracer: otel_trace.Tracer | None = None) -> None:
        self._otel = otel_tracer

    @property
    def exporting(self) -> bool:
        return self._otel is not None

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[SpanRecord]:
        record = SpanRecord(name=name, attributes=_sanitize(attributes))
        ctx = get_current_context()
        if ctx is not None:
            record.attributes.setdefault("request.id", ctx.request_id)
            record.attributes.setdefault("correlation.id", ctx.correlation_id)
            record.attributes.setdefault("channel", ctx.channel.value)

        start = time.perf_counter()
        if self._otel is None:
            try:
                yield record
            except Exception as exc:
                record.status = "ERROR"
                record.attributes["error.type"] = type(exc).__name__
                raise
            finally:
                record.duration_ms = (time.perf_counter() - start) * 1000
                record.attributes = _sanitize(record.attributes)
                recorder.record(record)
            return

        # Exceptions are handled here, not by the SDK, so the message never reaches a span.
        with self._otel.start_as_current_span(
            name,
            attributes=_otel_attributes(record.attributes),
            record_exception=False,
            set_status_on_exception=False,
        ) as otel_span:
            record.recording = otel_span.is_recording()
            try:
                yield record
            except Exception as exc:
                record.status = "ERROR"
                record.attributes["error.type"] = type(exc).__name__
                otel_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                record.duration_ms = (time.perf_counter() - start) * 1000
                record.attributes = _sanitize(record.attributes)
                otel_span.set_attributes(_otel_attributes(record.attributes))
                recorder.record(record)


tracer = Tracer()

_state_lock = threading.Lock()
_provider: TracerProvider | None = None
_exporter_endpoints: set[str] = set()


def _load_otlp_exporter() -> Any:
    """The OTLP exporter is an optional extra (``pip install .[otel]``); fail loudly if absent."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise ConfigurationError("otel_exporter_not_installed") from exc
    return OTLPSpanExporter


def _traces_url(endpoint: str) -> str:
    stripped = endpoint.rstrip("/")
    return stripped if stripped.endswith(_OTLP_TRACES_PATH) else stripped + _OTLP_TRACES_PATH


def configure_tracing(
    *,
    enabled: bool,
    service_name: str,
    exporter_endpoint: str | None,
    sample_rate: float,
    environment: str,
    app_version: str,
) -> None:
    """Install the application tracer.

    The ``TracerProvider`` is created once per process and registered as the global
    provider; later calls reuse it (adding an exporter if a new endpoint is given) rather
    than re-setting it, which the OTEL SDK forbids. Consequently the sampler and resource
    are fixed by the first enabled call.
    """
    global tracer, _provider
    if not enabled:
        tracer = Tracer()
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    with _state_lock:
        if _provider is None:
            resource = Resource.create(
                {
                    "service.name": service_name,
                    "service.version": app_version,
                    "deployment.environment": environment,
                }
            )
            _provider = TracerProvider(
                resource=resource, sampler=ParentBased(root=TraceIdRatioBased(sample_rate))
            )
            otel_trace.set_tracer_provider(_provider)
        if exporter_endpoint and exporter_endpoint not in _exporter_endpoints:
            exporter_cls = _load_otlp_exporter()
            _provider.add_span_processor(
                BatchSpanProcessor(exporter_cls(endpoint=_traces_url(exporter_endpoint)))
            )
            _exporter_endpoints.add(exporter_endpoint)
        provider = _provider

    tracer = Tracer(otel_tracer=provider.get_tracer(service_name, app_version))


def add_span_processor(processor: SpanProcessor) -> None:
    """Attach an extra processor (tests use an in-memory exporter) to the live provider."""
    with _state_lock:
        if _provider is None:
            raise ConfigurationError("tracing_not_configured")
        _provider.add_span_processor(processor)
