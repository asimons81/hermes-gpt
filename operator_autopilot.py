"""Durable Autopilot runtime for Hermes GPT v0.13 (PR1 slice).

This is the machinery that says "this Mission is under Autopilot control" and
nothing more. See ``docs/design/v0.13-autopilot.md`` for the full slice design
and invariants; this module implements PR1 only:

- ``hermes_autopilot_start`` / ``hermes_autopilot_status`` / ``hermes_autopilot_stop``.
- A durable ``autopilot_runs`` store (orchestration metadata only — never a
  shadow copy of Mission/node/delegation truth, which stay authoritative in
  ``operator_mission_runtime`` / ``operator_mission_plan`` / ``operator_delegations``).
- A detached worker process, reusing the exact spawn/register/reconcile
  pattern ``operator_codex.py`` already uses via ``operator_job_supervisor``,
  so Autopilot survives an MCP server restart or disconnect without ever
  trusting a cached in-memory belief about whether it is still running.

PR4 derives the Approval Frontier (``_frontier_view``) and reports
``waiting_for_owner``; it adds no approval mechanism.

PR3 adds observation and advancement (``_advance_nodes``): each in-flight node
is reconciled through ``hermes_delegation_reconcile`` and completes only when
the Work Contract validates ``SATISFIED`` against observed state.

PR2 adds the parallel DAG scheduler (``schedule_tick``): a peer caller one
level above ``operator_controller`` — it never changes ``_frontier()`` or
``hermes_controller_reconcile``. Each tick it dispatches up to
``max_concurrency - in_flight`` ready nodes through the existing
``hermes_placement_score`` -> ``hermes_contract_define`` ->
``hermes_delegation_dispatch`` chain, then moves the node to ``dispatched``
with a plan-version compare-and-swap. There is no ``autopilot_dispatch()``.
Observing/validating/completing nodes is PR3.

Reused, not rebuilt (BOUNDARY.md:25-29 — "the controller layer is a caller,
not a competing owner"): Mission lifecycle (``operator_mission_runtime``),
MissionPlan (``operator_mission_plan``), the canonical skill resolver
(``operator_skill_resolution``), the durable job/process primitive
(``operator_job_supervisor``), and the standard three-step OperatorPolicy gate
(``require_level`` -> ``require_mutation`` -> explicit ``confirm`` check) every
other mutating ``hermes_*`` tool in this codebase already follows.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_contract as contract_mod
import operator_controller as controller
import operator_delegations as deleg
import operator_failure_semantics as failure_semantics
import operator_job_supervisor as job_supervisor
import operator_live_events as live_events
import operator_mission_plan as mission_plan
import operator_mission_runtime as mission_runtime
import operator_placement as placement
import operator_policy as op
import operator_skill_resolution as skill_resolution

SCHEMA_VERSION = "hermes.autopilot/v1"

# Global machine gate (live read, never cached). Default OFF. Mirrors the
# idiom at operator_controller.py's CONTROLLER_EXECUTE_ENV / _execute_enabled.
AUTOPILOT_ENV = "HERMES_GPT_AUTOPILOT"

STATES = ("starting", "running", "waiting_for_owner", "stopping", "stopped", "completed", "failed")
TERMINAL_STATES = frozenset({"stopped", "completed", "failed"})

# Maps an operator_job_supervisor terminal job status onto an autopilot_runs
# state. A job "cancelled" via hermes_autopilot_stop maps to "stopped" (owner
# intent); any other terminal job status is a crash/exit and maps to "failed"
# unless the worker itself recorded a clean "completed".
_JOB_STATUS_TO_RUN_STATE = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "stopped",
    "timed_out": "failed",
}

MAX_CONCURRENCY_LIMIT = 16
MAX_REPLANS_LIMIT = 10
TICK_SECONDS = 2.0
IS_WINDOWS = os.name == "nt"

# --- PR2 scheduler constants -------------------------------------------------
# Nodes occupying a remote worker. awaiting_review/validated/awaiting_approval
# hold no worker slot (PR3/PR4 own them).
IN_FLIGHT_NODE_STATES = frozenset({"dispatched", "running"})
# Mission statuses in which Autopilot must not start new work (owner/budget stop).
MISSION_HOLD_STATUSES = frozenset({"paused", "blocked", "awaiting_approval"})
# Bounded retries of a *rejected* dispatch per (plan_version, node). Recovery on
# alternate placement is PR5; PR2 just refuses to hammer a failing backend.
MAX_DISPATCH_FAILURES = 3
SCHEDULER_LEASE_TTL_SECONDS = 120.0
SCHEDULER_TRIGGER_KIND = "autopilot"

# --- PR5 recovery constants ----------------------------------------------------
# Hard per-node attempt ceiling (first attempt included). Fed to the existing
# classifier as its retry breaker, so the ceiling is enforced by classify(), not
# by a second rule here. PR6 makes it owner-configurable.
MAX_NODE_ATTEMPTS = 3
# Backoff mirrors the taxonomy's transient action: base 30s, cap 15min, jitter.
RETRY_BACKOFF_BASE_SECONDS = 30.0
RETRY_BACKOFF_CAP_SECONDS = 900.0
RECOVERY_KEYS = ("placements", "nodes", "pending_supersede", "superseded_nodes", "replan_pending")

# --- PR3 advancement constants -------------------------------------------------
# The plan-node state machine path from dispatch to completion (§5.2). Autopilot
# only walks it for a node whose delegation it has just verified as SATISFIED.
NODE_ADVANCE_CHAIN = ("dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed")
# Nodes Autopilot observes each tick. The awaiting_* states are only advanced
# when Autopilot itself recorded that it was mid-walk (crash recovery); otherwise
# they may be an owner's manual gate and are left alone.
OBSERVED_NODE_STATES = frozenset({"dispatched", "running", "awaiting_review", "validated", "awaiting_approval"})
MID_WALK_STATES = frozenset({"awaiting_review", "validated", "awaiting_approval"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data_root(hermes_root: Path | None = None) -> Path:
    configured = hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    normalized = op.normalize_hermes_data_root(configured)
    return Path(normalized or configured).expanduser().resolve()


def _root(hermes_root: Path | None = None) -> Path:
    return _data_root(hermes_root) / "autopilot"


def _validate_mission_id(mission_id: str) -> str:
    value = str(mission_id or "").strip()
    if not mission_runtime.MISSION_ID_RE.fullmatch(value):
        raise ValueError("mission_id has an invalid format")
    return value


def job_id_for(mission_id: str, attempt: int) -> str:
    """A fresh job_id per start attempt.

    operator_job_supervisor terminal states are monotonic/final by design
    (mark_running refuses to resurrect a terminal record) — reusing one fixed
    job_id across restarts would make a second ``hermes_autopilot_start`` call
    after a stop/crash silently no-op forever. Attempt numbering lives on the
    autopilot_runs record (see ``_claim_run``); this function only formats it.
    """
    return f"autopilot:{_validate_mission_id(mission_id)}:{int(attempt)}"


def _run_path(mission_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{_validate_mission_id(mission_id)}.json"


def _lock_path(mission_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{_validate_mission_id(mission_id)}.lock"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@contextlib.contextmanager
def _record_lock(mission_id: str, hermes_root: Path | None = None) -> Iterator[None]:
    """Serialize autopilot_runs writers across independently restarted processes."""
    path = _lock_path(mission_id, hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+b")
    try:
        try:
            path.chmod(0o600)
        except OSError:
            pass
        if IS_WINDOWS:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_run(mission_id: str, hermes_root: Path | None = None) -> dict[str, Any] | None:
    return _load_json(_run_path(mission_id, hermes_root))


def _new_run_record(mission_id: str, *, attempt: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id,
        "enabled": True,
        "state": "starting",
        "attempt": attempt,
        "job_id": None,
        "max_concurrency": 0,
        "max_replans": 0,
        "replans_used": 0,
        "started_at": _now(),
        "last_tick_at": None,
        "last_event_cursor": 0,
        "config_sha256": "",
        "pid": None,
    }


def _write_run(mission_id: str, hermes_root: Path | None, **fields: Any) -> dict[str, Any]:
    with _record_lock(mission_id, hermes_root):
        path = _run_path(mission_id, hermes_root)
        record = _load_json(path) or _new_run_record(mission_id, attempt=0)
        record.update(fields)
        record["updated_at"] = _now()
        _atomic_json(path, record)
        return record


LIVE_STATES = frozenset({"starting", "running", "waiting_for_owner"})


def _set_live_state(mission_id: str, hermes_root: Path | None, state: str) -> None:
    """Move between live states only; never resurrect a stopping/terminal run.

    The worker reports ``running``/``waiting_for_owner`` every tick, while
    ``hermes_autopilot_stop`` and status reconciliation write ``stopped``/
    ``failed`` from other processes. An unconditional write here could overwrite
    a stop that landed mid-tick, so the check-and-write is one critical section.
    """
    with _record_lock(mission_id, hermes_root):
        path = _run_path(mission_id, hermes_root)
        current = _load_json(path)
        if current is None or current.get("state") not in LIVE_STATES or current.get("state") == state:
            return
        current["state"] = state
        current["updated_at"] = _now()
        _atomic_json(path, current)


def _claim_run(
    mission_id: str,
    hermes_root: Path | None,
    *,
    max_concurrency: int,
    max_replans: int,
    config_sha256: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Atomically claim the one-Mission-one-scheduler-lease slot.

    Returns ``(existing, None)`` when a non-terminal run already owns this
    Mission (idempotent — the caller must not spawn), or ``(None, claimed)``
    with a freshly written "starting" record (a new attempt number, therefore
    a fresh job_id) that the caller now owns and must spawn a worker for.
    Both the read and the write happen under one ``_record_lock`` critical
    section so two concurrent ``hermes_autopilot_start`` calls cannot both
    observe "nothing running" and both spawn a worker for the same Mission.
    """
    with _record_lock(mission_id, hermes_root):
        path = _run_path(mission_id, hermes_root)
        existing = _load_json(path)
        if existing is not None and existing.get("state") not in TERMINAL_STATES:
            return existing, None
        attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
        claimed = _new_run_record(mission_id, attempt=attempt)
        claimed.update({
            "state": "starting",
            "job_id": job_id_for(mission_id, attempt),
            "max_concurrency": max_concurrency,
            "max_replans": max_replans,
            "config_sha256": config_sha256,
            "last_event_cursor": live_events.high_watermark(hermes_root=hermes_root),
        })
        claimed["updated_at"] = _now()
        _atomic_json(path, claimed)
        return None, claimed


def _autopilot_enabled() -> bool:
    """Global machine gate (live read, never cached). Default OFF."""
    return os.environ.get(AUTOPILOT_ENV, "").strip() == "1"


def _bounded_int(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    try:
        ivalue = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not (minimum <= ivalue <= maximum):
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return ivalue


def _load_mission(mission_id: str, hermes_root: Path | None) -> dict[str, Any]:
    payload = json.loads(mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root))
    if not payload.get("success") or payload.get("found") is False:
        raise LookupError(f"mission {mission_id!r} was not found")
    return payload


def _load_plan(mission_id: str, hermes_root: Path | None) -> dict[str, Any]:
    payload = json.loads(mission_plan.hermes_plan_get(mission_id, hermes_root=hermes_root))
    if not payload.get("success") or payload.get("found") is False:
        raise LookupError(f"mission {mission_id!r} has no MissionPlan")
    if not payload.get("nodes"):
        raise ValueError("MissionPlan has no nodes")
    return payload


def _validate_plan_capabilities(plan: dict[str, Any], hermes_root: Path | None) -> None:
    """Reuse the same canonical resolver operator_mission_plan gates plan creation with."""
    for node in plan.get("nodes", []):
        capability = node.get("capability_req") or {}
        if not capability:
            continue
        rejection = skill_resolution.validate_required_skills(
            capability.get("profile", ""), capability.get("skills", []), hermes_root,
        )
        if rejection is not None:
            rejection = dict(rejection)
            rejection["node_id"] = node.get("node_id", "")
            raise skill_resolution.SkillRequirementsError(rejection)


def _error(exc: Exception, code: str, action: str, *, extra: dict[str, Any] | None = None) -> str:
    return json.dumps(op.error_from_exception(exc, layer="operator", code=code, suggested_action=action, extra=extra))


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    mission_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id}",
            extra={"mission_id": mission_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


# ---------------------------------------------------------------------------
# MCP-facing tools
# ---------------------------------------------------------------------------


def hermes_autopilot_start(
    mission_id: str,
    max_concurrency: int = 3,
    max_replans: int = 2,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Place a Mission under durable Autopilot control (PR1: runtime skeleton).

    Validates, before any write: the Mission exists and is not terminal, a
    MissionPlan exists with at least one node, and every node's
    ``capability_req`` resolves through the canonical skill resolver. A
    non-dry-run call additionally requires ``confirm=True`` and the
    ``HERMES_GPT_AUTOPILOT=1`` machine gate (default off). A second call while
    a non-terminal run already exists for this Mission is idempotent and does
    not spawn a second worker.

    PR1's worker only watches for external cancellation and Mission terminal
    state — it does not yet dispatch any node (see
    ``docs/design/v0.13-autopilot.md`` PR2/PR3).
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct autopilot start requires confirm=true")
        if not effective_dry and not _autopilot_enabled():
            raise PermissionError(f"direct autopilot start requires {AUTOPILOT_ENV}=1")

        mission_id = _validate_mission_id(mission_id)
        max_concurrency = _bounded_int(max_concurrency, minimum=1, maximum=MAX_CONCURRENCY_LIMIT, field="max_concurrency")
        max_replans = _bounded_int(max_replans, minimum=0, maximum=MAX_REPLANS_LIMIT, field="max_replans")

        mission = _load_mission(mission_id, hermes_root)
        if mission.get("status") in mission_runtime.TERMINAL_STATUSES:
            raise ValueError(f"mission is terminal ({mission.get('status')}); autopilot cannot start")

        plan = _load_plan(mission_id, hermes_root)
        _validate_plan_capabilities(plan, hermes_root)

        preview = _read_run(mission_id, hermes_root)
        if preview is not None and preview.get("state") not in TERMINAL_STATES:
            _audit("hermes_autopilot_start", policy, dry_run=effective_dry, success=True, changed=False,
                   mission_id=mission_id, extra={"idempotent": True})
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_start",
                "dry_run": effective_dry, "mission_id": mission_id, "idempotent": True, "run": preview,
            })

        config = {"max_concurrency": max_concurrency, "max_replans": max_replans}
        config_sha256 = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()

        if effective_dry:
            _audit("hermes_autopilot_start", policy, dry_run=True, success=True, changed=False, mission_id=mission_id)
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_start",
                "dry_run": True, "mission_id": mission_id, "would_start": True,
                "max_concurrency": max_concurrency, "max_replans": max_replans,
                "config_sha256": config_sha256, "node_count": len(plan.get("nodes", [])),
            })

        existing, claimed = _claim_run(
            mission_id, hermes_root,
            max_concurrency=max_concurrency, max_replans=max_replans, config_sha256=config_sha256,
        )
        if claimed is None:
            _audit("hermes_autopilot_start", policy, dry_run=False, success=True, changed=False,
                   mission_id=mission_id, extra={"idempotent": True})
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_start",
                "dry_run": False, "mission_id": mission_id, "idempotent": True, "run": existing,
            })

        job_id = claimed["job_id"]
        run_dir = _root(hermes_root)
        log_path = run_dir / f"{mission_id}.{claimed['attempt']}.log"
        job_supervisor.register_job(
            job_id, backend="autopilot", workspace=_data_root(hermes_root),
            log_path=log_path, source_record=_run_path(mission_id, hermes_root),
            hermes_root=hermes_root,
        )
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", mission_id,
                 "--job-id", job_id, "--root", str(_data_root(hermes_root))],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                cwd=str(_data_root(hermes_root)),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                start_new_session=not IS_WINDOWS,
            )
        except (OSError, ValueError) as exc:
            try:
                job_supervisor.terminalize(job_id, "failed", summary=op.redact_output(str(exc)), hermes_root=hermes_root)
            except FileNotFoundError:
                pass
            _write_run(mission_id, hermes_root, state="failed")
            return _error(exc, "AUTOPILOT_START_FAILED", "Check the Python interpreter and Hermes data root permissions.")

        # Deliberately do NOT call job_supervisor.mark_running from here with
        # proc.pid: reading /proc/<pid>/cmdline this soon after Popen() returns
        # can race a still-in-progress execve() and observe a transiently empty
        # cmdline (a documented Linux /proc quirk), which would durably record
        # a wrong process identity. The worker records its own (guaranteed
        # post-exec, therefore correct) identity as the first thing it does in
        # _worker() below. Until then job_supervisor reports status="queued",
        # which is truthful, not "running" with a corrupted identity.
        run = _write_run(mission_id, hermes_root, state="running", pid=proc.pid)
        _audit("hermes_autopilot_start", policy, dry_run=False, success=True, changed=True, mission_id=mission_id,
               extra={"job_id": job_id, "pid": proc.pid, "max_concurrency": max_concurrency, "max_replans": max_replans})
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_start",
            "dry_run": False, "mission_id": mission_id, "job_id": job_id, "run": run,
        })
    except skill_resolution.SkillRequirementsError as exc:
        payload = json.loads(_error(
            exc, "AUTOPILOT_SKILL_REQUIREMENTS_REJECTED",
            "Install the required skills in the requested Hermes profile before starting Autopilot.",
            extra={"skill_validation": exc.rejection},
        ))
        payload.update({"schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_start", "mission_id": mission_id})
        return json.dumps(payload)
    except (LookupError, ValueError, TypeError, PermissionError, OSError, json.JSONDecodeError) as exc:
        return _error(exc, "AUTOPILOT_START_REJECTED",
                      "Check Mission/Plan state, Operator policy level, and the HERMES_GPT_AUTOPILOT gate.")


def hermes_autopilot_status(mission_id: str, hermes_root: Path | None = None) -> str:
    """Read-only Autopilot status for a Mission; reconciles worker liveness first.

    Never trusts the cached ``autopilot_runs`` record alone: every call
    re-observes the owning ``operator_job_supervisor`` job (PID-reuse-resistant
    identity check) and syncs the run record if the worker terminated without
    Autopilot itself having recorded that yet — this is what keeps status
    truthful across an MCP server restart.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        mission_id = _validate_mission_id(mission_id)
        run = _read_run(mission_id, hermes_root)
        if run is None:
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_status",
                "mission_id": mission_id, "found": False,
            })
        job_id = run.get("job_id")
        job = job_supervisor.get_job(job_id, hermes_root=hermes_root, reconcile=True) if job_id else None
        if job is not None and run.get("state") not in TERMINAL_STATES:
            mapped = _JOB_STATUS_TO_RUN_STATE.get(str(job.get("status") or ""))
            if mapped and mapped != run.get("state"):
                run = _write_run(mission_id, hermes_root, state=mapped)
        _audit("hermes_autopilot_status", policy, dry_run=True, success=True, changed=False, mission_id=mission_id)
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_status",
            "mission_id": mission_id, "found": True, "run": run,
            "worker": {
                "pid": job.get("pid") if job else None,
                "status": job.get("status") if job else None,
                "process_verification": job.get("process_verification") if job else None,
            },
        })
    except (LookupError, ValueError, PermissionError, OSError, json.JSONDecodeError) as exc:
        return _error(exc, "AUTOPILOT_STATUS_FAILED", "Check the mission id and Operator read access.")


def hermes_autopilot_stop(
    mission_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Request that a Mission's Autopilot worker stop (owner-initiated, always allowed).

    Unlike ``hermes_autopilot_start``, stopping does not require the
    ``HERMES_GPT_AUTOPILOT`` machine gate — the safe direction is never gated,
    only starting new autonomous execution is.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct autopilot stop requires confirm=true")

        mission_id = _validate_mission_id(mission_id)
        run = _read_run(mission_id, hermes_root)
        if run is None or run.get("state") in TERMINAL_STATES:
            _audit("hermes_autopilot_stop", policy, dry_run=effective_dry, success=True, changed=False, mission_id=mission_id)
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
                "dry_run": effective_dry, "mission_id": mission_id, "changed": False,
                "state": run.get("state") if run else "not_found",
            })

        if effective_dry:
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
                "dry_run": True, "mission_id": mission_id, "would_stop": True, "state": run.get("state"),
            })

        job_id = run.get("job_id")
        if not job_id:
            run = _write_run(mission_id, hermes_root, state="stopped")
            _audit("hermes_autopilot_stop", policy, dry_run=False, success=True, changed=True, mission_id=mission_id)
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
                "dry_run": False, "mission_id": mission_id, "changed": True, "state": "stopped",
            })
        result = job_supervisor.request_cancel(job_id, hermes_root=hermes_root)
        if not result.get("success"):
            if result.get("code") == "JOB_NOT_FOUND":
                run = _write_run(mission_id, hermes_root, state="stopped")
                _audit("hermes_autopilot_stop", policy, dry_run=False, success=True, changed=True, mission_id=mission_id)
                return json.dumps({
                    "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
                    "dry_run": False, "mission_id": mission_id, "changed": True, "state": "stopped",
                })
            _audit("hermes_autopilot_stop", policy, dry_run=False, success=False, changed=False, mission_id=mission_id,
                   extra={"code": result.get("code")})
            return json.dumps({
                "success": False, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
                "mission_id": mission_id,
                "error": {
                    "code": result.get("code") or "AUTOPILOT_STOP_FAILED",
                    "message": result.get("safe_message") or "autopilot worker could not be safely stopped",
                },
            })

        mapped = _JOB_STATUS_TO_RUN_STATE.get(str(result.get("status") or ""), "stopped")
        run = _write_run(mission_id, hermes_root, state=mapped)
        _audit("hermes_autopilot_stop", policy, dry_run=False, success=True, changed=bool(result.get("changed")),
               mission_id=mission_id)
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_autopilot_stop",
            "dry_run": False, "mission_id": mission_id, "changed": bool(result.get("changed")), "state": mapped,
        })
    except (LookupError, ValueError, PermissionError, OSError, json.JSONDecodeError) as exc:
        return _error(exc, "AUTOPILOT_STOP_FAILED", "Check the mission id, Operator policy level, and job liveness.")


# ---------------------------------------------------------------------------
# PR2 — parallel DAG scheduler (peer caller above operator_controller)
# ---------------------------------------------------------------------------


def dispatch_key(mission_id: str, plan_version: int, node_id: str, attempt: int, contract_sha256: str) -> str:
    """Idempotency key for one node dispatch (design PR2).

    ``plan_version`` is part of the key because a replaced plan is a different
    lineage even when a node id and contract signature repeat.
    """
    return hashlib.sha256(
        f"{mission_id}|{int(plan_version)}|{node_id}|{int(attempt)}|{contract_sha256}".encode()
    ).hexdigest()


def _task_id(mission_id: str, node_id: str, key: str) -> str:
    """Deterministic task id: an exact retry maps onto the same delegation row."""
    return f"ap-{mission_id[:40]}-{node_id[:32]}-{key[:16]}"


def _delegation_id(key: str) -> str:
    """Deterministic delegation id.

    ``hermes_delegation_dispatch`` salts its default id with the wall clock, so a
    retry of the same ``task_id`` without an explicit id is rejected as a
    different lineage. A key-derived id makes an exact retry re-drive the same
    ``reserved`` row (one delegation, at most one accepted backend submission).
    """
    return f"dlg-{key[:20]}"


def _build_contract(
    mission_id: str, node: dict[str, Any], *, requirement: dict[str, Any], agent: str, key: str,
    attempt: int, hermes_root: Path | None,
) -> dict[str, Any]:
    """Bounded Work Contract for one node dispatch.

    INV-9: the plan store keeps only a hash of the node objective, so the
    objective is a deterministic pointer, never raw text. Completion evidence is
    never claimed here (``tests_pass``/``review_satisfied`` False): PR3 validates
    from observed state and fails closed when evidence is missing.
    """
    node_id = node["node_id"]
    profile = str(requirement.get("profile", ""))
    return {
        "schema": contract_mod.CONTRACT_SCHEMA,
        "task_id": _task_id(mission_id, node_id, key),
        "assigned_agent": agent,
        "assigned_profile": profile,
        "objective": f"autopilot dispatch: mission={mission_id} node={node_id} attempt={int(attempt)}",
        "allowed_scope": {"workspaces": [str(_data_root(hermes_root) / "missions")], "profiles": [profile]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {
            "class": str(requirement.get("authorization_class", "reversible_write")),
            "approved": True,
            "approved_by": "mission-owner",
            "approval_reference": f"mission:{mission_id}",
        },
    }


def _existing_delegation(mission_id: str, node_id: str, task_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    """A prior delegation for this node, from either scheduler.

    Matches Autopilot's own deterministic task id (crash between dispatch and
    the node transition) and the controller L2 rung's ``ctl-<mission>-<node>-``
    ids (the controller dispatches but never transitions ``plan_nodes``, so
    without this a controller-dispatched node would be dispatched twice).
    """
    dbp = deleg._db_path(hermes_root)
    if not dbp.is_file():
        return None
    ctl_prefix = f"ctl-{mission_id[:40]}-{node_id[:32]}-"
    try:
        with deleg._connect(dbp, write=False) as db:
            rows = db.execute(
                "SELECT delegation_id,task_id,state,dispatch_phase FROM delegations WHERE mission_id=? ORDER BY created_at",
                (mission_id,),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return None
    matches = [dict(r) for r in rows if r["task_id"] == task_id or str(r["task_id"]).startswith(ctl_prefix)]
    if not matches:
        return None
    # Prefer a live/successful lineage over a dead one.
    for row in matches:
        if row["state"] not in ("failed", "cancelled"):
            return row
    return matches[-1]


def _reached_backend(row: dict[str, Any]) -> bool:
    """A delegation row only proves a dispatch if it left the ``reserved`` phase.

    A rejected dispatch rolls back to ``reserved``/``reserved`` and never
    reached a backend; adopting it would mark a node ``dispatched`` that no
    worker holds.
    """
    return str(row.get("dispatch_phase") or "") != "reserved"


def _node_transition(
    mission_id: str, node_id: str, plan_version: int, hermes_root: Path | None, *, dry_run: bool,
    target: str = "dispatched", reason: str = "autopilot dispatch", bump_retries: bool = False,
) -> dict[str, Any]:
    return json.loads(mission_plan.hermes_plan_node_transition(
        mission_id, node_id, target, reason=reason,
        confirm=not dry_run, dry_run=dry_run, expected_plan_version=plan_version,
        bump_retries=bump_retries, hermes_root=hermes_root,
    ))


def _is_owner_gated(node: dict[str, Any]) -> bool:
    """Approval nodes and high-impact work stay with the human (PR4 owns the frontier)."""
    capability = node.get("capability_req") or {}
    return (
        node.get("kind") == mission_plan.KIND_APPROVAL
        or str(capability.get("authorization_class", "")) in controller.L2_FORBIDDEN_AUTH_CLASSES
    )


def _dispatch_one(
    mission_id: str, node: dict[str, Any], plan_version: int, hermes_root: Path | None,
    recovery: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Dispatch (or adopt) one ready node. Returns ``(outcome, detail)``.

    outcome: ``dispatched`` | ``adopted`` | ``held`` | ``failed`` | ``conflict``.
    Order matters: the delegation is created first, the node transition second.
    A crash between them leaves the node ``pending`` with a delegation that the
    next tick *adopts* (deterministic task id) instead of dispatching again.
    """
    node_id = node["node_id"]
    if _is_owner_gated(node):
        return "held", "owner_gate"
    recovery = recovery if recovery is not None else _empty_recovery()
    retry_info = recovery["nodes"].get(node_id) or {}
    if float(retry_info.get("not_before", 0) or 0) > time.time():
        return "held", "retry_backoff"
    attempt = int(node.get("retries", 0) or 0)
    key = dispatch_key(mission_id, plan_version, node_id, attempt, str(node.get("contract_sha256", "")))
    task_id = _task_id(mission_id, node_id, key)

    existing = _existing_delegation(mission_id, node_id, task_id, hermes_root)
    if existing is not None and existing["state"] in ("failed", "cancelled"):
        return "held", "prior_attempt_terminal"
    if existing is not None and not _reached_backend(existing) and existing["task_id"] != task_id:
        return "held", "prior_attempt_incomplete"  # a foreign (controller) reservation: never re-drive it
    if existing is not None and _reached_backend(existing):
        result = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False)
        if result.get("success"):
            return "adopted", existing["delegation_id"]
        if result.get("code") == "PLAN_VERSION_CONFLICT":
            return "conflict", "plan_version_conflict"
        return "failed", "adopt_transition_rejected"

    scored = json.loads(placement.hermes_placement_score(
        mission_id, node_id, confirm=True, dry_run=False, hermes_root=hermes_root,
    ))
    if scored.get("success") is False:
        return "failed", "placement_rejected"
    classification = str(scored.get("classification", ""))
    if classification == placement.CLASS_HUMAN:
        return "held", "owner_gate"
    if classification == placement.CLASS_NO_TARGET:
        return "held", "no_capable_target"
    requirement = scored.get("requirement") or {}
    if str(requirement.get("authorization_class", "")) in controller.L2_FORBIDDEN_AUTH_CLASSES:
        return "held", "owner_gate"
    if attempt > 0:
        agent, _profile, refusal = _bind_alternate(scored, requirement, list(retry_info.get("excluded", [])))
    else:
        agent, _profile, refusal = controller._l2_target_binding(scored, requirement)
    if refusal:
        return "held", "no_dispatchable_target"

    contract_doc = _build_contract(
        mission_id, node, requirement=requirement, agent=agent, key=key, attempt=attempt, hermes_root=hermes_root,
    )
    defined = json.loads(contract_mod.hermes_contract_define(json.dumps(contract_doc), hermes_root=hermes_root))
    if defined.get("success") is False:
        return "failed", "contract_rejected"

    # Pre-dispatch CAS: refuse before any remote side effect if the plan moved.
    pre = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=True)
    if not pre.get("success"):
        if pre.get("code") == "PLAN_VERSION_CONFLICT":
            return "conflict", "plan_version_conflict"
        return "held", "node_not_dispatchable"

    recovery["placements"][node_id] = agent  # remembered so a later failure can exclude this peer
    _save_recovery(mission_id, hermes_root, recovery)
    executed, result, reason, _linkage = controller._l2_dispatch(
        contract_doc, mission_id, hermes_root, delegation_id=_delegation_id(key),
    )
    if not executed:
        if reason == "ambiguous":
            return "held", "ambiguous_dispatch"  # delegation row exists; next tick adopts it
        return "failed", reason or result

    post = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False)
    if post.get("success"):
        return "dispatched", task_id
    if post.get("code") == "PLAN_VERSION_CONFLICT":
        return "conflict", "plan_version_conflict"
    return "failed", "transition_rejected"


# ---------------------------------------------------------------------------
# PR3 — observe, validate, advance (never trusts a worker's self-report)
# ---------------------------------------------------------------------------


def _walk_to(
    mission_id: str, node_id: str, current: str, target: str, plan_version: int, hermes_root: Path | None,
    *, reason: str,
) -> tuple[str, str]:
    """Walk the node state machine from ``current`` up to ``target`` along the chain.

    Returns ``(outcome, detail)``: ``ok`` | ``conflict`` | ``rejected``. Each step
    is its own CAS-guarded transition, so a partial walk is durable and resumable.
    """
    chain = NODE_ADVANCE_CHAIN
    for step in chain[chain.index(current) + 1: chain.index(target) + 1]:
        result = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False,
                                  target=step, reason=reason)
        if not result.get("success"):
            if result.get("code") == "PLAN_VERSION_CONFLICT":
                return "conflict", "plan_version_conflict"
            return "rejected", f"transition_to_{step}_rejected"
    return "ok", ""


def _verified_success(result: dict[str, Any]) -> bool:
    """Completion evidence gate: a node completes only on observed, validated state.

    ``hermes_delegation_reconcile`` promotes a delegation to ``succeeded`` only
    when the matching Work Contract validates ``SATISFIED`` against observed
    state; this re-checks that all three of state, verdict and the
    contract-bound ``evidence_ref`` agree, so a missing or partial result
    fails closed instead of completing the node.
    """
    delegation = result.get("delegation") or {}
    sha = str(delegation.get("contract_sha256") or "")
    return bool(
        result.get("success") is True
        and delegation.get("state") == "succeeded"
        and delegation.get("validation_verdict") == "SATISFIED"
        and sha
        and result.get("evidence_ref") == f"contract:{sha}"
    )


def _advance_one(
    mission_id: str, node: dict[str, Any], plan_version: int, walking: dict[str, Any], hermes_root: Path | None,
    recovery: dict[str, Any] | None = None, review_nodes: list[dict[str, Any]] | None = None,
    mission_ctx: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Observe one in-flight node and move it as far as observed evidence allows.

    outcome: ``completed`` | ``failed`` | ``running`` | ``retry`` | ``replanned`` | ``held`` | ``conflict``.
    """
    recovery = recovery if recovery is not None else _empty_recovery()
    mission_ctx = mission_ctx or {"status": "running", "final_approval_required": True}
    node_id = node["node_id"]
    state = node["state"]
    if _is_owner_gated(node):
        return "held", "owner_gate"
    if state in MID_WALK_STATES and walking.get(node_id) != plan_version:
        return "held", "review_gate"  # not parked by Autopilot: may be an owner's manual gate
    key = dispatch_key(mission_id, plan_version, node_id, int(node.get("retries", 0) or 0),
                       str(node.get("contract_sha256", "")))
    existing = _existing_delegation(mission_id, node_id, _task_id(mission_id, node_id, key), hermes_root)
    if existing is None or not _reached_backend(existing):
        return "held", "no_delegation"

    result = json.loads(deleg.hermes_delegation_reconcile(existing["delegation_id"], apply=True, hermes_root=hermes_root))
    if not result.get("success"):
        return "held", "reconcile_failed"
    if result.get("stale_observation"):
        return "held", "stale_observation"
    dstate = str((result.get("delegation") or {}).get("state") or "")

    if dstate == "cancelled":
        return _walk_to_failed(mission_id, node_id, plan_version, hermes_root, dstate)  # operator intent: no recovery
    if dstate == "failed":
        return _recover(mission_id, node, result, existing["delegation_id"], plan_version, recovery,
                        review_nodes or [], mission_ctx, hermes_root)
    if _verified_success(result):
        walking[node_id] = plan_version  # durable before the first step (see _advance_nodes)
        _write_run(mission_id, hermes_root, walking=dict(walking))
        outcome, detail = _walk_to(mission_id, node_id, state, "completed", plan_version, hermes_root,
                                   reason="autopilot: contract SATISFIED on observed state")
        if outcome == "ok":
            walking.pop(node_id, None)
            _write_run(mission_id, hermes_root, walking=dict(walking))
            return "completed", existing["delegation_id"]
        return ("conflict" if outcome == "conflict" else "held"), detail
    if dstate == "running" and state == "dispatched":
        outcome, detail = _walk_to(mission_id, node_id, "dispatched", "running", plan_version, hermes_root,
                                   reason="autopilot: backend observed running")
        return ("running", "") if outcome == "ok" else (("conflict" if outcome == "conflict" else "held"), detail)
    # queued / reconciling / running-already: nothing observed that justifies a move.
    return "held", dstate or "unobserved"


# ---------------------------------------------------------------------------
# PR5 — recovery: bounded retry (Level A) and bounded rework replan (Level B)
# ---------------------------------------------------------------------------
#
# Every decision comes from the existing failure classifier
# (operator_failure_semantics.classify): only a ``transient`` failure whose
# breaker is closed is retried, only a ``semantic`` failure with an eligible
# replan proposal is replanned, and every other class (authority, capability,
# environment, ambiguous, unknown) fails the node and is reported for a human.
# Recovery state lives in ``autopilot_runs.recovery`` (orchestration metadata);
# plan_nodes stays the only node-state store and the delegation store the only
# attempt history.


def _empty_recovery() -> dict[str, Any]:
    return {key: {} for key in RECOVERY_KEYS}


def _load_recovery(run: dict[str, Any]) -> dict[str, Any]:
    recovery = _empty_recovery()
    stored = run.get("recovery") or {}
    for key in RECOVERY_KEYS:
        if isinstance(stored.get(key), dict):
            recovery[key] = dict(stored[key])
    return recovery


def _save_recovery(mission_id: str, hermes_root: Path | None, recovery: dict[str, Any]) -> None:
    _write_run(mission_id, hermes_root, recovery=recovery)


def _backoff_seconds(node_id: str, attempt: int) -> float:
    base = min(RETRY_BACKOFF_CAP_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, int(attempt) - 1)))
    jitter = int(hashlib.sha256(f"{node_id}|{attempt}".encode()).hexdigest()[:4], 16) / 0xFFFF
    return min(RETRY_BACKOFF_CAP_SECONDS, base * (1 + 0.25 * jitter))


def _observation_env(
    node: dict[str, Any], result: dict[str, Any], mission_status: str, final_approval: bool,
) -> dict[str, Any]:
    """The classifier's observation envelope, built only from observed state."""
    delegation = result.get("delegation") or {}
    observed = result.get("observed") or {}
    retries = int(node.get("retries", 0) or 0)
    error = str(observed.get("error") or "")
    return {
        "delegation": {
            "state": str(delegation.get("state") or ""),
            "backend_state": str(delegation.get("backend_state") or ""),
            "outcome": str(delegation.get("outcome") or ""),
            # A failed run always fails the contract's own run-state criterion, so
            # NOT_SATISFIED here is a consequence of the failure, not independent
            # evidence about the work. Submitted as-is it would outrank every
            # error-text signal in the classifier and label a rate-limit as a
            # semantic defect. Withheld for failed delegations; the error channel
            # then decides, and unflavored failures fail closed as unknown.
            "validation_verdict": "" if str(delegation.get("state") or "") == "failed"
            else str(delegation.get("validation_verdict") or ""),
        },
        "runner": {
            "status": str(observed.get("status") or observed.get("state") or ""),
            "outcome": str(observed.get("outcome") or ""),
            "error": error,
        } if observed else None,
        "worker_exit": None,
        "last_failure_error": error,
        "capability": None,
        "plan": {
            "node_state": str(node.get("state") or ""),
            "parent_done": True,
            "all_children_terminal": False,
            "retries": retries,
            # Replan eligibility is judged on the failure class alone; the owner's
            # max_replans (enforced below) is the bound Autopilot honors.
            "replan_attempts_used": 0,
        },
        "mission": {"status": mission_status, "final_approval_required": bool(final_approval)},
        "breaker": {"consecutive_failures": retries + 1, "limit": MAX_NODE_ATTEMPTS, "gave_up": False},
    }


def _classify_failure(mission_id: str, node: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
    """Classify via the existing classifier; anything malformed fails closed (unknown)."""
    try:
        return failure_semantics.classify(mission_id, node["node_id"], failure_semantics._validate_envelope(env))
    except (failure_semantics.ObservationError, ValueError, TypeError, KeyError):
        return {"classification": failure_semantics.CLASS_UNKNOWN, "row_key": "unknown_fail_closed",
                "auto_retry": False, "need_attention": True}


def _bind_alternate(scored: dict[str, Any], requirement: dict[str, Any], excluded: list[str]) -> tuple[str, str, str]:
    """Prefer a dispatchable peer other than the ones this node already failed on.

    Falls back to the placement's own top binding (same peer) only when no other
    ``fleet_peer`` candidate survives placement's hard filters — a transient
    failure on the only capable peer is still worth a bounded retry.
    """
    for candidate in scored.get("candidate_set") or []:
        if not isinstance(candidate, dict) or candidate.get("kind") != "fleet_peer":
            continue
        name = str(candidate.get("name") or "")
        if name and name not in excluded:
            return name, str(requirement.get("profile", "")), ""
    return controller._l2_target_binding(scored, requirement)


def _find_clone(review_nodes: list[dict[str, Any]], failed_id: str) -> str | None:
    prefix = f"{failed_id[:58]}-r"
    clones = sorted(n["node_id"] for n in review_nodes
                    if n["node_id"].startswith(prefix) and n["node_id"][len(prefix):].isdigit())
    return clones[-1] if clones else None


def _settle_supersessions(
    mission_id: str, plan_version: int, nodes: list[dict[str, Any]], recovery: dict[str, Any],
    hermes_root: Path | None,
) -> list[str]:
    """Mark replaced failed attempts as superseded once their successor exists.

    Runs every tick and is idempotent, so a crash between dispatching a
    successor and marking the old attempt just retries. Until it succeeds, the
    failed attempt still counts against the Mission (fail closed).
    """
    settled: list[str] = []
    by_id = {n["node_id"]: n for n in nodes}
    for node_id, old_delegation in sorted(recovery["pending_supersede"].items()):
        node = by_id.get(node_id)
        if node is None or node["state"] in ("pending", "blockable"):
            continue  # the successor has not been dispatched yet
        key = dispatch_key(mission_id, plan_version, node_id, int(node.get("retries", 0) or 0),
                           str(node.get("contract_sha256", "")))
        successor = _existing_delegation(mission_id, node_id, _task_id(mission_id, node_id, key), hermes_root)
        if successor is None or not _reached_backend(successor):
            continue
        if mission_runtime.supersede_delegation_attachment(
            mission_id, old_delegation, successor["delegation_id"], hermes_root=hermes_root,
        ):
            recovery["pending_supersede"].pop(node_id, None)
            settled.append(node_id)
    if settled:
        _save_recovery(mission_id, hermes_root, recovery)
    return settled


def _replan(
    mission_id: str, node_id: str, plan_version: int, old_delegation: str, recovery: dict[str, Any],
    review_nodes: list[dict[str, Any]], hermes_root: Path | None,
) -> tuple[str, str]:
    """Level B: replace a failed node by a rework clone. Idempotent across crashes.

    ``replan_pending`` is written before anything changes; a resumed call finds
    an already-created clone and adopts it instead of creating a second one.
    """
    clone_id = _find_clone(review_nodes, node_id)
    if clone_id is None:
        try:
            patched = mission_plan.apply_rework_patch(
                mission_id, node_id, expected_plan_version=plan_version, hermes_root=hermes_root)
        except mission_plan.PlanVersionConflict:
            return "conflict", "plan_version_conflict"
        except (ValueError, LookupError, OSError, sqlite3.Error, skill_resolution.SkillRequirementsError):
            recovery["replan_pending"].pop(node_id, None)
            _save_recovery(mission_id, hermes_root, recovery)
            return "failed", "replan_refused"
        clone_id = patched["clone_node_id"]
    recovery["replan_pending"].pop(node_id, None)
    recovery["superseded_nodes"][node_id] = clone_id
    recovery["pending_supersede"][clone_id] = old_delegation
    _save_recovery(mission_id, hermes_root, recovery)
    # Derived, not incremented: a crash between patching and recording cannot
    # double-count or lose a replan.
    _write_run(mission_id, hermes_root, replans_used=len(recovery["superseded_nodes"]))
    return "replanned", clone_id


def _recover(
    mission_id: str, node: dict[str, Any], result: dict[str, Any], delegation_id: str, plan_version: int,
    recovery: dict[str, Any], review_nodes: list[dict[str, Any]], mission_ctx: dict[str, Any],
    hermes_root: Path | None,
) -> tuple[str, str]:
    """A delegation for this node failed: retry, replan, or fail it — per the classifier.

    outcome: ``retry`` | ``replanned`` | ``failed`` | ``conflict`` | ``held``.
    """
    node_id = node["node_id"]
    decision = _classify_failure(mission_id, node, _observation_env(
        node, result, mission_ctx["status"], mission_ctx["final_approval_required"]))
    classification = str(decision.get("classification") or "")
    label = f"{classification}:{decision.get('row_key', '')}"
    run = _read_run(mission_id, hermes_root) or {}
    attempt = int(node.get("retries", 0) or 0)

    if (
        classification == failure_semantics.CLASS_TRANSIENT
        and decision.get("row_key") == "retry_transient_backoff"
        and decision.get("auto_retry") is True
    ):
        info = recovery["nodes"].setdefault(node_id, {})
        info["prev_delegation"] = delegation_id
        info["not_before"] = time.time() + _backoff_seconds(node_id, attempt + 1)
        peer = recovery["placements"].get(node_id)
        if peer:
            info["excluded"] = sorted(set(info.get("excluded", [])) | {peer})
        recovery["pending_supersede"][node_id] = delegation_id
        _save_recovery(mission_id, hermes_root, recovery)  # intent first: a crash mid-walk resumes from here
        for target, bump in (("paused", False), ("blockable", True)):
            step = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False, target=target,
                                    reason=f"autopilot retry: {label}", bump_retries=bump)
            if not step.get("success"):
                return ("conflict", "plan_version_conflict") if step.get("code") == "PLAN_VERSION_CONFLICT" else ("held", "retry_transition_rejected")
        return "retry", label

    if (
        classification == failure_semantics.CLASS_SEMANTIC
        and (decision.get("replan_proposal") or {}).get("eligible") is True
        and int(run.get("replans_used", 0) or 0) + len(recovery["replan_pending"]) < int(run.get("max_replans", 0) or 0)
        and not _is_owner_gated(node)
    ):
        recovery["replan_pending"][node_id] = delegation_id
        _save_recovery(mission_id, hermes_root, recovery)
        step = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False, target="failed",
                                reason=f"autopilot replan: {label}")
        if not step.get("success"):
            return ("conflict", "plan_version_conflict") if step.get("code") == "PLAN_VERSION_CONFLICT" else ("held", "fail_transition_rejected")
        return _replan(mission_id, node_id, plan_version, delegation_id, recovery, review_nodes, hermes_root)

    outcome, detail = _walk_to_failed(mission_id, node_id, plan_version, hermes_root, "failed")
    return outcome, f"{label}{' need_attention' if decision.get('need_attention') else ''}" if outcome == "failed" else detail


def _walk_to_failed(
    mission_id: str, node_id: str, plan_version: int, hermes_root: Path | None, dstate: str,
) -> tuple[str, str]:
    result = _node_transition(mission_id, node_id, plan_version, hermes_root, dry_run=False,
                              target="failed", reason=f"autopilot: delegation {dstate}")
    if result.get("success"):
        return "failed", dstate
    return ("conflict", "plan_version_conflict") if result.get("code") == "PLAN_VERSION_CONFLICT" else ("held", "fail_transition_rejected")


def _advance_nodes(
    mission_id: str, review: dict[str, Any], plan_version: int, hermes_root: Path | None, summary: dict[str, Any],
    db: sqlite3.Connection, lease_lock: str, recovery: dict[str, Any], mission_ctx: dict[str, Any],
) -> bool:
    """Observe every in-flight node. Returns False if the plan moved under us."""
    run = _read_run(mission_id, hermes_root) or {}
    walking = {k: int(v) for k, v in (run.get("walking") or {}).items()}
    nodes = sorted(review.get("nodes", []), key=lambda n: n["node_id"])

    # Resume a replan that crashed after the node was failed but before the clone
    # was recorded (idempotent: an existing clone is adopted, never duplicated).
    for node_id, old_delegation in sorted(recovery["replan_pending"].items()):
        node = next((n for n in nodes if n["node_id"] == node_id), None)
        if node is not None and node["state"] == "failed":
            outcome, detail = _replan(mission_id, node_id, plan_version, old_delegation, recovery, nodes, hermes_root)
            if outcome == "conflict":
                summary["skipped"] = detail
                return False
            summary["replanned" if outcome == "replanned" else "failed_nodes"][node_id] = detail
    _settle_supersessions(mission_id, plan_version, nodes, recovery, hermes_root)

    for node in nodes:
        if node["state"] not in OBSERVED_NODE_STATES:
            continue
        controller.renew_lease(db, mission_id, lease_lock, ttl=SCHEDULER_LEASE_TTL_SECONDS)
        outcome, detail = _advance_one(mission_id, node, plan_version, walking, hermes_root,
                                       recovery, nodes, mission_ctx)
        if outcome == "completed":
            summary["completed"].append(node["node_id"])
        elif outcome == "failed":
            summary["failed_nodes"][node["node_id"]] = detail
        elif outcome == "retry":
            summary["retried"][node["node_id"]] = detail
        elif outcome == "replanned":
            summary["replanned"][node["node_id"]] = detail
        elif outcome == "running":
            summary["advanced"].append(node["node_id"])
        elif outcome == "conflict":
            summary["skipped"] = detail
            return False
        else:
            summary["observed_held"][node["node_id"]] = detail
    return True


# ---------------------------------------------------------------------------
# PR4 — Approval Frontier (a derived read, never a new gate)
# ---------------------------------------------------------------------------


def _frontier_view(review: dict[str, Any], mission_status: str) -> dict[str, Any]:
    """Derive where Autopilot must hand control to the owner.

    Active when a gated node (approval-kind or ``high_impact``) has been
    *reached* — it is ready, or the owner has already started moving it — or the
    Mission itself is ``awaiting_approval``. Descendants of a gated node are
    blocked without any extra logic (their parent is not ``completed``), so
    independent branches keep running. ``waiting`` is True only when the
    frontier is active and Autopilot has nothing else it could do, which is what
    ``waiting_for_owner`` means. Resolution stays exactly the existing owner
    tools (``hermes_mission_approve`` / the owner's own node transitions).
    """
    nodes = review.get("nodes", [])
    ready = set(review.get("ready_nodes", []))
    reached = sorted(
        n["node_id"] for n in nodes
        if _is_owner_gated(n)
        and n["state"] not in mission_plan.TERMINAL_NODE_STATES
        and (n["node_id"] in ready or n["state"] != "pending")
    )
    mission_gate = mission_status == "awaiting_approval"
    actionable = any(
        (n["state"] in OBSERVED_NODE_STATES or n["node_id"] in ready) and not _is_owner_gated(n)
        for n in nodes
    )
    reasons = (["mission_awaiting_approval"] if mission_gate else []) + (["owner_gate_node"] if reached else [])
    active = bool(reasons)
    return {"active": active, "waiting": active and (mission_gate or not actionable), "reasons": reasons, "nodes": reached}


def schedule_tick(mission_id: str, hermes_root: Path | None, *, max_concurrency: int) -> dict[str, Any]:
    """One scheduling pass: fill free worker slots with ready nodes.

    Holds the controller's per-mission pass lease for the tick so a controller
    reconcile pass cannot dispatch the same Mission concurrently (two
    schedulers controlling one Mission is a release blocker). Returns a bounded
    summary (ids and counts only).
    """
    summary: dict[str, Any] = {
        "plan_version": None, "slots": 0, "in_flight": 0,
        "dispatched": [], "adopted": [], "held": {}, "failed": {}, "skipped": "",
        "completed": [], "failed_nodes": {}, "advanced": [], "observed_held": {},
        "frontier": None, "mission_status": "", "mission_reconciled": False,
        "retried": {}, "replanned": {},
    }
    if not _autopilot_enabled():
        summary["skipped"] = "autopilot_gate_off"
        return summary
    mission = _load_mission(mission_id, hermes_root)
    status = str(mission.get("status") or "")
    if status in mission_runtime.TERMINAL_STATUSES or status in MISSION_HOLD_STATUSES:
        summary["skipped"] = f"mission_{status}"
        if status == "awaiting_approval":
            summary["frontier"] = {"active": True, "waiting": True, "reasons": ["mission_awaiting_approval"], "nodes": []}
        return summary

    db_path = controller._db_path(hermes_root)
    lease_lock = f"autopilot-{os.getpid()}-{secrets.token_hex(4)}"
    with controller._connect(db_path, write=True) as db:
        lease = controller.acquire_lease(
            db, mission_id, SCHEDULER_TRIGGER_KIND, ttl=SCHEDULER_LEASE_TTL_SECONDS, lease_lock=lease_lock,
        )
        if not lease.get("acquired"):
            summary["skipped"] = "controller_pass_active"
            return summary
        try:
            mission_ctx = {"status": status, "final_approval_required": bool(mission.get("final_approval_required", True))}
            return _schedule_locked(mission_id, hermes_root, max_concurrency, summary, db, lease_lock, mission_ctx)
        finally:
            controller.release_lease(db, mission_id, lease_lock)


def _dispatch_candidates(review: dict[str, Any]) -> list[str]:
    """Ready nodes: pending (the plan's own ready set) plus ``blockable`` retries.

    A retried node parks in ``blockable`` (paused -> blockable is the state
    machine's own path back to ``dispatched``). ``hermes_plan_review`` only
    lists ``pending`` nodes as ready, so retry candidates are derived here with
    the same "every parent completed" rule.
    """
    nodes = {n["node_id"]: n for n in review.get("nodes", [])}
    ready = set(review.get("ready_nodes", []))
    for node in nodes.values():
        if node["state"] == "blockable" and all(
            parent in nodes and nodes[parent]["state"] == "completed" for parent in node.get("parents", [])
        ):
            ready.add(node["node_id"])
    return sorted(ready)


def _schedule_locked(
    mission_id: str, hermes_root: Path | None, max_concurrency: int, summary: dict[str, Any],
    db: sqlite3.Connection, lease_lock: str, mission_ctx: dict[str, Any],
) -> dict[str, Any]:
    review = json.loads(mission_plan.hermes_plan_review(mission_id, hermes_root=hermes_root))
    if not review.get("success") or review.get("found") is False:
        summary["skipped"] = "no_plan"
        return summary
    plan_version = int(review["version"])
    summary["plan_version"] = plan_version
    recovery = _load_recovery(_read_run(mission_id, hermes_root) or {})
    if not _advance_nodes(mission_id, review, plan_version, hermes_root, summary, db, lease_lock, recovery, mission_ctx):
        return summary
    # Re-read: completions free slots and unblock children for this same tick.
    review = json.loads(mission_plan.hermes_plan_review(mission_id, hermes_root=hermes_root))
    if not review.get("success") or int(review.get("version", -1)) != plan_version:
        summary["skipped"] = "plan_version_conflict"
        return summary
    nodes = {n["node_id"]: n for n in review.get("nodes", [])}
    in_flight = sum(1 for n in nodes.values() if n["state"] in IN_FLIGHT_NODE_STATES)
    slots = max(0, int(max_concurrency) - in_flight)
    summary.update({"plan_version": plan_version, "slots": slots, "in_flight": in_flight})

    run = _read_run(mission_id, hermes_root) or {}
    failures = {k: int(v) for k, v in (run.get("dispatch_failures") or {}).items()}

    for node_id in sorted(_dispatch_candidates(review)):
        if slots <= 0:
            break
        fail_key = f"{plan_version}:{node_id}"
        if failures.get(fail_key, 0) >= MAX_DISPATCH_FAILURES:
            summary["held"][node_id] = "dispatch_failed"
            continue
        controller.renew_lease(db, mission_id, lease_lock, ttl=SCHEDULER_LEASE_TTL_SECONDS)
        outcome, detail = _dispatch_one(mission_id, nodes[node_id], plan_version, hermes_root, recovery)
        if outcome in ("dispatched", "adopted"):
            summary[outcome].append(node_id)
            slots -= 1
        elif outcome == "held":
            summary["held"][node_id] = detail
        elif outcome == "conflict":
            # The plan was replaced under us: every remaining decision is stale.
            summary["skipped"] = detail
            break
        else:
            failures[fail_key] = failures.get(fail_key, 0) + 1
            summary["failed"][node_id] = detail
    _write_run(mission_id, hermes_root, dispatch_failures=failures)
    final = json.loads(mission_plan.hermes_plan_review(mission_id, hermes_root=hermes_root))
    if final.get("success") and int(final.get("version", -1)) == plan_version:
        summary["frontier"] = _frontier_view(final, "")
        nodes_now = final.get("nodes", [])
        _settle_supersessions(mission_id, plan_version, nodes_now, recovery, hermes_root)
        # A failed node that was replaced by a rework clone no longer counts: the
        # clone (and its superseded-attempt marker) carries the Mission forward.
        replaced = set(recovery["superseded_nodes"]) - set(recovery["pending_supersede"])
        remaining = [n for n in nodes_now if not (n["state"] == "failed" and n["node_id"] in replaced)]
        if remaining and all(n["state"] == "completed" for n in remaining):
            # Every node is done on observed evidence: let the *existing* Mission
            # reconcile derive the Mission status from verified children. It only
            # reaches awaiting_approval (never completed) while final approval is
            # required, and Autopilot never approves.
            reconciled = json.loads(mission_runtime.hermes_mission_reconcile(
                mission_id, confirm=True, dry_run=False, hermes_root=hermes_root))
            current = _load_mission(mission_id, hermes_root)
            summary["mission_status"] = str(current.get("status") or "")
            if summary["mission_status"] == "awaiting_approval":
                summary["frontier"] = _frontier_view(final, "awaiting_approval")
            summary["mission_reconciled"] = bool(reconciled.get("success"))
    return summary


# ---------------------------------------------------------------------------
# Detached worker (PR2: watch + schedule; node observation/completion is PR3)
# ---------------------------------------------------------------------------


def _worker(mission_id: str, job_id: str, hermes_root: Path | None) -> int:
    try:
        job_supervisor.mark_running(job_id, os.getpid(), hermes_root=hermes_root)
    except FileNotFoundError:
        return 2
    _write_run(mission_id, hermes_root, state="running", pid=os.getpid())
    try:
        while True:
            job = job_supervisor.get_job(job_id, hermes_root=hermes_root, reconcile=False)
            if job is None:
                return 2
            job_status = str(job.get("status") or "")
            if job_status in job_supervisor.TERMINAL_STATES:
                # Already finalized externally (e.g. hermes_autopilot_stop).
                # Sync our own record and exit without re-terminalizing.
                _write_run(mission_id, hermes_root,
                           state=_JOB_STATUS_TO_RUN_STATE.get(job_status, "stopped"))
                return 0
            mission = json.loads(mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root))
            if mission.get("status") in mission_runtime.TERMINAL_STATUSES:
                terminal = job_supervisor.terminalize(job_id, "completed", hermes_root=hermes_root)
                _write_run(mission_id, hermes_root,
                           state=_JOB_STATUS_TO_RUN_STATE.get(str(terminal.get("status")), "completed"))
                return 0
            run = _read_run(mission_id, hermes_root) or {}
            try:
                tick = schedule_tick(mission_id, hermes_root, max_concurrency=int(run.get("max_concurrency") or 1))
                last_error = ""
            except (OSError, sqlite3.Error, ValueError, LookupError, json.JSONDecodeError) as exc:
                # Transient store contention must not kill a durable worker;
                # anything else still fails closed via the outer handler.
                tick = {"skipped": "tick_error"}
                last_error = op.redact_output(f"{type(exc).__name__}: {exc}")[:200]
            _write_run(mission_id, hermes_root, last_tick_at=_now(), last_schedule=tick, last_error=last_error)
            frontier = tick.get("frontier")
            if frontier is not None:
                _set_live_state(mission_id, hermes_root, "waiting_for_owner" if frontier["waiting"] else "running")
            time.sleep(TICK_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a durable worker must fail closed, never crash silently
        try:
            job_supervisor.terminalize(job_id, "failed", summary=op.redact_output(str(exc))[:500], hermes_root=hermes_root)
        except FileNotFoundError:
            pass
        _write_run(mission_id, hermes_root, state="failed")
        return 1


def _main(argv: list[str]) -> int:
    if len(argv) >= 7 and argv[1] == "--worker" and argv[3] == "--job-id" and argv[5] == "--root":
        try:
            mission_id = _validate_mission_id(argv[2])
        except ValueError:
            return 2
        job_id = str(argv[4] or "").strip()
        if not job_id:
            return 2
        hermes_root = Path(argv[6]).expanduser().resolve()
        return _worker(mission_id, job_id, hermes_root)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))


__all__ = [
    "AUTOPILOT_ENV",
    "SCHEMA_VERSION",
    "STATES",
    "TERMINAL_STATES",
    "dispatch_key",
    "hermes_autopilot_start",
    "hermes_autopilot_status",
    "hermes_autopilot_stop",
    "job_id_for",
    "schedule_tick",
]
