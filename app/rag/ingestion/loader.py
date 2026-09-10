"""Knowledge ingestion: parse, validate, chunk, checksum, scan, persist (§6.1, §12, §34).

Documents are Markdown files with a YAML front-matter block carrying the governance
metadata. Ingestion refuses a document whose metadata is incomplete rather than
guessing a version, effective date or status. It also:

* demotes an ACTIVE document with no ``approved_by`` to DRAFT (an unapproved document
  is never served);
* quarantines a document whose content scores as a prompt-injection attempt, so a
  poisoned file never reaches the model at all (M-11);
* re-applies persisted lifecycle overrides after every load, so a revoked document
  stays revoked across restarts and re-ingestion (§34).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from app.core.errors.taxonomy import ValidationError
from app.core.logging.structured import get_logger
from app.core.security.injection import injection_detector
from app.rag.governance.documents import (
    Chunk,
    DocumentMetadata,
    DocumentStatus,
    KnowledgeCorpus,
)

logger = get_logger(__name__)

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_SECTION = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

#: Injection score at or above which a document is quarantined at ingestion. Matches
#: the request-time block threshold so the two layers agree on what an attack is.
QUARANTINE_SCORE = 0.6

REQUIRED_METADATA_KEYS = (
    "document_id",
    "document_name",
    "version",
    "effective_date",
    "source_system",
    "classification",
    "document_type",
    "domain",
    "status",
)


def estimate_tokens(text: str) -> int:
    """Provider-agnostic token estimate.

    Deliberately approximate and *conservative* (over-counts slightly) so budget
    checks fail safe. The model adapter reports exact usage when the provider does.
    """
    if not text:
        return 0
    words = len(text.split())
    return max(1, int(words * 1.35) + text.count("\n"))


def parse_document(raw: str, *, source_path: str) -> tuple[DocumentMetadata, str]:
    match = _FRONT_MATTER.match(raw)
    if match is None:
        raise ValidationError("knowledge_document_missing_front_matter", details={"path": source_path})
    header: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
    missing = [k for k in REQUIRED_METADATA_KEYS if k not in header or header[k] is None]
    if missing:
        raise ValidationError(
            "knowledge_document_missing_metadata", details={"path": source_path, "fields": missing}
        )
    body = match.group(2).strip()
    metadata = DocumentMetadata.model_validate(header).with_checksum(body)
    if metadata.status is DocumentStatus.ACTIVE and not (metadata.approved_by or "").strip():
        logger.warning(
            "knowledge_document_demoted_to_draft",
            extra={"document_id": metadata.document_id, "path": source_path, "reason": "approved_by_missing"},
        )
        metadata = metadata.model_copy(update={"status": DocumentStatus.DRAFT})
    return metadata, body


def chunk_document(metadata: DocumentMetadata, body: str, *, max_tokens: int = 320) -> list[Chunk]:
    """Split on Markdown sections, then on paragraph boundaries within a section.

    Section-aware chunking keeps a citation meaningful and avoids dumping whole
    documents into context (§6.2). No chunk ever exceeds ``max_tokens``: an oversized
    paragraph is split at sentence boundaries, and an oversized sentence by words.
    """
    sections: list[tuple[str | None, str]] = []
    matches = list(_SECTION.finditer(body))
    if not matches:
        sections.append((None, body))
    else:
        if matches[0].start() > 0:
            preamble = body[: matches[0].start()].strip()
            if preamble:
                sections.append((None, preamble))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            sections.append((match.group(1).strip(), body[start:end].strip()))

    chunks: list[Chunk] = []
    for section_name, section_text in sections:
        for part in _split_to_budget(section_text, max_tokens):
            text = part.strip()
            if not text:
                continue
            chunk_id = f"{metadata.document_id}::{len(chunks):03d}"
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    document_id=metadata.document_id,
                    section=section_name,
                    text=text,
                    token_estimate=estimate_tokens(text),
                    metadata=metadata,
                )
            )
    return chunks


def _split_to_budget(text: str, max_tokens: int) -> list[str]:
    """Pack paragraphs into parts of at most ``max_tokens`` (by the conservative estimate)."""
    units: list[str] = []
    for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        if estimate_tokens(paragraph) <= max_tokens:
            units.append(paragraph)
        else:
            units.extend(_split_oversized(paragraph, max_tokens))

    return _pack(units, max_tokens, separator="\n\n")


def _pack(units: list[str], max_tokens: int, *, separator: str) -> list[str]:
    """Greedily join units while the *joined* text stays within the budget."""
    parts: list[str] = []
    current: list[str] = []
    for unit in units:
        if current and estimate_tokens(separator.join([*current, unit])) > max_tokens:
            parts.append(separator.join(current))
            current = []
        current.append(unit)
    if current:
        parts.append(separator.join(current))
    return parts


def _split_oversized(paragraph: str, max_tokens: int) -> list[str]:
    """Split one paragraph at sentence boundaries; split a run-on sentence by words."""
    flat = " ".join(paragraph.split())
    units: list[str] = []
    for sentence in (s for s in _SENTENCE_BOUNDARY.split(flat) if s):
        if estimate_tokens(sentence) > max_tokens:
            units.extend(_split_by_words(sentence, max_tokens))
        else:
            units.append(sentence)
    return _pack(units, max_tokens, separator=" ")


def _split_by_words(sentence: str, max_tokens: int) -> list[str]:
    words = sentence.split()
    # estimate_tokens(n words, no newline) == int(n * 1.35); invert conservatively.
    per_piece = max(1, int(max_tokens / 1.35))
    pieces = [" ".join(words[i : i + per_piece]) for i in range(0, len(words), per_piece)]
    return [p for p in pieces if estimate_tokens(p) <= max_tokens] or [words[0]]


def scan_for_injection(chunks: list[Chunk]) -> tuple[float, tuple[str, ...]]:
    """Highest injection score over the chunks and the union of categories seen."""
    worst = 0.0
    categories: set[str] = set()
    for chunk in chunks:
        assessment = injection_detector.assess(chunk.text)
        if assessment.score >= QUARANTINE_SCORE:
            categories.update(assessment.categories)
        worst = max(worst, assessment.score)
    return worst, tuple(sorted(categories))


class KnowledgeIngestionService:
    """Add / validate / approve / activate / supersede / revoke / re-index (§34).

    When ``lifecycle_state_path`` is given, explicit lifecycle changes are persisted
    as ``{document_id: status}`` and re-applied after every (re-)ingestion, so a
    revoked document cannot be reinstated by restarting the service. Quarantined
    documents are exempt: persisted state never auto-activates them.
    """

    def __init__(
        self,
        corpus: KnowledgeCorpus,
        *,
        max_chunk_tokens: int = 320,
        lifecycle_state_path: str | None = None,
    ) -> None:
        self._corpus = corpus
        self._max_chunk_tokens = max_chunk_tokens
        self._state_path = Path(lifecycle_state_path) if lifecycle_state_path else None
        self._overrides: dict[str, DocumentStatus] = self._load_overrides()
        if self._state_path is not None:
            corpus.on_status_change = self._record_override

    # ------------------------------------------------------------ ingestion ---
    def ingest_text(self, raw: str, *, source_path: str = "<inline>") -> DocumentMetadata:
        metadata, body = parse_document(raw, source_path=source_path)
        chunks = chunk_document(metadata, body, max_tokens=self._max_chunk_tokens)

        score, categories = scan_for_injection(chunks)
        if score >= QUARANTINE_SCORE:
            logger.warning(
                "knowledge_document_quarantined",
                extra={
                    "document_id": metadata.document_id,
                    "path": source_path,
                    "injection_score": score,
                    "categories": list(categories),
                },
            )
            metadata = metadata.model_copy(update={"status": DocumentStatus.QUARANTINED})
            chunks = [chunk.model_copy(update={"metadata": metadata}) for chunk in chunks]

        stored = self._corpus.upsert(metadata, chunks)
        return self._apply_override(stored.document_id)

    def ingest_directory(self, directory: str | Path) -> list[DocumentMetadata]:
        root = Path(directory)
        if not root.exists():
            return []
        ingested: list[DocumentMetadata] = []
        for path in sorted(root.rglob("*.md")):
            ingested.append(self.ingest_text(path.read_text(encoding="utf-8"), source_path=str(path)))
        # Overrides apply to the resolved corpus, not to files in isolation.
        for document_id in list(self._overrides):
            if self._corpus.get(document_id) is not None:
                self._apply_override(document_id)
        return [self._corpus.get(m.document_id) or m for m in ingested]

    def _apply_override(self, document_id: str) -> DocumentMetadata:
        current = self._corpus.get(document_id)
        if current is None:
            raise KeyError(document_id)
        override = self._overrides.get(document_id)
        if override is None or current.status is DocumentStatus.QUARANTINED or override is current.status:
            return current
        return self._corpus.set_status(document_id, override, notify=False)

    # ------------------------------------------------------------ lifecycle ---
    def set_status(self, document_id: str, status: DocumentStatus) -> DocumentMetadata:
        """Explicit, audited lifecycle change; persisted when a state path is set."""
        return self._corpus.set_status(document_id, status)

    def activate(self, document_id: str) -> DocumentMetadata:
        return self.set_status(document_id, DocumentStatus.ACTIVE)

    def supersede(self, old_document_id: str, new_document_id: str) -> None:
        self._corpus.supersede(old_document_id, new_document_id)

    def revoke(self, document_id: str) -> DocumentMetadata:
        return self.set_status(document_id, DocumentStatus.REVOKED)

    # ---------------------------------------------------------- persistence ---
    def _load_overrides(self) -> dict[str, DocumentStatus]:
        if self._state_path is None or not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("lifecycle state must be a JSON object")
            return {str(doc_id): DocumentStatus(str(status)) for doc_id, status in raw.items()}
        except (OSError, ValueError, TypeError) as exc:
            # Fail closed: an unreadable override file could silently reinstate a
            # revoked document, so refuse to start rather than guess.
            raise ValidationError(
                "knowledge_lifecycle_state_unreadable",
                details={"path": str(self._state_path), "error": str(exc)},
            ) from exc

    def _record_override(self, document_id: str, status: DocumentStatus) -> None:
        self._overrides[document_id] = status
        self._persist_overrides()

    def _persist_overrides(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {doc_id: status.value for doc_id, status in sorted(self._overrides.items())},
            indent=2,
            sort_keys=True,
        )
        tmp = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._state_path)

    @property
    def lifecycle_overrides(self) -> dict[str, DocumentStatus]:
        return dict(self._overrides)

    @property
    def corpus(self) -> KnowledgeCorpus:
        return self._corpus
