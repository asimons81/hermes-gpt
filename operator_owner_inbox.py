"""Owner inbox: one place that lists what is waiting on the Owner.

The inbox is a projection over EXISTING read paths. Its only write is the
acknowledgement writer ``hermes_owner_inbox_ack``, which appends to
``missions/attention_acks.jsonl``. An acknowledgement only hides an item from
the inbox and from notifications; it never approves, transitions or changes any
Mission, plan, Work Contract or delegation. Sources (each reported as ``ok`` |
``empty`` | ``unavailable`` in ``sources``):

- ``missions``: first-class Missions in ``awaiting_approval`` (via
  ``operator_mission_runtime.hermes_mission_list``). Suggested action:
  ``hermes_mission_approve`` (Owner).
- ``autopilot``: Autopilot runs whose effective state is ``waiting_for_owner`` or
  whose derived summary reports ``needs_owner`` (via
  ``operator_autopilot.observe_status``, the non-writing Autopilot read).
- ``controller_attention``: the newest controller attention envelopes in the
  spool ``missions/controller_attention_spool.jsonl`` (budget breaker, RED
  health, ``no_capable_target``), read with a bounded tail read.
- ``work_contract_review``: delegations whose execution succeeded, whose
  immutable Work Contract requires review, and that have no matching review
  acceptance yet (``operator_delegations`` rows + validation manifests +
  ``operator_review`` acceptance records). Read-only.
- ``acks``: the acknowledgement store ``missions/attention_acks.jsonl`` (bounded
  tail read). When unreadable, every item is treated as unacknowledged.

A source that raises or finds an unreadable record is reported ``unavailable``
and ``complete`` becomes false: the inbox never claims emptiness it did not
verify.

Stable ids: every item carries ``item_id``, the first 24 hex characters of
sha256 over ``"<kind>|<mission_id>|<stable_key>"`` (see ``_item_id``).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_autopilot as autopilot
import operator_contract as contract_mod
import operator_controller as controller
import operator_delegations as delegations
import operator_mission_runtime as missions
import operator_policy as op
import operator_review as op_review
import ui_security

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

SOURCE_MISSIONS = "missions"
SOURCE_AUTOPILOT = "autopilot"
SOURCE_CONTROLLER = "controller_attention"
SOURCE_REVIEW = "work_contract_review"
SOURCE_ACKS = "acks"
SOURCE_NAMES: tuple[str, ...] = (
    SOURCE_MISSIONS, SOURCE_AUTOPILOT, SOURCE_CONTROLLER, SOURCE_REVIEW, SOURCE_ACKS,
)
# Every source is now read. The field stays so a future unread source is visible.
SKIPPED_SOURCES: tuple[str, ...] = ()

KIND_MISSION = "mission_awaiting_approval"
KIND_AUTOPILOT = "autopilot_waiting_owner"
KIND_BUDGET = "budget_breaker"
KIND_RED = "red_health"
KIND_NO_TARGET = "no_capable_target"
KIND_REVIEW = "contract_review_pending"

TOOL_MISSION_APPROVE = "hermes_mission_approve"
TOOL_NODE_TRANSITION = "hermes_plan_node_transition"
TOOL_BUDGET_CHECK = "hermes_budget_check"
TOOL_CONTROLLER_STATUS = "hermes_controller_status"
TOOL_REVIEW_ACCEPT = "hermes_review_accept"

ACK_STORE_RELATIVE = "missions/attention_acks.jsonl"
ACK_BY = "owner_inbox_ack"
ACK_MAX_IDS = 50
ACK_SCHEMA_VERSION = "0.16-ack.1"

_SPOOL_TAIL_BYTES = 256 * 1024
_SPOOL_MAX_LINES = 200
_ACK_TAIL_BYTES = 1024 * 1024
_RUN_FILE_MAX_BYTES = 1024 * 1024
_RUN_SCAN_MAX = 200
_REVIEW_ROW_MAX = 200
_SUMMARY_MAX = 240
_NODE_LIST_MAX = 8
_ITEM_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ACK_LOCK = threading.Lock()


class _SourceUnavailable(Exception):
    """Raised inside a source reader when its data cannot be trusted."""


def _bounded(text: Any) -> str:
    """Redact (Mission Control / ui_security rules) and bound one summary string."""
    value = ui_security.redact_browser(str(text or ""))
    return str(value)[:_SUMMARY_MAX]


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _newest_key(item: dict[str, Any]) -> float:
    epoch = _epoch(item.get("since"))
    return epoch if epoch is not None else float("-inf")


def _item_id(kind: str, mission_id: str, stable_key: str) -> str:
    """Stable item id: first 24 hex chars of sha256("<kind>|<mission_id>|<stable_key>")."""
    seed = f"{kind}|{mission_id}|{stable_key}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:24]


def _item(
    *,
    kind: str,
    mission_id: str,
    summary: str,
    since: Any,
    suggested_tool: str | None,
    requires: str,
    stable_key: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mid = str(mission_id or "")[:128]
    item: dict[str, Any] = {
        "item_id": _item_id(kind, mid, stable_key),
        "kind": kind,
        "mission_id": mid,
        "summary": _bounded(summary),
        "since": str(since) if since else None,
        "suggested_tool": suggested_tool,
        "requires": requires,
    }
    # Structured ids for kinds whose Flight Deck actions need them. They are
    # not part of the stable id (see ``_item_id``), so they can change without
    # changing the item's identity.
    if extra:
        item.update(extra)
    return item


# ---------------------------------------------------------------------------
# Source: Missions awaiting approval
# ---------------------------------------------------------------------------


def _mission_items(root: Path) -> tuple[str, list[dict[str, Any]]]:
    payload = json.loads(
        missions.hermes_mission_list(status="awaiting_approval", limit=MAX_LIMIT, hermes_root=root)
    )
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise _SourceUnavailable("mission list failed")
    items: list[dict[str, Any]] = []
    for row in payload.get("missions") or []:
        if not isinstance(row, dict) or row.get("status") != "awaiting_approval":
            continue
        items.append(
            _item(
                kind=KIND_MISSION,
                mission_id=str(row.get("mission_id") or ""),
                summary="Mission is awaiting Owner approval; children must be terminal with a verified success.",
                since=row.get("updated_at"),
                suggested_tool=TOOL_MISSION_APPROVE,
                requires="owner",
                stable_key=str(row.get("status") or ""),
            )
        )
    return ("ok" if items else "empty"), items


# ---------------------------------------------------------------------------
# Source: Autopilot runs waiting on the Owner
# ---------------------------------------------------------------------------


def _run_files(base: Path) -> list[Path]:
    files = [p for p in base.glob("*.json") if not p.name.startswith(".") and p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:_RUN_SCAN_MAX]


def _autopilot_items(root: Path) -> tuple[str, list[dict[str, Any]]]:
    base = autopilot._root(root)  # noqa: SLF001 - same-package read of the run-record directory
    if not base.is_dir():
        return "empty", []
    items: list[dict[str, Any]] = []
    unreadable = False
    for path in _run_files(base):
        mission_id = path.stem
        try:
            autopilot._validate_mission_id(mission_id)  # noqa: SLF001
        except ValueError:
            continue  # not a run-record name
        try:
            if path.stat().st_size > _RUN_FILE_MAX_BYTES:
                raise ValueError("run record too large")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("run record is not an object")
            view = autopilot.observe_status(mission_id, root)
        except (OSError, ValueError, LookupError, TypeError, KeyError, UnicodeDecodeError):
            unreadable = True
            continue
        if view is None:
            continue
        summary = view.get("summary") if isinstance(view.get("summary"), dict) else {}
        state = str(view.get("effective_state") or "")
        needs_owner = bool(summary.get("needs_owner"))
        if state != "waiting_for_owner" and not needs_owner:
            continue
        run = view.get("run") if isinstance(view.get("run"), dict) else {}
        frontier = summary.get("frontier") if isinstance(summary.get("frontier"), dict) else {}
        all_nodes = sorted(str(n) for n in (frontier.get("nodes") or []))
        nodes = all_nodes[:_NODE_LIST_MAX]
        codes = [
            str(a.get("code"))
            for a in (summary.get("attention") or [])
            if isinstance(a, dict) and a.get("severity") == "owner"
        ]
        mission_status = str(summary.get("mission_status") or "")
        if mission_status == "awaiting_approval":
            suggested: str | None = TOOL_MISSION_APPROVE
        elif nodes:
            suggested = TOOL_NODE_TRANSITION
        else:
            suggested = None
        text = f"Autopilot waits on Owner (state={state or 'unknown'})"
        if nodes:
            text += f"; frontier nodes: {', '.join(nodes)}"
        if codes:
            text += f"; attention: {', '.join(codes[:6])}"
        items.append(
            _item(
                kind=KIND_AUTOPILOT,
                mission_id=mission_id,
                summary=text,
                since=run.get("updated_at") or run.get("last_tick_at"),
                suggested_tool=suggested,
                requires="owner",
                stable_key=",".join(all_nodes),
                # A mission has at most one plan, keyed by mission_id in
                # mission_plans; frontier_node_ids is the full sorted frontier.
                extra={"plan_id": mission_id, "frontier_node_ids": list(all_nodes)},
            )
        )
    if unreadable:
        raise _SourceUnavailable("one or more autopilot run records are unreadable")
    return ("ok" if items else "empty"), items


# ---------------------------------------------------------------------------
# Source: controller attention spool (newest, bounded tail read)
# ---------------------------------------------------------------------------


def _classify_envelope(envelope: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(kind, mission_id, requires)`` for one spooled RED envelope."""
    meta = envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
    reasons = meta.get("escalation_reasons") if isinstance(meta.get("escalation_reasons"), list) else []
    text = " ".join(
        [" ".join(str(r) for r in reasons), str(meta.get("row_key") or ""),
         str(meta.get("classification") or ""), str(meta.get("proposed_action") or "")]
    ).lower()
    dedupe = str(envelope.get("dedupe_key") or "")
    parts = dedupe.split(":")
    mission_id = ""
    if dedupe.startswith("controller:red:") and len(parts) >= 3:
        mission_id = parts[2]
    elif dedupe.startswith("controller:l2-no-target:") and len(parts) >= 4:
        mission_id = parts[2]
    if "no_capable_target" in text or dedupe.startswith("controller:l2-no-target:") or "no-target" in text:
        return KIND_NO_TARGET, mission_id, "workspace"
    if "budget" in text:
        return KIND_BUDGET, mission_id, "owner"
    return KIND_RED, mission_id, "owner"


_SUGGESTED_FOR_KIND = {
    KIND_BUDGET: TOOL_BUDGET_CHECK,
    KIND_RED: TOOL_CONTROLLER_STATUS,
    KIND_NO_TARGET: TOOL_CONTROLLER_STATUS,
}


def _spool_items(root: Path) -> tuple[str, list[dict[str, Any]]]:
    path = controller._attention_spool_path(root)  # noqa: SLF001 - same-package read of the spool path
    if not path.is_file():
        return "empty", []
    size = path.stat().st_size
    start = max(0, size - _SPOOL_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line of a tail window may be cut mid-record
    lines = [line for line in lines if line.strip()][-_SPOOL_MAX_LINES:]
    malformed = 0
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for raw in reversed(lines):  # newest first
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(envelope, dict):
            malformed += 1
            continue
        dedupe = str(envelope.get("dedupe_key") or envelope.get("event_id") or "")
        if dedupe and dedupe in seen:
            continue  # repeated RED pass for the same condition; keep the newest
        seen.add(dedupe)
        kind, mission_id, requires = _classify_envelope(envelope)
        title = str(envelope.get("title") or kind)
        stable = str(envelope.get("dedupe_key") or "") or f"{envelope.get('occurred_at') or ''}|{kind}"
        items.append(
            _item(
                kind=kind,
                mission_id=mission_id,
                summary=f"{kind}: {title}",
                since=envelope.get("occurred_at"),
                suggested_tool=_SUGGESTED_FOR_KIND.get(kind),
                requires=requires,
                stable_key=stable,
            )
        )
    if malformed:
        raise _SourceUnavailable(f"{malformed} attention spool line(s) are malformed")
    return ("ok" if items else "empty"), items


# ---------------------------------------------------------------------------
# Source: Work Contracts awaiting review acceptance
# ---------------------------------------------------------------------------


def _accepted(records: list[dict[str, Any]], contract_sha256: str, identities: set[str]) -> bool:
    """Same evidence rule as the validator's review-evidence path (operator_contract._check_review)."""
    for rec in records:
        if rec.get("contract_sha256") != contract_sha256 or rec.get("verdict") != "SATISFIED":
            continue
        reviewer = str(rec.get("reviewer") or "")
        if reviewer and reviewer not in identities:
            return True
    return False


def _review_items(root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Delegations waiting on review: executed successfully, review required, no acceptance.

    Selection uses the persisted reconcile state, not a fresh validation. A row
    qualifies when it is ``state='reconciling'`` with no cancellation latch, its
    backend outcome normalizes to ``succeeded``, its persisted ``review`` check is
    FAIL or UNVERIFIED, and every other check the immutable contract requires is
    PASS. A row whose own records cannot be classified (corrupt summary, or a
    missing or mismatched manifest) makes the source unavailable, not silently
    skipped.
    """
    path = delegations._db_path(root)  # noqa: SLF001 - same-package read of the delegation store
    if not path.is_file():
        return "empty", []
    items: list[dict[str, Any]] = []
    with delegations._connect(path, write=False) as db:  # noqa: SLF001
        rows = db.execute(
            "SELECT delegation_id,mission_id,task_id,contract_sha256,outcome,backend_state,"
            "validation_summary_json,updated_at FROM delegations "
            "WHERE state='reconciling' AND cancel_requested=0 AND cancellation_in_progress=0 "
            "ORDER BY updated_at DESC LIMIT ?",
            (_REVIEW_ROW_MAX + 1,),
        ).fetchall()
        if len(rows) > _REVIEW_ROW_MAX:
            raise _SourceUnavailable("more reconciling delegations than the bounded review scan")
        for row in rows:
            if delegations._normalize_state(row["outcome"] or row["backend_state"]) != "succeeded":  # noqa: SLF001
                continue
            summary = delegations.validation_summary(json.loads(row["validation_summary_json"] or "{}"))
            checks = {c["kind"]: c["status"] for c in summary.get("checks", [])}
            if checks.get("review") not in {"FAIL", "UNVERIFIED"}:
                continue
            manifest = delegations._manifest_row(db, row["delegation_id"])  # noqa: SLF001
            contract, sha = contract_mod._contract_from_validation_manifest(manifest)  # noqa: SLF001
            if sha != row["contract_sha256"] or contract["task_id"] != row["task_id"]:
                raise _SourceUnavailable("delegation manifest does not match its lineage")
            if not contract["review_requirements"]["required"]:
                continue
            criteria = contract["completion_criteria"]
            required = {"run_state", "authorization"}
            if criteria["artifacts_present"]:
                required.add("artifacts")
            if criteria["tests_pass"]:
                required.add("tests")
            if criteria["no_forbidden_actions"]:
                required.add("forbidden")
            if any(checks.get(kind) != "PASS" for kind in required):
                continue  # another required check blocks; this is not a pure review wait
            # Same whole-store, per-contract read the validator uses. An unreadable
            # store or an over-limit match raises and makes this source unavailable.
            records = op_review.acceptance_records_for(root, sha)
            if _accepted(records, sha, contract_mod._attributable_identities(contract)):  # noqa: SLF001
                continue
            items.append(
                _item(
                    kind=KIND_REVIEW,
                    mission_id=str(row["mission_id"] or ""),
                    summary=(
                        f"Work Contract {sha[:12]} task {row['task_id']}: execution succeeded; "
                        "review acceptance is required and none is recorded"
                    ),
                    since=row["updated_at"],
                    suggested_tool=TOOL_REVIEW_ACCEPT,
                    requires="review",
                    stable_key=f"{sha}|{row['delegation_id']}",
                )
            )
    return ("ok" if items else "empty"), items


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------


def _read_ack_ids(root: Path) -> set[str]:
    """Item ids acknowledged in the bounded tail (last 1 MiB) of the ack store.

    Raises ``OSError`` when the store cannot be read and ``ValueError`` when a
    record in the window is malformed.
    """
    path = root / ACK_STORE_RELATIVE
    if not path.exists():
        return set()
    size = path.stat().st_size
    start = max(0, size - _ACK_TAIL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line of a tail window may be cut mid-record
    ids: set[str] = set()
    for raw in lines:
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("attention ack store has a malformed line") from exc
        item_id = record.get("item_id") if isinstance(record, dict) else None
        if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
            raise ValueError("attention ack store has an invalid record")
        ids.add(item_id)
    return ids


def acknowledged_ids(hermes_root: Path | None = None) -> set[str]:
    """Return acknowledged item ids (bounded tail read of ``ACK_STORE_RELATIVE``).

    Raises ``OSError`` or ``ValueError`` when the store is unreadable; the caller
    decides how to treat that. The inbox reports the same condition as the
    ``acks`` source being ``unavailable``.
    """
    return _read_ack_ids(missions._root(hermes_root))  # noqa: SLF001 - same resolution as the Mission runtime


# ---------------------------------------------------------------------------
# Public projection
# ---------------------------------------------------------------------------


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return limit


def build_inbox(
    hermes_root: Path | None = None,
    limit: int = DEFAULT_LIMIT,
    include_acknowledged: bool = False,
) -> dict[str, Any]:
    """Aggregate what is waiting on the Owner. Pure read: writes nothing.

    ``limit`` must be an int in 1..200 (``ValueError`` otherwise). Acknowledged
    items are hidden unless ``include_acknowledged`` is true; ``hidden_acknowledged``
    counts the acknowledged items hidden from this result. Items are sorted newest
    first and capped at ``limit``; ``truncated`` reports whether more existed.
    ``complete`` is false whenever any source is ``unavailable``.
    """
    limit = _validate_limit(limit)
    if not isinstance(include_acknowledged, bool):
        raise ValueError("include_acknowledged must be a bool")
    root = missions._root(hermes_root)  # noqa: SLF001 - same resolution as the Mission runtime
    readers = (
        (SOURCE_MISSIONS, _mission_items),
        (SOURCE_AUTOPILOT, _autopilot_items),
        (SOURCE_CONTROLLER, _spool_items),
        (SOURCE_REVIEW, _review_items),
    )
    sources: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for name, reader in readers:
        try:
            status, found = reader(root)
        except Exception:  # noqa: BLE001 - a broken source must fail visible, not crash the inbox
            sources[name] = "unavailable"
            continue
        sources[name] = status
        items.extend(found)

    acked: set[str] = set()
    try:
        acked = _read_ack_ids(root)
        sources[SOURCE_ACKS] = "ok" if acked else "empty"
    except Exception:  # noqa: BLE001 - unreadable acks: every item is treated as unacknowledged
        sources[SOURCE_ACKS] = "unavailable"
        acked = set()
    for item in items:
        item["acknowledged"] = item["item_id"] in acked
    hidden = 0
    if not include_acknowledged:
        hidden = sum(1 for item in items if item["acknowledged"])
        items = [item for item in items if not item["acknowledged"]]

    items.sort(key=_newest_key, reverse=True)
    truncated = len(items) > limit
    items = items[:limit]
    return {
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "limit": limit,
        "sources": {name: sources.get(name, "unavailable") for name in SOURCE_NAMES},
        "complete": all(sources.get(name) != "unavailable" for name in SOURCE_NAMES),
        "hidden_acknowledged": hidden,
        "skipped_sources": list(SKIPPED_SOURCES),
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def hermes_owner_inbox(
    limit: int = DEFAULT_LIMIT,
    include_acknowledged: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """List everything waiting on the Owner: Missions awaiting approval, Autopilot runs
    waiting for Owner input, recent controller attention (budget breaker, RED health,
    no capable target), and Work Contracts awaiting review acceptance. Acknowledged
    items are hidden unless ``include_acknowledged`` is true. Read-only: this lists
    items and suggests the existing tool to use, but it never approves, advances, or
    changes anything. Check ``sources`` and ``complete`` before treating an empty list
    as no pending work."""
    try:
        op.OperatorPolicy().require_level("read_only")
        payload = build_inbox(hermes_root=hermes_root, limit=limit, include_acknowledged=include_acknowledged)
    except ValueError as exc:
        return json.dumps(
            op.error_from_exception(
                exc, layer="operator", code="OWNER_INBOX_INVALID",
                suggested_action="Pass limit as an integer between 1 and 200 and include_acknowledged as a boolean.",
            )
        )
    except PermissionError as exc:
        return json.dumps(
            op.error_from_exception(
                exc, layer="operator", code="OWNER_INBOX_DENIED",
                suggested_action="Enable at least the read_only Operator level.",
            )
        )
    try:
        op.audit_record(
            tool="hermes_owner_inbox",
            level=op.OperatorPolicy().level or "read_only",
            apply_mode="read_only",
            dry_run=True,
            success=True,
            changed=False,
            summary=(
                f"owner inbox read count={payload['count']} complete={payload['complete']} "
                f"hidden_acknowledged={payload['hidden_acknowledged']}"
            ),
        )
    except Exception:  # noqa: BLE001 - audit must never break a read
        pass
    return json.dumps({"success": True, "tool": "hermes_owner_inbox", **payload}, sort_keys=True)


def _ack_error(tid: str, code: str, message: str, action: str) -> str:
    return json.dumps(
        op.make_error_envelope(
            layer="operator",
            code=code,
            safe_message=op.redact_output(message)[:300],
            suggested_action=action,
            trace_id=tid,
        ),
        ensure_ascii=False,
        indent=2,
    )


def _validate_item_ids(item_ids: Any) -> list[str]:
    if not isinstance(item_ids, list):
        raise ValueError("item_ids must be a list of 24-character lowercase hex ids")
    if not 1 <= len(item_ids) <= ACK_MAX_IDS:
        raise ValueError(f"item_ids must contain between 1 and {ACK_MAX_IDS} ids")
    out: list[str] = []
    for value in item_ids:
        if not isinstance(value, str) or not _ITEM_ID_RE.fullmatch(value):
            # Never echo the raw value: it may be arbitrary input.
            raise ValueError("item_ids entries must be 24 lowercase hex characters")
        if value not in out:
            out.append(value)
    return out


def hermes_owner_inbox_ack(
    item_ids: list[str],
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Acknowledge inbox items so they are hidden from the inbox and from notifications.

    Gate: workspace Operator level plus direct apply mode (``require_mutation``), with
    ``dry_run=True`` by default. Acknowledging never approves, transitions or changes
    a Mission, plan, Work Contract or delegation. Only ids present in the current inbox
    (including already-acknowledged ones) are accepted; others are reported as
    ``unknown``. Re-acknowledging an id writes nothing. Audited.
    """
    tool = "hermes_owner_inbox_ack"
    tid = op.new_trace_id()
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
    except PermissionError as exc:
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=bool(dry_run),
            success=False, changed=False, summary="owner inbox ack denied", error=str(exc)[:300],
        )
        return _ack_error(
            tid, "OWNER_INBOX_ACK_DENIED", str(exc),
            "Enable the workspace Operator level with direct apply mode "
            "(HERMES_GPT_OPERATOR_LEVEL=workspace, HERMES_GPT_OPERATOR_APPLY_MODE=direct), or keep dry_run=true.",
        )
    effective_dry = policy.effective_dry_run(dry_run)
    root = missions._root(hermes_root)  # noqa: SLF001

    def rejected(code: str, message: str, action: str) -> str:
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=effective_dry,
            success=False, changed=False, summary=f"owner inbox ack rejected: {code}", error=message[:300],
        )
        return _ack_error(tid, code, message, action)

    try:
        ids = _validate_item_ids(item_ids)
    except ValueError as exc:
        return rejected("INVALID_OWNER_INBOX_ACK", str(exc), "Pass 1 to 50 item_id values from the current inbox.")

    current = build_inbox(hermes_root=root, limit=MAX_LIMIT, include_acknowledged=True)
    if current["sources"][SOURCE_ACKS] == "unavailable":
        return rejected(
            "OWNER_INBOX_ACK_STORE_UNAVAILABLE",
            "the acknowledgement store is unreadable; refusing to write",
            "Repair or move missions/attention_acks.jsonl, then retry.",
        )
    by_id = {item["item_id"]: item for item in current["items"]}
    known = [i for i in ids if i in by_id]
    unknown = [i for i in ids if i not in by_id]
    already = [i for i in known if by_id[i]["acknowledged"]]
    planned = [i for i in known if not by_id[i]["acknowledged"]]

    if effective_dry:
        payload = {
            "success": True,
            "schema_version": ACK_SCHEMA_VERSION,
            "tool": tool,
            "dry_run": True,
            "changed": False,
            "planned": planned,
            "already_acknowledged": already,
            "unknown": unknown,
            "trace_id": tid,
        }
        op.audit_record(
            tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=True,
            success=True, changed=False, summary=f"owner inbox ack planned count={len(planned)}",
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    now = datetime.now(timezone.utc).isoformat()
    with _ACK_LOCK:
        try:
            stored = _read_ack_ids(root)
        except (OSError, ValueError) as exc:
            return rejected(
                "OWNER_INBOX_ACK_STORE_UNAVAILABLE", str(exc),
                "Repair or move missions/attention_acks.jsonl, then retry.",
            )
        to_write = [i for i in planned if i not in stored]
        if to_write:
            path = root / ACK_STORE_RELATIVE
            body = "".join(
                json.dumps({"item_id": i, "acked_at": now, "by": ACK_BY}, sort_keys=True) + "\n"
                for i in to_write
            )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(body)
            except OSError as exc:
                return rejected(
                    "OWNER_INBOX_ACK_STORE_WRITE_ERROR",
                    f"could not write acknowledgement store: {op.redact_output(str(exc))[:200]}",
                    "Check missions/ directory permissions.",
                )
            try:
                path.chmod(0o600)
            except OSError:
                pass
    already_all = already + [i for i in planned if i in stored]
    changed = bool(to_write)
    payload = {
        "success": True,
        "schema_version": ACK_SCHEMA_VERSION,
        "tool": tool,
        "dry_run": False,
        "changed": changed,
        "acknowledged": to_write,
        "already_acknowledged": already_all,
        "unknown": unknown,
        "acked_at": now if changed else None,
        "trace_id": tid,
    }
    op.audit_record(
        tool=tool, level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
        success=True, changed=changed,
        summary=(
            f"owner inbox ack acknowledged={len(to_write)} "
            f"already={len(already_all)} unknown={len(unknown)}"
        ),
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "ACK_STORE_RELATIVE",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "acknowledged_ids",
    "build_inbox",
    "hermes_owner_inbox",
    "hermes_owner_inbox_ack",
]
