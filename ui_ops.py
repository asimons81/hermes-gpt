"""Browser-facing operator adapters for the Hermes GPT Flight Deck (v0.7).

This module is the **backend half of the Flight Deck integration card**
(kanban t_1135e15b). It adapts the existing, shipped ``operator_*``
capability to browser JSON — it never re-implements a surface and never
adds authority. Read surfaces call the **dict-returning** internal
functions (e.g. ``operator_mission.hermes_mission_health``) rather than the
``*_tool`` string wrappers; JSON-string surfaces (overview, events, swarm,
cron, fleet, review, oauth) are parsed at the adapter boundary.

Mutations are funneled through ONE endpoint, ``POST /api/ops/action``,
which executes the **existing gated ``hermes_*`` tool path** (operator level
check, dry-run-first, confirm gate, audit). The browser UI has no bypass:
the adapter forwards only an allowlisted arg set, never weakens a gate, and
maps operator confirmation gates to ``409 CONFIRM_REQUIRED`` dialogs.

Route list is exposed via ``ui_ops_routes()`` and composed by ``ui_api.py``.

Architecture contract: docs/design/v0.7-flight-deck-ui-contract.md (D2-A:
read-only presentation layer; no new authority). Adapter gaps that would
require new backend surface code are tracked as follow-up cards, never
silently added here.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import operator_policy as op
import operator_mission as op_mission
import operator_events as op_events
import operator_swarm as op_swarm
import operator_codex as op_codex
import operator_cron as op_cron
import operator_fleet as op_fleet
import operator_review as op_review
import operator_diagnostics as op_diagnostics
import operator_recovery as op_recovery
import operator_oauth as op_oauth
import operator_owner_inbox as op_owner_inbox
import operator_mission_plan as op_plan
import operator_mission_runtime as op_mission_rt
import operator_autopilot as op_autopilot
import ui_security
from urllib.parse import urlsplit

from versioning import VERSION

# The 12 Mission Control surfaces (must match operator_mission.MISSION_SURFACES).
MISSION_SURFACES: tuple[str, ...] = op_mission.MISSION_SURFACES

# 11 of the 12 surfaces have dict-returning builders. ``overview`` returns a
# bounded JSON string and is handled separately.
_SURFACE_DICT_FNS: dict[str, Callable[..., dict[str, Any]]] = {
    "health": op_mission.hermes_mission_health,
    "cron": op_mission.hermes_mission_cron,
    "fleet": op_mission.hermes_mission_fleet,
    "audit": op_mission.hermes_mission_audit,
    "profiles": op_mission.hermes_mission_profiles,
    "delegations": op_mission.hermes_mission_delegations,
    "failures": op_mission.hermes_mission_failures,
    "approvals": op_mission.hermes_mission_approvals,
    "codex": op_mission.hermes_mission_codex,
    "vault": op_mission.hermes_mission_vault,
    "usage": op_mission.hermes_mission_usage,
}

# Max review-acceptance records surfaced to the browser (bounded read-model).
_REVIEW_RECORD_LIMIT = 200
# Max workflow rows surfaced in the contracts list read-model.
_CONTRACT_WORKFLOW_LIMIT = 50


# ---------------------------------------------------------------------------
# Envelope helpers (interface-contracts.md §4, §7)
# ---------------------------------------------------------------------------


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(code: str, message: str, status: int = 400, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if extra:
        err.update(extra)
    return {"ok": False, "error": err}


def _json_resp(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(parsed, hi))


def _resolve_root() -> Any:
    """Resolve the active Hermes data root (mirrors server.py defaults)."""
    return op_mission._resolve_root(None)  # noqa: SLF001 - same-package reuse


def _parse_payload(result: Any) -> dict[str, Any]:
    """Parse a tool result that may be a JSON string or a dict."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError(f"operator tool returned non-JSON payload: {op.redact_output(str(exc))[:200]}") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("operator tool returned a non-object JSON payload")
    raise ValueError(f"operator tool returned unsupported payload type {type(result).__name__}")


def _audit(tool: str, *, success: bool, summary: str, extra: dict[str, Any] | None = None) -> None:
    """Best-effort adapter-level audit record (never breaks a request)."""
    try:
        policy = op.OperatorPolicy()
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=bool(success),
            changed=False,
            summary=str(summary)[:500],
            extra=extra or {},
        )
    except Exception:  # noqa: BLE001 - audit must never break the surface
        pass


# ---------------------------------------------------------------------------
# Mission Control surfaces: GET /api/ops/{surface}
# ---------------------------------------------------------------------------


def _surface_payload(surface: str, force_refresh: bool) -> dict[str, Any]:
    """Return the redacted mission envelope for one surface.

    ``overview`` is a composite bounded JSON string (parsed here); the other
    eleven surfaces return dicts directly. Allowlist denial mirrors the
    mission module's ``_mission_denied`` envelope so the UI renders
    "not allowed for this client" as a state, not an error.
    """
    root = _resolve_root()
    tid = op.new_trace_id()
    if not op_mission._surface_allowed(surface):  # noqa: SLF001
        denied = op_mission._mission_denied(tool=f"hermes_mission_{surface}", surface=surface)  # noqa: SLF001
        _audit(f"hermes_mission_{surface}", success=False, summary=f"denied:{surface}", extra={"trace_id": tid})
        return denied
    try:
        if surface == "overview":
            raw = op_mission.hermes_mission_overview(  # noqa: SLF001
                hermes_root=root, force_refresh=force_refresh, trace_id=tid
            )
            payload = _parse_payload(raw)
        else:
            payload = _SURFACE_DICT_FNS[surface](hermes_root=root, trace_id=tid)
    except Exception as exc:  # noqa: BLE001 - surfaces degrade gracefully
        payload = op_mission._mission_error(  # noqa: SLF001
            tool=f"hermes_mission_{surface}",
            surface=surface,
            code="MISSION_SURFACE_ERROR",
            safe_message=op.redact_output(str(exc))[:300],
            trace_id=tid,
        )
    _audit(
        f"hermes_mission_{surface}",
        success=bool(payload.get("success", True)),
        summary=f"surface:{surface}",
        extra={"trace_id": payload.get("trace_id") or tid},
    )
    return payload


def _ops_surface(request: Request) -> JSONResponse:
    """GET /api/ops/{surface} — one of the 12 Mission Control surfaces."""
    surface = str(request.path_params.get("surface", "")).strip()
    force_refresh = str(request.query_params.get("force_refresh", "")).lower() in ("1", "true", "yes")
    if surface not in MISSION_SURFACES:
        return _json_resp(_err("NOT_FOUND", f"unknown surface {surface!r}"), 404)
    payload = _surface_payload(surface, force_refresh)
    return _json_resp(
        _ok(
            {
                "surface": surface,
                "fetched_at": payload.get("generated_at") or _now_iso(),
                "ttl": op_mission.SURFACE_TTL.get(surface),
                "data": payload,
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# Event History: GET /api/events
# ---------------------------------------------------------------------------


def _events(request: Request) -> JSONResponse:
    """GET /api/events — normalized timeline (wraps hermes_events_query/tail)."""
    params = request.query_params
    mode = str(params.get("mode", "query")).strip().lower()
    limit = _clamp_int(params.get("limit"), 50, 1, op_events.MAX_QUERY_LIMIT if mode != "tail" else op_events.MAX_TAIL_LIMIT)
    root = _resolve_root()
    try:
        if mode == "tail":
            raw = op_events.hermes_events_tail(limit=limit, hermes_root=root)
        else:
            raw = op_events.hermes_events_query(
                source=str(params.get("source", "")).strip(),
                subject_id=str(params.get("subject_id", "")).strip(),
                kind=str(params.get("kind", "")).strip(),
                since=str(params.get("since", "")).strip(),
                until=str(params.get("until", "")).strip(),
                limit=limit,
                hermes_root=root,
            )
        payload = _parse_payload(raw)
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"events query failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_events_query", success=True, summary=f"events mode={mode} limit={limit}")
    return _json_resp(_ok(payload), 200)


# ---------------------------------------------------------------------------
# Status reads: contracts / swarm / codex / cron / fleet / review / account
# ---------------------------------------------------------------------------


def _read_review_records(root: Any, limit: int = _REVIEW_RECORD_LIMIT) -> list[dict[str, Any]]:
    """Read review-acceptance records (newest first) from the existing store."""
    records = op_review.read_review_acceptances(root, limit=limit)
    return list(reversed(records))


def _contracts_list(request: Request) -> JSONResponse:
    """GET /api/ops/contracts — contract evidence read-model.

    Contracts are declarative JSON documents with no persistent store (M1
    D10), so the browser-facing "contracts" read-model composes the two
    durable contract-bearing stores that exist: review-acceptance records
    and swarm workflow instances (each stage carries a contract_sha256).
    """
    root = _resolve_root()
    try:
        acceptances = _read_review_records(root)
        workflows_raw = _parse_payload(op_swarm.hermes_swarm_workflow_list(hermes_root=root))
        workflows = workflows_raw.get("workflows", []) if workflows_raw.get("success", True) else []
        payload = {
            "success": True,
            "source": "adapter-composed (review-evidence + swarm workflows)",
            "count": len(acceptances),
            "review_acceptances": acceptances[:_REVIEW_RECORD_LIMIT],
            "workflows": workflows[:_CONTRACT_WORKFLOW_LIMIT],
            "generated_at": _now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"contracts read failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_contract_status", success=True, summary="contracts list read-model")
    return _json_resp(_ok(payload), 200)


def _contracts_detail(request: Request) -> JSONResponse:
    """GET /api/ops/contracts/{contract_sha256} — evidence for one contract."""
    sha = str(request.path_params.get("contract_sha256", "")).strip().lower()
    root = _resolve_root()
    try:
        acceptances = [r for r in _read_review_records(root, limit=500) if r.get("contract_sha256") == sha]
        workflows_raw = _parse_payload(op_swarm.hermes_swarm_workflow_list(hermes_root=root))
        workflows = workflows_raw.get("workflows", []) if workflows_raw.get("success", True) else []
        matching_workflows: list[dict[str, Any]] = []
        for wf in workflows[:_CONTRACT_WORKFLOW_LIMIT]:
            stages = wf.get("stages") if isinstance(wf.get("stages"), list) else []
            if any(st.get("contract_sha256") == sha for st in stages if isinstance(st, dict)):
                matching_workflows.append(
                    {"workflow_id": wf.get("workflow_id"), "title": wf.get("title"), "status": wf.get("status")}
                )
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"contract detail failed: {op.redact_output(str(exc))[:300]}"), 500)
    if not acceptances and not matching_workflows:
        return _json_resp(_err("NOT_FOUND", f"no evidence found for contract {sha[:16]}…"), 404)
    payload = {
        "success": True,
        "contract_sha256": sha,
        "review_acceptances": acceptances[:_REVIEW_RECORD_LIMIT],
        "workflows": matching_workflows,
        "generated_at": _now_iso(),
    }
    _audit("hermes_contract_status", success=True, summary=f"contract detail {sha[:16]}")
    return _json_resp(_ok(payload), 200)


def _review_detail(request: Request) -> JSONResponse:
    """GET /api/ops/review/{contract_sha256} — review-acceptance records."""
    sha = str(request.path_params.get("contract_sha256", "")).strip().lower()
    root = _resolve_root()
    try:
        acceptances = [r for r in _read_review_records(root, limit=500) if r.get("contract_sha256") == sha]
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"review read failed: {op.redact_output(str(exc))[:300]}"), 500)
    payload = {"success": True, "contract_sha256": sha, "count": len(acceptances), "records": acceptances}
    _audit("operator_review.read_review_acceptances", success=True, summary=f"review records {sha[:16]}")
    return _json_resp(_ok(payload), 200)


def _swarm_list(request: Request) -> JSONResponse:
    """GET /api/ops/swarm — workflow instances (read-only)."""
    root = _resolve_root()
    try:
        payload = _parse_payload(op_swarm.hermes_swarm_workflow_list(hermes_root=root))
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"swarm list failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_swarm_workflow_list", success=True, summary="swarm list")
    return _json_resp(_ok(payload), 200)


def _swarm_detail(request: Request) -> JSONResponse:
    """GET /api/ops/swarm/{workflow_id} — one workflow's stage map."""
    workflow_id = str(request.path_params.get("workflow_id", "")).strip()
    root = _resolve_root()
    try:
        payload = _parse_payload(op_swarm.hermes_swarm_workflow_status(workflow_id=workflow_id, hermes_root=root))
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"swarm status failed: {op.redact_output(str(exc))[:300]}"), 500)
    if not payload.get("success", True) and payload.get("code") == "WORKFLOW_NOT_FOUND":
        return _json_resp(_err("NOT_FOUND", payload.get("safe_message") or f"workflow {workflow_id!r} not found"), 404)
    _audit("hermes_swarm_workflow_status", success=True, summary=f"swarm status {workflow_id[:40]}")
    return _json_resp(_ok(payload), 200)


def _codex_detail(request: Request) -> JSONResponse:
    """GET /api/ops/codex/{job_id} — one codex job + capped redacted result."""
    job_id = str(request.path_params.get("job_id", "")).strip()
    root = _resolve_root()
    try:
        status = op_codex.hermes_codex_job_status(job_id=job_id, hermes_root=root)
        if not status.get("success", True):
            if status.get("code") == "JOB_NOT_FOUND":
                return _json_resp(_err("NOT_FOUND", status.get("message") or f"codex job {job_id!r} not found"), 404)
            return _json_resp(_ok(status), 200)
        result = op_codex.hermes_codex_job_result(job_id=job_id, hermes_root=root)
        payload = {"success": True, "job": status.get("job"), "result": result}
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"codex job status failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_codex_job_status", success=True, summary=f"codex job {job_id[:40]}")
    return _json_resp(_ok(payload), 200)


def _cron_detail(request: Request) -> JSONResponse:
    """GET /api/ops/cron/{job_id} — one cron job (from the list read-model)."""
    job_id = str(request.path_params.get("job_id", "")).strip()
    root = _resolve_root()
    try:
        payload = _parse_payload(op_cron.hermes_cron_list(profile="default", include_disabled=True, hermes_root=root))
        jobs = payload.get("jobs", []) if payload.get("success", True) else []
        job = next((j for j in jobs if str(j.get("id") or "") == job_id), None)
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"cron detail failed: {op.redact_output(str(exc))[:300]}"), 500)
    if job is None:
        return _json_resp(_err("NOT_FOUND", f"cron job {job_id!r} not found"), 404)
    _audit("hermes_cron_list", success=True, summary=f"cron job {job_id[:40]}")
    return _json_resp(_ok({"success": True, "job": job}), 200)


def _owner_inbox(request: Request) -> JSONResponse:
    """GET /api/ops/owner-inbox — read-only projection of what waits on the Owner.

    Read-only: lists items and suggested existing tools; never approves or
    advances. Gated by the ``approvals`` Mission Control allowlist entry, since
    the inbox aggregates Mission approval state.
    """
    raw_limit = request.query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit not in (None, "") else op_owner_inbox.DEFAULT_LIMIT
    except ValueError:
        return _json_resp(_err("INVALID_LIMIT", "limit must be an integer between 1 and 200", 400), 400)
    if not op_mission._surface_allowed("approvals"):  # noqa: SLF001
        return _json_resp(ui_security.error_envelope("AUTHZ_DENIED", "Owner inbox is not allowed for this client."), 403)
    try:
        payload = op_owner_inbox.build_inbox(hermes_root=_resolve_root(), limit=limit)
    except ValueError as exc:
        return _json_resp(_err("INVALID_LIMIT", op.redact_output(str(exc))[:200], 400), 400)
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"owner inbox failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_owner_inbox", success=True, summary=f"owner inbox count={payload['count']} complete={payload['complete']}")
    return _json_resp(ui_security.ok(payload), 200)


def _account(request: Request) -> JSONResponse:
    """GET /api/ops/account — operator policy + OAuth store status + version.

    Never contains token material: oauth_status exposes presence/expiry only.
    """
    root = _resolve_root()
    try:
        policy_summary = op.OperatorPolicy().to_summary()
        oauth_payload = _parse_payload(op_oauth.hermes_oauth_status(hermes_root=root))
        oauth_store = oauth_payload.get("store", {}) if oauth_payload.get("success", True) else {"available": False}
        payload = {
            "success": True,
            "policy": policy_summary,
            "oauth": oauth_store,
            "server_version": VERSION,
            "generated_at": _now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"account status failed: {op.redact_output(str(exc))[:300]}"), 500)
    _audit("hermes_oauth_status", success=True, summary="account status")
    return _json_resp(_ok(payload), 200)


# ---------------------------------------------------------------------------
# Gated mutations: POST /api/ops/action {tool, args, dry_run?}
# ---------------------------------------------------------------------------
#
# Each entry maps a browser-visible tool name to the EXISTING operator
# function that enforces level checks, dry-run-first, confirm gates, and
# audit. ``args`` is an allowlist: anything else is rejected with
# INVALID_ARGS, so the UI cannot smuggle hermes_root / runner / internal
# kwargs. The adapter never sets confirm=True on the client's behalf; if a
# gate is pending the operator returns CONFIRMATION_REQUIRED and the adapter
# surfaces 409 CONFIRM_REQUIRED for the UI's confirm dialog.


class _MutationSpec:
    __slots__ = ("fn", "args", "required_level", "confirm_gated", "long_running", "apply_flag", "precheck")

    def __init__(
        self,
        fn: Callable[..., Any],
        args: set[str],
        *,
        required_level: str,
        confirm_gated: bool = False,
        long_running: bool = False,
        apply_flag: bool = False,
        precheck: Callable[[dict[str, Any], Any, bool], JSONResponse | None] | None = None,
    ) -> None:
        self.fn = fn
        self.args = set(args)
        self.required_level = required_level
        self.confirm_gated = confirm_gated
        self.long_running = long_running
        # apply_flag tools use an ``apply`` boolean instead of dry_run/confirm
        # (hermes_operator_recover / hermes_swarm_reconcile).
        self.apply_flag = apply_flag
        # Optional read-only check run before the gated function (both dry and
        # direct). It may refuse (return a JSONResponse) or add server-derived
        # kwargs to ``kwargs`` in place. Never client-controlled.
        self.precheck = precheck


# Plan node states from which an approval frontier is resolved. Approve is no
# longer a browser transition: it is ``hermes_plan_approve_gate``, which walks
# the legal path to completed. Reject stays a browser transition, and only from
# ``awaiting_approval`` to ``failed``. ``paused`` is legal in the state machine
# but does not resolve the frontier, so it is never a browser target.
_APPROVAL_REJECT_TARGET = "failed"
_ACK_MAX_IDS = 50


def _refuse_approval(tool: str, message: str) -> JSONResponse:
    code = "APPROVAL_GATE_REFUSED" if tool == "hermes_plan_approve_gate" else "APPROVAL_TRANSITION_REFUSED"
    return _json_resp(_err(code, message, 400, extra={"tool": tool}), 400)


def _refuse_approval_transition(message: str) -> JSONResponse:
    return _refuse_approval("hermes_plan_node_transition", message)


def _read_gated_plan_node(
    kwargs: dict[str, Any], root: Any, tool: str
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Read-only lookup of the plan node a browser approval request names.

    Returns ``(node, None)`` for a gated node on a readable plan, or
    ``(None, refusal)``. The node is always re-derived here from the plan, never
    trusted from the request.
    """
    mission_id = kwargs.get("mission_id")
    node_id = kwargs.get("node_id")
    if not all(isinstance(v, str) and v for v in (mission_id, node_id)):
        return None, _json_resp(_err("INVALID_ARGS", "mission_id and node_id must be non-empty strings", 400), 400)
    try:
        plan = _parse_payload(op_plan.hermes_plan_get(mission_id=mission_id, hermes_root=root))
    except Exception:  # noqa: BLE001 - fail closed; never echo internals
        return None, _refuse_approval(tool, "the mission plan could not be read")
    if plan.get("success") is not True or plan.get("found") is False:
        return None, _refuse_approval(tool, "no mission plan was found for this mission")
    node = next(
        (n for n in (plan.get("nodes") or []) if isinstance(n, dict) and n.get("node_id") == node_id),
        None,
    )
    if node is None:
        return None, _refuse_approval(tool, "the node is not part of this mission plan")
    cap = node.get("capability_req") if isinstance(node.get("capability_req"), dict) else {}
    gated = node.get("kind") == op_plan.KIND_APPROVAL or cap.get("authorization_class") == "high_impact"
    if not gated:
        return None, _refuse_approval(tool, "the node is not an approval or high_impact node")
    version = plan.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None, _refuse_approval(tool, "the mission plan version could not be read")
    node = {**node, "_plan_version": version, "_plan_nodes": plan.get("nodes") or []}
    return node, None


def _precheck_approval_transition(kwargs: dict[str, Any], root: Any, dry_run: bool) -> JSONResponse | None:
    """Restrict browser ``hermes_plan_node_transition`` to rejecting an approval frontier.

    Browser-only restriction layered on the same tool: the node must be an
    ``approval`` node or have ``capability_req.authorization_class ==
    high_impact``, must currently be ``awaiting_approval``, and the target must
    be ``failed`` (reject). Approve goes through ``hermes_plan_approve_gate``.
    The browser path also requires Owner Mode (the inbox item's ``requires`` is
    ``owner``). The plan ``version`` is passed as ``expected_plan_version`` so a
    plan replaced in between is refused by the tool's own compare-and-swap.
    """
    try:
        op.OperatorPolicy().require_owner(dry_run)
    except PermissionError as exc:
        return _json_resp(
            _err("LEVEL_REQUIRED", op.redact_output(str(exc))[:500], 403,
                 extra={"required": "owner", "tool": "hermes_plan_node_transition"}),
            403,
        )
    target = kwargs.get("target_state")
    if target != _APPROVAL_REJECT_TARGET:
        return _refuse_approval_transition(
            "target_state must be failed (reject); approve with hermes_plan_approve_gate"
        )
    node, refusal = _read_gated_plan_node(kwargs, root, "hermes_plan_node_transition")
    if refusal is not None or node is None:
        return refusal
    # Reject a reached gate from any state the plan state machine lets fail;
    # Autopilot leaves a reached gate in ``pending``.
    states = {n.get("node_id"): n.get("state") for n in node["_plan_nodes"] if isinstance(n, dict)}
    if any(states.get(parent) != "completed" for parent in node.get("parents") or []):
        return _refuse_approval_transition("the gate has not been reached (a parent is not completed)")
    if "failed" not in op_plan.NODE_TRANSITIONS.get(str(node.get("state")), ()):
        return _refuse_approval_transition("the node cannot be rejected from its current state")
    kwargs["expected_plan_version"] = node["_plan_version"]
    return None


def _precheck_approval_gate(kwargs: dict[str, Any], root: Any, dry_run: bool) -> JSONResponse | None:
    """Browser precheck for ``hermes_plan_approve_gate`` (Owner Mode, gated node).

    The node must be an approval or high_impact node, on the forward path
    (``op_plan.GATE_APPROVABLE_STATES``), with every parent completed. The tool
    re-checks the same conditions and its own CAS; this only refuses early and
    fixes ``expected_plan_version`` from the plan read here.
    """
    try:
        op.OperatorPolicy().require_owner(dry_run)
    except PermissionError as exc:
        return _json_resp(
            _err("LEVEL_REQUIRED", op.redact_output(str(exc))[:500], 403,
                 extra={"required": "owner", "tool": "hermes_plan_approve_gate"}),
            403,
        )
    node, refusal = _read_gated_plan_node(kwargs, root, "hermes_plan_approve_gate")
    if refusal is not None or node is None:
        return refusal
    if node.get("state") not in op_plan.GATE_APPROVABLE_STATES:
        return _refuse_approval("hermes_plan_approve_gate", "the node is not on the forward path to completed")
    by_id = {n.get("node_id"): n for n in node["_plan_nodes"] if isinstance(n, dict)}
    if any((by_id.get(parent) or {}).get("state") != "completed" for parent in node.get("parents", [])):
        return _refuse_approval("hermes_plan_approve_gate", "the node's parents are not completed")
    kwargs["expected_plan_version"] = node["_plan_version"]
    return None


def _precheck_owner_inbox_ack(kwargs: dict[str, Any], root: Any, dry_run: bool) -> JSONResponse | None:
    ids = kwargs.get("item_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= _ACK_MAX_IDS:
        return _json_resp(
            _err("INVALID_ARGS", f"item_ids must be a list of 1 to {_ACK_MAX_IDS} item ids", 400),
            400,
        )
    return None


_MUTATION_TOOLS: dict[str, _MutationSpec] = {
    "hermes_mission_approve": _MutationSpec(
        op_mission_rt.hermes_mission_approve,
        {"mission_id", "approval_reference", "confirm", "dry_run"},
        required_level="owner",
        confirm_gated=True,
    ),
    "hermes_plan_node_transition": _MutationSpec(
        op_plan.hermes_plan_node_transition,
        {"mission_id", "node_id", "target_state", "reason", "confirm", "dry_run"},
        required_level="owner",
        confirm_gated=True,
        precheck=_precheck_approval_transition,
    ),
    "hermes_plan_approve_gate": _MutationSpec(
        op_plan.hermes_plan_approve_gate,
        {"mission_id", "node_id", "reason", "confirm", "dry_run"},
        required_level="owner",
        confirm_gated=True,
        precheck=_precheck_approval_gate,
    ),
    "hermes_autopilot_stop": _MutationSpec(
        op_autopilot.hermes_autopilot_stop,
        {"mission_id", "confirm", "dry_run"},
        required_level="workspace",
        confirm_gated=True,
    ),
    "hermes_owner_inbox_ack": _MutationSpec(
        op_owner_inbox.hermes_owner_inbox_ack,
        {"item_ids", "dry_run"},
        required_level="workspace",
        precheck=_precheck_owner_inbox_ack,
    ),
    "hermes_review_accept": _MutationSpec(
        op_review.hermes_review_accept,
        {"contract_sha256", "task_id", "assignee", "reviewer", "verdict", "evidence_refs", "approval_reference", "dry_run", "confirm"},
        required_level="owner",
        confirm_gated=True,
    ),
    "hermes_swarm_stage_advance": _MutationSpec(
        op_swarm.hermes_swarm_stage_advance,
        {"workflow_id", "stage_id", "confirm", "dry_run"},
        required_level="workspace",
        confirm_gated=True,
    ),
    "hermes_swarm_approve": _MutationSpec(
        op_swarm.hermes_swarm_approve,
        {"workflow_id", "confirm", "dry_run"},
        required_level="owner",
        confirm_gated=True,
    ),
    "hermes_cron_run": _MutationSpec(
        op_cron.hermes_cron_run,
        {"profile", "job_id", "dry_run", "timeout"},
        required_level="cron",
        long_running=True,
    ),
    "hermes_cron_pause": _MutationSpec(
        op_cron.hermes_cron_pause,
        {"profile", "job_id", "reason", "dry_run"},
        required_level="cron",
    ),
    "hermes_cron_create": _MutationSpec(
        op_cron.hermes_cron_create,
        {
            "profile", "schedule", "prompt", "name", "skills", "deliver", "repeat", "script", "workdir",
            "no_agent", "context_from", "enabled_toolsets", "model_provider", "model_name", "dry_run",
        },
        required_level="cron",
    ),
    "hermes_cron_copy": _MutationSpec(
        op_cron.hermes_cron_copy,
        {"source_profile", "target_profile", "job_id", "dry_run"},
        required_level="cron",
    ),
    "hermes_cron_move": _MutationSpec(
        op_cron.hermes_cron_move,
        {"source_profile", "target_profile", "job_id", "pause_source", "test_run_target", "dry_run"},
        required_level="cron",
    ),
    "hermes_codex_cancel": _MutationSpec(
        op_codex.hermes_codex_cancel,
        {"job_id", "confirm", "dry_run"},
        required_level="workspace",
        confirm_gated=True,
    ),
    "hermes_operator_recover": _MutationSpec(
        op_diagnostics.hermes_operator_recover,
        {"profile", "apply"},
        required_level="workspace",
        apply_flag=True,
    ),
    "hermes_swarm_reconcile": _MutationSpec(
        op_recovery.hermes_operator_reconcile,
        {"apply"},
        required_level="workspace",
        apply_flag=True,
    ),
    "hermes_oauth_revoke": _MutationSpec(
        op_oauth.hermes_oauth_revoke,
        {"confirm", "dry_run", "rotate_key"},
        required_level="owner",
        confirm_gated=True,
    ),
}


def _action_effective_dry_run(spec: _MutationSpec, kwargs: dict[str, Any]) -> bool:
    if spec.apply_flag:
        return not bool(kwargs.get("apply", False))
    return bool(kwargs.get("dry_run", True))


def _map_mutation_result(tool: str, spec: _MutationSpec, payload: dict[str, Any], dry_run_effective: bool) -> JSONResponse:
    """Map an operator mutation envelope to the browser contract.

    - success + dry-run plan        -> 200 {tool, dry_run, requires_confirm}
    - success + applied             -> 200 {tool, dry_run:false, requires_confirm:false}
    - CONFIRMATION_REQUIRED         -> 409 CONFIRM_REQUIRED (UI shows confirm dialog)
    - level/policy denial           -> 403 LEVEL_REQUIRED (required level from spec)
    - other failure                 -> 400 with the operator code
    """
    if payload.get("success") is True:
        applied_dry_run = payload.get("dry_run")
        if applied_dry_run is None:
            # The operator may omit dry_run on a confirmed write; fall back to
            # the adapter's effective dry-run computed from the request.
            applied_dry_run = dry_run_effective
        requires_confirm = bool(spec.confirm_gated) and applied_dry_run is not False
        return _json_resp(
            _ok(
                {
                    "tool": tool,
                    "dry_run": bool(applied_dry_run) if applied_dry_run is not None else dry_run_effective,
                    "requires_confirm": requires_confirm,
                    "result": payload,
                }
            ),
            200,
        )

    code = str(payload.get("code") or payload.get("error") or "MUTATION_FAILED")
    message = (
        payload.get("safe_message")
        or payload.get("message")
        or payload.get("error")
        or f"{tool} failed"
    )
    upper = code.upper()
    if "CONFIRM" in upper:
        return _json_resp(
            _err("CONFIRM_REQUIRED", str(message)[:500], 409, extra={"details": payload, "tool": tool}),
            409,
        )
    if any(marker in upper for marker in ("POLICY_DENIED", "PERMISSION_DENIED", "_DENIED", "LEVEL")):
        return _json_resp(
            _err(
                "LEVEL_REQUIRED",
                str(message)[:500],
                403,
                extra={"required": spec.required_level, "details": payload, "tool": tool},
            ),
            403,
        )
    return _json_resp(_err(code, str(message)[:500], 400, extra={"tool": tool, "details": payload}), 400)


def _cross_site_refusal(request: Request) -> JSONResponse | None:
    """Refuse browser requests that could have been forged by another site.

    Loopback no-auth mode has no credential to protect the action route, so a
    foreign page could otherwise send a "simple" cross-site POST. A JSON
    Content-Type cannot be sent cross-site without a CORS preflight, and a
    browser's Origin / Sec-Fetch-Site headers identify the initiating site.
    Non-browser clients that send neither header are unaffected.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _json_resp(_err("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json", 415), 415)
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return _json_resp(_err("CROSS_SITE_REFUSED", "cross-site action requests are refused", 403), 403)
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "").strip().lower()
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:
            origin_host = ""
        if not host or origin_host != host:
            return _json_resp(_err("CROSS_SITE_REFUSED", "cross-site action requests are refused", 403), 403)
    return None


async def _action(request: Request) -> JSONResponse:
    """POST /api/ops/action — execute an existing gated hermes_* tool path."""
    refusal = _cross_site_refusal(request)
    if refusal is not None:
        return refusal
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_resp(_err("INVALID_JSON", "request body must be a JSON object"), 400)
    if not isinstance(body, dict):
        return _json_resp(_err("INVALID_JSON", "request body must be a JSON object"), 400)

    tool = str(body.get("tool") or "").strip()
    raw_args = body.get("args")
    if not isinstance(raw_args, dict) or raw_args is None:
        return _json_resp(_err("INVALID_ARGS", "args must be a JSON object"), 400)
    requested_dry_run = body.get("dry_run")

    spec = _MUTATION_TOOLS.get(tool)
    if spec is None:
        return _json_resp(_err("UNKNOWN_TOOL", f"unknown operator tool {tool!r}"), 404)

    unknown = set(raw_args) - spec.args
    if unknown:
        return _json_resp(
            _err("INVALID_ARGS", f"unexpected args for {tool}: {', '.join(sorted(unknown)[:5])}", 400),
            400,
        )

    kwargs: dict[str, Any] = {k: v for k, v in raw_args.items()}
    if requested_dry_run is not None:
        if spec.apply_flag:
            # dry_run maps to apply=false (and vice versa) for apply-style tools.
            kwargs["apply"] = not bool(requested_dry_run)
        elif "dry_run" in spec.args:
            kwargs["dry_run"] = bool(requested_dry_run)

    dry_run_effective = _action_effective_dry_run(spec, kwargs)

    if spec.precheck is not None:
        try:
            refusal = spec.precheck(kwargs, _resolve_root(), bool(kwargs.get("dry_run", True)))
        except Exception as exc:  # noqa: BLE001
            return _json_resp(_err("INTERNAL", f"{tool} precheck failed: {op.redact_output(str(exc))[:300]}"), 500)
        if refusal is not None:
            _audit(tool, success=False, summary=f"{tool} refused before execution (dry_run={dry_run_effective})")
            return refusal

    # Long-running tools (hermes_cron_run) block 30-7200s; never hold the
    # HTTP request. Dry-run plans are fast and run inline; real executions
    # are dispatched to a daemon thread and the UI polls /api/ops/cron.
    if spec.long_running and not dry_run_effective:
        root = _resolve_root()
        try:
            threading.Thread(
                target=lambda: spec.fn(hermes_root=root, **kwargs),
                name=f"ui-ops-{tool}",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001
            return _json_resp(_err("INTERNAL", f"failed to dispatch {tool}: {op.redact_output(str(exc))[:300]}"), 500)
        _audit(tool, success=True, summary=f"{tool} dispatched (long-running)")
        return _json_resp(_ok({"tool": tool, "accepted": True, "status": "running", "dry_run": False}), 202)

    try:
        root = _resolve_root()
        result = await run_in_threadpool(spec.fn, hermes_root=root, **kwargs)
        payload = _parse_payload(result)
    except Exception as exc:  # noqa: BLE001
        return _json_resp(_err("INTERNAL", f"{tool} failed: {op.redact_output(str(exc))[:300]}"), 500)

    _audit(tool, success=bool(payload.get("success", True)), summary=f"{tool} executed dry_run={dry_run_effective}")
    return _map_mutation_result(tool, spec, payload, dry_run_effective)


# ---------------------------------------------------------------------------
# Route registration (composed by ui_api.py)
# ---------------------------------------------------------------------------


def ui_ops_routes() -> list[Route]:
    """Return the Flight Deck operator routes (composition-only registry).

    Order matters: fixed status-read paths are registered before the
    ``/api/ops/{surface}`` mission catch-all so Starlette matches them
    first.
    """
    return [
        Route("/api/events", _events, methods=["GET"]),
        Route("/api/ops/account", _account, methods=["GET"]),
        Route("/api/ops/owner-inbox", _owner_inbox, methods=["GET"]),
        Route("/api/ops/contracts", _contracts_list, methods=["GET"]),
        Route("/api/ops/contracts/{contract_sha256}", _contracts_detail, methods=["GET"]),
        Route("/api/ops/swarm", _swarm_list, methods=["GET"]),
        Route("/api/ops/swarm/{workflow_id}", _swarm_detail, methods=["GET"]),
        Route("/api/ops/codex/{job_id}", _codex_detail, methods=["GET"]),
        Route("/api/ops/cron/{job_id}", _cron_detail, methods=["GET"]),
        Route("/api/ops/review/{contract_sha256}", _review_detail, methods=["GET"]),
        Route("/api/ops/{surface}", _ops_surface, methods=["GET"]),
        Route("/api/ops/action", _action, methods=["POST"]),
    ]
