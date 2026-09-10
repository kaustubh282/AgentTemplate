"""Prometheus text exposition for the in-process metrics registry (master prompt §21.2).

The registry is dependency-free and OTEL-shaped; this renders it in the Prometheus
0.0.4 text format so the shipped dashboards and alert rules (``ops/``) work against a
standard scrape with no extra library. Histograms are exposed as ``_count``, ``_sum``
and pre-computed quantile samples, which is what the SLO panels and alerts consume.
"""

from __future__ import annotations

from typing import Any

from app.core.observability.metrics import MetricsRegistry


def _sanitize(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)


def _labels(labels: dict[str, str], extra: dict[str, str] | None = None) -> str:
    merged = {**labels, **(extra or {})}
    if not merged:
        return ""
    inner = ",".join(
        f'{_sanitize(k)}="{str(v).replace(chr(34), chr(39))}"' for k, v in sorted(merged.items())
    )
    return "{" + inner + "}"


def render_prometheus(registry: MetricsRegistry) -> str:
    snapshot: dict[str, Any] = registry.snapshot()
    lines: list[str] = []
    seen: set[str] = set()

    def header(name: str, kind: str) -> None:
        if name not in seen:
            lines.append(f"# TYPE {name} {kind}")
            seen.add(name)

    for item in snapshot["counters"]:
        name = _sanitize(item["name"])
        header(name, "counter")
        lines.append(f"{name}{_labels(item['labels'])} {item['value']:g}")

    for item in snapshot["gauges"]:
        name = _sanitize(item["name"])
        header(name, "gauge")
        lines.append(f"{name}{_labels(item['labels'])} {item['value']:g}")

    for item in snapshot["histograms"]:
        name = _sanitize(item["name"])
        header(name, "summary")
        labels = item["labels"]
        for quantile, key in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99")):
            lines.append(f"{name}{_labels(labels, {'quantile': quantile})} {item[key]:g}")
        lines.append(f"{name}_sum{_labels(labels)} {item['sum']:g}")
        lines.append(f"{name}_count{_labels(labels)} {item['count']:g}")

    return "\n".join(lines) + "\n"
