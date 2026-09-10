"""Knowledge document lifecycle and provenance (master prompt §6.1, §34).

Every indexed unit carries provenance so an answer can be traced to an approved,
active source. Draft, superseded, revoked and quarantined material is excluded from
active retrieval unless explicitly configured, and the newest file name is never
assumed to be authoritative - only the recorded lifecycle status, the document
version within its lineage and the effective date are.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    #: Set automatically at ingestion when the content scores as a prompt-injection
    #: attempt. Never retrievable and never auto-activated by persisted state (§12).
    QUARANTINED = "QUARANTINED"


class DocumentClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"


class DocumentType(StrEnum):
    POLICY_WORDING = "POLICY_WORDING"
    BROCHURE = "BROCHURE"
    FAQ = "FAQ"
    PRODUCT_DOCUMENT = "PRODUCT_DOCUMENT"
    OPERATIONAL = "OPERATIONAL"
    REGULATORY_REFERENCE = "REGULATORY_REFERENCE"


class Audience(StrEnum):
    PUBLIC = "PUBLIC"
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    INTERNAL = "INTERNAL"


#: A lineage is "the same document over time": every version of one document shares it.
LineageKey = tuple[str, str, str | None, DocumentType]

_VERSION_PART = re.compile(r"\d+|[A-Za-z]+")


def version_sort_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Order versions numerically where possible ("1.10" > "1.9"), else lexically.

    Each dotted component becomes a comparable tuple: numeric parts sort before and
    among themselves by value, alphabetic parts (``"1.0b"``) sort after by string.
    """
    key: list[tuple[int, int, str]] = []
    for component in version.strip().split("."):
        for part in _VERSION_PART.findall(component) or [component]:
            if part.isdigit():
                key.append((0, int(part), ""))
            else:
                key.append((1, 0, part.lower()))
    return tuple(key)


def today_utc() -> date:
    return datetime.now(UTC).date()


class DocumentMetadata(BaseModel):
    """Provenance recorded for every ingested document."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    version: str
    effective_date: date
    source_system: str
    classification: DocumentClassification
    document_type: DocumentType
    product: str | None = None
    domain: str
    audience: Audience = Audience.PUBLIC
    language: str = "en"
    #: Required. A document with no declared status is refused at ingestion rather
    #: than being assumed ACTIVE (fail closed, §34).
    status: DocumentStatus
    supersedes: str | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checksum: str = ""
    approved_by: str | None = None

    def with_checksum(self, content: str) -> DocumentMetadata:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return self.model_copy(update={"checksum": digest})

    @property
    def lineage_key(self) -> LineageKey:
        return (self.document_name, self.domain, self.product, self.document_type)

    @property
    def is_effective(self) -> bool:
        """False for a document whose effective date has not yet arrived (UTC)."""
        return self.effective_date <= today_utc()

    @property
    def is_retrievable(self) -> bool:
        return self.status is DocumentStatus.ACTIVE and self.is_effective


class Chunk(BaseModel):
    """One indexed unit, carrying its document's provenance."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    section: str | None
    text: str
    token_estimate: int
    metadata: DocumentMetadata

    @property
    def citation_label(self) -> str:
        section = f"#{self.section}" if self.section else ""
        return f"{self.metadata.document_name} v{self.metadata.version}{section}"


class KnowledgeCorpus:
    """The active corpus, with a version that changes whenever content changes.

    The corpus version is embedded in FAQ cache keys and audit records so a knowledge
    change can never serve a stale cached answer (§18.1, §24).

    Two governance rules are enforced here rather than left to file order:

    * the same ``document_id`` arriving twice resolves to the highest version;
    * among ACTIVE documents of one lineage only the highest version stays ACTIVE,
      the rest are marked SUPERSEDED and the winner records what it supersedes.
    """

    def __init__(self) -> None:
        self._documents: dict[str, DocumentMetadata] = {}
        self._chunks: dict[str, list[Chunk]] = {}
        #: Called after every explicit lifecycle change so it can be persisted (§34).
        self.on_status_change: Callable[[str, DocumentStatus], None] | None = None

    # ------------------------------------------------------------ mutation ---
    def upsert(self, metadata: DocumentMetadata, chunks: list[Chunk]) -> DocumentMetadata:
        """Add or replace a document, then resolve its lineage.

        Returns the metadata as stored, which may differ from the input when the
        document lost a version comparison (the stored, higher version is returned)
        or was superseded by an already-ingested higher version of its lineage.
        """
        existing = self._documents.get(metadata.document_id)
        if existing is not None and self._outranks(existing, metadata):
            # Same id ingested twice: the highest version wins, not the later file.
            return existing
        self._documents[metadata.document_id] = metadata
        self._chunks[metadata.document_id] = chunks
        self._resolve_lineage(metadata.lineage_key)
        return self._documents[metadata.document_id]

    @staticmethod
    def _outranks(existing: DocumentMetadata, candidate: DocumentMetadata) -> bool:
        """Deterministic tie-break for two documents sharing one ``document_id``."""
        left = (version_sort_key(existing.version), existing.effective_date, existing.checksum)
        right = (version_sort_key(candidate.version), candidate.effective_date, candidate.checksum)
        return left > right

    def _resolve_lineage(self, lineage: LineageKey) -> None:
        active = [
            m
            for m in self._documents.values()
            if m.lineage_key == lineage and m.status is DocumentStatus.ACTIVE
        ]
        if len(active) < 2:
            return
        active.sort(key=lambda m: (version_sort_key(m.version), m.effective_date, m.document_id))
        winner, losers = active[-1], active[:-1]
        for loser in losers:
            self._store(loser.model_copy(update={"status": DocumentStatus.SUPERSEDED}))
        if winner.supersedes is None:
            self._store(winner.model_copy(update={"supersedes": losers[-1].document_id}))

    def _store(self, metadata: DocumentMetadata) -> None:
        """Replace a document's metadata and keep every chunk's provenance in step."""
        self._documents[metadata.document_id] = metadata
        self._chunks[metadata.document_id] = [
            chunk.model_copy(update={"metadata": metadata})
            for chunk in self._chunks.get(metadata.document_id, [])
        ]

    def set_status(
        self, document_id: str, status: DocumentStatus, *, notify: bool = True
    ) -> DocumentMetadata:
        metadata = self._documents[document_id]
        updated = metadata.model_copy(update={"status": status})
        self._store(updated)
        if notify and self.on_status_change is not None:
            self.on_status_change(document_id, status)
        return updated

    def supersede(self, old_document_id: str, new_document_id: str) -> None:
        self.set_status(old_document_id, DocumentStatus.SUPERSEDED)
        new_meta = self._documents[new_document_id]
        self._store(
            new_meta.model_copy(update={"supersedes": old_document_id, "status": DocumentStatus.ACTIVE})
        )
        if self.on_status_change is not None:
            self.on_status_change(new_document_id, DocumentStatus.ACTIVE)

    # ------------------------------------------------------------- queries ---
    def get(self, document_id: str) -> DocumentMetadata | None:
        return self._documents.get(document_id)

    def documents(self) -> tuple[DocumentMetadata, ...]:
        return tuple(self._documents.values())

    def quarantined(self) -> tuple[DocumentMetadata, ...]:
        return tuple(m for m in self._documents.values() if m.status is DocumentStatus.QUARANTINED)

    def active_chunks(self, *, allow_draft: bool = False) -> list[Chunk]:
        """Chunks eligible for retrieval: ACTIVE (or DRAFT if allowed) and in effect.

        A future-dated document is not served before its effective date even when it
        is ACTIVE; quarantined, superseded and revoked material is never served.
        """
        allowed = {DocumentStatus.ACTIVE} | ({DocumentStatus.DRAFT} if allow_draft else set())
        today = today_utc()
        return [
            chunk
            for doc_id, chunks in self._chunks.items()
            if self._documents[doc_id].status in allowed and self._documents[doc_id].effective_date <= today
            for chunk in chunks
        ]

    def all_chunks(self) -> list[Chunk]:
        return [c for chunks in self._chunks.values() for c in chunks]

    @property
    def version(self) -> str:
        """Deterministic corpus fingerprint over active document identity + checksum."""
        parts = sorted(
            f"{m.document_id}:{m.version}:{m.status.value}:{m.checksum}" for m in self._documents.values()
        )
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16] if parts else "empty"

    def __len__(self) -> int:
        return len(self._documents)
