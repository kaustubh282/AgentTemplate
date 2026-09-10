"""Knowledge lifecycle administration and FinOps reporting (§34, §35).

Both are permission-gated. Knowledge lifecycle changes are audited so a stale-answer
investigation can reconstruct which corpus version was active when.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_container, require_permissions
from app.bootstrap import Container
from app.core.audit.events import AuditAction, AuditResult
from app.core.auth.auth_context import Permission
from app.core.context.request_context import RequestContext, set_current_context
from app.core.errors.taxonomy import ResourceNotFoundError
from app.finops.reporting import build_cost_report
from app.rag.governance.documents import DocumentStatus

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class DocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    version: str
    status: str
    domain: str
    document_type: str
    effective_date: str
    checksum: str


class CorpusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_version: str
    documents: list[DocumentSummary]


class LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    status: DocumentStatus


@router.get("/knowledge", response_model=CorpusResponse)
async def list_knowledge(
    ctx: Annotated[RequestContext, Depends(require_permissions(Permission.KNOWLEDGE_ADMIN))],
    container: Annotated[Container, Depends(get_container)],
) -> CorpusResponse:
    set_current_context(ctx)
    return CorpusResponse(
        corpus_version=container.corpus.version,
        documents=[
            DocumentSummary(
                document_id=d.document_id,
                document_name=d.document_name,
                version=d.version,
                status=d.status.value,
                domain=d.domain,
                document_type=d.document_type.value,
                effective_date=d.effective_date.isoformat(),
                checksum=d.checksum[:16],
            )
            for d in container.corpus.documents()
        ],
    )


@router.post("/knowledge/lifecycle", response_model=CorpusResponse)
async def change_lifecycle(
    body: LifecycleRequest,
    ctx: Annotated[RequestContext, Depends(require_permissions(Permission.KNOWLEDGE_ADMIN))],
    container: Annotated[Container, Depends(get_container)],
) -> CorpusResponse:
    """Activate / supersede / revoke a document, then let retrieval re-index (§34)."""
    set_current_context(ctx)
    known = {d.document_id for d in container.corpus.documents()}
    if body.document_id not in known:
        raise ResourceNotFoundError("unknown_document")

    # Through the ingestion service so the change is persisted and survives re-ingestion (§34).
    container.ingestion.set_status(body.document_id, body.status)
    await container.audit.record(
        ctx,
        AuditAction.KNOWLEDGE_LIFECYCLE_CHANGED,
        AuditResult.SUCCESS,
        resource_type="KNOWLEDGE_DOCUMENT",
        reason_code=body.status.value,
        attributes={"documentId": body.document_id, "corpusVersion": container.corpus.version},
    )
    return await list_knowledge(ctx, container)


@router.get("/finops/report")
async def finops_report(
    ctx: Annotated[RequestContext, Depends(require_permissions(Permission.FINOPS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """Cost and token report built from Harness execution records (§35.1)."""
    set_current_context(ctx)
    return build_cost_report(
        container.harness.execution_records,
        environment=container.settings.app_env.value,
        currency=container.settings.model_currency,
        ceilings={
            "maxCostPer1000Faq": container.settings.max_cost_per_1000_faq,
            "maxCostPer1000Mixed": container.settings.max_cost_per_1000_mixed_requests,
            "maxCostPerCompletedTransaction": container.settings.max_cost_per_completed_transaction,
        },
    )
