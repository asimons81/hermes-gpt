"""Production review-acceptance writer for hermes-gpt v0.7 (Flight Deck, S3).

Implements ADR-003 / ADR-006: ``hermes_review_accept`` writes an append-only
ReviewAcceptanceRecord (``<hermes_data>/review-evidence/review-acceptances.jsonl``)
at **owner** level with distinct-reviewer enforcement at write time. The M1
contract validator (``operator_contract._check_review``) reads this store as an
additional evidence source alongside the existing audit + human-approval paths.

Evidence is referenced (``evidence_refs``), never copied; raw prompts and
transcripts never appear. Every call is audited.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_policy as op

SCHEMA_VERSION = "0.7-rv.1"
REVIEW_SCHEMA = "hermes.review-acceptance/v1"
VERDICTS = ("SATISFIED", "NOT_SATISFIED")
RECORD_ID_RE = re.compile(r"^rev-[0-9a-f]{16}$")
TOOL_VERSION = "0.7.0"

REVIEW_EVIDENCE_DIR = "review-evidence"
REVIEW_EVIDENCE_FILENAME = "review-acceptances.jsonl"
# Per-line bound for the review-evidence store; longer lines are skipped.
REVIEW_LINE_MAX_BYTES = 64 * 1024
# Matching records per contract that a validator or inbox will read.
ACCEPTANCE_FOR_CONTRACT_MAX = 1000

_ACCEPT_LOCK = threading.Lock()


def review_evidence_path(hermes_root: Path) -> Path:
    return hermes_root / REVIEW_EVIDENCE_DIR / REVIEW_EVIDENCE_FILENAME


def _default_hermes_root() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def _new_record_id() -> str:
    return f"rev-{uuid.uuid4().hex[:16]}"


def _validate_refs(evidence_refs: list[str]) -> list[str]:
    """Validate evidence reference list; reject secret-path refs (legal L4)."""
    if not isinstance(evidence_refs, list):
        raise ValueError("evidence_refs must be a list of artifact/approval references")
    out: list[str] = []
    for ref in evidence_refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("evidence_refs entries must be non-empty strings")
        if op.is_denied_path(ref):
            raise ValueError(f"evidence_ref refuses denied path {ref!r}")
        out.append(ref[:300])
    return out


def _iter_acceptance_records(path: Path, needle: bytes | None = None):
    """Yield review-acceptance records from the store, oldest first.

    Streams the whole file line by line. A line longer than
    ``REVIEW_LINE_MAX_BYTES`` is skipped without being buffered, as is any line
    that is not a JSON object with the review schema, so a corrupt line can
    never count as an acceptance. When ``needle`` is given, lines that do not
    contain it are skipped before JSON parsing. ``OSError`` propagates.
    """
    with path.open("rb") as fh:
        while True:
            raw = fh.readline(REVIEW_LINE_MAX_BYTES + 1)
            if not raw:
                return
            if len(raw) > REVIEW_LINE_MAX_BYTES:
                if not raw.endswith(b"\n"):
                    while True:  # discard the remainder of the oversized line
                        rest = fh.readline(REVIEW_LINE_MAX_BYTES)
                        if not rest or rest.endswith(b"\n"):
                            break
                continue
            if needle is not None and needle not in raw:
                continue
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("schema") == REVIEW_SCHEMA:
                yield rec


def read_review_acceptances(hermes_root: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return the newest ``limit`` review-acceptance records, oldest first.

    This is a recent-records view (the Flight Deck review list). It is not
    evidence: validation and the owner inbox use ``acceptance_records_for``.
    Returns ``[]`` when the store is missing or unreadable.
    """
    path = review_evidence_path(hermes_root)
    if not path.exists():
        return []
    try:
        return list(deque(_iter_acceptance_records(path), maxlen=limit))
    except OSError:
        return []


def acceptance_records_for(
    hermes_root: Path,
    contract_sha256: str,
    limit: int = ACCEPTANCE_FOR_CONTRACT_MAX,
) -> list[dict[str, Any]]:
    """Every review-acceptance record for one contract, oldest first.

    Reads the whole store, so an acceptance written late is never hidden by
    older unrelated records. Raises ``OSError`` when the store is unreadable
    and ``ValueError`` when more than ``limit`` records match, so callers fail
    closed rather than act on a partial list.
    """
    path = review_evidence_path(hermes_root)
    if not path.exists():
        return []
    sha = str(contract_sha256)
    needle = sha.encode("utf-8")
    out: list[dict[str, Any]] = []
    for rec in _iter_acceptance_records(path, needle):
        if rec.get("contract_sha256") != sha:
            continue
        if len(out) >= limit:
            raise ValueError("more review acceptances than the per-contract limit")
        out.append(rec)
    return out


def _write_acceptance(hermes_root: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record to the review-evidence store (append-only).

    Mirrors the operator audit pattern: a single locked append so concurrent
    writers cannot interleave partial lines. The store is append-only; records
    are never rewritten in place.
    """
    path = review_evidence_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _ACCEPT_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    # Best-effort 0600 like the secrets store family.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def hermes_review_accept(
    contract_sha256: str,
    task_id: str,
    assignee: str,
    reviewer: str,
    verdict: str,
    evidence_refs: list[str] | None = None,
    approval_reference: str = "",
    dry_run: bool = True,
    confirm: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Write a review-acceptance record for a work contract (owner-gated).

    Authority: **owner** + direct + confirm (same class as
    ``hermes_swarm_approve``). Distinct-reviewer rule enforced at write time:
    ``reviewer != assignee``; self-review is rejected. Verdict vocabulary is
    bounded to ``SATISFIED`` / ``NOT_SATISFIED``. Evidence is referenced, never
    copied. Every call is audited.
    """
    tool = "hermes_review_accept"
    tid = op.new_trace_id()
    root = hermes_root or _default_hermes_root()
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="REVIEW_POLICY_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable Owner Mode (owner level, owner active, owner ack) with direct apply before writing review acceptance.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level="owner", apply_mode="dry_run", dry_run=True, success=False,
            changed=False, summary="review accept denied", error=str(exc)[:300],
            extra={"contract_sha256": contract_sha256[:16], "task_id": task_id},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        if not isinstance(contract_sha256, str) or len(contract_sha256) != 64:
            raise ValueError("contract_sha256 must be a 64-char hex digest")
        int(contract_sha256, 16)
        if not task_id or len(task_id) > 128:
            raise ValueError("task_id is required (max 128 chars)")
        if not reviewer or reviewer == assignee:
            raise ValueError("reviewer must be distinct from assignee (self-review rejected)")
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        refs = _validate_refs(evidence_refs or [])
        if verdict == "SATISFIED" and not refs and not approval_reference:
            raise ValueError("SATISFIED requires at least one evidence_ref or approval_reference")
    except ValueError as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="INVALID_REVIEW_ACCEPTANCE",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the review-acceptance fields and retry.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=bool(effective),
            success=False, changed=False, summary="review accept invalid", error=str(exc)[:300],
            extra={"contract_sha256": contract_sha256[:16], "task_id": task_id},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = {
        "record_id": _new_record_id(),
        "schema": REVIEW_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "contract_sha256": contract_sha256,
        "task_id": task_id,
        "assignee": assignee,
        "reviewer": reviewer,
        "verdict": verdict,
        "evidence_refs": refs,
        "approval_reference": approval_reference[:300] if approval_reference else "",
        "created_by": os.environ.get("HERMES_GPT_PROFILE", "unknown") or "unknown",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_version": TOOL_VERSION,
    }

    if effective:
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "review_accept",
            "dry_run": True,
            "record": {k: v for k, v in record.items() if k != "contract_sha256"},
            "trace_id": tid,
        }
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=True,
            success=True, changed=False, summary="review accept dry-run plan",
            extra={"contract_sha256": contract_sha256[:16], "task_id": task_id, "reviewer": reviewer},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = op.make_error_envelope(
            layer="operator",
            code="CONFIRMATION_REQUIRED",
            safe_message="review accept requires confirm=true for direct execution.",
            suggested_action="Review the record and call again with confirm=true, dry_run=false.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
            success=False, changed=False, summary="review accept confirmation required",
            extra={"contract_sha256": contract_sha256[:16], "task_id": task_id},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        _write_acceptance(root, record)
    except OSError as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="REVIEW_STORE_WRITE_ERROR",
            safe_message=f"could not write review-evidence store: {op.redact_output(str(exc))[:200]}",
            suggested_action="Check review-evidence directory permissions.",
            trace_id=tid,
        )
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
            success=False, changed=False, summary="review accept store write failed",
            error=str(exc)[:300],
            extra={"contract_sha256": contract_sha256[:16], "task_id": task_id},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    op.audit_record(
        tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
        success=True, changed=True, summary=f"review acceptance written verdict={verdict} reviewer={reviewer}",
        extra={
            "record_id": record["record_id"],
            "contract_sha256": contract_sha256[:16],
            "task_id": task_id,
            "reviewer": reviewer,
            "verdict": verdict,
        },
    )
    return json.dumps(
        {
            "success": True,
            "changed": True,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "review_accept",
            "record_id": record["record_id"],
            "contract_sha256": contract_sha256,
            "task_id": task_id,
            "reviewer": reviewer,
            "verdict": verdict,
            "record": {k: v for k, v in record.items() if k != "contract_sha256"},
            "generated_at": record["created_at"],
            "trace_id": tid,
        },
        ensure_ascii=False,
        indent=2,
    )


def acceptance_exists(hermes_root: Path, contract_sha256: str, reviewer: str, assignee: str) -> bool:
    """True when a SATISFIED acceptance by a distinct reviewer exists."""
    for rec in acceptance_records_for(hermes_root, contract_sha256):
        if rec.get("contract_sha256") != contract_sha256:
            continue
        if rec.get("verdict") != "SATISFIED":
            continue
        r = rec.get("reviewer") or ""
        a = rec.get("assignee") or assignee
        if r and r != a and r != assignee:
            return True
    return False
