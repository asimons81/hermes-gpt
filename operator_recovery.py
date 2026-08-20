"""Restart reconciliation for hermes-gpt v0.7+ (Flight Deck, S2).

Per ADR-007 the recovery surface is **fail-closed and never auto-advancing**:
after a process restart it marks swarm stages found in ``running`` as
``blocked`` with ``reason: interrupted_by_restart`` and reports a bounded
summary; the operator explicitly re-advances through the existing gated
``hermes_swarm_stage_advance``. It also reloads the durable token envelope
(when S5's token store is present) and verifies integrity.

Fabric v0.8 adds an idempotent runner-backend bootstrap here because this
module is imported unconditionally by the operator server before tools are
registered. Fabric's own distributed restart reconciliation remains fail-closed
and is implemented in its durable coordinator/peer journals; this module does
not auto-execute remote work.

No auto-execution path exists. All mutations are dry-run-first with an apply
gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import operator_fabric as op_fabric
import operator_policy as op
import operator_swarm as op_swarm

# Core runtime registration is idempotent and performs no network or mutation.
# It makes execution.backend="fabric" visible through the existing runner
# registry before Work Contract tools are called.
op_fabric.register_runner_backend()

TOOL_NAME = "hermes_operator_recover"
RECONCILE_SURFACE = "swarm_restart_reconcile"
INTERRUPTED_REASON = "interrupted_by_restart"

MAX_RECONCILE_REPORT = 64


def _reconcile_swarm_stages(hermes_root: Path, apply: bool) -> dict[str, Any]:
    """Mark interrupted swarm stages blocked. Never auto-advances.

    Returns a bounded summary of interrupted workflows/stages.
    """
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
    """Reload and integrity-check the durable token envelope (S5, optional)."""
    try:
        import token_store  # local import: S5 module, may not exist in older installs
    except Exception:
        return {"available": False, "detail": "token_store not available"}
    try:
        envelope = token_store.load_envelope(hermes_root=hermes_root)
    except Exception as exc:
        return {"available": True, "integrity": "FAIL", "detail": str(exc)[:300]}
    if envelope is None:
        return {"available": True, "integrity": "EMPTY", "detail": "no token envelope on disk"}
    return {"available": True, "integrity": "OK", "kid": envelope.get("kid", "")}


def hermes_operator_reconcile(
    apply: bool = False,
    hermes_root: Path | None = None,
    *,
    audit: Callable[..., Any] | None = None,
) -> str:
    """Reconcile state after a restart. Dry-run by default; apply gate required.

    Marks swarm stages stuck in ``running`` as ``blocked`` (never
    auto-advances), reloads the durable token envelope, and returns a bounded
    reconciliation summary.
    """
    trace_id = op.new_trace_id()
    policy = op.OperatorPolicy()
    can_apply = policy.enabled and policy.apply_mode == "direct" and op.has_level("workspace", policy.level)
    if apply and not can_apply:
        payload = op.make_error_envelope(
            layer="operator",
            code="PERMISSION_DENIED",
            safe_message="apply=true requires operator enabled, apply_mode=direct, and level>=workspace.",
            suggested_action="Enable workspace-level Operator Mode (direct) before applying reconciliation.",
            trace_id=trace_id,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    root = hermes_root or op_swarm._default_hermes_root() or Path.home() / ".hermes"

    try:
        swarm_summary = _reconcile_swarm_stages(root, apply=apply)
        token_summary = _reload_token_store(root)
    except Exception as exc:
        payload = op.make_error_envelope(
            layer="operator",
            code="RECONCILE_ERROR",
            safe_message=f"reconciliation failed: {op.redact_output(str(exc))[:300]}",
            suggested_action="Check the swarm-workflows and secrets directories, then retry.",
            trace_id=trace_id,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    result = {
        "success": True,
        "schema_version": "0.7",
        "tool": TOOL_NAME,
        "surface": RECONCILE_SURFACE,
        "trace_id": trace_id,
        "dry_run": not apply,
        "applied": bool(apply),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "swarm": swarm_summary,
        "tokens": token_summary,
        "note": (
            "Restart reconciliation is fail-closed: interrupted stages are blocked, "
            "never auto-advanced. Re-advance explicitly with hermes_swarm_stage_advance."
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
            summary=f"swarm reconcile applied={apply} interrupted={swarm_summary['interrupted_count']}",
            extra={
                "workflow_count": len({s["workflow_id"] for s in swarm_summary["interrupted_stages"]}),
                "interrupted_count": swarm_summary["interrupted_count"],
                "records_changed": swarm_summary["records_changed"],
                "token_integrity": token_summary.get("integrity", ""),
            },
        )
    except Exception:
        pass

    return json.dumps(result, ensure_ascii=False, indent=2)
