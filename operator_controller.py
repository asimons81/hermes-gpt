"""Supervised mission controller — shadow/observe reconciler loop.

Implements architecture proposal §17 item 6 / §7 / D2 / D10: a **long-running
controller process** in the hermes-gpt sidecar that reconciles missions against
their plan, driving the *existing* host Kanban dispatcher + ProviderProfile
routing (D2: via import_hermes/seams protocols; host kanban_watchers left
running as-is). This slice ships the controller in **shadow/observe mode**:

- **Observes** authoritative sidecar state (mission, plan_nodes, delegation,
  runner) and Classifies it via ``operator_failure_semantics`` (the Ops 8-class
  taxonomy + smallest-first §11.2 recovery matrix).
- **Emits** a plan + next-action + the commands it *would* run (``would_be_commands``)
  and the smallest recovery action **as decision output only** — ``would_execute``
  is always ``False``; no dispatch/reclaim/redispatch/approval/completion path exists.
- **Writes ONLY** the controller's own surfaces: ``controller_plan`` (the recorded
  decision), ``controller_pass_lease`` (per-mission concurrency safety), and
  ``controller_telemetry`` (per-pass telemetry) — plus a heartbeat file. It never
  mutates mission/plan/delegation/budget/attachment state, never dispatches work,
  never completes or approves a Mission (D5/P3/§7.7 hard walls).

Loop mechanics (§7.1–§7.6):
1. Trigger model T1–T5 (§7.2) enqueues a work request per mission.
2. **Conflate** — one in-flight pass per mission; a second trigger while a pass
   holds the lease marks "recheck needed" (§7.1).
3. **Per-mission pass lease** (§7.3) — a single guarded ``UPDATE`` acquired on
   ``(lease_expires IS NULL OR lease_expires < now)``; TTL =
   ``min(max_pass_duration, reconcile_interval*2)``; heartbeat-renewed;
   crash-reclaimed by TTL expiry (kill -9 of the owning process reclaims the pass
   on the next trigger).
4. **Classify** — the trigger is not the decision; classify the observation
   envelope (§11.1). Un-observable state fail-closes to ``blocked``/``reconciling``
   with ``need_attention`` (§7.5).
5. **Smallest action** from the recovery matrix as decision output (§11.2).
6. **Idempotency** (§7.4) — controller operation idempotency key =
   ``sha256(mission_id|node|stage_id|contract_sha|attempt_seq)``.
7. **Replan** (§7.6) — proposal-only for ``semantic_failure``, bounded (≤1), routed
   through the existing decompose/advance tools, never executed here.

Authority (D10): mutating autonomy is gated on BOTH the QA evidence model and the
Security authority model being present — both parents are ``done`` for this card.
This slice is L0/L1 only: the controller never jumps rungs, never holds completion
authority, and records under ``read_only`` + ``dry_run`` (an L1 shadow record).

INV-9 data containment holds: no raw prompt / transcript / error body / objective
is persisted — only enums, hashes, matched token ids, counts, and bounded refs.

§12.2 telemetry/health + alerting (t_e8468723): every pass records its
``escalation_tier`` (GREEN/YELLOW/RED); ``controller_status()`` serves the
24h-windowed aggregate health + tier rollup; a RED pass spools a
fleet-attention INTERRUPT envelope (``missions/controller_attention_spool.jsonl``)
for the existing delivery-broker lane — the controller NEVER sends (no
self-send from inside the loop), and INV-10 keeps refused gates loud: a stale
heartbeat reads RED, a spool failure raises, and the status tool audits tier
+ spool counts.

Conventions mirror ``operator_failure_semantics`` / ``operator_placement``:
public functions return a bounded JSON envelope; read surfaces require
``read_only``; every call is audited (bounded summary + counts only).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import operator_delegations as deleg
import operator_failure_semantics as fs
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_policy as op
import operator_runners as runners

SCHEMA_VERSION = "0.9-controller.1"
PASS_SCHEMA = "hermes.controller-pass/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = plan.NODE_ID_RE
SHA_RE = mission.SHA_RE

# ---------------------------------------------------------------------------
# §7.2 trigger model (T1–T5) + §12.2 pass-result vocabulary
# ---------------------------------------------------------------------------

TRIGGER_PERIODIC = "T1_periodic"
TRIGGER_LIVE_EVENT = "T2_live_event"
TRIGGER_DEPENDENCY = "T3_dependency"
TRIGGER_HEALTH = "T4_health"
TRIGGER_MANUAL = "T5_manual"
TRIGGERS = (
    TRIGGER_PERIODIC,
    TRIGGER_LIVE_EVENT,
    TRIGGER_DEPENDENCY,
    TRIGGER_HEALTH,
    TRIGGER_MANUAL,
)

# pass_result (telemetry §12.2): noop | dispatched | recovered | escalated |
# blocked | stale
PASS_NOOP = "noop"
PASS_DISPATCHED = "dispatched"
PASS_RECOVERED = "recovered"
PASS_ESCALATED = "escalated"
PASS_BLOCKED = "blocked"
PASS_STALE = "stale"
PASS_RESULTS = (
    PASS_NOOP,
    PASS_DISPATCHED,
    PASS_RECOVERED,
    PASS_ESCALATED,
    PASS_BLOCKED,
    PASS_STALE,
)

# §12.2 alerting tiers. GREEN = healthy/silent; YELLOW = degraded but
# self-healing (count-only in status output); RED = a human gate is genuinely
# required -> the pass spools a fleet-attention INTERRUPT envelope for the
# delivery broker (never self-sent from inside the loop).
TIER_GREEN = "GREEN"
TIER_YELLOW = "YELLOW"
TIER_RED = "RED"
TIERS = (TIER_GREEN, TIER_YELLOW, TIER_RED)

CONTROLLER_MODE = "shadow/observe"  # this slice ships at L0/L1 only (D10 gate)

# ---------------------------------------------------------------------------
# §7.3 lease TTL = min(max_pass_duration, reconcile_interval*2)
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_SECONDS = 90.0
MIN_INTERVAL_SECONDS = 15.0
MAX_PASS_DURATION_SECONDS = 300.0
HEARTBEAT_STALE_SECONDS = 180.0  # watchdog: fresh if last beat within this window

# §12.2 aggregate-health window. Telemetry older than this falls out of the
# "reconciled_24h" and per-class counters (all-time totals stay available via
# pass_results_all_time below).
AGGREGATE_WINDOW_SECONDS = 24 * 3600.0

# Stale-reconciling threshold for the RED tier (§12.2 alerting): a mission
# blocked/reconciling with need_attention for longer than this is RED.
STALE_RECONCILING_SECONDS = 3600.0

MAX_MISSIONS_PER_PASS = 64
MAX_WOULD_BE_COMMANDS = 8
MAX_NODES = 512
MAX_TARGET_STRING = 256
MAX_PLAN_NODE_LIMIT = 200


def _lease_ttl_seconds(interval: float = DEFAULT_INTERVAL_SECONDS) -> float:
    """TTL = min(max_pass_duration, reconcile_interval*2) (§7.3)."""
    return max(MIN_INTERVAL_SECONDS, min(MAX_PASS_DURATION_SECONDS, interval * 2.0))


# ---------------------------------------------------------------------------
# Small helpers (mirror sibling operator modules)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _root(hermes_root: Path | None) -> Path:
    return mission._root(hermes_root)


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


def _audit(
    tool: str,
    policy: op.OperatorPolicy | None,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    mission_id: str = "",
    node_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level if policy else "read_only",
            apply_mode=policy.apply_mode if policy else "dry_run",
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id} node={node_id}",
            extra={"mission_id": mission_id, "node_id": node_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


def _sanitize(text: Any, limit: int = MAX_TARGET_STRING) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    if len(value) > limit:
        return value[:limit] + "…"
    return value


def _idempotency_key(
    mission_id: str,
    node_id: str,
    stage_id: str,
    contract_sha: str,
    attempt_seq: int,
) -> str:
    """§7.4 controller operation idempotency key."""
    return hashlib.sha256(
        f"{mission_id}|{node_id}|{stage_id}|{contract_sha}|{int(attempt_seq)}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Durable controller store: controller_plan (reused) + controller_pass_lease +
# controller_telemetry. All additive, IF NOT EXISTS, INDEPENDENT of the mission
# lifecycle tables — the controller never touches missions/plan_nodes/attachments.
# ---------------------------------------------------------------------------


def _init_controller_tables(db: sqlite3.Connection) -> None:
    fs._init_tables(db)  # controller_plan (shared with failure semantics)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS controller_pass_lease (
            mission_id TEXT PRIMARY KEY,
            lease_lock TEXT NOT NULL,
            lease_expires TEXT NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT '',
            pass_seq INTEGER NOT NULL DEFAULT 0,
            recheck_needed INTEGER NOT NULL DEFAULT 0,
            node_id TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ctl_lease_expires ON controller_pass_lease(lease_expires);
        CREATE TABLE IF NOT EXISTS controller_telemetry (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            trigger_kind TEXT NOT NULL DEFAULT '',
            node_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            pass_result TEXT NOT NULL DEFAULT '',
            classification TEXT NOT NULL DEFAULT '',
            row_key TEXT NOT NULL DEFAULT '',
            would_execute INTEGER NOT NULL DEFAULT 0,
            lease_acquired INTEGER NOT NULL DEFAULT 0,
            actions_taken_json TEXT NOT NULL DEFAULT '[]',
            need_attention INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ctl_telem_mission ON controller_telemetry(mission_id, created_at);
        CREATE TABLE IF NOT EXISTS controller_trigger_queue (
            mission_id TEXT PRIMARY KEY,
            trigger_kind TEXT NOT NULL,
            ref TEXT NOT NULL DEFAULT '',
            seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    # §12.2 telemetry additions (t_e8468723): lease_reclaimed (true reclaim
    # counting, distinct from conflation stale passes) + escalation_tier.
    # Additive ALTERs guarded by presence checks — safe on existing stores.
    telem_cols = {
        r["name"] for r in db.execute("PRAGMA table_info(controller_telemetry)")
    }
    if "lease_reclaimed" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN lease_reclaimed "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "escalation_tier" not in telem_cols:
        db.execute(
            "ALTER TABLE controller_telemetry ADD COLUMN escalation_tier "
            "TEXT NOT NULL DEFAULT ''"
        )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_controller_tables(db)
        return db
    return mission._connect(path, write=False)


# ---------------------------------------------------------------------------
# §7.3 per-mission pass lease: single guarded UPDATE, TTL, heartbeat, reclaim
# ---------------------------------------------------------------------------


def _now_iso_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expiry(ttl: float) -> str:
    return datetime.fromtimestamp(_now_ts() + ttl, tz=timezone.utc).isoformat()


def acquire_lease(
    db: sqlite3.Connection,
    mission_id: str,
    trigger_kind: str,
    *,
    ttl: float,
    lease_lock: str,
    node_id: str = "",
) -> dict[str, Any]:
    """Acquire (or reclaim) the per-mission pass lease.

    §7.3: a single row UPDATE guarded by ``(lease_expires IS NULL OR
    lease_expires < now)``; if the row is absent, a guarded INSERT. A live pass
    (un-expired lease) is never stolen — the acquire returns ``acquired=False``
    and the caller conflates. A crashed pass (no release, TTL expired) is
    reclaimed on the next acquire.
    """
    now = _now_iso_ts()
    expiry = _lease_expiry(ttl)
    now_iso = _now()
    # 1. Try to reclaim/renew an existing expired-or-empty lease via a single
    #    guarded UPDATE (rowcount is the authoritative signal).
    cur = db.execute(
        "UPDATE controller_pass_lease SET lease_lock=?, lease_expires=?, trigger_kind=?, "
        "pass_seq=pass_seq+1, heartbeat_at=?, node_id=?, updated_at=? "
        "WHERE mission_id=? AND (lease_expires IS NULL OR lease_expires < ?)",
        (lease_lock, expiry, trigger_kind, now_iso, node_id, now_iso, mission_id, now),
    )
    if cur.rowcount == 1:
        db.commit()
        seq = _lease_seq(db, mission_id)
        return {
            "acquired": True,
            "mission_id": mission_id,
            "lease_lock": lease_lock,
            "lease_expires": expiry,
            "pass_seq": seq,
            "recheck": False,
            "reclaimed": True,
            "ttl_seconds": ttl,
        }
    # 2. Row absent → guarded INSERT. IGNORE so a concurrently-held lease wins.
    cur = db.execute(
        "INSERT OR IGNORE INTO controller_pass_lease("
        "mission_id,lease_lock,lease_expires,trigger_kind,pass_seq,recheck_needed,node_id,heartbeat_at,created_at,updated_at) "
        "VALUES(?,?,?,?,1,0,?,?,?,?)",
        (
            mission_id,
            lease_lock,
            expiry,
            trigger_kind,
            node_id,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    if cur.rowcount == 1:
        db.commit()
        return {
            "acquired": True,
            "mission_id": mission_id,
            "lease_lock": lease_lock,
            "lease_expires": expiry,
            "pass_seq": 1,
            "recheck": False,
            "reclaimed": False,
            "ttl_seconds": ttl,
        }
    # 3. A live lease is held by another pass → do not steal; conflate.
    db.commit()
    holder = _lease_info(db, mission_id)
    return {
        "acquired": False,
        "mission_id": mission_id,
        "lease_lock": lease_lock,
        "lease_expires": "",
        "pass_seq": holder.get("pass_seq", 0),
        "recheck": True,
        "reclaimed": False,
        "holder_lock": holder.get("lease_lock", ""),
        "holder_expires": holder.get("lease_expires", ""),
    }


def renew_lease(
    db: sqlite3.Connection, mission_id: str, lease_lock: str, *, ttl: float
) -> dict[str, Any]:
    """Heartbeat renewal — only the current lock may renew (CAS)."""
    now = _now_iso_ts()
    expiry = _lease_expiry(ttl)
    cur = db.execute(
        "UPDATE controller_pass_lease SET lease_expires=?, heartbeat_at=?, updated_at=? "
        "WHERE mission_id=? AND lease_lock=? AND (lease_expires IS NULL OR lease_expires >= ?)",
        (expiry, now, now, mission_id, lease_lock, now),
    )
    db.commit()
    return {
        "renewed": cur.rowcount == 1,
        "lease_expires": expiry if cur.rowcount else "",
    }


def release_lease(
    db: sqlite3.Connection, mission_id: str, lease_lock: str
) -> dict[str, Any]:
    """Release the lease on pass completion (CAS on lock)."""
    cur = db.execute(
        "DELETE FROM controller_pass_lease WHERE mission_id=? AND lease_lock=?",
        (mission_id, lease_lock),
    )
    db.commit()
    return {"released": cur.rowcount == 1}


def _lease_seq(db: sqlite3.Connection, mission_id: str) -> int:
    row = db.execute(
        "SELECT pass_seq FROM controller_pass_lease WHERE mission_id=?", (mission_id,)
    ).fetchone()
    return int(row["pass_seq"]) if row else 0


def _lease_info(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT lease_lock,lease_expires,pass_seq,recheck_needed,node_id,heartbeat_at "
        "FROM controller_pass_lease WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "lease_lock": row["lease_lock"],
        "lease_expires": row["lease_expires"],
        "pass_seq": int(row["pass_seq"]),
        "recheck_needed": bool(row["recheck_needed"]),
        "node_id": row["node_id"],
        "heartbeat_at": row["heartbeat_at"],
    }


def mark_recheck(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    """Conflation (§7.1): a second trigger while a pass holds the lease."""
    cur = db.execute(
        "UPDATE controller_pass_lease SET recheck_needed=1, updated_at=? "
        "WHERE mission_id=? AND lease_expires >= ?",
        (_now(), mission_id, _now_iso_ts()),
    )
    db.commit()
    return {"recheck_needed": cur.rowcount == 1}


# ---------------------------------------------------------------------------
# §7.1 trigger queue (T1–T5) + conflation
# ---------------------------------------------------------------------------


def trigger(
    mission_id: str,
    trigger_kind: str,
    ref: str = "",
    *,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Enqueue a work request (mission_id + trigger kind + monotonic seq).

    One row per mission: a second trigger overwrites (conflate) and, if a pass
    currently holds the lease, marks ``recheck_needed`` so the loop re-runs
    after the in-flight pass.
    """
    if not MISSION_ID_RE.fullmatch(mission_id or ""):
        raise ValueError("mission_id is invalid")
    if trigger_kind not in TRIGGERS:
        raise ValueError(f"trigger_kind must be one of {TRIGGERS}")
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        mission._get_row(db, mission_id)  # verify the mission exists
        row = db.execute(
            "SELECT seq FROM controller_trigger_queue WHERE mission_id=?", (mission_id,)
        ).fetchone()
        seq = (int(row["seq"]) if row else 0) + 1
        db.execute(
            "INSERT INTO controller_trigger_queue(mission_id,trigger_kind,ref,seq,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET "
            "trigger_kind=excluded.trigger_kind, ref=excluded.ref, seq=excluded.seq, "
            "created_at=excluded.created_at",
            (mission_id, trigger_kind, _sanitize(ref, 128), seq, _now()),
        )
        mark_recheck(db, mission_id)  # conflate a live pass if one is running
        db.commit()
    return {
        "enqueued": True,
        "mission_id": mission_id,
        "trigger_kind": trigger_kind,
        "seq": seq,
    }


def conflate(
    db: sqlite3.Connection, *, limit: int = MAX_MISSIONS_PER_PASS
) -> list[dict[str, Any]]:
    """§7.1: de-duplicate to one work request per mission (latest trigger wins)."""
    rows = db.execute(
        "SELECT mission_id,trigger_kind,ref,seq FROM controller_trigger_queue "
        "ORDER BY seq ASC LIMIT ?",
        (max(1, min(int(limit), MAX_MISSIONS_PER_PASS)),),
    ).fetchall()
    return [
        {
            "mission_id": r["mission_id"],
            "trigger_kind": r["trigger_kind"],
            "ref": r["ref"],
            "seq": int(r["seq"]),
        }
        for r in rows
    ]


def consume_trigger(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    cur = db.execute(
        "DELETE FROM controller_trigger_queue WHERE mission_id=?", (mission_id,)
    )
    db.commit()
    return {"consumed": cur.rowcount == 1}


# ---------------------------------------------------------------------------
# Observation adapter — §11.1 "authoritative observation only"
# ---------------------------------------------------------------------------


class HostObservationAdapter(Protocol):
    """Seam (D2) — a deployed controller reads host-kanban signals through this.

    Shadow standalone ships ``NullHostAdapter``: host-only signals are missing,
    so the classifier fail-closes to ``unknown``/``blocked`` + ``need_attention``
    (§7.5) rather than guessing. A real deployment injects a host adapter that
    reads the host Kanban store (worker exit classification, dispatcher breaker,
    last-failure error) via ``import_hermes`` — never re-implementing it.
    """

    def worker_exit(self, mission_id: str, node_id: str) -> dict[str, Any] | None: ...
    def breaker(self, mission_id: str) -> dict[str, Any]: ...
    def last_failure_error(self, mission_id: str, node_id: str) -> str: ...
    def capability(self, mission_id: str, node_id: str) -> dict[str, Any] | None: ...


class NullHostAdapter:
    """Default for shadow mode: no host signals, so observation is fail-closed."""

    def worker_exit(self, mission_id: str, node_id: str) -> dict[str, Any] | None:
        return None

    def breaker(self, mission_id: str) -> dict[str, Any]:
        return {"consecutive_failures": 0, "limit": 3, "gave_up": False}

    def last_failure_error(self, mission_id: str, node_id: str) -> str:
        return ""

    def capability(self, mission_id: str, node_id: str) -> dict[str, Any] | None:
        return None


# ---------------------------------------------------------------------------
# Frontier selection + observation envelope building
# ---------------------------------------------------------------------------

INFlight_ORDER = ("failed", "dispatched", "running", "awaiting_review", "validated")
READY_STATES = ("pending", "blockable")
NODE_TERMINAL = frozenset({"completed", "failed"})


def _read_nodes(db: sqlite3.Connection, mission_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT node_id,state,deps,contract_sha256,failure_kind,retries,epoch "
        "FROM plan_nodes WHERE mission_id=? ORDER BY node_id LIMIT ?",
        (mission_id, MAX_NODES),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            deps = json.loads(r["deps"] or "[]")
        except json.JSONDecodeError:
            deps = []
        out.append(
            {
                "node_id": r["node_id"],
                "state": r["state"],
                "deps": deps if isinstance(deps, list) else [],
                "contract_sha256": r["contract_sha256"],
                "failure_kind": r["failure_kind"],
                "retries": int(r["retries"] or 0),
                "epoch": int(r["epoch"] or 0),
            }
        )
    return out


def _parent_done(nodes: list[dict[str, Any]], node: dict[str, Any]) -> bool:
    parents = [p for p in node["deps"] if p]
    if not parents:
        return True
    by_id = {n["node_id"]: n for n in nodes}
    for p in parents:
        parent = by_id.get(p)
        if parent is None or parent["state"] != "completed":
            return False
    return True


def _all_terminal(nodes: list[dict[str, Any]]) -> bool:
    if not nodes:
        return False
    return all(n["state"] in NODE_TERMINAL for n in nodes)


def _frontier(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Deterministic smallest-action frontier (priority, stable by node_id)."""
    for state in INFlight_ORDER:
        for n in nodes:
            if n["state"] == state:
                return n
    for n in nodes:
        if n["state"] in READY_STATES and _parent_done(nodes, n):
            return n
    return None


def _mission_plan_meta(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT status,version,plan_sha256 FROM mission_plans WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    if not row:
        return {"exists": False, "status": "", "version": 0, "plan_sha256": ""}
    return {
        "exists": True,
        "status": row["status"],
        "version": int(row["version"]),
        "plan_sha256": row["plan_sha256"],
    }


def _replan_attempts_used(db: sqlite3.Connection, mission_id: str) -> int:
    try:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM controller_plan "
            "WHERE mission_id=? AND row_key='escalate_semantic'",
            (mission_id,),
        ).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0


def _latest_delegation(
    hermes_root: Path | None, mission_id: str, contract_sha256: str = ""
) -> dict[str, Any] | None:
    """Read authoritative delegation state, optionally bound to a contract.

    When a frontier node supplies a contract hash, never substitute an
    unrelated mission-level latest delegation: parallel nodes must be observed
    against their own durable lineage.
    """
    dbp = deleg._db_path(hermes_root)
    if not dbp.is_file():
        return None
    try:
        with deleg._connect(dbp, write=False) as db:
            if contract_sha256:
                row = db.execute(
                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "
                    "FROM delegations WHERE mission_id=? AND contract_sha256=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (mission_id, contract_sha256),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "
                    "FROM delegations WHERE mission_id=? ORDER BY updated_at DESC LIMIT 1",
                    (mission_id,),
                ).fetchone()
            if not row:
                return None
            return dict(row)
    except (sqlite3.Error, FileNotFoundError):
        return None


def _runner_observation(
    hermes_root: Path | None, task_id: str
) -> dict[str, Any] | None:
    """Best-effort runner observation (authoritative where available)."""
    if not task_id:
        return None
    try:
        runs = runners.observed_runs(task_id, hermes_root=hermes_root)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        sqlite3.Error,
    ):
        runs = []
    if not runs:
        return None
    latest = runs[-1] if isinstance(runs, list) and runs else None
    if not isinstance(latest, dict):
        return None
    return {
        "status": _sanitize(latest.get("status", ""), 64),
        "outcome": _sanitize(latest.get("outcome", ""), 64),
        "error": _sanitize(latest.get("error", ""), fs.MAX_ERROR_TEXT),
    }


def build_observation(
    db: sqlite3.Connection,
    hermes_root: Path | None,
    mission_id: str,
    host: HostObservationAdapter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the authoritative observation envelope + the frontier node.

    Returns ``(env, frontier)`` where ``frontier`` is ``{}`` when there is no
    actionable plan node (mission-level classification).
    """
    mrow = mission._get_row(db, mission_id)
    spec = json.loads(mrow["spec_json"])
    mission_status = mrow["status"]
    final_approval = bool(spec.get("final_approval_required", True))

    nodes = _read_nodes(db, mission_id)
    frontier = _frontier(nodes) or {}
    node_id = frontier.get("node_id", "")
    node_state = frontier.get("state", "")
    parent_done = _parent_done(nodes, frontier) if frontier else False
    all_terminal = _all_terminal(nodes)
    retries = int(frontier.get("retries", 0) or 0)
    replan_used = _replan_attempts_used(db, mission_id)

    deleg_state: dict[str, Any] | None = None
    if node_id and frontier.get("contract_sha256"):
        deleg_state = _latest_delegation(
            hermes_root, mission_id, str(frontier["contract_sha256"])
        )

    delegation: dict[str, Any] | None = None
    runner: dict[str, Any] | None = None
    task_id = ""
    if deleg_state:
        delegation = {
            "state": deleg_state.get("state", ""),
            "backend_state": deleg_state.get("backend_state", ""),
            "outcome": deleg_state.get("outcome", ""),
            "validation_verdict": deleg_state.get("validation_verdict", ""),
        }
        task_id = deleg_state.get("task_id", "")
        runner = _runner_observation(hermes_root, task_id)

    worker_exit = host.worker_exit(mission_id, node_id)
    breaker = host.breaker(mission_id)
    last_error = host.last_failure_error(mission_id, node_id)
    capability = host.capability(mission_id, node_id)

    env: dict[str, Any] = {
        "mission": {
            "status": mission_status,
            "final_approval_required": final_approval,
        },
        "plan": {
            "node_state": node_state,
            "parent_done": parent_done,
            "all_children_terminal": all_terminal,
            "retries": retries,
            "replan_attempts_used": replan_used,
        },
        "delegation": delegation,
        "runner": runner,
        "worker_exit": worker_exit,
        "last_failure_error": last_error,
        "capability": capability,
        "breaker": breaker,
    }
    return env, frontier


# ---------------------------------------------------------------------------
# Would-be commands (§7.4 idempotency) — descriptive only, never executed
# ---------------------------------------------------------------------------


def _would_be_commands(
    row_key: str,
    mission_id: str,
    node_id: str,
    contract_sha: str,
    attempt_seq: int,
    delegation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The commands a higher-autonomy rung *would* run (D10: L0/L1 only)."""
    if not row_key:
        return []
    commands: list[dict[str, Any]] = []
    tgt = node_id or (delegation or {}).get("delegation_id", "")
    idem = _idempotency_key(
        mission_id, node_id or "", node_id or "", contract_sha, attempt_seq
    )
    spec_by_row = {
        "dispatch_ready_child": (
            "hermes_swarm_stage_dispatch",
            "dispatch ready child to the assigned profile",
        ),
        "reclaim_dead_worker": (
            "host:kanban reclaim_task",
            "reclaim a dead worker + bounded retry",
        ),
        "observe_reconciling": (
            "hermes_delegation_reconcile",
            "observe + report; never auto-redispatch",
        ),
        "retry_transient_backoff": (
            "dispatcher retry (breaker respected)",
            "backoff + retry on a transient failure",
        ),
        "breaker_exhausted": (
            "controller signal (broker) + operator",
            "blocked + gave_up + human signal",
        ),
        "escalate_semantic": (
            "hermes_swarm_stage_advance rework | hermes_plan_decompose",
            "escalate + bounded replan proposal",
        ),
        "signal_awaiting_approval": (
            "hermes_mission_reconcile",
            "signal awaiting_approval, then stop",
        ),
        "request_completion": (
            "hermes_mission_reconcile",
            "request completion via verified lifecycle",
        ),
        "fail_closed_evidence": (
            "reconcile tools (hermes_mission_reconcile)",
            "fail-closed on missing evidence; need_attention",
        ),
        "park_authority": (
            "controller signal + operator/owner",
            "park blocked; human action; no auto-retry",
        ),
        "park_capability": (
            "controller signal + Orchestrator",
            "park blocked; placement reviewed",
        ),
        "recover_environment": (
            "controller signal + operator",
            "recover around or escalate; never silently retried",
        ),
        "terminal_no_action": ("", "terminal; no action"),
        "wait_recheck": ("", "no action; recheck next trigger"),
        "unknown_fail_closed": (
            "controller signal (broker)",
            "fail-closed blocked + need_attention",
        ),
    }
    tool, rationale = spec_by_row.get(row_key, ("", ""))
    if tool:
        commands.append(
            {
                "action": row_key,
                "tool": tool,
                "target": _sanitize(tgt, 64),
                "rationale": rationale,
                "idempotency_key": idem,
                "executed": False,
            }
        )
    return commands[:MAX_WOULD_BE_COMMANDS]


# ---------------------------------------------------------------------------
# pass_result mapping (§12.2 telemetry vocabulary)
# ---------------------------------------------------------------------------


def _pass_result(row_key: str) -> str:
    if row_key in ("dispatch_ready_child",):
        return PASS_DISPATCHED
    if row_key in (
        "reclaim_dead_worker",
        "retry_transient_backoff",
        "recover_environment",
    ):
        return PASS_RECOVERED
    if row_key in ("escalate_semantic",):
        return PASS_ESCALATED
    if row_key in (
        "park_authority",
        "park_capability",
        "fail_closed_evidence",
        "breaker_exhausted",
        "unknown_fail_closed",
    ):
        return PASS_BLOCKED
    return PASS_NOOP


# ---------------------------------------------------------------------------
# §12.2 alerting tiers: GREEN silent / YELLOW count-only / RED -> broker
# ---------------------------------------------------------------------------

# Hard-wall rows: the controller can propose NO safe next action and a human
# gate is genuinely required (RED by definition in §12.2).
RED_ROW_KEYS = frozenset(
    {
        "park_authority",
        "park_capability",
        "breaker_exhausted",
        "unknown_fail_closed",
        "fail_closed_evidence",
    }
)
# YELLOW classes: abnormal, but self-healing or routable through normal lanes
# without a human gate (counted in status, never interrupted on).
YELLOW_CLASSES = frozenset(
    {
        fs.CLASS_TRANSIENT,
        fs.CLASS_SEMANTIC,
        fs.CLASS_ENVIRONMENT,
        fs.CLASS_AMBIGUOUS,
    }
)
_GREEN_CLASSES = frozenset(
    {
        fs.CLASS_NONE,
        fs.CLASS_NONE_DISPATCHABLE,
        fs.CLASS_NONE_APPROVAL,
        fs.CLASS_NONE_COMPLETION,
        fs.CLASS_WAITING,
        fs.CLASS_TERMINAL,
    }
)


def derive_pass_tier(
    *,
    mission_status: str,
    classification: str,
    row_key: str,
    need_attention: bool,
    stuck_s: float | None = None,
) -> tuple[str, list[str]]:
    """Deterministic §12.2 tier for one pass.

    RED (§12.2): mission blocked/reconciling past the stale threshold with
    ``need_attention``, breaker ``gave_up``, or a hard capability/authority
    wall (including fail-closed unknown/evidence walls — INV-10: a refused
    gate is as loud as permitted). YELLOW: abnormal but self-healing or
    routable without a human gate. GREEN: steady state (silent).
    """
    if row_key in RED_ROW_KEYS:
        return TIER_RED, [f"hard_wall:{row_key}"]
    if (
        need_attention
        and mission_status in ("blocked",)
        and stuck_s is not None
        and stuck_s > STALE_RECONCILING_SECONDS
    ):
        return TIER_RED, ["stale_blocked_reconciling"]
    if classification in YELLOW_CLASSES:
        return TIER_YELLOW, [f"class:{classification}"]
    if classification == fs.CLASS_UNKNOWN:
        # Unknown without a hard-wall row key still fail-closes loudly.
        return TIER_RED, ["fail_closed:unknown"]
    if classification in _GREEN_CLASSES:
        return TIER_GREEN, ["steady_state"]
    return TIER_YELLOW, [f"unmapped_class:{classification or 'none'}"]


def _attention_spool_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "controller_attention_spool.jsonl"


def build_attention_envelope(
    *,
    mission_id: str,
    node_id: str,
    classification: str,
    row_key: str,
    proposed_action: str,
    reasons: list[str],
    uncertainty: str,
    tier_reasons: list[str],
    pass_seq: int,
) -> dict[str, Any]:
    """Build one fleet-attention INTERRUPT envelope for a RED pass (§12.2).

    Schema: fleet-attention-envelope/v1 (ops delivery contract). INV-9: only
    bounded enums, ids, and fixed matrix strings — never raw objective/error/
    secret text. Delivery is NOT performed here: the envelope is spooled for
    the existing delivery-broker lane (no self-send from inside the loop).
    """
    approval_wall = row_key == "park_authority"
    summary = (
        f"Supervised mission controller (shadow/observe) classified a pass as "
        f"RED — a human gate is genuinely required. mission={mission_id} "
        f"node={node_id or '-'} classification={classification} "
        f"row_key={row_key} proposed_action={_sanitize(proposed_action, 200)} "
        f"reasons={','.join(tier_reasons[:4])}"
    )
    if uncertainty:
        summary += f" uncertainty={_sanitize(uncertainty, 64)}"
    return {
        "schema_version": 1,
        "event_id": f"ctl-{uuid.uuid4().hex[:24]}",
        "source": {
            "kind": "system",
            "profile": "ops",
            "job_id": "hermes-gpt-controller",
            "job_name": "supervised-mission-controller (shadow)",
            "run_id": f"pass:{pass_seq}",
        },
        "domain": "operations",
        "severity": "P1",
        "attention_class": "INTERRUPT",
        "state": "open",
        "action_required": True,
        "approval_required": approval_wall,
        "title": f"Mission controller RED: {row_key} ({mission_id})"[:200],
        "summary": summary[:4000],
        "dedupe_key": f"controller:red:{mission_id}:{row_key}"[:300],
        "occurred_at": _now(),
        "evidence": [
            f"missions/missions.db#controller_telemetry(mission_id={mission_id})",
            f"missions/missions.db#controller_plan(mission_id={mission_id})",
            "missions/controller_heartbeat.json",
        ][:32],
        "metadata": {
            "tier": TIER_RED,
            "mode": CONTROLLER_MODE,
            "would_execute": False,
            "classification": classification,
            "row_key": row_key,
            "classification_uncertainty": _sanitize(uncertainty, 64),
            "escalation_reasons": [
                _sanitize(r, 64) for r in (*tier_reasons, *reasons[:2])
            ][:8],
            "proposed_action": _sanitize(proposed_action, 200),
        },
    }


def spool_attention_envelope(
    envelope: dict[str, Any], hermes_root: Path | None = None
) -> Path:
    """Append one RED envelope to the controller attention spool.

    INV-10 (loud as permitted): a spool failure raises — the pass fails loudly
    rather than silently dropping a human-gate signal. This never sends; the
    existing fleet-attention delivery lane owns transport.
    """
    path = _attention_spool_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(envelope, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def attention_envelopes(hermes_root: Path | None = None) -> list[dict[str, Any]]:
    """Read-only: spooled RED envelopes awaiting broker flush (oldest first)."""
    path = _attention_spool_path(hermes_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _stuck_seconds(db: sqlite3.Connection, mission_id: str) -> float | None:
    """Seconds since the oldest still-relevant need_attention pass (approx.).

    Shadow-slice approximation: age of the oldest need_attention telemetry
    row inside the aggregate window. ``None`` when the mission has never
    raised attention (nothing is stuck).
    """
    cutoff = datetime.fromtimestamp(
        _now_ts() - AGGREGATE_WINDOW_SECONDS, tz=timezone.utc
    ).isoformat()
    try:
        row = db.execute(
            "SELECT MIN(created_at) AS c FROM controller_telemetry "
            "WHERE mission_id=? AND need_attention=1 AND created_at>=?",
            (mission_id, cutoff),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["c"]:
        return None
    try:
        started = datetime.fromisoformat(row["c"])
    except ValueError:
        return None
    return max(0.0, _now_ts() - started.timestamp())


# ---------------------------------------------------------------------------
# Telemetry (per-pass §12.2) + aggregate health §12.2
# ---------------------------------------------------------------------------


def _record_telemetry(db: sqlite3.Connection, entry: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO controller_telemetry("
        "mission_id,trigger_kind,node_id,started_at,duration_ms,pass_result,"
        "classification,row_key,would_execute,lease_acquired,actions_taken_json,need_attention,created_at,"
        "lease_reclaimed,escalation_tier) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry["mission_id"],
            entry.get("trigger_kind", ""),
            entry.get("node_id", ""),
            entry["started_at"],
            int(entry.get("duration_ms", 0)),
            entry.get("pass_result", PASS_NOOP),
            entry.get("classification", ""),
            entry.get("row_key", ""),
            0,
            1 if entry.get("lease_acquired") else 0,
            _canonical(entry.get("actions_taken", [])),
            1 if entry.get("need_attention") else 0,
            entry["started_at"],
            1 if entry.get("lease_reclaimed") else 0,
            entry.get("escalation_tier", ""),
        ),
    )


# ---------------------------------------------------------------------------
# The single-pass reconciler (shadow/observe): observe → classify → smallest
# action as decision output; writes ONLY controller_plan + controller_telemetry
# + controller_pass_lease.
# ---------------------------------------------------------------------------


def reconcile_pass(
    mission_id: str,
    trigger_kind: str,
    *,
    host: HostObservationAdapter | None = None,
    hermes_root: Path | None = None,
    lease_lock: str = "",
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Run one shadow pass over a mission.

    This is the heart of the loop (§7.1): acquire the pass lease → observe →
    classify → smallest action from the recovery matrix → record to
    ``controller_plan`` + ``controller_telemetry`` → release the lease. In
    shadow mode nothing dispatches, reclaims, redispatch, completes, or approves;
    ``would_execute`` is always ``False`` and the returned envelope carries the
    ``would_be_commands`` for a higher-autonomy rung.
    """
    started = _now()
    started_ts = _now_ts()
    host = host or NullHostAdapter()
    lease_lock = lease_lock or f"shadow:{os.getpid()}"
    ttl = _lease_ttl_seconds(interval)
    path = _db_path(hermes_root)

    pass_env: dict[str, Any] = {
        "schema": PASS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id,
        "node_id": "",
        "trigger_kind": trigger_kind,
        "mode": CONTROLLER_MODE,
        "would_execute": False,
        "lease_acquired": False,
        "lease_reclaimed": False,
        "pass_result": PASS_STALE,
        "classification": "",
        "row_key": "",
        "proposed_action": "",
        "would_be_commands": [],
        "need_attention": False,
        "actions_taken": [],
        "started_at": started,
    }

    with _connect(path, write=True) as db:
        db.execute("BEGIN IMMEDIATE")
        mission._get_row(db, mission_id)  # verify the mission exists
        acq = acquire_lease(
            db, mission_id, trigger_kind, ttl=ttl, lease_lock=lease_lock
        )
        pass_env["lease_acquired"] = bool(acq.get("acquired"))
        pass_env["lease_reclaimed"] = bool(acq.get("reclaimed")) and bool(
            acq.get("acquired")
        )
        pass_env["lease"] = acq
        if not acq.get("acquired"):
            # §7.1 conflation: a live pass holds the lease; mark recheck needed.
            mark_recheck(db, mission_id)
            pass_env["pass_result"] = PASS_STALE
            # Conflation is normal loop behavior, not an anomaly: GREEN tier
            # with the conflated pass visible in telemetry (escalation_tier
            # '' on early rows predating the tier column reads as GREEN here).
            pass_env["actions_taken"].append(
                {"action": "conflate", "detail": "pass in-flight; recheck needed"}
            )
            _record_telemetry(db, pass_env)
            db.commit()
            pass_env["duration_ms"] = int((_now_ts() - started_ts) * 1000)
            return pass_env

        # Observe (authoritative sidecar channels only) + classify.
        env, frontier = build_observation(db, hermes_root, mission_id, host)
        node_id = frontier.get("node_id", "") if frontier else ""
        pass_env["node_id"] = node_id
        try:
            decision = fs.finalize(fs.classify(mission_id, node_id, env))
        except fs.ObservationError as exc:
            decision = fs.finalize(
                fs.classify(
                    mission_id,
                    node_id,
                    {
                        "mission": env["mission"],
                        "plan": env["plan"],
                        "delegation": env.get("delegation"),
                        "runner": env.get("runner"),
                        "worker_exit": {"kind": "unknown", "code": 0},
                        "last_failure_error": "",
                        "capability": env.get("capability"),
                        "breaker": env.get("breaker"),
                    },
                )
            )
            decision["classification_uncertainty"] = "invalid_observation:" + _sanitize(
                str(exc), 64
            )

        row_key = decision["row_key"]
        contract_sha = frontier.get("contract_sha256", "") if frontier else ""
        attempt_seq = (int(frontier.get("retries", 0) or 0) + 1) if frontier else 1
        cmds = _would_be_commands(
            row_key,
            mission_id,
            node_id,
            contract_sha,
            attempt_seq,
            env.get("delegation"),
        )
        pass_result = _pass_result(row_key)
        need_attention = bool(decision.get("need_attention"))

        # §12.2 alerting tier (GREEN/YELLOW/RED) for this pass.
        tier, tier_reasons = derive_pass_tier(
            mission_status=str(env["mission"].get("status", "")),
            classification=str(decision["classification"]),
            row_key=row_key,
            need_attention=need_attention,
            stuck_s=_stuck_seconds(db, mission_id),
        )
        uncertainty = str(decision.get("classification_uncertainty", ""))

        # Build the durable decision envelope (decision output only).
        pass_env.update(
            {
                "node_id": node_id,
                "classification": decision["classification"],
                "failure_class": decision.get("failure_class", ""),
                "row_key": row_key,
                "proposed_action": decision["proposed_action"],
                "proposed_tool": decision["proposed_tool"],
                "verify": decision["verify"],
                "auto_retry": bool(decision.get("auto_retry")),
                "would_execute": False,
                "need_attention": need_attention,
                "pass_result": pass_result,
                "escalation_tier": tier,
                "escalation_reasons": tier_reasons,
                "would_be_commands": cmds,
                "actions_taken": [
                    {"action": "observe", "detail": "shadow observe only"}
                ],
                "classification_uncertainty": decision.get(
                    "classification_uncertainty", ""
                ),
                "decision_sha256": decision["decision_sha256"],
                "replan_proposal": decision.get("replan_proposal"),
                "observation": {
                    "mission": env["mission"],
                    "plan": env["plan"],
                    "delegation": env.get("delegation"),
                    "runner": env.get("runner"),
                },
            }
        )

        # --- the ONLY durable writes: controller_plan + controller_telemetry ---
        record = {
            "schema": fs.DECISION_SCHEMA,
            "schema_version": fs.SCHEMA_VERSION,
            "mission_id": mission_id,
            "node_id": node_id,
            "classification": decision["classification"],
            "failure_class": decision.get("failure_class", ""),
            "row_key": row_key,
            "proposed_action": decision["proposed_action"],
            "would_execute": False,
            "need_attention": need_attention,
            "decision_sha256": decision["decision_sha256"],
            "generated_at": started,
            "trigger_kind": trigger_kind,
            "mode": CONTROLLER_MODE,
            "pass_result": pass_result,
            "would_be_commands": cmds,
        }
        fs._record_decision(db, record)
        _record_telemetry(db, pass_env)
        heartbeat_pulse(hermes_root)  # liveness signal (writer side)

        # §12.2 RED routing: spool a fleet-attention INTERRUPT envelope for
        # the delivery-broker lane. Never self-sent from inside the loop; a
        # spool failure raises (INV-10: refused gates stay as loud as
        # permitted — the pass fails loudly rather than dropping the signal).
        if tier == TIER_RED:
            envelope = build_attention_envelope(
                mission_id=mission_id,
                node_id=node_id,
                classification=str(decision["classification"]),
                row_key=row_key,
                proposed_action=str(decision["proposed_action"]),
                reasons=decision.get("matched_tokens", [])[:2],
                uncertainty=uncertainty,
                tier_reasons=tier_reasons,
                pass_seq=int(acq.get("pass_seq", 0) or 0),
            )
            spool_attention_envelope(envelope, hermes_root)
            pass_env["attention_spooled"] = True

        # Renew + release the lease now that the pass is complete.
        db.commit()
        release_lease(db, mission_id, lease_lock)
        db.commit()

    pass_env["duration_ms"] = int((_now_ts() - started_ts) * 1000)
    return pass_env


# ---------------------------------------------------------------------------
# Loop driver (long-running §7.1; NOT started by this slice — HARD RULES)
# ---------------------------------------------------------------------------


def run_loop_tick(
    *,
    host: HostObservationAdapter | None = None,
    hermes_root: Path | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    lease_lock: str = "shadow-loop",
    limit: int = MAX_MISSIONS_PER_PASS,
) -> dict[str, Any]:
    """One tick of the reconciler loop: conflate → pass per mission.

    The long-running process calls this on interval (T1) and on event wake
    (T2/T3/T4). This slice does NOT start the process (no deploy / no process
    mutation), but the loop body is the real, testable unit.
    """
    path = _db_path(hermes_root)
    results: list[dict[str, Any]] = []
    with _connect(path, write=True) as db:
        work = conflate(db, limit=limit)
        for w in work:
            mid = w["mission_id"]
            try:
                result = reconcile_pass(
                    mid,
                    w["trigger_kind"],
                    host=host,
                    hermes_root=hermes_root,
                    lease_lock=lease_lock,
                    interval=interval,
                )
                results.append(result)
                if result.get("lease_acquired"):
                    consume_trigger(db, mid)
            except (ValueError, LookupError, sqlite3.Error, KeyError) as exc:
                results.append(
                    {
                        "mission_id": mid,
                        "error": _sanitize(str(exc), 160),
                        "pass_result": PASS_BLOCKED,
                        "classification": fs.CLASS_UNKNOWN,
                        "need_attention": True,
                        # INV-10: a failed pass is as loud as permitted —
                        # surfaced in-band as RED even though the pass could
                        # not record telemetry.
                        "escalation_tier": TIER_RED,
                        "escalation_reasons": ["pass_error"],
                        "would_execute": False,
                    }
                )
    return {"tick": _now(), "missions": len(results), "results": results}


# ---------------------------------------------------------------------------
# §12.2 telemetry / health surface: heartbeat file + controller_status()
# ---------------------------------------------------------------------------


def _heartbeat_path(hermes_root: Path | None) -> Path:
    return _root(hermes_root) / "missions" / "controller_heartbeat.json"


def heartbeat_pulse(
    hermes_root: Path | None = None, *, interval: float = DEFAULT_INTERVAL_SECONDS
) -> dict[str, Any]:
    """Write/refresh the watchdog liveness file (§12.2)."""
    path = _heartbeat_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "live": True,
        "mode": CONTROLLER_MODE,
        "pid": os.getpid(),
        "last_beat": _now(),
        "ts": _now_ts(),
        "interval": interval,
        "lease_ttl": _lease_ttl_seconds(interval),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _read_heartbeat(hermes_root: Path | None) -> dict[str, Any]:
    path = _heartbeat_path(hermes_root)
    if not path.is_file():
        return {"live": False, "last_beat": "", "age_s": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"live": False, "last_beat": "", "age_s": None}
    age = _now_ts() - float(payload.get("ts", 0))
    payload["age_s"] = age
    payload["live"] = bool(payload.get("live")) and age < HEARTBEAT_STALE_SECONDS
    return payload


def controller_status(hermes_root: Path | None = None) -> dict[str, Any]:
    """Read-only aggregate health (§12.2) + GREEN/YELLOW/RED tier rollup."""
    path = _db_path(hermes_root)
    hb = _read_heartbeat(hermes_root)
    status: dict[str, Any] = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "mode": CONTROLLER_MODE,
        "would_execute": False,
        "controller_live": bool(hb.get("live")),
        "last_pass_age_s": hb.get("age_s"),
        "last_beat": hb.get("last_beat", ""),
    }
    empty = {
        "active_passes": 0,
        "missions_reconciled_24h": 0,
        "passes_24h": 0,
        "recoveries": 0,
        "escalations": 0,
        "retry_storms_prevented": 0,
        "stale_lease_reclaims": 0,
        "classification_uncertainty": 0,
        "per_class": {},
        "per_tier": {},
        "attention_spooled": 0,
        "leases": [],
    }
    if not path.is_file():
        status.update(empty)
        status["tier"] = _rollup_tier(status, hb)
        status["tier_reasons"] = _rollup_reasons(status, hb)
        return status
    with _connect(path, write=False) as db:
        try:
            active = db.execute(
                "SELECT COUNT(*) AS c FROM controller_pass_lease"
            ).fetchone()
        except sqlite3.Error:
            active = {"c": 0}
        cutoff = datetime.fromtimestamp(
            _now_ts() - AGGREGATE_WINDOW_SECONDS, tz=timezone.utc
        ).isoformat()
        try:
            tele = db.execute(
                "SELECT pass_result,classification,need_attention,escalation_tier,"
                "lease_reclaimed,COUNT(*) AS c "
                "FROM controller_telemetry WHERE created_at>=? "
                "GROUP BY pass_result,classification,need_attention,escalation_tier,lease_reclaimed",
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            tele = []
        try:
            tele_all = db.execute(
                "SELECT pass_result,COUNT(*) AS c FROM controller_telemetry "
                "GROUP BY pass_result"
            ).fetchall()
        except sqlite3.Error:
            tele_all = []
        try:
            reconciled = db.execute(
                "SELECT COUNT(DISTINCT mission_id) AS c FROM controller_telemetry "
                "WHERE created_at>=?",
                (cutoff,),
            ).fetchone()
        except sqlite3.Error:
            reconciled = {"c": 0}
        try:
            leases = [
                _lease_info(db, r["mission_id"])
                for r in db.execute(
                    "SELECT mission_id FROM controller_pass_lease ORDER BY mission_id LIMIT ?",
                    (MAX_MISSIONS_PER_PASS,),
                ).fetchall()
            ]
        except sqlite3.Error:
            leases = []
        per_class: dict[str, int] = {}
        per_result: dict[str, int] = {}
        per_tier: dict[str, int] = {}
        reclaims = 0
        passes = 0
        uncertainty = _count_uncertainty(db, cutoff)
        for row in tele:
            n = int(row["c"])
            passes += n
            per_result[row["pass_result"]] = per_result.get(row["pass_result"], 0) + n
            per_class[row["classification"]] = (
                per_class.get(row["classification"], 0) + n
            )
            tier = row["escalation_tier"] or ""
            if tier:
                per_tier[tier] = per_tier.get(tier, 0) + n
            reclaims += n if int(row["lease_reclaimed"] or 0) else 0
        status.update(
            {
                "active_passes": int(active["c"]),
                "missions_reconciled_24h": int(reconciled["c"]),
                "passes_24h": passes,
                "recoveries": per_result.get(PASS_RECOVERED, 0),
                "escalations": per_result.get(PASS_ESCALATED, 0)
                + per_result.get(PASS_BLOCKED, 0),
                "retry_storms_prevented": per_result.get(PASS_BLOCKED, 0),
                "stale_lease_reclaims": reclaims,
                "classification_uncertainty": uncertainty,
                "per_class": per_class,
                "per_tier": per_tier,
                "pass_results": per_result,
                "pass_results_all_time": {
                    r["pass_result"]: int(r["c"]) for r in tele_all
                },
                "leases": leases,
                "attention_spooled": len(attention_envelopes(hermes_root)),
            }
        )
        status["tier"] = _rollup_tier(status, hb)
        status["tier_reasons"] = _rollup_reasons(status, hb)
    return status


def _rollup_tier(status: dict[str, Any], hb: dict[str, Any]) -> str:
    """Controller-level tier: worst of liveness and pass-tier signals."""
    if not status.get("controller_live"):
        # No fresh heartbeat → the watchdog surface itself is dark. §12.2
        # treats an unobservable controller as RED (fail-closed liveness).
        return TIER_RED
    per_tier: dict[str, int] = status.get("per_tier", {})
    if per_tier.get(TIER_RED, 0) > 0:
        return TIER_RED
    if per_tier.get(TIER_YELLOW, 0) > 0:
        return TIER_YELLOW
    return TIER_GREEN


def _rollup_reasons(status: dict[str, Any], hb: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not status.get("controller_live"):
        reasons.append("heartbeat_stale_or_missing")
    per_tier: dict[str, int] = status.get("per_tier", {})
    if per_tier.get(TIER_RED, 0) > 0:
        reasons.append(f"red_passes_24h={per_tier[TIER_RED]}")
    if per_tier.get(TIER_YELLOW, 0) > 0:
        reasons.append(f"yellow_passes_24h={per_tier[TIER_YELLOW]}")
    if int(status.get("classification_uncertainty", 0) or 0) > 0:
        reasons.append(
            f"classification_uncertainty={status['classification_uncertainty']}"
        )
    if not reasons:
        reasons.append("steady_state")
    return reasons[:8]


def _count_uncertainty(db: sqlite3.Connection, cutoff: str | None = None) -> int:
    try:
        if cutoff is not None:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM controller_telemetry "
                "WHERE classification=? AND created_at>=?",
                (fs.CLASS_UNKNOWN, cutoff),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM controller_telemetry WHERE classification=?",
                (fs.CLASS_UNKNOWN,),
            ).fetchone()
        return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0


# ---------------------------------------------------------------------------
# Public MCP surfaces
# ---------------------------------------------------------------------------


def hermes_controller_reconcile(
    mission_id: str,
    trigger_kind: str = TRIGGER_MANUAL,
    *,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Run one supervised shadow reconciliation pass.

    The pass is decision-only with respect to Mission/work execution, but it
    persists controller plans, telemetry, leases, heartbeats, and attention
    envelopes. Therefore every invocation requires the normal ``workspace`` +
    ``direct`` mutation gates even when ``dry_run`` is true.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        # The pass persists controller bookkeeping (plans, telemetry, leases,
        # heartbeats) unconditionally, so it requires direct apply mode even
        # when ``dry_run`` is true. ``require_mutation`` alone cannot express
        # this: with a non-direct apply mode it silently downgrades instead
        # of raising, which would leave the persistence ungated.
        if policy.apply_mode != "direct":
            raise PermissionError(
                "Controller reconcile persists controller bookkeeping and requires "
                f"{op.OPERATOR_APPLY_MODE_ENV}=direct."
            )
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        if trigger_kind not in TRIGGERS:
            raise ValueError(f"trigger_kind must be one of {TRIGGERS}")

        result = reconcile_pass(
            mission_id,
            trigger_kind,
            host=NullHostAdapter(),
            hermes_root=hermes_root,
            interval=DEFAULT_INTERVAL_SECONDS,
        )
        result["dry_run"] = bool(dry_run)
        result["changed"] = True
        _audit(
            "hermes_controller_reconcile",
            policy,
            dry_run=bool(dry_run),
            success=not any(k in result for k in ("error",)),
            changed=True,
            mission_id=mission_id,
            node_id=result.get("node_id", ""),
            extra={
                "classification": result.get("classification", ""),
                "row_key": result.get("row_key", ""),
                "pass_result": result.get("pass_result", ""),
                "lease_acquired": bool(result.get("lease_acquired")),
            },
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_controller_reconcile",
            policy,
            dry_run=bool(dry_run),
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "CONTROLLER_RECONCILE_REJECTED",
            "Check the mission id, trigger kind, and Operator mutation policy.",
        )


def hermes_controller_status(hermes_root: Path | None = None) -> str:
    """Read-only controller health surface (§12.2 + GREEN/YELLOW/RED tier)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        status = controller_status(hermes_root)
        _audit(
            "hermes_controller_status",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={
                "controller_live": bool(status.get("controller_live")),
                "active_passes": status.get("active_passes", 0),
                "tier": status.get("tier", ""),
                "attention_spooled": status.get("attention_spooled", 0),
            },
        )
        return json.dumps(status, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        return _error(
            exc, "CONTROLLER_STATUS_REJECTED", "Operator policy must be enabled."
        )


def hermes_controller_lease_list(
    mission_id: str = "", hermes_root: Path | None = None
) -> str:
    """Read-only: current per-mission pass leases + trigger queue conflation state."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        leases: dict[str, Any] = {}
        triggers: list[dict[str, Any]] = []
        if path.is_file():
            with _connect(path, write=False) as db:
                try:
                    rows = db.execute(
                        "SELECT mission_id FROM controller_pass_lease ORDER BY mission_id"
                    ).fetchall()
                    for r in rows:
                        mid = r["mission_id"]
                        if mission_id and mid != mission_id:
                            continue
                        leases[mid] = _lease_info(db, mid)
                except sqlite3.Error:
                    pass
                if not mission_id:
                    try:
                        for r in db.execute(
                            "SELECT mission_id,trigger_kind,ref,seq FROM controller_trigger_queue "
                            "ORDER BY seq LIMIT ?",
                            (MAX_MISSIONS_PER_PASS,),
                        ).fetchall():
                            triggers.append(
                                {
                                    "mission_id": r["mission_id"],
                                    "trigger_kind": r["trigger_kind"],
                                    "ref": r["ref"],
                                    "seq": int(r["seq"]),
                                }
                            )
                    except sqlite3.Error:
                        pass
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "mode": CONTROLLER_MODE,
            "leases": leases,
            "trigger_queue": triggers,
        }
        _audit(
            "hermes_controller_lease_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={"lease_count": len(leases)},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "CONTROLLER_LEASE_LIST_REJECTED",
            "Check the mission id and Operator policy.",
        )


def hermes_controller_trigger(
    mission_id: str,
    trigger_kind: str,
    ref: str = "",
    hermes_root: Path | None = None,
) -> str:
    """Enqueue a T1–T5 request (persistent controller mutation)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        # Enqueueing persists trigger-queue + recheck state unconditionally:
        # require direct apply mode. ``require_mutation`` cannot express this
        # (a non-direct apply mode silently downgrades instead of raising).
        if policy.apply_mode != "direct":
            raise PermissionError(
                "Controller trigger persists queue/recheck state and requires "
                f"{op.OPERATOR_APPLY_MODE_ENV}=direct."
            )
        result = trigger(mission_id, trigger_kind, ref, hermes_root=hermes_root)
        _audit(
            "hermes_controller_trigger",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={"trigger_kind": trigger_kind, "seq": result.get("seq", 0)},
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_controller_trigger",
            policy,
            dry_run=False,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc, "CONTROLLER_TRIGGER_REJECTED", "Check the mission id, trigger kind, and Operator mutation policy."
        )
