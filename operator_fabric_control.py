"""Policy-gated coordinator controls for Hermes GPT v0.8 Fabric G4-A.

This module is the operator-facing control seam for distributed attempt status,
reconciliation, evidence admission, and cancellation. It deliberately keeps
Work Contract validation observational: callers explicitly reconcile/collect
first, then the existing validator reads already-admitted Fabric evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import operator_fabric as fabric
import operator_policy as op

_ATTEMPT_RE = re.compile(r"^faba-[0-9a-f]{32}$")


def _bounded_error(exc: BaseException) -> str:
    return op.redact_output(str(exc))[:300]


def _audit(
    *,
    tool: str,
    policy: op.OperatorPolicy,
    dry_run: bool,
    success: bool,
    changed: bool,
    summary: str,
    attempt_id: str,
    result: dict[str, Any] | None = None,
    audit: Callable[..., Any] | None = None,
) -> None:
    audit_fn = audit or op.audit_record
    extra: dict[str, Any] = {"attempt_id": attempt_id}
    if isinstance(result, dict):
        for key in ("dispatch_id", "task_id", "node", "state", "peer_state"):
            value = result.get(key)
            if isinstance(value, (str, int, float, bool)):
                extra[key] = value
    try:
        audit_fn(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=summary[:500],
            extra=extra,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return


def _coordinator(
    hermes_root: Path | None,
    coordinator: fabric.FabricCoordinator | None,
) -> fabric.FabricCoordinator:
    return coordinator or fabric.FabricCoordinator(hermes_root=hermes_root)


def _attempt_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not _ATTEMPT_RE.fullmatch(normalized):
        raise ValueError("attempt_id has an invalid Fabric format")
    return normalized


def hermes_fabric_status(
    attempt_id: str,
    reconcile: bool = False,
    hermes_root: Path | None = None,
    *,
    coordinator: fabric.FabricCoordinator | None = None,
    audit: Callable[..., Any] | None = None,
) -> str:
    """Poll or reconcile one durable Fabric attempt.

    ``reconcile=false`` performs bounded status observation. ``reconcile=true``
    is the explicit recovery path for ambiguous/restarted distributed state.
    Neither operation creates a replacement attempt.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        attempt = _attempt_id(attempt_id)
        result = _coordinator(hermes_root, coordinator).poll(
            attempt,
            reconcile=bool(reconcile),
            timeout=15,
        )
        tool = "hermes_fabric_reconcile" if reconcile else "hermes_fabric_status"
        _audit(
            tool=tool,
            policy=policy,
            dry_run=True,
            success=True,
            changed=False,
            summary=(
                "reconciled Fabric attempt without replacement"
                if reconcile
                else "observed Fabric attempt status"
            ),
            attempt_id=attempt,
            result=result,
            audit=audit,
        )
        if result.get("state") in {"TERMINAL_REPORTED", "CANCELLED", "BLOCKED"}:
            _audit(
                tool="hermes_fabric_terminal",
                policy=policy,
                dry_run=True,
                success=True,
                changed=False,
                summary="observed Fabric terminal/blocked state",
                attempt_id=attempt,
                result=result,
                audit=audit,
            )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, fabric.FabricError) as exc:
        tool = "hermes_fabric_reconcile" if reconcile else "hermes_fabric_status"
        _audit(
            tool=tool,
            policy=policy,
            dry_run=True,
            success=False,
            changed=False,
            summary="Fabric status/reconcile failed closed",
            attempt_id=str(attempt_id or "")[:128],
            audit=audit,
        )
        return json.dumps(
            {
                "success": False,
                "code": getattr(exc, "code", "FABRIC_STATUS_ERROR"),
                "safe_message": _bounded_error(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


def hermes_fabric_evidence(
    attempt_id: str,
    hermes_root: Path | None = None,
    *,
    coordinator: fabric.FabricCoordinator | None = None,
    audit: Callable[..., Any] | None = None,
) -> str:
    """Collect, lineage-check, and admit bounded evidence for one attempt."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        attempt = _attempt_id(attempt_id)
        result = _coordinator(hermes_root, coordinator).collect(attempt, timeout=15)
        _audit(
            tool="hermes_fabric_evidence_receipt",
            policy=policy,
            dry_run=True,
            success=True,
            changed=False,
            summary="received and admitted Fabric evidence",
            attempt_id=attempt,
            result=result,
            audit=audit,
        )
        _audit(
            tool="hermes_fabric_terminal",
            policy=policy,
            dry_run=True,
            success=True,
            changed=False,
            summary="Fabric attempt reached evidence-backed terminal state",
            attempt_id=attempt,
            result=result,
            audit=audit,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, fabric.FabricError) as exc:
        _audit(
            tool="hermes_fabric_evidence_receipt",
            policy=policy,
            dry_run=True,
            success=False,
            changed=False,
            summary="Fabric evidence admission failed closed",
            attempt_id=str(attempt_id or "")[:128],
            audit=audit,
        )
        return json.dumps(
            {
                "success": False,
                "code": getattr(exc, "code", "FABRIC_EVIDENCE_ERROR"),
                "safe_message": _bounded_error(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


def hermes_fabric_cancel(
    attempt_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
    *,
    coordinator: fabric.FabricCoordinator | None = None,
    audit: Callable[..., Any] | None = None,
) -> str:
    """Cancel one Fabric attempt through the durable attempt identity."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
        attempt = _attempt_id(attempt_id)
        if effective:
            result = {
                "success": True,
                "dry_run": True,
                "changed": False,
                "backend": "fabric",
                "attempt_id": attempt,
                "plan": "cancel",
            }
            _audit(
                tool="hermes_fabric_cancel",
                policy=policy,
                dry_run=True,
                success=True,
                changed=False,
                summary="Fabric cancel plan",
                attempt_id=attempt,
                result=result,
                audit=audit,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        if not confirm:
            result = {
                "success": False,
                "code": "CONFIRMATION_REQUIRED",
                "backend": "fabric",
                "attempt_id": attempt,
                "safe_message": "Fabric cancellation requires confirm=true.",
            }
            _audit(
                tool="hermes_fabric_cancel",
                policy=policy,
                dry_run=True,
                success=False,
                changed=False,
                summary="Fabric cancellation confirmation required",
                attempt_id=attempt,
                result=result,
                audit=audit,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        result = _coordinator(hermes_root, coordinator).cancel(attempt, timeout=15)
        _audit(
            tool="hermes_fabric_cancel",
            policy=policy,
            dry_run=False,
            success=bool(result.get("success")),
            changed=bool(result.get("changed")),
            summary=(
                "Fabric cancellation accepted"
                if result.get("success")
                else "Fabric cancellation failed closed"
            ),
            attempt_id=attempt,
            result=result,
            audit=audit,
        )
        if result.get("state") == "CANCELLED":
            _audit(
                tool="hermes_fabric_terminal",
                policy=policy,
                dry_run=True,
                success=True,
                changed=False,
                summary="Fabric attempt reached cancelled terminal state",
                attempt_id=attempt,
                result=result,
                audit=audit,
            )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, fabric.FabricError) as exc:
        _audit(
            tool="hermes_fabric_cancel",
            policy=policy,
            dry_run=bool(dry_run),
            success=False,
            changed=False,
            summary="Fabric cancellation failed closed",
            attempt_id=str(attempt_id or "")[:128],
            audit=audit,
        )
        return json.dumps(
            {
                "success": False,
                "code": getattr(exc, "code", "FABRIC_CANCEL_ERROR"),
                "safe_message": _bounded_error(exc),
            },
            ensure_ascii=False,
            indent=2,
        )


__all__ = [
    "hermes_fabric_cancel",
    "hermes_fabric_evidence",
    "hermes_fabric_status",
]
