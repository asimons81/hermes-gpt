"""Read-only Owner inbox: one place that lists what is waiting on the Owner.

This module is a pure projection over EXISTING read paths. It never writes,
never heals, never approves and never advances anything; it adds no authority.
Sources (each reported as ``ok`` | ``empty`` | ``unavailable`` in ``sources``):

- ``missions``: first-class Missions in ``awaiting_approval`` (via
  ``operator_mission_runtime.hermes_mission_list``). Suggested action:
  ``hermes_mission_approve`` (Owner).
- ``autopilot``: Autopilot runs whose effective state is ``waiting_for_owner`` or
  whose derived summary reports ``needs_owner`` (via
  ``operator_autopilot.observe_status``, the non-writing Autopilot read).
- ``controller_attention``: the newest controller attention envelopes in the
  spool ``missions/controller_attention_spool.jsonl`` (budget breaker, RED
  health, ``no_capable_target``), read with a bounded tail read. The spool has no
  acknowledgement record, so every spooled envelope in the window is treated as
  unacknowledged.

Skipped: Work Contracts awaiting review acceptance. ``operator_review`` only
stores completed acceptance records, and no cheap pending-review read path
exists; ``skipped_sources`` names it so the omission is visible.

A source that raises or finds an unreadable record is reported ``unavailable``
and ``complete`` becomes false: the inbox never claims emptiness it did not
verify.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_autopilot as autopilot
import operator_controller as controller
import operator_mission_runtime as missions
import operator_policy as op
import ui_security

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

SOURCE_MISSIONS = "missions"
SOURCE_AUTOPILOT = "autopilot"
SOURCE_CONTROLLER = "controller_attention"
SOURCE_NAMES: tuple[str, ...] = (SOURCE_MISSIONS, SOURCE_AUTOPILOT, SOURCE_CONTROLLER)
SKIPPED_SOURCES: tuple[str, ...] = ("work_contract_review",)

KIND_MISSION = "mission_awaiting_approval"
KIND_AUTOPILOT = "autopilot_waiting_owner"
KIND_BUDGET = "budget_breaker"
KIND_RED = "red_health"
KIND_NO_TARGET = "no_capable_target"

TOOL_MISSION_APPROVE = "hermes_mission_approve"
TOOL_NODE_TRANSITION = "hermes_plan_node_transition"
TOOL_BUDGET_CHECK = "hermes_budget_check"
TOOL_CONTROLLER_STATUS = "hermes_controller_status"

_SPOOL_TAIL_BYTES = 256 * 1024
_SPOOL_MAX_LINES = 200
_RUN_FILE_MAX_BYTES = 1024 * 1024
_RUN_SCAN_MAX = 200
_SUMMARY_MAX = 240
_NODE_LIST_MAX = 8


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


def _item(
    *,
    kind: str,
    mission_id: str,
    summary: str,
    since: Any,
    suggested_tool: str | None,
    requires: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "mission_id": str(mission_id or "")[:128],
        "summary": _bounded(summary),
        "since": str(since) if since else None,
        "suggested_tool": suggested_tool,
        "requires": requires,
    }


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
        nodes = [str(n) for n in (frontier.get("nodes") or [])][:_NODE_LIST_MAX]
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
        items.append(
            _item(
                kind=kind,
                mission_id=mission_id,
                summary=f"{kind}: {title}",
                since=envelope.get("occurred_at"),
                suggested_tool=_SUGGESTED_FOR_KIND.get(kind),
                requires=requires,
            )
        )
    if malformed:
        raise _SourceUnavailable(f"{malformed} attention spool line(s) are malformed")
    return ("ok" if items else "empty"), items


# ---------------------------------------------------------------------------
# Public projection
# ---------------------------------------------------------------------------


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return limit


def build_inbox(hermes_root: Path | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Aggregate what is waiting on the Owner. Pure read: writes nothing.

    ``limit`` must be an int in 1..200 (``ValueError`` otherwise). Items are sorted
    newest first and capped at ``limit``; ``truncated`` reports whether more
    existed. ``complete`` is false whenever any read source is ``unavailable``.
    """
    limit = _validate_limit(limit)
    root = missions._root(hermes_root)  # noqa: SLF001 - same resolution as the Mission runtime
    readers = (
        (SOURCE_MISSIONS, _mission_items),
        (SOURCE_AUTOPILOT, _autopilot_items),
        (SOURCE_CONTROLLER, _spool_items),
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
        "skipped_sources": list(SKIPPED_SOURCES),
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def hermes_owner_inbox(limit: int = DEFAULT_LIMIT, hermes_root: Path | None = None) -> str:
    """List everything waiting on the Owner: Missions awaiting approval, Autopilot runs
    waiting for Owner input, and recent controller attention (budget breaker, RED health,
    no capable target). Read-only: this lists items and suggests the existing tool to use,
    but it never approves, advances, or changes anything. Check ``sources`` and ``complete``
    before treating an empty list as no pending work."""
    try:
        op.OperatorPolicy().require_level("read_only")
        payload = build_inbox(hermes_root=hermes_root, limit=limit)
    except ValueError as exc:
        return json.dumps(
            op.error_from_exception(
                exc, layer="operator", code="OWNER_INBOX_INVALID",
                suggested_action="Pass limit as an integer between 1 and 200.",
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
            summary=f"owner inbox read count={payload['count']} complete={payload['complete']}",
        )
    except Exception:  # noqa: BLE001 - audit must never break a read
        pass
    return json.dumps({"success": True, "tool": "hermes_owner_inbox", **payload}, sort_keys=True)


__all__ = ["build_inbox", "hermes_owner_inbox", "DEFAULT_LIMIT", "MAX_LIMIT"]
