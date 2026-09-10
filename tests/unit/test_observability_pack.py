"""The shipped dashboards and alert rules must reference only metrics the application
emits, and the Prometheus exposition must be parseable (master prompt §21, §37)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from app.core.observability import metrics as metrics_module
from app.core.observability.exposition import render_prometheus
from app.core.observability.metrics import MetricsRegistry
from tests.conftest import auth_headers, make_token

OPS = Path("ops")
EMITTED = {
    value
    for name, value in vars(metrics_module).items()
    if name.isupper() and isinstance(value, str) and value.startswith("protec_")
}
#: Suffixes the exposition adds for histograms.
SUFFIXES = ("_sum", "_count")
METRIC_TOKEN = re.compile(r"\bprotec_[a-z_]+")


def _base_name(token: str) -> str:
    for suffix in SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)] in EMITTED:
            return token[: -len(suffix)]
    return token


def _alert_rules() -> list[dict]:
    doc = yaml.safe_load((OPS / "prometheus" / "alerts.yml").read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _dashboards() -> list[Path]:
    return sorted((OPS / "grafana").glob("*.json"))


def test_alert_rules_parse_and_cover_the_documented_alerts():
    rules = _alert_rules()
    assert len(rules) >= 15
    for rule in rules:
        assert rule["alert"] and rule["expr"] and rule["labels"]["severity"] in {"critical", "high", "medium"}
        assert rule["annotations"]["summary"]


@pytest.mark.parametrize("rule", _alert_rules(), ids=lambda r: r["alert"])
def test_every_alert_references_only_emitted_metrics(rule):
    unknown = {_base_name(t) for t in METRIC_TOKEN.findall(rule["expr"])} - EMITTED
    assert not unknown, f"{rule['alert']} references metrics the app does not emit: {unknown}"


@pytest.mark.parametrize("path", _dashboards(), ids=lambda p: p.stem)
def test_every_dashboard_panel_references_only_emitted_metrics(path):
    model = json.loads(path.read_text(encoding="utf-8"))
    assert model["uid"] and model["title"] and model["panels"]
    for panel in model["panels"]:
        assert panel["targets"], f"{path.stem}/{panel['title']} has no query"
        for target in panel["targets"]:
            unknown = {_base_name(t) for t in METRIC_TOKEN.findall(target["expr"])} - EMITTED
            assert not unknown, f"{path.stem}/{panel['title']} references unknown metrics: {unknown}"


def test_dashboards_cover_the_five_documented_views():
    assert {p.stem for p in _dashboards()} == {
        "service-health",
        "ai-efficiency",
        "answer-quality",
        "business-flow",
        "security",
    }


def test_prometheus_exposition_renders_counters_gauges_and_summaries():
    registry = MetricsRegistry()
    registry.increment("protec_requests_total", labels={"route": "/api/v1/chat"})
    registry.set_gauge("protec_inflight_requests", 3)
    for v in (1.0, 2.0, 10.0):
        registry.observe("protec_request_latency_ms", v, labels={"route": "/api/v1/chat"})
    text = render_prometheus(registry)
    assert "# TYPE protec_requests_total counter" in text
    assert 'protec_requests_total{route="/api/v1/chat"} 1' in text
    assert "protec_inflight_requests 3" in text
    assert 'protec_request_latency_ms{quantile="0.95",route="/api/v1/chat"}' in text
    assert 'protec_request_latency_ms_count{route="/api/v1/chat"} 3' in text
    # Every non-comment line is `name{labels} value`, which is what a scraper needs.
    for line in text.strip().splitlines():
        if line.startswith("#"):
            continue
        assert re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9.e+-]+(inf|nan)?", line), line


def test_prometheus_endpoint_requires_the_finops_permission(client):
    anonymous = client.get("/api/v1/internal/metrics/prometheus")
    assert anonymous.status_code == 401
    customer = client.get("/api/v1/internal/metrics/prometheus", headers=auth_headers(make_token()))
    assert customer.status_code == 403
    service = make_token(
        subject="svc-scraper", actor_type="SERVICE", roles=["SERVICE"], permissions=["finops:read"]
    )
    ok = client.get("/api/v1/internal/metrics/prometheus", headers=auth_headers(service))
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")
    assert "protec_requests_total" in ok.text
