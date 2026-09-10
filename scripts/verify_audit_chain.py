"""Verify the integrity of a hash-chained audit log (master prompt §22).

    python scripts/verify_audit_chain.py var/audit/audit.log --secret-env AUDIT_CHAIN_SECRET

Exit code 0 when every record's tag and link verify and the log agrees with its signed
head file (``<log>.head``), 1 otherwise, 2 for usage errors. Prints the first bad
sequence number and the reason (``tag_mismatch``, ``sequence_gap_or_reorder``,
``broken_link``, ``tail_truncated``, ``head_tag_mismatch`` ...), so an auditor can see
exactly where the log stops being trustworthy. Non-fatal observations (``multi_writer``,
``head_behind_log``) are printed as warnings and do not change the exit code.

The secret is read from an environment variable, never from argv, so it does not end
up in shell history.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.audit.chain import HEAD_SUFFIX, verify_chain  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an HMAC hash-chained audit log.")
    parser.add_argument("path", help="newline-delimited chained audit log")
    parser.add_argument(
        "--secret-env", default="AUDIT_CHAIN_SECRET", help="environment variable holding the chain key"
    )
    parser.add_argument(
        "--head",
        default=None,
        help=f"signed head file (default: <path>{HEAD_SUFFIX}); pass --no-head to skip the check",
    )
    parser.add_argument(
        "--no-head", action="store_true", help="verify the chain only; tail truncation will NOT be detected"
    )
    args = parser.parse_args(argv)

    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"ERROR: environment variable {args.secret_env} is not set", file=sys.stderr)
        return 2

    log_path = Path(args.path)
    lines = log_path.read_text(encoding="utf-8").splitlines()

    head: str | None = None
    if not args.no_head:
        head_path = Path(args.head) if args.head else log_path.with_name(log_path.name + HEAD_SUFFIX)
        if head_path.exists():
            head = head_path.read_text(encoding="utf-8").strip() or None
        else:
            print(
                f"WARNING: no head file at {head_path}; tail truncation cannot be detected",
                file=sys.stderr,
            )

    result = verify_chain(lines, secret=secret, head=head)
    for warning in result.warnings:
        detail = f" (instances: {', '.join(result.instances)})" if warning == "multi_writer" else ""
        print(f"WARNING: {warning}{detail}", file=sys.stderr)

    if result.valid:
        anchored = "head verified" if head is not None else "no head"
        print(f"AUDIT CHAIN OK: {result.records_checked} records verified, chain intact, {anchored}")
        return 0
    where = f"at seq {result.first_bad_seq}" if result.first_bad_seq is not None else "at head"
    print(
        f"AUDIT CHAIN BROKEN {where}: {result.reason} "
        f"({result.records_checked} records verified before the break)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
