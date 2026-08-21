"""Read-only, redacted Flight Deck views for Hermes GPT v0.8 Fabric G4-D.

This module is deliberately observational. It reads coordinator-owned config,
Fabric journals, routing receipts, admitted artifact metadata, and the bounded
operator audit tail. It never polls peers, reconciles attempts, collects
artifacts/evidence, or mutates Fabric state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_fabric as fabric
import operator_fabric_router as router
import operator_policy as op

VIEW_SCHEMA = "hermes.fabric-flight-deck/v1"
_MAX_ATTEMPTS = 200
_MAX_EVENTS = 50
_MAX_DECISIONS = 200
_MAX_TAIL_BYTES = 512 * 1024
_MAX_LINE_BYTES = 64 * 1024


def _safe_text(value: Any, maximum: int = 500) -> str:
    if value is None:
        return ""
    text = op.redact_output(str(value))
    text = " ".join(text.split())
    return text[:maximum]


def _safe_token(value: Any, maximum: int = 128) -> str:
    return _safe_text(value, maximum)


def _read_tail(path: Path, *, maximum: int = _MAX_TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > maximum:
                fh.seek(size - maximum)
                fh.readline()
            raw = fh.read(maximum)
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _coordinator_path(hermes_root: Path | None) -> Path:
    configured = os.environ.get(fabric.COORDINATOR_DB_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        path = fabric._root(hermes_root) / "fabric" / "coordinator.db"
    return path


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def _facts_summary(facts: router.TargetFacts | None, now: datetime) -> dict[str, Any]:
    if facts is None:
        return {
            "fresh": False,
            "freshness": "unknown",
            "observed_at": "",
            "capacity": None,
            "active": None,
            "capabilities": {},
        }
    fresh = facts.fresh(now)
    capabilities = {
        "os": sorted(facts.os_names)[:12],
        "runtimes": sorted(facts.runtimes)[:12],
        "runners": sorted(facts.runners)[:12],
        "providers": sorted(facts.providers)[:12],
        "models": sorted(facts.models)[:12],
        "tools": sorted(facts.tools)[:16],
        "browser": facts.browser,
        "vision": facts.vision,
        "gpu": {
            "available": facts.gpu_available,
            "vendor": _safe_token(facts.gpu_vendor, 64),
            "memory_mb": facts.gpu_memory_mb,
        },
    }
    return {
        "fresh": fresh,
        "freshness": "fresh" if fresh else "stale",
        "observed_at": facts.observed_at.isoformat(),
        "max_age_seconds": facts.max_age_seconds,
        "capacity": facts.capacity,
        "active": facts.active,
        "capabilities": capabilities,
    }


def nodes_view(*, hermes_root: Path | None = None) -> dict[str, Any]:
    """Return enrolled Fabric nodes without contacting peers or exposing secrets."""
    warnings: list[dict[str, str]] = []
    try:
        nodes = fabric.load_node_registry(hermes_root=hermes_root)
    except fabric.FabricError as exc:
        return {
            "schema": VIEW_SCHEMA,
            "success": False,
            "available": False,
            "code": exc.code,
            "safe_message": "Fabric node registry is unavailable.",
            "nodes": [],
        }
    except (OSError, ValueError, TypeError):
        return {
            "schema": VIEW_SCHEMA,
            "success": False,
            "available": False,
            "code": "FABRIC_NODE_REGISTRY_INVALID",
            "safe_message": "Fabric node registry is unavailable.",
            "nodes": [],
        }

    try:
        policy = router.load_routing_policy(hermes_root=hermes_root)
    except router.RoutingError as exc:
        policy = router.RoutingPolicy(targets={})
        warnings.append({"code": exc.code, "detail": "routing capability facts are unavailable"})
    except (OSError, ValueError, TypeError):
        policy = router.RoutingPolicy(targets={})
        warnings.append(
            {"code": "FABRIC_ROUTING_CONFIG_INVALID", "detail": "routing capability facts are unavailable"}
        )

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for name in sorted(nodes):
        node = nodes[name]
        facts = policy.targets.get(name)
        summary = _facts_summary(facts, now)
        if not node.enabled:
            availability = "disabled"
        elif facts is None:
            availability = "unknown"
        elif summary["fresh"]:
            availability = "observed"
        else:
            availability = "stale"
        rows.append(
            {
                "name": node.name,
                "peer": node.a2a_peer_name,
                "identity": _safe_token(node.expected_identity, 128),
                "enabled": node.enabled,
                "availability": availability,
                "health_basis": "coordinator_observation_only",
                "freshness": summary["freshness"],
                "observed_at": summary["observed_at"],
                "max_age_seconds": summary.get("max_age_seconds"),
                "active": summary["active"],
                "capacity": summary["capacity"],
                "allowed_profiles": list(node.allowed_profiles)[:16],
                "authority_ceiling": node.max_authorization,
                "remote_backends": list(node.allowed_remote_backends)[:16],
                "logical_workspaces": list(node.logical_workspaces)[:16],
                "required_features": list(node.required_features)[:16],
                "capabilities": summary["capabilities"],
            }
        )
    return {
        "schema": VIEW_SCHEMA,
        "success": True,
        "available": True,
        "generated_at": now.isoformat(),
        "health_note": "No peer is contacted by this read model; fresh means the coordinator-owned capability observation is within policy age.",
        "warnings": warnings,
        "nodes": rows,
    }


def _sanitize_exclusion(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    code = _safe_token(value.get("code"), 96)
    if not code:
        return None
    return {"code": code, "detail": _safe_text(value.get("detail"), 240)}


def _optional_bool(value: dict[str, Any], key: str) -> bool | None:
    item = value.get(key)
    return item if isinstance(item, bool) else None


def _sanitize_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    exclusions: list[dict[str, str]] = []
    raw_exclusions = value.get("exclusions")
    if isinstance(raw_exclusions, list):
        for item in raw_exclusions[:32]:
            clean = _sanitize_exclusion(item)
            if clean:
                exclusions.append(clean)
    rank_raw = value.get("rank")
    rank: list[Any] = []
    if isinstance(rank_raw, list):
        for item in rank_raw[:8]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                rank.append(item)
    return {
        "node": _safe_token(value.get("node"), 64),
        "backend": _safe_token(value.get("backend"), 64),
        "transport_backend": _safe_token(value.get("transport_backend"), 64),
        "remote": _optional_bool(value, "remote"),
        "healthy": _optional_bool(value, "healthy"),
        "capability_fresh": _optional_bool(value, "capability_fresh"),
        "authority_ceiling": _safe_token(value.get("authority_ceiling"), 32),
        "eligible": _optional_bool(value, "eligible"),
        "exclusions": exclusions,
        "rank": rank,
    }


def _sanitize_requirements(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "location",
        "browser",
        "vision",
        "gpu",
        "min_gpu_memory_mb",
    ):
        item = value.get(key)
        if isinstance(item, (str, int, bool)):
            out[key] = item
    for key in ("os", "runtimes", "runners", "providers", "models", "tools"):
        item = value.get(key)
        if isinstance(item, list):
            out[key] = [_safe_token(v, 128) for v in item[:32]]
    return out


def _sanitize_decision(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    task_id = _safe_token(value.get("task_id"), 128)
    placed_sha = _safe_token(value.get("placed_contract_sha256"), 128)
    if not task_id and not placed_sha:
        return None
    selected = _sanitize_candidate(value.get("selected"))
    candidates: list[dict[str, Any]] = []
    raw_candidates = value.get("candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates[:129]:
            clean = _sanitize_candidate(item)
            if clean:
                candidates.append(clean)
    return {
        "router": _safe_token(value.get("router"), 64),
        "mode": "auto",
        "task_id": task_id,
        "original_contract_sha256": _safe_token(value.get("original_contract_sha256"), 128),
        "placed_contract_sha256": placed_sha,
        "created_at": _safe_token(value.get("created_at"), 128),
        "requirements": _sanitize_requirements(value.get("requirements")),
        "selected": selected,
        "candidates": candidates,
        "explanation_available": bool(candidates or value.get("requirements")),
    }


def routing_decisions_view(*, hermes_root: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), _MAX_DECISIONS))
    path = fabric._root(hermes_root) / "fabric" / "routing-decisions.jsonl"
    rows: list[dict[str, Any]] = []
    for line in reversed(_read_tail(path)):
        if len(line.encode("utf-8", errors="replace")) > _MAX_LINE_BYTES:
            continue
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        clean = _sanitize_decision(raw)
        if clean:
            rows.append(clean)
        if len(rows) >= limit:
            break
    return rows


def _routing_for_dispatch(
    *,
    task_id: str,
    contract_sha256: str,
    decisions: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    for decision in decisions:
        if decision.get("placed_contract_sha256") == contract_sha256 and decision.get("task_id") == task_id:
            return "auto", decision
    return "explicit", None


def _authority_summary(row: sqlite3.Row, columns: set[str]) -> dict[str, Any]:
    epoch = row["write_epoch"] if "write_epoch" in columns else None
    if isinstance(epoch, int):
        granted = "write_capable"
        precision = "coarse"
    else:
        granted = "read_only_or_none"
        precision = "coarse"
    return {
        "granted": granted,
        "precision": precision,
        "write_epoch": epoch if isinstance(epoch, int) else None,
        "note": "The v0.8 coordinator journal records write ownership but not the original authorization subclass; the node policy ceiling is shown separately.",
    }


def _peer_observations(row: sqlite3.Row, columns: set[str]) -> dict[str, str]:
    claim = _safe_token(row["write_claim_state"], 32) if "write_claim_state" in columns else ""
    unit = _safe_token(row["execution_unit_state"], 32) if "execution_unit_state" in columns else ""
    if claim not in {"NONE", "ACTIVE", "RELEASED", "SUPERSEDED", "UNKNOWN"}:
        claim = "UNKNOWN"
    if unit not in {
        "active", "activating", "deactivating", "reloading", "inactive", "failed",
        "dead", "not-found", "terminal", "unknown",
    }:
        unit = "unknown"
    return {
        "basis": "durable_reconciled_peer_observation",
        "label": "Peer observations; not coordinator authority or a completion verdict.",
        "write_claim_state": claim,
        "execution_unit_state": unit,
    }


def _attempt_row(
    row: sqlite3.Row,
    *,
    attempt_columns: set[str],
    decisions: list[dict[str, Any]],
    node_ceiling: str,
) -> dict[str, Any]:
    task_id = _safe_token(row["task_id"], 128)
    contract_sha = _safe_token(row["contract_sha256"], 128)
    placement_mode, decision = _routing_for_dispatch(
        task_id=task_id,
        contract_sha256=contract_sha,
        decisions=decisions,
    )
    state = _safe_token(row["state"], 64)
    code = _safe_token(row["error_code"], 128)
    blocker = code if code else (state if state in {"BLOCKED", "RECONCILING", "SUBMISSION_AMBIGUOUS", "CANCEL_AMBIGUOUS", "EVIDENCE_PENDING", "LOST_AMBIGUOUS"} else "")
    return {
        "attempt_id": _safe_token(row["attempt_id"], 128),
        "dispatch_id": _safe_token(row["dispatch_id"], 128),
        "task_id": task_id,
        "contract_sha256": contract_sha,
        "node": _safe_token(row["node_name"], 64),
        "peer": _safe_token(row["peer_name"], 128),
        "backend": _safe_token(row["remote_backend"], 64),
        "remote": True,
        "placement_mode": placement_mode,
        "routing": decision,
        "state": state,
        "error_code": code,
        "blocker": blocker,
        "remote_task_id": _safe_token(row["remote_task_id"], 128),
        "created_at": _safe_token(row["created_at"], 128),
        "updated_at": _safe_token(row["updated_at"], 128),
        "retry_parent_attempt_id": (
            _safe_token(row["retry_parent_attempt_id"], 128)
            if "retry_parent_attempt_id" in attempt_columns
            else ""
        ),
        "authority": _authority_summary(row, attempt_columns),
        "peer_observations": _peer_observations(row, attempt_columns),
        "authority_ceiling": node_ceiling,
    }


def attempts_view(
    *,
    hermes_root: Path | None = None,
    limit: int = 50,
    task_id: str = "",
) -> dict[str, Any]:
    """Return coordinator-journal attempts without creating or changing the DB."""
    limit = max(1, min(int(limit), _MAX_ATTEMPTS))
    path = _coordinator_path(hermes_root)
    if not path.is_file():
        return {
            "schema": VIEW_SCHEMA,
            "success": True,
            "available": True,
            "attempts": [],
            "count": 0,
            "note": "No Fabric coordinator journal exists yet.",
        }
    try:
        registry = fabric.load_node_registry(hermes_root=hermes_root)
    except (fabric.FabricError, OSError, ValueError, TypeError):
        registry = {}
    decisions = routing_decisions_view(hermes_root=hermes_root, limit=_MAX_DECISIONS)
    try:
        with fabric._connect_readonly(path) as db:
            if not _table_exists(db, "attempts") or not _table_exists(db, "dispatches"):
                return {"schema": VIEW_SCHEMA, "success": True, "available": True, "attempts": [], "count": 0}
            attempt_columns = _columns(db, "attempts")
            query = (
                "SELECT a.*,d.task_id,d.contract_sha256 FROM attempts a "
                "JOIN dispatches d ON d.dispatch_id=a.dispatch_id"
            )
            args: list[Any] = []
            if task_id:
                query += " WHERE d.task_id=?"
                args.append(task_id[:128])
            query += " ORDER BY a.updated_at DESC,a.created_at DESC LIMIT ?"
            args.append(limit)
            rows = db.execute(query, tuple(args)).fetchall()
    except (sqlite3.Error, OSError):
        return {
            "schema": VIEW_SCHEMA,
            "success": False,
            "available": False,
            "code": "FABRIC_JOURNAL_UNAVAILABLE",
            "safe_message": "Fabric coordinator journal could not be read.",
            "attempts": [],
        }
    attempts: list[dict[str, Any]] = []
    for row in rows:
        node = registry.get(str(row["node_name"]))
        ceiling = node.max_authorization if node else "unknown"
        attempts.append(
            _attempt_row(
                row,
                attempt_columns=attempt_columns,
                decisions=decisions,
                node_ceiling=ceiling,
            )
        )
    return {
        "schema": VIEW_SCHEMA,
        "success": True,
        "available": True,
        "attempts": attempts,
        "count": len(attempts),
    }


def _evidence_view(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    observations: list[dict[str, Any]] = []
    items = raw.get("observations")
    if isinstance(items, list):
        for item in items[:32]:
            if not isinstance(item, dict):
                continue
            observations.append(
                {
                    "kind": _safe_token(item.get("kind"), 64),
                    "provenance": _safe_token(item.get("provenance"), 64),
                    "state": _safe_text(item.get("state"), 200),
                    "outcome": _safe_text(item.get("outcome"), 200),
                    "started_at": _safe_token(item.get("started_at"), 128),
                    "ended_at": _safe_token(item.get("ended_at"), 128),
                    "source": _safe_text(item.get("source"), 160),
                    "error": _safe_text(item.get("error"), 240),
                }
            )
    return {
        "terminal_state": _safe_token(raw.get("terminal_state"), 64),
        "created_at": _safe_token(raw.get("created_at"), 128),
        "policy_sha256": _safe_token(raw.get("policy_sha256"), 128),
        "observations": observations,
        "verdict_note": "Remote observations are admitted evidence inputs; completion remains coordinator-validated.",
    }


def _artifact_views(db: sqlite3.Connection, attempt_id: str) -> list[dict[str, Any]]:
    if not _table_exists(db, "artifact_admissions"):
        return []
    rows = db.execute(
        "SELECT artifact_id,logical_name,size_bytes,sha256,media_type,active_content,admitted_at "
        "FROM artifact_admissions WHERE attempt_id=? ORDER BY logical_name LIMIT 32",
        (attempt_id,),
    ).fetchall()
    return [
        {
            "artifact_id": _safe_token(row["artifact_id"], 128),
            "logical_name": _safe_text(row["logical_name"], 512),
            "size_bytes": int(row["size_bytes"]),
            "sha256": _safe_token(row["sha256"], 128),
            "media_type": _safe_token(row["media_type"], 128),
            "active_content": bool(row["active_content"]),
            "admitted_at": _safe_token(row["admitted_at"], 128),
            "render_policy": (
                "isolated_metadata_only" if bool(row["active_content"]) else "metadata_only"
            ),
        }
        for row in rows
    ]


def _attempt_events(attempt_id: str, *, limit: int = _MAX_EVENTS) -> list[dict[str, Any]]:
    path = op.audit_log_path()
    events: list[dict[str, Any]] = []
    for line in reversed(_read_tail(path)):
        if len(line.encode("utf-8", errors="replace")) > _MAX_LINE_BYTES:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("attempt_id") != attempt_id:
            continue
        tool = _safe_token(record.get("tool"), 128)
        if not tool.startswith("hermes_fabric"):
            continue
        events.append(
            {
                "timestamp": _safe_token(record.get("timestamp"), 128),
                "tool": tool,
                "success": bool(record.get("success")),
                "changed": bool(record.get("changed")),
                "summary": _safe_text(record.get("summary") or record.get("error"), 300),
                "state": _safe_token(record.get("state"), 64),
                "code": _safe_token(record.get("code"), 128),
            }
        )
        if len(events) >= limit:
            break
    return events


def attempt_detail(attempt_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
    attempt_id = _safe_token(attempt_id, 128)
    if not attempt_id:
        return {"schema": VIEW_SCHEMA, "success": False, "available": False, "code": "FABRIC_ATTEMPT_INVALID"}
    path = _coordinator_path(hermes_root)
    if not path.is_file():
        return {
            "schema": VIEW_SCHEMA,
            "success": False,
            "available": False,
            "code": "FABRIC_ATTEMPT_NOT_FOUND",
            "safe_message": "Fabric attempt was not found.",
        }
    decisions = routing_decisions_view(hermes_root=hermes_root, limit=_MAX_DECISIONS)
    try:
        registry = fabric.load_node_registry(hermes_root=hermes_root)
    except (fabric.FabricError, OSError, ValueError, TypeError):
        registry = {}
    try:
        with fabric._connect_readonly(path) as db:
            attempt_columns = _columns(db, "attempts")
            row = db.execute(
                "SELECT a.*,d.task_id,d.contract_sha256 FROM attempts a "
                "JOIN dispatches d ON d.dispatch_id=a.dispatch_id WHERE a.attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return {
                    "schema": VIEW_SCHEMA,
                    "success": False,
                    "available": False,
                    "code": "FABRIC_ATTEMPT_NOT_FOUND",
                    "safe_message": "Fabric attempt was not found.",
                }
            evidence: dict[str, Any] | None = None
            if row["evidence_json"]:
                try:
                    evidence = _evidence_view(json.loads(row["evidence_json"]))
                except (json.JSONDecodeError, TypeError):
                    evidence = None
            artifacts = _artifact_views(db, attempt_id)
    except (sqlite3.Error, OSError):
        return {
            "schema": VIEW_SCHEMA,
            "success": False,
            "available": False,
            "code": "FABRIC_JOURNAL_UNAVAILABLE",
            "safe_message": "Fabric coordinator journal could not be read.",
        }
    node = registry.get(str(row["node_name"]))
    base_row = _attempt_row(
        row,
        attempt_columns=attempt_columns,
        decisions=decisions,
        node_ceiling=node.max_authorization if node else "unknown",
    )
    base_row["evidence"] = evidence
    base_row["artifacts"] = artifacts
    base_row["events"] = _attempt_events(attempt_id)
    base_row["active_content_policy"] = "Active HTML/SVG/JavaScript artifacts are never rendered in the trusted Flight Deck origin."
    return {
        "schema": VIEW_SCHEMA,
        "success": True,
        "available": True,
        "attempt": base_row,
    }


__all__ = [
    "VIEW_SCHEMA",
    "attempt_detail",
    "attempts_view",
    "nodes_view",
    "routing_decisions_view",
]
