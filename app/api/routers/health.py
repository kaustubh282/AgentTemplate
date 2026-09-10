"""Health, readiness and internal metrics endpoints (§30, §21.2).

Public health endpoints expose no credentials, no hostnames and no dependency detail
beyond a coarse status.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import PlainTextResponse

from app.api.deps import get_container, require_permissions
from app.api.schemas.api_models import DependencyHealth, HealthResponse, ReadinessResponse
from app.bootstrap import Container
from app.core.auth.auth_context import Permission
from app.core.context.request_context import RequestContext
from app.core.observability.exposition import render_prometheus
from app.core.observability.metrics import metrics

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health/live", response_model=HealthResponse)
async def liveness(container: Annotated[Container, Depends(get_container)]) -> HealthResponse:
    return HealthResponse(status="alive", version=container.settings.app_version)


READY_STATUSES = frozenset({"up", "configured"})


def _model_status(container: Container) -> str:
    """Configuration *and* recent behaviour, not a constant (H-4)."""
    settings = container.settings
    if settings.is_production and settings.model_provider == "deterministic":
        return "down"
    if settings.model_provider in ("openai", "bedrock") and not (
        settings.model_api_key or settings.model_provider == "bedrock"
    ):
        return "down"
    return "degraded" if container.invoker.is_degraded else "configured"


def _knowledge_status(container: Container) -> str:
    """Counts only *retrievable* chunks: a corpus whose documents are all revoked or
    superseded can answer nothing and must not report ready (H-4)."""
    return "up" if container.corpus.active_chunks() else "degraded"


async def _provider_status(container: Container) -> str:
    if container.provider_policy.breaker.state.value == "OPEN":
        return "down"
    return await container.providers.health()


async def _state_store_status(container: Container) -> str:
    """The shared store is a hard dependency when configured: probe it, do not assume it."""
    if container.redis is None:
        return "up"  # process-local stores cannot be unreachable
    store = container.workflow_store
    ping = getattr(store, "ping", None)
    if ping is None:
        return "up"
    return "up" if await ping() else "down"


@router.get("/health/ready", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}})
async def readiness(
    response: Response, container: Annotated[Container, Depends(get_container)]
) -> ReadinessResponse:
    """Reports measured dependency health. Returns 503 when the instance cannot serve,
    so an orchestrator stops routing traffic to it (§30)."""
    dependencies: list[DependencyHealth] = [
        DependencyHealth(name="knowledge", status=_knowledge_status(container)),
        DependencyHealth(
            name="workflow_store",
            status="up" if container.workflow_registry.all() else "degraded",
        ),
        DependencyHealth(name="model", status=_model_status(container)),
        DependencyHealth(name="providers", status=await _provider_status(container)),
        DependencyHealth(name="state_store", status=await _state_store_status(container)),
    ]
    ready = all(d.status in READY_STATUSES for d in dependencies)
    if not ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if ready else "degraded",
        version=container.settings.app_version,
        dependencies=dependencies,
    )


@router.get("/internal/metrics")
async def internal_metrics(
    _ctx: Annotated[RequestContext, Depends(require_permissions(Permission.FINOPS_READ))],
) -> dict[str, Any]:
    """Internal metrics snapshot. Requires an explicit permission (§30)."""
    return metrics.snapshot()


@router.get("/internal/metrics/prometheus", response_class=PlainTextResponse)
async def internal_metrics_prometheus(
    _ctx: Annotated[RequestContext, Depends(require_permissions(Permission.FINOPS_READ))],
) -> str:
    """Prometheus text exposition of the same registry, for the shipped dashboards and
    alert rules in ``ops/`` (§21.2)."""
    return render_prometheus(metrics)
