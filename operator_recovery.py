"""Restart reconciliation for hermes-gpt v0.7+.

Recovery is fail-closed and never auto-advancing. Interrupted local swarm
stages are blocked for explicit operator action. Fabric v0.8 also reconciles
its durable distributed attempt journals, but only on an applied, workspace-
gated recovery pass. Dry-run recovery never contacts a peer or mutates a Fabric
journal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import operator_fabric as op_fabric
import operator_fabric_g4c as op_fabric_g4c
import operator_fabric_router as op_fabric_router
import operator_policy as op
import operator_swarm as op_swarm

# Runtime registration is idempotent. G4-C intentionally layers over the
# existing G4-A/G4-B backends rather than creating a second authority path.
op_fabric.register_runner_backend()
op_fabric_router.register_runner_backend()
op_fabric_g4c.register_runtime()

TOOL_NAME = "hermes_operator_recover"
RECONCILE_SURFACE = "swarm_restart_reconcile"
INTERRUPTED_REASON = "interrupted_by_restart"
MAX_RECONCILE_REPORT = 64


def _reconcile_swarm_stages(hermes_root: Path, apply: bool) -> dict[str, Any]:
    interrupted: list[dict[str, Any]] = []
    changed_records = 0
    for record in op_swarm._list_records(hermes_root):
        workflow_id = record.get("workflow_id", "")
        if record.get("status") != op_swarm.WORKFLOW_STATUS_RUNNING:
            continue
        record_mutated = False
        for stage in record.get("stages", []):
            if stage.get("status") != op_swarm.STAGE_STATUS_RUNNING:
                continue
            interrupted.append(
                {
                    "workflow_id": workflow_id,
                    "stage_id": stage.get("id", ""),
                    "owner": stage.get("owner", ""),
                    "task_id": stage.get("task_id", ""),
                    "blocked_reason": INTERRUPTED_REASON,
                }
            )
            if apply:
                stage["status"] = op_swarm.STAGE_STATUS_BLOCKED
                stage["blocked_reason"] = INTERRUPTED_REASON
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                if record.get("status") == op_swarm.WORKFLOW_STATUS_RUNNING:
                    record["status"] = op_swarm.WORKFLOW_STATUS_BLOCKED
                record_mutated = True
        if apply and record_mutated:
            op_swarm._save_workflow(hermes_root, record)
            changed_records += 1
    return {
        "interrupted_stages": interrupted[:MAX_RECONCILE_REPORT],
        "interrupted_count": len(interrupted),
        "records_changed": changed_records,
        "applied": apply,
    }


def _reload_token_store(hermes_root: Path) -> dict[str, Any]:
    try:
        import token_store
    except Exception:
        return {"available": False, "detail": "token_store not available"}
    try:
        envelope = token_store.load_envelope(hermes_root=hermes_root)
    except Exception as exc:
        return {"available": True, "integrity": "FAIL", "detail": str(exc)[:300]}
    if envelope is None:
        return {"available": True, "integrity": "EMPTY", "detail": "no token envelope on disk"}
    return {"available": True, "integrity": "OK", "kid": envelope.get("kid", "")}


def _fabric_reconcile_summary(hermes_root: Path, *, apply: bool) -> dict[str, Any]:
    db_path = op_fabric._db_path(op_fabric.COORDINATOR_DB_ENV, "coordinator.db", hermes_root)
    if not db_path.is_file():
        return {"available": True, "active_count": 0, "reconciled": [], "applied": apply}
    try:
        with op_fabric._connect_readonly(db_path) as db:
            active_count = int(
                db.execute(
                    "SELECT COUNT(*) AS n FROM attempts "
                    "WHERE state NOT IN ('COMPLETED','FAILED','CANCELLED')"
                ).fetchone()["n"]
            )
    except Exception as exc:
        return {
            "available": True,
            "active_count": 0,
            "reconciled": [],
            "applied": apply,
            "error": op.redact_output(str(exc))[:300],
        }
    if not apply or not active_count:
        return {
            "available": True,
            "active_count": active_count,
            "reconciled": [],
            "applied": apply,
            "note": "dry-run does not contact Fabric peers",
        }
    coordinator = op_fabric_g4c.FabricCoordinator(hermes_root=hermes_root, db_path=db_path)
    results = coordinator.reconcile_active(timeout=10)
    return {
        "available": True,
        "active_count": active_count,
        "reconciled": results[:MAX_RECONCILE_REPORT],
        "reconciled_count": len(results),
        "applied": True,
    }


def hermes_operator_reconcile(
    apply: bool = False,
    hermes_root: Path | None = None,
    *,
    audit: Callable[..., Any] | None = None,
) -> str:
    trace_id = op.new_trace_id()
    policy = op.OperatorPolicy()
    can_apply = (
        policy.enabled
        and policy.apply_mode == "direct"
        and op.has_level("workspace", policy.level)
    )
    if apply and not can_apply:
        payload = op.make_error_envelope(
            layer="operator",
            code="PERMISSION_DENIED",
            safe_message=(
                "apply=true requires operator enabled, apply_mode=direct, "
                "and level>=workspace."
            ),
            suggested_action=(
                "Enable workspace-level Operator Mode (direct) before applying reconciliation."
            ),
            trace_id=trace_id,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    root = hermes_root or op_swarm._default_hermes_root() or Path.home() / ".hermes"
    try:
        swarm_summary = _reconcile_swarm_stages(root, apply=apply)
        token_summary = _reload_token_store(root)
        fabric_summary = _fabric_reconcile_summary(root, apply=apply)
    except Exception as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="RECONCILE_ERROR",
            safe_message=f"reconciliation failed: {op.redact_output(str(exc))[:300]}",
            suggested_action=(
                "Check local swarm/Fabric journals and peer connectivity, then retry."
            ),
            trace_id=trace_id,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    result = {
        "success": True,
        "schema_version": "0.8",
        "tool": TOOL_NAME,
        "surface": RECONCILE_SURFACE,
        "trace_id": trace_id,
        "dry_run": not apply,
        "applied": bool(apply),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "swarm": swarm_summary,
        "fabric": fabric_summary,
        "tokens": token_summary,
        "note": (
            "Restart reconciliation is fail-closed. Interrupted or ambiguous work is never "
            "auto-advanced and Fabric recovery never creates a replacement writer."
        ),
    }

    audit_fn = audit or op.audit_record
    try:
        audit_fn(
            tool=TOOL_NAME,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=not apply,
            success=True,
            changed=bool(apply and swarm_summary["records_changed"]),
            summary=(
                f"reconcile applied={apply} swarm_interrupted="
                f"{swarm_summary['interrupted_count']} fabric_active="
                f"{fabric_summary.get('active_count', 0)}"
            ),
            extra={
                "workflow_count": len(
                    {s["workflow_id"] for s in swarm_summary["interrupted_stages"]}
                ),
                "interrupted_count": swarm_summary["interrupted_count"],
                "records_changed": swarm_summary["records_changed"],
                "fabric_active_count": fabric_summary.get("active_count", 0),
                "fabric_reconciled_count": fabric_summary.get("reconciled_count", 0),
                "token_integrity": token_summary.get("integrity", ""),
            },
        )
    except Exception:
        pass
    return json.dumps(result, ensure_ascii=False, indent=2)
