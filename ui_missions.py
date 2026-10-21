"""Read-only browser adapters for v0.9 first-class Missions.

This module adds Flight Deck visibility only. It adapts the existing Mission,
Delegation, and live-event read surfaces into browser JSON and does not add any
mutation path or authority.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import operator_autopilot as autopilot
import operator_delegations as delegations
import operator_live_events as live_events
import operator_mission_runtime as missions
import ui_security

_MAX_MISSIONS = 200
_MAX_EVENTS = 100
_MAX_WAIT_MS = 25_000


def _root() -> Any:
    return missions._root(None)


def _decode(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("operator read surface returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("operator read surface returned an invalid payload")
    return payload


def _success(payload: dict[str, Any]) -> bool:
    return bool(payload.get("success"))


def _error(payload: dict[str, Any], *, fallback: str, status: int = 400) -> JSONResponse:
    code = str(payload.get("code") or fallback)[:96]
    message = str(payload.get("safe_message") or payload.get("error") or "Mission read failed")[:500]
    return ui_security.err(code, message, status_code=status)


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def _mission_list(request: Request) -> JSONResponse:
    limit = _clamp(request.query_params.get("limit"), 100, 1, _MAX_MISSIONS)
    status = str(request.query_params.get("status") or "")[:64]
    payload = _decode(missions.hermes_mission_list(status=status, limit=limit, hermes_root=_root()))
    if not _success(payload):
        return _error(payload, fallback="MISSION_LIST_FAILED")
    data = {
        "schema_version": payload.get("schema_version"),
        "missions": payload.get("missions") if isinstance(payload.get("missions"), list) else [],
        "count": int(payload.get("count") or 0),
        "live_cursor": live_events.high_watermark(_root()),
        "read_only": True,
    }
    return JSONResponse(ui_security.ok(data))


def _mission_detail(request: Request) -> JSONResponse:
    mission_id = str(request.path_params.get("mission_id") or "")[:128]
    # Capture the event cursor before reading durable Mission state. An event
    # racing with this read will therefore either already be represented in the
    # snapshot or remain visible to the browser's next long-poll; it cannot be
    # skipped by advancing the cursor after the snapshot.
    start_cursor = live_events.high_watermark(_root())
    payload = _decode(missions.hermes_mission_get(mission_id, hermes_root=_root()))
    if not _success(payload):
        message = str(payload.get("safe_message") or payload.get("error") or "")
        if "not found" in message.lower():
            return ui_security.err("MISSION_NOT_FOUND", "Mission was not found", status_code=404)
        return _error(payload, fallback="MISSION_READ_FAILED")
    if payload.get("found") is False:
        return ui_security.err("MISSION_NOT_FOUND", "Mission was not found", status_code=404)

    delegation_payload = _decode(
        delegations.hermes_delegation_list(mission_id=mission_id, limit=200, hermes_root=_root())
    )
    delegation_rows = (
        delegation_payload.get("delegations")
        if _success(delegation_payload) and isinstance(delegation_payload.get("delegations"), list)
        else []
    )
    data = {
        "mission": {key: value for key, value in payload.items() if key != "success"},
        "delegations": delegation_rows,
        "delegation_count": len(delegation_rows),
        "live_cursor": start_cursor,
        "read_only": True,
    }
    return JSONResponse(ui_security.ok(data))


async def _mission_events(request: Request) -> JSONResponse:
    mission_id = str(request.path_params.get("mission_id") or "")[:128]
    cursor = _clamp(request.query_params.get("cursor"), 0, 0, 2**63 - 1)
    limit = _clamp(request.query_params.get("limit"), 100, 1, _MAX_EVENTS)
    wait_ms = _clamp(request.query_params.get("wait_ms"), 0, 0, _MAX_WAIT_MS)
    payload = await run_in_threadpool(
        live_events.hermes_live_events_since,
        cursor,
        mission_id,
        "",
        "",
        limit,
        wait_ms,
        _root(),
    )
    decoded = _decode(payload)
    if not _success(decoded):
        return _error(decoded, fallback="LIVE_EVENT_READ_FAILED")
    data = {
        "cursor": int(decoded.get("cursor") or 0),
        "next_cursor": int(decoded.get("next_cursor") or cursor),
        "high_watermark": int(decoded.get("high_watermark") or 0),
        "events": decoded.get("events") if isinstance(decoded.get("events"), list) else [],
        "count": int(decoded.get("count") or 0),
        "read_only": True,
    }
    return JSONResponse(ui_security.ok(data))


# Explicit allow-list. The stored run record is orchestration metadata, but the
# browser gets only these fields so a field added to it later is never exposed
# by accident. No pid, config hash or raw recovery internals.
_RUN_FIELDS = (
    "state", "attempt", "started_at", "updated_at", "last_tick_at", "last_error", "last_wake",
    "max_concurrency", "max_replans", "replans_used", "max_attempts_per_node", "max_runtime_seconds",
    "last_event_cursor", "wakeups",
)
_SCHEDULE_FIELDS = (
    "plan_version", "slots", "in_flight", "dispatched", "adopted", "completed", "failed_nodes", "retried",
    "replanned", "held", "observed_held", "advanced", "failed", "skipped", "frontier", "limit", "budget",
    "mission_status",
)


_SUMMARY_KEYS = ("available", "mission_status", "plan_version", "progress", "frontier", "budget", "limits", "wake",
                 "needs_owner")


def _project_summary(summary: Any) -> dict[str, Any]:
    """Allow-list of the derived summary. Worker peers and delegation ids stay out of the browser."""
    if not isinstance(summary, dict) or not summary.get("available"):
        return {"available": False}
    projected = {key: summary.get(key) for key in _SUMMARY_KEYS if key in summary}
    projected["workers"] = [
        {"node_id": w.get("node_id"), "state": w.get("state"), "attempt": w.get("attempt"),
         "delegation_state": w.get("delegation_state"),
         "validation": delegations.validation_summary(w.get("validation")),
         "validation_failure_since": w.get("validation_failure_since", "")}
        for w in (summary.get("workers") or []) if isinstance(w, dict)
    ]
    projected["attention"] = [
        {"code": a.get("code"), "severity": a.get("severity"), "nodes": list(a.get("nodes") or [])}
        for a in (summary.get("attention") or []) if isinstance(a, dict)
    ]
    recovery = summary.get("recovery") if isinstance(summary.get("recovery"), dict) else {}
    projected["recovery"] = {key: recovery.get(key) for key in (
        "retries", "replans_used", "max_replans", "max_attempts_per_node", "superseded_nodes",
        "pending_supersede", "replan_pending", "failed_nodes") if key in recovery}
    return projected


def _project_run(run: dict[str, Any]) -> dict[str, Any]:
    projected = {key: run.get(key) for key in _RUN_FIELDS if key in run}
    schedule = run.get("last_schedule")
    if isinstance(schedule, dict):
        projected["last_schedule"] = {key: schedule.get(key) for key in _SCHEDULE_FIELDS if key in schedule}
    recovery = run.get("recovery") if isinstance(run.get("recovery"), dict) else {}
    projected["recovery"] = {
        "superseded_nodes": dict(recovery.get("superseded_nodes") or {}),
        "pending_supersede": sorted((recovery.get("pending_supersede") or {}).keys()),
        "replan_pending": sorted((recovery.get("replan_pending") or {}).keys()),
    }
    return projected


def _mission_autopilot(request: Request) -> JSONResponse:
    mission_id = str(request.path_params.get("mission_id") or "")[:128]
    # Same ordering rule as the Mission detail route: capture the event cursor
    # first so a change racing with this read either is in the snapshot or wakes
    # the browser for another durable read.
    start_cursor = live_events.high_watermark(_root())
    try:
        view = autopilot.observe_status(mission_id, _root())
    except PermissionError as exc:
        return ui_security.err("AUTOPILOT_READ_DENIED", str(exc)[:500], status_code=403)
    except (ValueError, LookupError, OSError) as exc:
        return ui_security.err("AUTOPILOT_READ_FAILED", str(exc)[:500], status_code=400)
    data: dict[str, Any] = {"mission_id": mission_id, "found": view is not None, "live_cursor": start_cursor,
                            "read_only": True}
    if view is not None:
        data.update({
            "effective_state": view["effective_state"],
            "stale": view["stale"],
            "worker": view["worker"],
            "run": _project_run(view["run"]),
            "summary": _project_summary(view.get("summary")),
        })
    return JSONResponse(ui_security.ok(data))


def _delegation_detail(request: Request) -> JSONResponse:
    delegation_id = str(request.path_params.get("delegation_id") or "")[:128]
    payload = _decode(delegations.hermes_delegation_get(delegation_id, hermes_root=_root()))
    if not _success(payload):
        return _error(payload, fallback="DELEGATION_GET_FAILED", status=404)
    return JSONResponse(ui_security.ok({"delegation": payload.get("delegation"), "read_only": True}))


def ui_missions_routes() -> list[Route]:
    return [
        Route("/api/ops/missions", _mission_list, methods=["GET"]),
        Route("/api/ops/missions/{mission_id}/events", _mission_events, methods=["GET"]),
        Route("/api/ops/missions/{mission_id}/autopilot", _mission_autopilot, methods=["GET"]),
        Route("/api/ops/missions/{mission_id}", _mission_detail, methods=["GET"]),
        Route("/api/ops/delegations/{delegation_id}", _delegation_detail, methods=["GET"]),
    ]


__all__ = ["ui_missions_routes"]
