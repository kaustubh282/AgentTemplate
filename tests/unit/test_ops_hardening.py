"""Operational hardening from the master-template acceptance review.

M-2  real OpenTelemetry provider, sampler and exporter wiring
M-8  bounded histogram memory
M-15 body-size limit enforced on the byte stream, not on Content-Length alone
M-16 metric labels carry the route template, never the raw path
L-2  pinned lock file, container health check, real supply-chain CI job
L-3/L-4  no empty scaffold packages; one markup regex; one token estimator
L-8  client-supplied correlation identifiers are bounded
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.core.context.request_context import set_current_context
from app.core.errors.taxonomy import ConfigurationError
from app.core.observability import tracing
from app.core.observability.metrics import REQUEST_LATENCY_MS, REQUESTS_TOTAL, Histogram, metrics
from tests.conftest import customer_context

ROOT = Path(".")


# ------------------------------------------------------------------ tracing ---
@pytest.fixture
def otel_tracing() -> Iterator[InMemorySpanExporter]:
    tracing.configure_tracing(
        enabled=True,
        service_name="protec-test",
        exporter_endpoint=None,
        sample_rate=1.0,
        environment="test",
        app_version="9.9.9-test",
    )
    exporter = InMemorySpanExporter()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter
    finally:
        set_current_context(None)
        tracing.configure_tracing(
            enabled=False,
            service_name="protec-test",
            exporter_endpoint=None,
            sample_rate=1.0,
            environment="test",
            app_version="9.9.9-test",
        )


def test_configured_tracer_records_real_spans_with_request_context(otel_tracing):
    ctx = customer_context()
    set_current_context(ctx)
    assert tracing.tracer.exporting

    with tracing.tracer.span("x", capability="faq.answer", prompt="must never appear") as record:
        assert record.recording, "the span must be sampled and recording, not a NonRecordingSpan"

    spans = [s for s in otel_tracing.get_finished_spans() if s.name == "x"]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["request.id"] == ctx.request_id
    assert span.attributes["correlation.id"] == ctx.correlation_id
    assert span.attributes["channel"] == "WEB_CUSTOMER"
    assert span.attributes["capability"] == "faq.answer"
    assert "prompt" not in span.attributes, "forbidden keys are dropped before export"
    assert span.status.status_code is StatusCode.UNSET
    # The provider carries a real resource: the identity a backend groups spans by.
    assert span.resource.attributes["service.name"]
    assert "deployment.environment" in span.resource.attributes
    assert "service.version" in span.resource.attributes
    # And the in-process recorder still sees the same span, so existing tests keep working.
    assert tracing.recorder.spans("x")


def test_exceptions_mark_the_span_with_error_type_but_not_the_message(otel_tracing):
    with pytest.raises(ValueError), tracing.tracer.span("boom"):
        raise ValueError("customer 9876543210 did something")

    span = next(s for s in otel_tracing.get_finished_spans() if s.name == "boom")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"
    assert not span.events, "exception messages are never recorded as span events"
    assert "9876543210" not in json.dumps(dict(span.attributes))


def test_exporter_endpoint_without_the_otel_extra_fails_configuration(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)
    with pytest.raises(ConfigurationError) as excinfo:
        tracing.configure_tracing(
            enabled=True,
            service_name="protec-test",
            exporter_endpoint="http://collector.invalid:4318/missing-extra",
            sample_rate=1.0,
            environment="test",
            app_version="9.9.9-test",
        )
    assert excinfo.value.reason == "otel_exporter_not_installed"
    tracing.configure_tracing(
        enabled=False,
        service_name="protec-test",
        exporter_endpoint=None,
        sample_rate=1.0,
        environment="test",
        app_version="9.9.9-test",
    )


def test_add_span_processor_requires_a_configured_provider_or_is_explicit():
    # Either a provider exists (an earlier test configured it) or the call refuses clearly.
    exporter = InMemorySpanExporter()
    if tracing._provider is None:
        with pytest.raises(ConfigurationError):
            tracing.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        tracing.add_span_processor(SimpleSpanProcessor(exporter))


def test_disabled_tracing_still_records_in_process():
    tracing.recorder.reset()
    plain = tracing.Tracer()
    with plain.span("local") as record:
        pass
    assert not record.recording and not plain.exporting
    assert tracing.recorder.spans("local")


# ------------------------------------------------------------------ metrics ---
def test_histogram_memory_is_bounded_while_totals_stay_exact():
    hist = Histogram(max_samples=1_000)
    n = 50_000
    for value in range(n):
        hist.observe(float(value))
    summary = hist.summary()
    assert len(hist.values) == 1_000
    assert summary["count"] == n
    assert summary["sum"] == float(sum(range(n)))
    assert summary["max"] == float(n - 1)
    assert summary["avg"] == pytest.approx((n - 1) / 2)
    # A uniform reservoir keeps quantiles representative (generous tolerance: it is random).
    assert abs(summary["p50"] - n / 2) < n * 0.08
    assert summary["p99"] > n * 0.9


def test_histogram_below_the_reservoir_size_is_exact():
    hist = Histogram()
    for value in (1.0, 2.0, 10.0):
        hist.observe(value)
    assert hist.summary() == {
        "count": 3.0,
        "sum": 13.0,
        "avg": pytest.approx(13 / 3),
        "p50": 2.0,
        "p95": pytest.approx(9.2),
        "p99": pytest.approx(9.84),
        "max": 10.0,
    }


def test_request_metrics_are_labelled_by_route_template_not_raw_path(client):
    metrics.reset()
    for i in range(50):
        client.get(f"/api/v1/policies/POL-{i}")
    client.get("/no/such/route")

    snapshot = metrics.snapshot()
    routes = {c["labels"]["route"] for c in snapshot["counters"] if c["name"] == REQUESTS_TOTAL}
    assert routes == {"/api/v1/policies/{policy_id}", "unmatched"}, routes
    latency_routes = {h["labels"]["route"] for h in snapshot["histograms"] if h["name"] == REQUEST_LATENCY_MS}
    assert latency_routes == {"/api/v1/policies/{policy_id}", "unmatched"}
    total = sum(c["value"] for c in snapshot["counters"] if c["name"] == REQUESTS_TOTAL)
    assert total == 51
    assert not any("POL-" in c["labels"].get("route", "") for c in snapshot["counters"])


# ------------------------------------------------------------- body limit ---
def _chunks(total: int, size: int = 8 * 1024) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        chunk = min(size, total - sent)
        yield b"x" * chunk
        sent += chunk


def test_chunked_body_over_the_limit_is_rejected_with_413(client):
    response = client.post(
        "/api/v1/chat",
        content=_chunks(70 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["retryable"] is False
    assert set(body) == {"code", "message", "retryable", "requestId", "correlationId"}
    assert response.headers["X-Request-Id"] == body["requestId"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_declared_content_length_over_the_limit_is_rejected_before_reading(client):
    response = client.post(
        "/api/v1/chat", content=b"x" * (70 * 1024), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_small_chunked_body_passes_through_the_limit(client):
    response = client.post(
        "/api/v1/chat",
        content=_chunks(2 * 1024),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code != 413


# ------------------------------------------------------------------- L-8 ---
def test_oversized_or_malformed_client_correlation_ids_are_replaced(client):
    huge = "a" * 5000
    response = client.get(
        "/api/v1/health/live", headers={"X-Correlation-Id": huge, "X-Request-Id": "bad id\n"}
    )
    assert response.status_code == 200
    assert response.headers["X-Correlation-Id"] != huge
    assert len(response.headers["X-Correlation-Id"]) <= 128
    assert response.headers["X-Request-Id"].startswith("req_")


def test_well_formed_client_correlation_ids_are_honoured(client):
    response = client.get("/api/v1/health/live", headers={"X-Correlation-Id": "corr_upstream-123"})
    assert response.headers["X-Correlation-Id"] == "corr_upstream-123"


# ---------------------------------------------------------------- dedupe ---
def test_one_markup_regex_is_shared_by_guardrails_and_directive_registry():
    from app.core.security import guardrails
    from app.ui_directives.registry import registry

    assert registry.UNSAFE_MARKUP_PATTERN is guardrails.UNSAFE_MARKUP_PATTERN
    pattern = guardrails.UNSAFE_MARKUP_PATTERN
    # Union of the two former definitions.
    for sample in (
        "<script>",
        "<IFRAME src=x>",
        "javascript:alert(1)",
        "onerror=",
        "onfocus=",
        "data:text/html,x",
        "<object",
        "<embed",
    ):
        assert pattern.search(sample), sample
    assert not pattern.search("Your premium is Rs 4,200 per year.")


def test_prompt_registry_uses_the_shared_token_estimator():
    from app.ai.models.provider import approx_tokens
    from app.ai.prompts.registry import PromptRegistry

    template = next(iter(PromptRegistry().load().all()))
    assert template.approx_tokens == max(1, approx_tokens(template.text))


def test_no_package_under_app_is_an_empty_scaffold():
    """L-3: a package containing only ``__init__.py`` files is architecture theatre."""
    empty = []
    for init in Path("app").rglob("__init__.py"):
        package = init.parent
        if not any(p.name != "__init__.py" for p in package.rglob("*.py")):
            empty.append(str(package).replace("\\", "/"))
    assert not empty, f"empty scaffold packages: {empty}"


def test_otel_available_stub_is_gone():
    assert not hasattr(tracing, "otel_available")


# ---------------------------------------------------------- supply chain ---
def test_lock_file_pins_every_core_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock_lines = [
        ln for ln in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines() if "==" in ln
    ]
    pins = {canonicalize_name(ln.split("==")[0]) for ln in lock_lines}
    for dep in project["dependencies"]:
        assert canonicalize_name(Requirement(dep).name) in pins, f"{dep} is not pinned in requirements.lock"
    assert all("==" in ln and ">=" not in ln for ln in lock_lines), "the lock must be exact pins"
    assert "opentelemetry-sdk" in {canonicalize_name(Requirement(d).name) for d in project["dependencies"]}
    assert "otel" in project["optional-dependencies"]


def test_dockerfile_installs_from_the_lock_and_declares_a_healthcheck():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.lock" in text
    assert "HEALTHCHECK" in text and "/api/v1/health/live" in text
    assert "USER protec" in text
    assert text.index("USER protec") < text.index("CMD ["), "the process must run as the non-root user"


def test_ci_supply_chain_job_is_real():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "NOT YET IMPLEMENTED" not in text
    assert "pip-audit -r requirements.lock --strict" in text
    assert "pip-licenses" in text
    assert "cyclonedx-py environment" in text
    assert "continue-on-error: false" in text
