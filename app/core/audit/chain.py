"""Tamper-evident audit log: an HMAC hash chain with a signed head (master prompt §22, §41).

Every appended record carries the sequence number, the hash of the previous record, the
identity of the writing instance and an HMAC-SHA256 over
``version | seq | prev_hash | instance | event``. Any edit, deletion, reordering or
insertion breaks the chain from that point on, and :func:`verify_chain` reports the
first bad sequence number and why.

A hash chain alone cannot prove that the *last* records were not removed. For that the
sink rewrites a small **head file** (``<log>.head``) atomically after every append,
carrying the latest sequence number and tag under the same key. The verifier compares
the head with the log: a log shorter than its head is reported as ``tail_truncated``.

What this gives, precisely:

* **tamper-evident** - modification, deletion, reordering or tail truncation of stored
  records is *detectable* by anyone holding the chain key
* **forgery-resistant** - a forger without ``AUDIT_CHAIN_SECRET`` cannot produce a
  record or head the verifier accepts, because the tag is keyed, not a bare digest
* **writer-attributable** - each record names the instance that wrote it, so two
  processes appending to one file are visible (``multi_writer`` warning)

What it does **not** give, and does not claim:

* **tamper-proof storage** - an attacker who can rewrite the file *and* the head *and*
  holds the key can rebuild the chain. Keeping the key out of the application's write
  path and storing the log on WORM / object-lock storage closes that; both are platform
  concerns recorded as open items. The chain makes such storage *verifiable*.

The sink is storage-agnostic: it writes chained lines to any :class:`AppendOnlyLog`
(file, memory, or a future object-store adapter), so the integrity property does not
depend on which medium the platform supplies.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.audit.events import AuditEvent

GENESIS_HASH = "0" * 64
#: v1 records (no instance id) remain verifiable; new records are written as v2.
CHAIN_VERSION = "2"
_LEGACY_VERSION = "1"
HEAD_SUFFIX = ".head"


class AppendOnlyLog(Protocol):
    def append_line(self, line: str) -> None: ...
    def lines(self) -> list[str]: ...
    def write_head(self, content: str) -> None: ...
    def read_head(self) -> str | None: ...


class MemoryAppendOnlyLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._head: str | None = None

    def append_line(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def write_head(self, content: str) -> None:
        with self._lock:
            self._head = content

    def read_head(self) -> str | None:
        with self._lock:
            return self._head


class FileAppendOnlyLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._head_path = self._path.with_name(self._path.name + HEAD_SUFFIX)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def head_path(self) -> Path:
        return self._head_path

    def append_line(self, line: str) -> None:
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def lines(self) -> list[str]:
        if not self._path.exists():
            return []
        return [ln for ln in self._path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def write_head(self, content: str) -> None:
        """Atomic replace: a reader never sees a half-written head."""
        tmp = self._head_path.with_name(self._head_path.name + ".tmp")
        with self._lock:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, self._head_path)

    def read_head(self) -> str | None:
        if not self._head_path.exists():
            return None
        content = self._head_path.read_text(encoding="utf-8").strip()
        return content or None


def _canonical(event_json: str) -> str:
    """Canonical form so verification does not depend on key order or whitespace."""
    return json.dumps(json.loads(event_json), sort_keys=True, separators=(",", ":"))


def compute_tag(
    key: bytes,
    seq: int,
    prev_hash: str,
    canonical_event: str,
    *,
    instance_id: str | None = None,
    version: str = CHAIN_VERSION,
) -> str:
    if version == _LEGACY_VERSION:
        message = f"{version}|{seq}|{prev_hash}|{canonical_event}".encode()
    else:
        message = f"{version}|{seq}|{prev_hash}|{instance_id or ''}|{canonical_event}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def compute_head_tag(key: bytes, seq: int, tag: str, instance_id: str) -> str:
    message = f"head|{CHAIN_VERSION}|{seq}|{tag}|{instance_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def default_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class HashChainedAuditSink:
    """Append-only, chained sink. Satisfies :class:`~app.core.audit.service.AuditSink`."""

    def __init__(self, log: AppendOnlyLog, *, secret: str, instance_id: str | None = None) -> None:
        if not secret:
            raise ValueError("audit chain secret is required")
        self._log = log
        self._key = secret.encode("utf-8")
        self._instance = instance_id or default_instance_id()
        self._lock = threading.Lock()
        # Resume from an existing log so a restarted instance extends, not restarts, the chain.
        self._seq, self._prev = self._tail()
        self._refuse_truncated_tail()

    def _tail(self) -> tuple[int, str]:
        lines = self._log.lines()
        if not lines:
            return 0, GENESIS_HASH
        last = json.loads(lines[-1])
        return int(last["seq"]), str(last["hash"])

    def _refuse_truncated_tail(self) -> None:
        """A head claiming more records than the log holds means the tail was removed.
        Appending would overwrite the head and destroy the evidence, so refuse."""
        raw = self._log.read_head()
        if raw is None:
            return
        try:
            head = json.loads(raw)
            claimed = int(head["seq"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("audit_chain_head_malformed") from None
        if claimed > self._seq:
            raise ValueError(
                f"audit_chain_tail_truncated: head claims seq {claimed}, log ends at {self._seq}"
            )

    async def append(self, event: AuditEvent) -> None:
        await asyncio.to_thread(self._append_sync, event.model_dump_json())

    def _append_sync(self, event_json: str) -> None:
        with self._lock:
            seq = self._seq + 1
            canonical = _canonical(event_json)
            tag = compute_tag(self._key, seq, self._prev, canonical, instance_id=self._instance)
            record = {
                "v": CHAIN_VERSION,
                "seq": seq,
                "prev_hash": self._prev,
                "hash": tag,
                "instance": self._instance,
                "event": json.loads(canonical),
            }
            self._log.append_line(json.dumps(record, sort_keys=True, separators=(",", ":")))
            self._seq, self._prev = seq, tag
            self._log.write_head(self._head_document(seq, tag))

    def _head_document(self, seq: int, tag: str) -> str:
        head = {
            "v": CHAIN_VERSION,
            "seq": seq,
            "tag": tag,
            "instance": self._instance,
            "hmac": compute_head_tag(self._key, seq, tag, self._instance),
        }
        return json.dumps(head, sort_keys=True, separators=(",", ":"))

    # Convenience for tests and the verifier CLI.
    def records(self) -> list[dict]:
        return [json.loads(line) for line in self._log.lines()]

    def events(self, action: object | None = None) -> list[AuditEvent]:
        parsed = [AuditEvent.model_validate(r["event"]) for r in self.records()]
        if action is None:
            return parsed
        return [e for e in parsed if e.action is action]

    def reset(self) -> None:  # pragma: no cover - used only by memory-backed tests
        if isinstance(self._log, MemoryAppendOnlyLog):
            self._log._lines.clear()
            self._log._head = None
            self._seq, self._prev = 0, GENESIS_HASH


@dataclass(slots=True)
class ChainVerification:
    valid: bool
    records_checked: int
    first_bad_seq: int | None = None
    reason: str | None = None
    events: list[AuditEvent] = field(default_factory=list)
    #: Non-fatal observations: ``multi_writer`` (more than one instance appended),
    #: ``head_behind_log`` (head older than the log; a crash between append and head write).
    warnings: list[str] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)


def verify_chain(lines: Iterable[str], *, secret: str, head: str | None = None) -> ChainVerification:
    """Recompute every tag and link, then reconcile the log with its signed head.

    Stops at the first break and names it. When ``head`` is given, a log that holds fewer
    records than the head attests is ``tail_truncated``; a head that does not verify under
    the key is ``head_tag_mismatch``.
    """
    key = secret.encode("utf-8")
    prev = GENESIS_HASH
    expected_seq = 1
    checked = 0
    events: list[AuditEvent] = []
    tags: list[str] = []
    instances: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            version = str(record.get("v", _LEGACY_VERSION))
            seq = int(record["seq"])
            prev_hash = str(record["prev_hash"])
            tag = str(record["hash"])
            instance = record.get("instance")
            canonical = json.dumps(record["event"], sort_keys=True, separators=(",", ":"))
        except (KeyError, ValueError, TypeError):
            return ChainVerification(False, checked, expected_seq, "malformed_record", events)
        if seq != expected_seq:
            return ChainVerification(False, checked, expected_seq, "sequence_gap_or_reorder", events)
        if prev_hash != prev:
            return ChainVerification(False, checked, seq, "broken_link", events)
        expected = compute_tag(key, seq, prev_hash, canonical, instance_id=instance, version=version)
        if not hmac.compare_digest(tag, expected):
            return ChainVerification(False, checked, seq, "tag_mismatch", events)
        events.append(AuditEvent.model_validate(record["event"]))
        tags.append(tag)
        if instance and instance not in instances:
            instances.append(str(instance))
        prev = tag
        expected_seq += 1
        checked += 1

    warnings: list[str] = []
    if len(instances) > 1:
        warnings.append("multi_writer")

    if head is not None:
        failure = _reconcile_head(key, head, tags, warnings)
        if failure is not None:
            reason, bad_seq = failure
            return ChainVerification(False, checked, bad_seq, reason, events, warnings, instances)

    return ChainVerification(True, checked, None, None, events, warnings, instances)


def _reconcile_head(
    key: bytes, head: str, tags: list[str], warnings: list[str]
) -> tuple[str, int | None] | None:
    try:
        document = json.loads(head)
        seq = int(document["seq"])
        tag = str(document["tag"])
        instance = str(document["instance"])
        head_tag = str(document["hmac"])
    except (KeyError, ValueError, TypeError):
        return "head_malformed", None
    if not hmac.compare_digest(head_tag, compute_head_tag(key, seq, tag, instance)):
        return "head_tag_mismatch", None
    if seq > len(tags):
        return "tail_truncated", len(tags) + 1
    if seq >= 1 and tags[seq - 1] != tag:
        return "head_mismatch", seq
    if seq < len(tags):
        warnings.append("head_behind_log")
    return None
