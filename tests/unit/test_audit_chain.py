"""Tamper-evident audit chain (master prompt §22; blocker B2)."""

from __future__ import annotations

import json

import pytest

from app.core.audit.chain import (
    GENESIS_HASH,
    FileAppendOnlyLog,
    HashChainedAuditSink,
    MemoryAppendOnlyLog,
    verify_chain,
)
from app.core.audit.events import AuditAction, AuditEvent, AuditResult
from app.core.audit.service import AuditService, build_audit_sink
from tests.conftest import customer_context

SECRET = "unit-test-chain-secret-not-a-real-key"


def _event(n: int) -> AuditEvent:
    return AuditEvent(
        correlation_id=f"corr_{n}",
        request_id=f"req_{n}",
        actor_ref="sub_000000000001",
        actor_type="CUSTOMER",
        channel="WEB_CUSTOMER",
        action=AuditAction.FLOW_STATE_CHANGED,
        result=AuditResult.SUCCESS,
        attributes={"n": n},
    )


async def _chained(n: int, log: MemoryAppendOnlyLog | None = None) -> tuple[HashChainedAuditSink, list[str]]:
    log = log or MemoryAppendOnlyLog()
    sink = HashChainedAuditSink(log, secret=SECRET)
    for i in range(n):
        await sink.append(_event(i))
    return sink, log.lines()


async def test_intact_chain_verifies_and_returns_every_event():
    _, lines = await _chained(25)
    result = verify_chain(lines, secret=SECRET)
    assert result.valid and result.records_checked == 25
    assert [e.attributes["n"] for e in result.events] == list(range(25))
    first = json.loads(lines[0])
    assert first["seq"] == 1 and first["prev_hash"] == GENESIS_HASH


async def test_editing_a_record_breaks_the_chain_at_that_record():
    _, lines = await _chained(10)
    record = json.loads(lines[4])
    record["event"]["attributes"]["n"] = 999  # rewrite history
    lines[4] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    result = verify_chain(lines, secret=SECRET)
    assert not result.valid
    assert result.first_bad_seq == 5 and result.reason == "tag_mismatch"
    assert result.records_checked == 4, "everything before the edit is still trustworthy"


async def test_deleting_a_record_is_detected():
    _, lines = await _chained(10)
    del lines[6]
    result = verify_chain(lines, secret=SECRET)
    assert not result.valid and result.first_bad_seq == 7
    assert result.reason == "sequence_gap_or_reorder"


async def test_reordering_records_is_detected():
    _, lines = await _chained(10)
    lines[2], lines[3] = lines[3], lines[2]
    result = verify_chain(lines, secret=SECRET)
    assert not result.valid and result.first_bad_seq == 3


async def test_truncating_the_tail_is_invisible_to_the_chain_alone_but_caught_by_the_head():
    """A hash chain cannot prove the *last* records were not removed. The signed head
    written after every append can: a log shorter than its head is ``tail_truncated``."""
    _sink, lines = await _chained(10)
    log = _sink._log
    assert verify_chain(lines[:7], secret=SECRET).valid, "the chain alone is blind to truncation"

    result = verify_chain(lines[:7], secret=SECRET, head=log.read_head())
    assert not result.valid
    assert result.reason == "tail_truncated" and result.first_bad_seq == 8
    assert result.records_checked == 7

    intact = verify_chain(lines, secret=SECRET, head=log.read_head())
    assert intact.valid and intact.warnings == []


async def test_the_head_is_keyed_so_a_forger_cannot_shrink_it():
    sink, lines = await _chained(5)
    head = json.loads(sink._log.read_head())
    head["seq"], head["tag"] = 3, json.loads(lines[2])["hash"]  # pretend the log always had 3
    result = verify_chain(lines[:3], secret=SECRET, head=json.dumps(head))
    assert not result.valid and result.reason == "head_tag_mismatch"


async def test_a_head_one_step_behind_the_log_is_a_warning_not_a_break():
    """A crash between ``append_line`` and ``write_head`` leaves the head one record
    behind; that is recoverable and must not read as tampering."""
    sink, lines = await _chained(5)
    stale_head = sink._head_document(4, json.loads(lines[3])["hash"])
    result = verify_chain(lines, secret=SECRET, head=stale_head)
    assert result.valid and "head_behind_log" in result.warnings


async def test_two_writers_on_one_log_are_reported_as_a_warning():
    log = MemoryAppendOnlyLog()
    first = HashChainedAuditSink(log, secret=SECRET, instance_id="pod-a:1")
    await first.append(_event(1))
    second = HashChainedAuditSink(log, secret=SECRET, instance_id="pod-b:1")
    await second.append(_event(2))
    result = verify_chain(log.lines(), secret=SECRET, head=log.read_head())
    assert result.valid, "a second honest writer does not break the chain"
    assert "multi_writer" in result.warnings and result.instances == ["pod-a:1", "pod-b:1"]
    assert {json.loads(ln)["instance"] for ln in log.lines()} == {"pod-a:1", "pod-b:1"}


async def test_a_restart_on_a_truncated_log_refuses_to_append_over_the_evidence(tmp_path):
    log_path = tmp_path / "audit.chain.log"
    sink = HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)
    for i in range(6):
        await sink.append(_event(i))
    lines = log_path.read_text(encoding="utf-8").splitlines()
    log_path.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="audit_chain_tail_truncated"):
        HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)


def test_legacy_v1_records_without_an_instance_still_verify():
    from app.core.audit.chain import compute_tag

    key = SECRET.encode()
    lines = []
    prev = GENESIS_HASH
    for seq in (1, 2):
        canonical = json.dumps(_event(seq).model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        tag = compute_tag(key, seq, prev, canonical, version="1")
        lines.append(
            json.dumps({"v": "1", "seq": seq, "prev_hash": prev, "hash": tag, "event": json.loads(canonical)})
        )
        prev = tag
    result = verify_chain(lines, secret=SECRET)
    assert result.valid and result.records_checked == 2 and result.instances == []


async def test_a_forged_record_without_the_key_is_rejected():
    _sink, lines = await _chained(3)
    forged_log = MemoryAppendOnlyLog()
    forger = HashChainedAuditSink(forged_log, secret="attacker-does-not-know-the-key")
    for line in lines:
        forged_log.append_line(line)
    forger._seq, forger._prev = 3, json.loads(lines[-1])["hash"]
    await forger.append(_event(99))
    result = verify_chain(forged_log.lines(), secret=SECRET)
    assert not result.valid and result.first_bad_seq == 4 and result.reason == "tag_mismatch"


async def test_a_restarted_instance_extends_the_chain(tmp_path):
    log_path = tmp_path / "audit.chain.log"
    first = HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)
    for i in range(5):
        await first.append(_event(i))
    del first  # crash

    second = HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)
    for i in range(5, 8):
        await second.append(_event(i))

    result = verify_chain(log_path.read_text(encoding="utf-8").splitlines(), secret=SECRET)
    assert result.valid and result.records_checked == 8


async def test_malformed_line_is_reported_not_skipped():
    _, lines = await _chained(3)
    lines.insert(1, "not json at all")
    result = verify_chain(lines, secret=SECRET)
    assert not result.valid and result.reason == "malformed_record"


async def test_the_service_writes_redacted_events_into_the_chain():
    sink = build_audit_sink("chained_memory", "unused", chain_secret=SECRET)
    service = AuditService(sink)  # type: ignore[arg-type]
    ctx = customer_context()
    await service.record(
        ctx,
        AuditAction.FLOW_STARTED,
        AuditResult.SUCCESS,
        attributes={"mobile": "9876543210", "note": "asha.verma@example.com"},
    )
    lines = sink._log.lines()  # type: ignore[attr-defined]
    assert "9876543210" not in lines[0] and "asha.verma@example.com" not in lines[0]
    assert verify_chain(lines, secret=SECRET).valid


def test_chained_sink_requires_a_secret():
    with pytest.raises(ValueError):
        HashChainedAuditSink(MemoryAppendOnlyLog(), secret="")


def test_verifier_cli_reports_a_break(tmp_path, monkeypatch, capsys):
    import asyncio

    from scripts.verify_audit_chain import main

    log_path = tmp_path / "audit.chain.log"
    sink = HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)
    asyncio.run(sink.append(_event(1)))
    asyncio.run(sink.append(_event(2)))
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", SECRET)
    assert main([str(log_path)]) == 0

    lines = log_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["event"]["result"] = "FAILURE"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main([str(log_path)]) == 1
    assert "seq 2" in capsys.readouterr().err


def test_verifier_cli_detects_tail_truncation_via_the_head_file(tmp_path, monkeypatch, capsys):
    import asyncio

    from scripts.verify_audit_chain import main

    log_path = tmp_path / "audit.chain.log"
    sink = HashChainedAuditSink(FileAppendOnlyLog(log_path), secret=SECRET)
    for i in range(10):
        asyncio.run(sink.append(_event(i)))
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", SECRET)
    assert (tmp_path / "audit.chain.log.head").exists()
    assert main([str(log_path)]) == 0
    assert "head verified" in capsys.readouterr().out

    lines = log_path.read_text(encoding="utf-8").splitlines()
    log_path.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")  # delete the last 3 records
    assert main([str(log_path)]) == 1
    err = capsys.readouterr().err
    assert "tail_truncated" in err and "seq 8" in err

    # Opting out of the head check is explicit and loud, and then the chain alone passes.
    assert main([str(log_path), "--no-head"]) == 0
