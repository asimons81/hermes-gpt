"""Test matrix for the supervised mission controller — shadow/observe reconciler loop.

Implements the card's acceptance criteria for t_ad1e6d07 (proposal §17 item 6 /
§7, D2, D10):

- **Shadow / zero side effects**: a full pass over a real mission classifies and
  emits the smallest action, and the store diff proves the ONLY writes are the
  controller's own surfaces (controller_plan / controller_telemetry /
  controller_pass_lease / controller_trigger_queue + heartbeat file); missions,
  mission_events, mission_plans, plan_nodes, attachments, and the delegations
  store are untouched; ``would_execute`` is always False; no dispatch / reclaim /
  completion / approval path exists.
- **Per-mission pass lease (§7.3)**: single guarded UPDATE acquire, TTL =
  min(max_pass_duration, interval*2), heartbeat renew, crash-reclaim on expiry
  (kill -9), CAS on lock.
- **Conflation (§7.1)**: a live lease is never stolen; a second trigger sets
  ``recheck_needed``.
- **Idempotency (§7.4)**: deterministic controller operation idempotency key.
- **Fail-closed (§7.5)**: an un-observable / failed node with no channel
  classifies ``unknown`` → blocked + ``need_attention``.
- **Telemetry/health (§12.2)**: heartbeat + controller_status().
- **INV-9**: no raw objective/error/secret text persisted in any controller store.

Pytest-compatible AND standalone-runnable (the module defines a ``run()`` harness
so the matrix executes without pytest, which is not installable in this sandbox —
same caveat the sibling slice-1 cards recorded).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operator_contract as contract_mod
import operator_controller as ctl
import operator_delegations as deleg
import operator_failure_semantics as fs
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_placement as placement
import operator_policy as op

failures: list[str] = []
passed: list[str] = []


def check(cond, label: str) -> None:
    if cond:
        passed.append(label)
    else:
        failures.append(label)
        print(f"  FAIL: {label}")


def eq(actual, expected, label: str) -> None:
    check(actual == expected, f"{label} (got {actual!r}, want {expected!r})")


def _j(value: str) -> dict:
    return json.loads(value)


def _spec(mid: str) -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Controller sandbox",
            "objective": "Exercise the shadow reconciler loop.",
            "owner_profile": "default",
            "acceptance_criteria": ["controller green"],
            "context_refs": [],
            "skills": [],
            "final_approval_required": True,
        }
    )


def _dag(mid: str) -> str:
    return json.dumps(
        {
            "schema": plan.PLAN_SCHEMA,
            "mission_id": mid,
            "version": 1,
            "decomposition": "operator-provided",
            "objective": "Raw mission objective text.",
            "nodes": [
                {
                    "node_id": "a",
                    "kind": "single",
                    "owner": "hermes-dev",
                    "parents": [],
                    "objective": "Raw node objective A.",
                    "capability_req": {
                        "profile": "hermes-dev",
                        "skills": [],
                        "authorization_class": "reversible_write",
                    },
                    "budget": {"est_minutes": 30, "est_tokens": 50_000},
                    "expected_artifacts": ["contract.json"],
                },
                {
                    "node_id": "b",
                    "kind": "approval",
                    "owner": "owner",
                    "parents": ["a"],
                    "objective": "Raw node objective B.",
                    "capability_req": {
                        "profile": "owner",
                        "skills": [],
                        "authorization_class": "read_only",
                    },
                    "budget": {"est_minutes": 0, "est_tokens": 0},
                },
            ],
        }
    )


def _setup_mission(root: Path, mid: str = "msn-ctrl") -> None:
    out = _j(
        mission.hermes_mission_create(
            _spec(mid), confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out
    p = _j(
        plan.hermes_plan_create(
            mid, _dag(mid), confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert p["success"] is True, p
    t = _j(
        mission.hermes_mission_transition(
            mid, "running", confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert t["changed"] is True, t


def _table_rows(db: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()]


def run_lease_machinery() -> None:
    """§7.3 lease: acquire / TTL / renew / reclaim / CAS / no-steal."""

    with tempfile.TemporaryDirectory(prefix="ctl-lease-") as td:
        root = Path(td) / "hermes"
        root.mkdir()
        op.set_audit_log_override(Path(td) / "audit.jsonl")
        os_env = __import__("os").environ
        saved = {
            k: os_env.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
            )
        }
        os_env[op.OPERATOR_ENABLED_ENV] = "1"
        os_env[op.OPERATOR_LEVEL_ENV] = "workspace"
        os_env[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        try:
            _setup_mission(root)
            mdb = mission._db_path(root)
            with mission._connect(mdb, write=True) as db:
                ctl._init_controller_tables(db)
                # TTL formula
                eq(ctl._lease_ttl_seconds(90.0), 180.0, "TTL = interval*2 (90→180)")
                eq(
                    ctl._lease_ttl_seconds(500.0),
                    300.0,
                    "TTL capped at max_pass_duration (500→300)",
                )
                eq(
                    ctl._lease_ttl_seconds(5.0),
                    15.0,
                    "TTL floored at min interval (5→15)",
                )

                a1 = ctl.acquire_lease(
                    db, "msn-ctrl", ctl.TRIGGER_PERIODIC, ttl=60.0, lease_lock="w1"
                )
                check(a1["acquired"] is True, "lease acquired (fresh)")
                # second holder cannot steal a live lease
                a2 = ctl.acquire_lease(
                    db, "msn-ctrl", ctl.TRIGGER_LIVE_EVENT, ttl=60.0, lease_lock="w2"
                )
                check(a2["acquired"] is False, "live lease not stolen")
                eq(a2["recheck"], True, "no-steal marks recheck needed")
                # renewal only by the current lock (CAS)
                r_ok = ctl.renew_lease(db, "msn-ctrl", "w1", ttl=60.0)
                check(r_ok["renewed"] is True, "lease renewed by current lock")
                r_bad = ctl.renew_lease(db, "msn-ctrl", "w2", ttl=60.0)
                check(r_bad["renewed"] is False, "lease not renewed by foreign lock")
                # crash reclaim: force an expired TTL, then any holder reclaims
                db.execute(
                    "UPDATE controller_pass_lease SET lease_expires=?, lease_lock=? "
                    "WHERE mission_id=?",
                    ("2000-01-01T00:00:00+00:00", "crashed-pid", "msn-ctrl"),
                )
                db.commit()
                a3 = ctl.acquire_lease(
                    db, "msn-ctrl", ctl.TRIGGER_HEALTH, ttl=60.0, lease_lock="w3"
                )
                check(a3["acquired"] is True, "expired (kill-9) lease reclaimed")
                check(a3["reclaimed"] is True, "reclaim flagged as reclaimed")
                # release (CAS on lock)
                rel = ctl.release_lease(db, "msn-ctrl", "w3")
                check(rel["released"] is True, "lease released by current lock")
        finally:
            for k, v in saved.items():
                if v is None:
                    os_env.pop(k, None)
                else:
                    os_env[k] = v
            op.set_audit_log_override(None)


def run_conflation() -> None:
    """§7.1 conflation: one in-flight pass; a second trigger marks recheck."""

    with tempfile.TemporaryDirectory(prefix="ctl-conflate-") as td:
        root = Path(td) / "hermes"
        root.mkdir()
        op.set_audit_log_override(Path(td) / "audit.jsonl")
        os_env = __import__("os").environ
        saved = {
            k: os_env.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
            )
        }
        os_env[op.OPERATOR_ENABLED_ENV] = "1"
        os_env[op.OPERATOR_LEVEL_ENV] = "workspace"
        os_env[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        try:
            _setup_mission(root)
            mdb = mission._db_path(root)
            with mission._connect(mdb, write=True) as db:
                ctl._init_controller_tables(db)
                # enqueue a trigger, conflate to one per mission, consume
                t = ctl.trigger(
                    "msn-ctrl", ctl.TRIGGER_DEPENDENCY, "ref-x", hermes_root=root
                )
                check(
                    t["enqueued"] is True and t["seq"] == 1, "trigger enqueued (seq 1)"
                )
                t2 = ctl.trigger(
                    "msn-ctrl", ctl.TRIGGER_HEALTH, "ref-y", hermes_root=root
                )
                check(t2["seq"] == 2, "second trigger bumps monotonic seq")
                work = ctl.conflate(db)
                eq(len(work), 1, "conflate collapses to one work request per mission")
                eq(
                    work[0]["trigger_kind"],
                    ctl.TRIGGER_HEALTH,
                    "latest trigger wins conflation",
                )
                # conflation while a pass holds the lease → recheck_needed
                ctl.acquire_lease(
                    db, "msn-ctrl", ctl.TRIGGER_PERIODIC, ttl=60.0, lease_lock="loop"
                )
                mk = ctl.mark_recheck(db, "msn-ctrl")
                check(
                    mk["recheck_needed"] is True,
                    "second trigger marks recheck_needed on a live pass",
                )
                info = ctl._lease_info(db, "msn-ctrl")
                check(
                    info.get("recheck_needed") is True, "lease carries recheck_needed"
                )
                # a pass on a held lease does NOT steal and reports stale
                res = _j(
                    ctl.hermes_controller_reconcile(
                        "msn-ctrl", ctl.TRIGGER_PERIODIC, dry_run=False, hermes_root=root
                    )
                )
                check(
                    res["lease_acquired"] is False,
                    "reconcile does not steal a held lease",
                )
                eq(
                    res["pass_result"],
                    ctl.PASS_STALE,
                    "held-lease pass reports stale (conflate)",
                )
                check(res["would_execute"] is False, "stale pass stays decision-only")
                # idempotency key determinism
                k1 = ctl._idempotency_key("msn-x", "node-a", "stage-1", "a" * 64, 1)
                k2 = ctl._idempotency_key("msn-x", "node-a", "stage-1", "a" * 64, 1)
                k3 = ctl._idempotency_key("msn-x", "node-a", "stage-1", "a" * 64, 2)
                eq(k1, k2, "idempotency key deterministic for identical inputs")
                check(k1 != k3, "idempotency key changes with attempt_seq")
        finally:
            for k, v in saved.items():
                if v is None:
                    os_env.pop(k, None)
                else:
                    os_env[k] = v
            op.set_audit_log_override(None)


def run_shadow_pass_zero_side_effect() -> None:
    """§17 item 6 / §7: a full shadow pass emits the smallest action with zero
    mutation outside the controller's own surfaces."""

    with tempfile.TemporaryDirectory(prefix="ctl-shadow-") as td:
        root = Path(td) / "hermes"
        root.mkdir()
        op.set_audit_log_override(Path(td) / "audit.jsonl")
        os_env = __import__("os").environ
        saved = {
            k: os_env.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
            )
        }
        os_env[op.OPERATOR_ENABLED_ENV] = "1"
        os_env[op.OPERATOR_LEVEL_ENV] = "workspace"
        os_env[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        try:
            _setup_mission(root)
            mdb = mission._db_path(root)
            with mission._connect(mdb, write=False) as db:
                snap = {
                    t: _table_rows(db, t)
                    for t in (
                        "missions",
                        "mission_events",
                        "mission_plans",
                        "plan_nodes",
                        "attachments",
                    )
                }
            deleg_before = (root / "delegations" / "delegations.db").exists()

            res = _j(
                ctl.hermes_controller_reconcile(
                    "msn-ctrl", ctl.TRIGGER_MANUAL, dry_run=False, hermes_root=root
                )
            )
            eq(
                res["classification"],
                "none_dispatchable",
                "ready child classifies none_dispatchable",
            )
            eq(
                res["row_key"],
                "dispatch_ready_child",
                "smallest action is dispatch_ready_child",
            )
            eq(
                res["would_execute"],
                False,
                "decision output only (would_execute=False)",
            )
            eq(res["node_id"], "a", "frontier node is the ready node")
            check(res["lease_acquired"] is True, "pass acquired the lease")
            check(len(res["would_be_commands"]) >= 1, "would_be_commands populated")
            check(
                res["would_be_commands"][0]["executed"] is False,
                "would-be commands never executed",
            )
            check(
                bool(res["would_be_commands"][0]["idempotency_key"]),
                "would-be command carries idempotency key",
            )

            # zero side effects: authoritative stores unchanged
            with mission._connect(mdb, write=False) as db:
                after = {t: _table_rows(db, t) for t in snap}
            check(
                all(after[t] == snap[t] for t in snap),
                "authoritative stores unchanged after pass",
            )
            check(
                (root / "delegations" / "delegations.db").exists() == deleg_before,
                "delegations store untouched",
            )
            # controller surfaces hold the only new writes
            with mission._connect(mdb, write=False) as db:
                cp = db.execute("SELECT COUNT(*) FROM controller_plan").fetchone()[0]
                ct = db.execute("SELECT COUNT(*) FROM controller_telemetry").fetchone()[
                    0
                ]
                cl = db.execute(
                    "SELECT COUNT(*) FROM controller_pass_lease"
                ).fetchone()[0]
            check(cp >= 1, "controller_plan recorded the decision")
            check(ct >= 1, "controller_telemetry recorded the pass")
            eq(cl, 0, "lease released after the pass completes")

            # INV-9: no raw objective/error/secret text persisted
            with mission._connect(mdb, write=False) as db:
                blob = " ".join(
                    str(r[0])
                    for r in db.execute(
                        "SELECT decision_json FROM controller_plan"
                    ).fetchall()
                )
                blob += " " + " ".join(
                    str(r[0])
                    for r in db.execute(
                        "SELECT actions_taken_json FROM controller_telemetry"
                    ).fetchall()
                )
            check(
                "Raw node objective A." not in blob
                and "Raw mission objective text." not in blob,
                "INV-9: no raw objective text persisted",
            )
            check(
                "sk-" not in blob.lower() and "Bearer" not in blob,
                "INV-9: no secret-shaped content persisted",
            )

            # heartbeat + status
            hb = ctl.heartbeat_pulse(root)
            check(
                hb["live"] is True and hb["mode"] == ctl.CONTROLLER_MODE,
                "heartbeat pulses liveness",
            )
            st = _j(ctl.hermes_controller_status(root))
            check(
                st["success"] is True and st["mode"] == ctl.CONTROLLER_MODE,
                "controller_status serves",
            )
            check(st["would_execute"] is False, "status reports decision-only")
        finally:
            for k, v in saved.items():
                if v is None:
                    os_env.pop(k, None)
                else:
                    os_env[k] = v
            op.set_audit_log_override(None)


def run_fail_closed() -> None:
    """§7.5: an un-observable failed node fail-closes to unknown + need_attention."""

    with tempfile.TemporaryDirectory(prefix="ctl-failclosed-") as td:
        root = Path(td) / "hermes"
        root.mkdir()
        op.set_audit_log_override(Path(td) / "audit.jsonl")
        os_env = __import__("os").environ
        saved = {
            k: os_env.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
            )
        }
        os_env[op.OPERATOR_ENABLED_ENV] = "1"
        os_env[op.OPERATOR_LEVEL_ENV] = "workspace"
        os_env[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        try:
            _setup_mission(root)
            mdb = mission._db_path(root)
            # set node 'a' to failed with NO delegation/runner/exit observation
            with mission._connect(mdb, write=True) as db:
                ctl._init_controller_tables(db)
                db.execute(
                    "UPDATE plan_nodes SET state='failed' WHERE mission_id=? AND node_id=?",
                    ("msn-ctrl", "a"),
                )
                db.commit()
            res = _j(
                ctl.hermes_controller_reconcile(
                    "msn-ctrl", ctl.TRIGGER_PERIODIC, hermes_root=root
                )
            )
            eq(
                res["classification"],
                fs.CLASS_UNKNOWN,
                "unflavored node failure classifies unknown",
            )
            check(
                res["need_attention"] is True,
                "unknown classification raises need_attention",
            )
            eq(
                res["row_key"],
                "unknown_fail_closed",
                "fail-closed row is unknown_fail_closed",
            )
            check(
                bool(res.get("classification_uncertainty")),
                "uncertainty reason recorded",
            )
            check(
                res.get("replan_proposal") is None,
                "no replan proposed for an unclassifiable failure",
            )
        finally:
            for k, v in saved.items():
                if v is None:
                    os_env.pop(k, None)
                else:
                    os_env[k] = v
            op.set_audit_log_override(None)


# ---------------------------------------------------------------------------
# v0.12 slice-2 (Pack B): L2-rung execution engine + placement-informed dispatch.
#
# The design contract is docs/design/v0.12-controller-l2.md. Everything below
# exercises the §2 gate matrix, the §2.2 execution/idempotency semantics, the
# §2.3 prohibition matrix, and the §3 placement-informed dispatch decision.
# ---------------------------------------------------------------------------

# Frozen L0/L1 pass-envelope contract: with the L2 machine gate unset the
# envelope must be exactly the pre-L2 shape (no additive `execution` member).
L1_PASS_KEYS = frozenset(
    {
        "actions_taken",
        "auto_retry",
        "changed",
        "classification",
        "classification_uncertainty",
        "decision_sha256",
        "dry_run",
        "duration_ms",
        "escalation_reasons",
        "escalation_tier",
        "failure_class",
        "lease",
        "lease_acquired",
        "lease_reclaimed",
        "mission_id",
        "mode",
        "need_attention",
        "node_id",
        "observation",
        "pass_result",
        "proposed_action",
        "proposed_tool",
        "replan_proposal",
        "row_key",
        "schema",
        "schema_version",
        "started_at",
        "trigger_kind",
        "verify",
        "would_be_commands",
        "would_execute",
    }
)

AUTH_TABLES = ("missions", "mission_events", "mission_plans", "plan_nodes", "attachments")
MID = "msn-ctrl"


def _peer_target(name="rza", profiles=("hermes-dev",), ceiling="reversible_write"):
    """A normalized placement target naming a fleet-authority peer."""
    return {
        "entity_id": f"fleet:{name}",
        "kind": "fleet_peer",
        "name": name,
        "enabled": True,
        "reachable": True,
        "identity_configured": True,
        "authorization_ceiling": ceiling,
        "allowed_profiles": list(profiles),
        "features": [],
        "workspaces": [],
        "backends": [],
        "skills": [],
        "model": "",
        "provider": "",
        "host_role": "worker",
        "allow_public_actions": False,
    }


def _model_target(name="hermes-dev", profiles=("hermes-dev",)):
    """A normalized placement target that carries no dispatchable agent identity."""
    target = _peer_target(name, profiles)
    target["entity_id"] = f"profile:{name}"
    target["kind"] = "profile"
    target["authorization_ceiling"] = ""
    return target


class _L2Env:
    """Temp hermes root + operator/controller env for one L2 scenario.

    Substitutes exactly two seams: the capability-manifest read (so the scored
    target set is controlled — no scoring/filter/classification change) and the
    delegation authority surface (so a real remote dispatch is never attempted).
    Both are restored on exit.
    """

    def __init__(
        self,
        *,
        gate="1",
        level="workspace",
        apply_mode="direct",
        targets=None,
        payload=None,
        on_dispatch=None,
        prefix="ctl-l2-",
    ):
        self.gate = gate
        self.level = level
        self.apply_mode = apply_mode
        self.targets = [_peer_target()] if targets is None else list(targets)
        self.payload = payload
        self.on_dispatch = on_dispatch
        self.prefix = prefix
        self.dispatches = []

    # -- seams ---------------------------------------------------------------
    def _dispatch(
        self,
        contract_json,
        mission_id="",
        delegation_id="",
        confirm=False,
        dry_run=True,
        timeout=30,
        hermes_root=None,
    ):
        self.dispatches.append(
            {
                "contract_json": contract_json,
                "mission_id": mission_id,
                "confirm": confirm,
                "dry_run": dry_run,
            }
        )
        if self.on_dispatch is not None:
            return self.on_dispatch(len(self.dispatches))
        if self.payload is not None:
            return json.dumps(self.payload)
        return json.dumps(
            {
                "success": True,
                "changed": True,
                "mission_linked": True,
                "delegation": {
                    "delegation_id": "dlg-ctl-l2-0001",
                    "task_id": "ctl-l2-task",
                    "state": "queued",
                },
            }
        )

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory(prefix=self.prefix)
        self.root = Path(self._td.name) / "hermes"
        self.root.mkdir()
        op.set_audit_log_override(Path(self._td.name) / "audit.jsonl")
        self._env = {
            k: os.environ.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
                ctl.CONTROLLER_EXECUTE_ENV,
            )
        }
        # Seed the mission under full workspace/direct authority (the L1 setup
        # itself is gated), then apply the scenario's policy.
        os.environ[op.OPERATOR_ENABLED_ENV] = "1"
        os.environ[op.OPERATOR_LEVEL_ENV] = "workspace"
        os.environ[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        if self.gate is None:
            os.environ.pop(ctl.CONTROLLER_EXECUTE_ENV, None)
        else:
            os.environ[ctl.CONTROLLER_EXECUTE_ENV] = self.gate
        self._orig_targets = placement.load_manifest_targets
        placement.load_manifest_targets = lambda hermes_root=None, **kw: list(
            self.targets
        )
        self._orig_dispatch = deleg.hermes_delegation_dispatch
        deleg.hermes_delegation_dispatch = self._dispatch
        _setup_mission(self.root, MID)
        # Create the controller tables (schema only) so zero-write assertions can
        # read them; a preview never adds rows.
        with mission._connect(mission._db_path(self.root), write=True) as db:
            ctl._init_controller_tables(db)
        os.environ[op.OPERATOR_LEVEL_ENV] = self.level
        os.environ[op.OPERATOR_APPLY_MODE_ENV] = self.apply_mode
        return self

    def __exit__(self, *exc):
        placement.load_manifest_targets = self._orig_targets
        deleg.hermes_delegation_dispatch = self._orig_dispatch
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        op.set_audit_log_override(None)
        self._td.cleanup()
        return False

    # -- helpers -------------------------------------------------------------
    def pass_(self, *, confirm=True, dry_run=False, via_pass=False):
        if via_pass:
            return ctl.reconcile_pass(
                MID, ctl.TRIGGER_MANUAL, hermes_root=self.root, confirm=confirm
            )
        return _j(
            ctl.hermes_controller_reconcile(
                MID,
                ctl.TRIGGER_MANUAL,
                confirm=confirm,
                dry_run=dry_run,
                hermes_root=self.root,
            )
        )

    def rows(self, table):
        path = mission._db_path(self.root)
        with mission._connect(path, write=False) as db:
            return [tuple(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()]

    def authoritative(self):
        return {t: self.rows(t) for t in AUTH_TABLES}

    def exec_blocks(self):
        """Every plan-row execution block recorded for the mission."""
        out = []
        for row in self.rows("controller_plan"):
            parsed = json.loads(row[2])  # decision_json
            if isinstance(parsed.get("execution"), dict):
                out.append(parsed["execution"])
        return out

    def telemetry(self):
        path = mission._db_path(self.root)
        with mission._connect(path, write=False) as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM controller_telemetry ORDER BY seq"
                ).fetchall()
            ]

    def set_node(self, node_id, **cols):
        path = mission._db_path(self.root)
        with mission._connect(path, write=True) as db:
            sets = ", ".join(f"{k}=?" for k in cols)
            db.execute(
                f"UPDATE plan_nodes SET {sets} WHERE mission_id=? AND node_id=?",
                (*cols.values(), MID, node_id),
            )
            db.commit()

    def add_reconciling_delegation(self, node_id="a"):
        """A delegation already in flight for the node's contract lineage."""
        mission_db = mission._db_path(self.root)
        with mission._connect(mission_db, write=False) as db:
            sha = db.execute(
                "SELECT contract_sha256 FROM plan_nodes WHERE mission_id=? AND node_id=?",
                (MID, node_id),
            ).fetchone()[0]
        path = deleg._db_path(self.root)
        with deleg._connect(path, write=True) as db:
            deleg._init(db)
            db.execute(
                "INSERT OR REPLACE INTO delegations(delegation_id,schema,mission_id,"
                "task_id,contract_sha256,backend,state,backend_state,outcome,"
                "backend_ref_json,validation_verdict,cancel_requested,dispatch_phase,"
                "created_at,dispatched_at,updated_at,terminal_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "dlg-ctl-inflight",
                    deleg.DELEGATION_SCHEMA,
                    MID,
                    "ctl-inflight-task",
                    sha,
                    "fleet",
                    "reconciling",
                    "ambiguous",
                    "submission_may_have_succeeded",
                    "{}",
                    "",
                    0,
                    "invoking",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    None,
                ),
            )
            db.commit()


def run_l2_disabled_identity() -> None:
    """§2.1/§2.2: gate unset ⇒ zero execution and a byte-identical L0/L1 envelope."""

    with _L2Env(gate=None) as env:
        res = env.pass_(confirm=True)
        eq(
            set(res.keys()),
            set(L1_PASS_KEYS),
            "gate unset: pass envelope key set is the frozen L0/L1 contract",
        )
        check("execution" not in res, "gate unset: no additive execution member")
        eq(res["would_execute"], False, "gate unset: would_execute stays False")
        eq(
            [row[7] for row in env.rows("controller_plan")],
            [0],
            "gate unset: the durable plan row is a decision-only record",
        )
        eq(res["pass_result"], ctl.PASS_DISPATCHED, "gate unset: L1 pass_result intact")
        eq(len(env.dispatches), 0, "gate unset: nothing dispatched despite confirm=True")
        eq(env.exec_blocks(), [], "gate unset: no execution state on the plan row")
        eq(
            [json.loads(t["actions_taken_json"]) for t in env.telemetry()],
            [[{"action": "observe", "detail": "shadow observe only"}]],
            "gate unset: L1 actions_taken intact",
        )
        # The loop driver must not execute either.
        ctl.trigger(MID, ctl.TRIGGER_PERIODIC, "loop-ref", hermes_root=env.root)
        tick = ctl.run_loop_tick(hermes_root=env.root, limit=4)
        check(bool(tick.get("results")), "loop tick ran")
        eq(len(env.dispatches), 0, "gate unset: loop tick dispatches nothing")


def run_l2_gate_matrix() -> None:
    """§2.1: every missing gate refuses with a stable reason and zero execution."""

    # 1. per-call confirm missing
    with _L2Env() as env:
        res = env.pass_(confirm=False)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_CONFIRM_REQUIRED,
            "missing confirm refuses with confirm_required",
        )
        eq(res["execution"]["executed"], False, "confirm refusal is not an execution")
        eq(res["would_execute"], False, "confirm refusal keeps would_execute False")
        eq(len(env.dispatches), 0, "confirm refusal dispatches nothing")
        eq(env.exec_blocks(), [], "confirm refusal records no execution state")
        eq(
            [row[7] for row in env.rows("controller_plan")],
            [0],
            "the durable plan row stays would_execute=0 on a confirm refusal",
        )

    # 2. live policy: apply_mode != direct
    with _L2Env(apply_mode="dry_run") as env:
        res = env.pass_(confirm=True, via_pass=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_POLICY,
            "non-direct apply mode refuses with operator_policy_required",
        )
        eq(len(env.dispatches), 0, "policy refusal dispatches nothing")

    # 3. live policy: workspace not bound
    with _L2Env(level="read_only") as env:
        res = env.pass_(confirm=True, via_pass=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_WORKSPACE,
            "unbound workspace refuses with workspace_required",
        )
        eq(len(env.dispatches), 0, "workspace refusal dispatches nothing")


def run_l2_dry_run_preview() -> None:
    """§2.1 gate 5: dry_run stays a truthful zero-write preview with all gates set."""

    with _L2Env() as env:
        res = env.pass_(confirm=True, dry_run=True)
        eq(res["execution"]["result"], "refused", "preview reports a refusal")
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_DRY_RUN,
            "preview refuses with dry_run",
        )
        eq(res["execution"]["executed"], False, "preview never executes")
        eq(res["would_execute"], False, "preview would_execute False")
        eq(len(env.dispatches), 0, "preview dispatches nothing")
        eq(len(env.rows("controller_plan")), 0, "preview writes no plan rows")
        eq(len(env.rows("controller_telemetry")), 0, "preview writes no telemetry rows")
        eq(len(env.rows("controller_pass_lease")), 0, "preview takes no lease")


def run_l2_executed_path() -> None:
    """§2.2/§3/§4: the gated pass executes one action through the authority surface."""

    with _L2Env() as env:
        before = env.authoritative()
        res = env.pass_(confirm=True)

        eq(res["execution"]["enabled"], True, "execution block is enabled")
        eq(res["execution"]["executed"], True, "gated pass executed the action")
        eq(res["execution"]["result"], "dispatched", "execution result is dispatched")
        eq(res["execution"]["refused_reason"], None, "no refusal on the executed path")
        eq(res["execution"]["action_kind"], "dispatch_ready_child", "action kind recorded")
        eq(
            res["execution"]["placement"]["would_assign"],
            True,
            "would_assign True exactly when the assignment was dispatched",
        )
        eq(
            res["execution"]["placement"]["top_candidate"],
            "fleet:rza",
            "the scored top candidate became the dispatch target",
        )
        eq(res["would_execute"], True, "would_execute True exactly when executed")
        eq(
            [row[7] for row in env.rows("controller_plan")],
            [1],
            "the durable plan row records would_execute=1 only for a real execution",
        )
        eq(res["pass_result"], ctl.PASS_DISPATCHED, "executed pass keeps its pass_result")
        eq(len(env.dispatches), 1, "at most one action per pass")

        # The dispatch went through the existing delegation authority surface
        # with a contract that surface accepts (no new dispatch mechanism).
        call = env.dispatches[0]
        check(call["confirm"] is True, "dispatch passes confirm through")
        eq(call["dry_run"], False, "dispatch is not a dry run")
        canonical, contract, sha = contract_mod._parse_contract(call["contract_json"])
        eq(contract["assigned_agent"], "rza", "contract targets the scored peer")
        eq(
            contract["assigned_profile"],
            "hermes-dev",
            "contract carries the node's required profile",
        )
        key = res["execution"]["idempotency_key"]
        check(
            key[:16] in contract["task_id"],
            "contract task_id is keyed by the operation idempotency key",
        )
        eq(
            contract["execution"] if "execution" in contract else {},
            {},
            "no backend selector is invented",
        )
        eq(
            contract["completion_criteria"]["tests_pass"],
            False,
            "prohibition: the controller never claims test evidence",
        )
        eq(
            contract["completion_criteria"]["review_satisfied"],
            False,
            "prohibition: the controller never claims review evidence",
        )
        eq(
            contract["expected_artifacts"],
            [],
            "prohibition: the controller fabricates no expected artifacts",
        )

        # Durable execution evidence: intent-before-dispatch, then the outcome.
        blocks = env.exec_blocks()
        eq(len(blocks), 1, "one plan-row execution block")
        eq(blocks[0]["state"], ctl.EXECUTION_STATE_DISPATCHED, "plan row state dispatched")
        eq(blocks[0]["idempotency_key"], key, "plan row carries the idempotency key")
        eq(blocks[0]["target"], "rza", "plan row records the assignment linkage target")
        eq(
            blocks[0]["linkage"]["delegation_id"],
            "dlg-ctl-l2-0001",
            "plan row records the delegation linkage",
        )
        tele = env.telemetry()
        check(
            any(
                t["executed_idempotency_key"] == key and t["executed_result"] == "dispatched"
                for t in tele
            ),
            "telemetry ledger records the executed idempotency key",
        )
        check(
            any(t["would_execute"] == 1 for t in tele),
            "telemetry would_execute is truthful for the executed pass",
        )

        # Prohibitions: the authoritative stores are untouched (no completion,
        # no approval, no plan rewrite, no Mission version CAS bypass).
        eq(env.authoritative(), before, "authoritative stores unchanged by execution")
        check(
            not (env.root / "delegations" / "delegations.db").exists(),
            "no delegation store is written by the controller itself",
        )

        # INV-9: no raw objective / secret-shaped text anywhere on controller rows.
        blob = json.dumps(env.rows("controller_plan")) + json.dumps(env.rows("controller_telemetry"))
        check(
            "Raw node objective A." not in blob and "Raw mission objective text." not in blob,
            "INV-9: execution path persists no raw objective text",
        )
        check("sk-" not in blob.lower(), "INV-9: execution path persists no secret text")

        # §4: bounded status counters on the existing health surface.
        st = _j(ctl.hermes_controller_status(env.root))
        eq(st["execution_enabled"], True, "status reports the machine gate state")
        eq(st["executions"]["executed"], 1, "status counts the executed action")
        eq(st["executions"]["dispatched"], 1, "status counts the dispatched action")
        eq(st["executions"]["refused"], 0, "status counts no refusals")


def run_l2_idempotent_replay() -> None:
    """§2.2: a replay of an executed action refuses and never duplicates it."""

    with _L2Env() as env:
        first = env.pass_(confirm=True)
        eq(first["execution"]["result"], "dispatched", "first pass dispatches")
        key = first["execution"]["idempotency_key"]

        replay = env.pass_(confirm=True)
        eq(
            replay["execution"]["refused_reason"],
            ctl.REFUSED_ALREADY_EXECUTED,
            "replay refuses with already_executed",
        )
        eq(replay["execution"]["executed"], False, "replay executes nothing")
        eq(replay["would_execute"], False, "replay would_execute False")
        eq(len(env.dispatches), 1, "replay never duplicates the dispatch")
        eq(
            replay["execution"]["prior_state"],
            ctl.EXECUTION_STATE_DISPATCHED,
            "replay names the prior execution state",
        )
        blocks = env.exec_blocks()
        eq(len(blocks), 1, "the executed evidence survives the replay")
        eq(blocks[0]["idempotency_key"], key, "carried-forward evidence keeps the key")
        eq(
            blocks[0]["state"],
            ctl.EXECUTION_STATE_DISPATCHED,
            "carried-forward evidence keeps the dispatched state",
        )


def run_l2_crash_reconciliation() -> None:
    """§5: a crash between the intent write and the dispatch never re-dispatches."""

    def _crash(_seq):
        raise KeyboardInterrupt("simulated crash between intent and dispatch")

    with _L2Env(on_dispatch=_crash) as env:
        crashed = False
        try:
            env.pass_(confirm=True)
        except KeyboardInterrupt:
            crashed = True
        check(crashed, "crash simulation propagated out of the pass")
        eq(len(env.dispatches), 1, "the crashed pass reached the dispatch call")
        blocks = env.exec_blocks()
        eq(len(blocks), 1, "the pre-execution intent row was persisted before dispatch")
        eq(
            blocks[0]["state"],
            ctl.EXECUTION_STATE_INTENT,
            "crash leaves the plan row in the intent state",
        )
        # Recovery: the reconciler reclaims the abandoned lease (TTL expiry) and
        # the replayed key refuses instead of dispatching a duplicate.
        path = mission._db_path(env.root)
        with mission._connect(path, write=True) as db:
            db.execute(
                "UPDATE controller_pass_lease SET lease_expires=?",
                ("2000-01-01T00:00:00+00:00",),
            )
            db.commit()
        after = env.pass_(confirm=True)
        eq(after["lease_reclaimed"], True, "recovery reclaimed the crashed lease")
        eq(
            after["execution"]["refused_reason"],
            ctl.REFUSED_ALREADY_EXECUTED,
            "recovery refuses the replayed key (fail closed)",
        )
        eq(len(env.dispatches), 1, "recovery never duplicates the dispatch")
        eq(
            env.exec_blocks()[0]["state"],
            ctl.EXECUTION_STATE_INTENT,
            "recovery keeps the intent evidence (still refused on the next pass)",
        )
        third = env.pass_(confirm=True)
        eq(
            third["execution"]["refused_reason"],
            ctl.REFUSED_ALREADY_EXECUTED,
            "the replayed key stays refused while the intent evidence remains",
        )
        eq(len(env.dispatches), 1, "no duplicate dispatch after repeated recovery passes")


def run_l2_failed_dispatch_fail_closed() -> None:
    """§4/§5: a failed dispatch surfaces on the pass and cannot retry in-loop."""

    with _L2Env(
        payload={"success": False, "changed": False, "code": "FLEET_DISPATCH_ERROR"}
    ) as env:
        res = env.pass_(confirm=True)
        eq(res["execution"]["result"], "failed", "failed dispatch is reported as failed")
        eq(res["execution"]["executed"], False, "failed dispatch is not an execution")
        eq(res["would_execute"], False, "failed dispatch keeps would_execute False")
        eq(
            res["execution"]["refused_reason"],
            "FLEET_DISPATCH_ERROR",
            "failed dispatch carries the bounded authority-surface code",
        )
        eq(res["pass_result"], ctl.PASS_BLOCKED, "execution failure surfaces on the pass")
        check(
            res["escalation_tier"] != ctl.TIER_GREEN,
            "execution failure is never silently GREEN",
        )
        eq(env.exec_blocks()[0]["state"], ctl.EXECUTION_STATE_FAILED, "plan row failed")
        eq(len(env.dispatches), 1, "no retry loop inside the pass")
        eq(
            [t for t in env.telemetry() if t["executed_idempotency_key"]][0][
                "executed_result"
            ],
            "failed",
            "telemetry records the failed attempt key",
        )


def run_l2_prohibition_matrix() -> None:
    """§2.3: every retained prohibition refuses with zero writes/actions."""

    # (a) unknown/new action kind → fail closed, never guess.
    with _L2Env() as env:
        path = mission._db_path(env.root)
        with mission._connect(path, write=True) as db:
            ctl._init_controller_tables(db)
        with mission._connect(path, write=True) as db:
            block, pending, carry = ctl._l2_plan_execution(
                db,
                path,
                env.root,
                mission_id=MID,
                node_id="a",
                row_key="do_something_new",
                cmds=[{"idempotency_key": "k" * 64}],
                confirm=True,
                attempt_seq=1,
                pass_seq=1,
            )
        eq(
            block["refused_reason"],
            ctl.REFUSED_UNSUPPORTED_ACTION,
            "unknown action kind refuses with unsupported_action",
        )
        eq((pending, carry), (None, None), "unknown action kind prepares nothing")

    # (b) no smallest action at all → no attempt.
    with _L2Env() as env:
        path = mission._db_path(env.root)
        with mission._connect(path, write=True) as db:
            ctl._init_controller_tables(db)
            block, pending, _ = ctl._l2_plan_execution(
                db,
                path,
                env.root,
                mission_id=MID,
                node_id="a",
                row_key="dispatch_ready_child",
                cmds=[],
                confirm=True,
                attempt_seq=1,
                pass_seq=1,
            )
        eq(
            block["refused_reason"],
            ctl.REFUSED_NO_ACTION,
            "no computed action refuses with no_action",
        )
        eq(pending, None, "no computed action prepares nothing")

    # (c) attention/park proposals are never executable, whatever their row.
    with _L2Env() as env:
        path = mission._db_path(env.root)
        with mission._connect(path, write=True) as db:
            ctl._init_controller_tables(db)
            for row_key, expected in (
                ("park_authority", ctl.REFUSED_ATTENTION),
                ("observe_reconciling", ctl.REFUSED_ATTENTION),
                ("escalate_semantic", ctl.REFUSED_ATTENTION),
                ("signal_awaiting_approval", ctl.REFUSED_ATTENTION),
                ("request_completion", ctl.REFUSED_UNSUPPORTED_ACTION),
                ("reclaim_dead_worker", ctl.REFUSED_UNSUPPORTED_ACTION),
                ("retry_transient_backoff", ctl.REFUSED_UNSUPPORTED_ACTION),
                ("fail_closed_evidence", ctl.REFUSED_ATTENTION),
            ):
                block, pending, _ = ctl._l2_plan_execution(
                    db,
                    path,
                    env.root,
                    mission_id=MID,
                    node_id="a",
                    row_key=row_key,
                    cmds=[{"idempotency_key": "k" * 64}],
                    confirm=True,
                    attempt_seq=1,
                    pass_seq=1,
                )
                eq(
                    block["refused_reason"],
                    expected,
                    f"prohibition: {row_key} is not executable ({expected})",
                )
                eq(pending, None, f"prohibition: {row_key} prepares no dispatch")
        eq(len(env.dispatches), 0, "prohibition row keys dispatch nothing")

    # (d) auto-redispatch of reconciling work, end to end.
    with _L2Env() as env:
        env.add_reconciling_delegation("a")
        res = env.pass_(confirm=True)
        eq(
            res["row_key"],
            "observe_reconciling",
            "an in-flight reconciling child classifies observe_reconciling",
        )
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_ATTENTION,
            "prohibition: reconciling work is never auto-redispatched",
        )
        eq(len(env.dispatches), 0, "reconciling work dispatches nothing")
        eq(env.exec_blocks(), [], "reconciling work records no execution state")

    # (e) approval nodes: the controller approves nothing.
    with _L2Env() as env:
        env.set_node("a", state="done")
        res = env.pass_(confirm=True)
        check(
            res["row_key"] in ctl.ATTENTION_ROW_KEYS
            or res["row_key"] not in ctl.EXECUTABLE_ROW_KEYS,
            "an approval frontier proposes a non-executable row",
        )
        check(
            res["execution"]["refused_reason"] in ctl.EXECUTION_REFUSAL_CODES,
            "an approval frontier refuses with a stable reason",
        )
        eq(res["execution"]["executed"], False, "prohibition: the controller approves nothing")
        eq(res["would_execute"], False, "approval frontier would_execute False")
        eq(len(env.dispatches), 0, "approval frontier dispatches nothing")
        eq(env.exec_blocks(), [], "approval frontier records no execution state")

    # (f) high-impact authorization: never self-authorized by the rung.
    with _L2Env(targets=[_peer_target(ceiling="high_impact")]) as env:
        env.set_node(
            "a",
            capability_req=json.dumps(
                {
                    "profile": "hermes-dev",
                    "skills": [],
                    "authorization_class": "high_impact",
                }
            ),
        )
        res = env.pass_(confirm=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_AUTH_CLASS,
            "prohibition: high-impact work keeps its human gate",
        )
        eq(res["execution"]["placement"]["would_assign"], False, "no assignment happened")
        eq(len(env.dispatches), 0, "high-impact work dispatches nothing")

    # (g) secret-like requirement input never crosses the dispatch surface.
    with _L2Env() as env:
        env.set_node(
            "a",
            capability_req=json.dumps(
                {
                    "profile": "hermes-dev",
                    "skills": ["sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"],
                    "authorization_class": "reversible_write",
                }
            ),
        )
        res = env.pass_(confirm=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_SECRET_REQUIREMENT,
            "prohibition: secret-like requirements refuse dispatch",
        )
        eq(len(env.dispatches), 0, "secret-like requirements dispatch nothing")

    # (h) fail-closed unknown state is never executed either.
    with _L2Env() as env:
        env.set_node("a", state="failed")
        res = env.pass_(confirm=True)
        eq(res["row_key"], "unknown_fail_closed", "un-observable failure stays fail-closed")
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_ATTENTION,
            "prohibition: fail-closed state is never auto-executed",
        )
        eq(len(env.dispatches), 0, "fail-closed state dispatches nothing")


def run_l2_no_capable_target() -> None:
    """§3: no scored target ⇒ escalate only; would_assign stays False."""

    # (a) no candidate passes the hard filters (wrong profile scope).
    with _L2Env(targets=[_peer_target(profiles=("other-profile",))]) as env:
        res = env.pass_(confirm=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_NO_TARGET,
            "no capable target refuses with no_capable_target",
        )
        eq(res["execution"]["escalated"], True, "no capable target escalates")
        eq(
            res["execution"]["placement"]["would_assign"],
            False,
            "would_assign stays False when nothing was dispatched",
        )
        eq(
            res["execution"]["placement"]["classification"],
            placement.CLASS_NO_TARGET,
            "placement classification surfaces unchanged",
        )
        eq(len(env.dispatches), 0, "no capable target dispatches nothing")
        spool = ctl.attention_envelopes(env.root)
        eq(len(spool), 1, "no capable target spools exactly one attention envelope")
        eq(spool[0]["attention_class"], "INTERRUPT", "escalation is an INTERRUPT envelope")

    # (b) a scored profile candidate carries no dispatchable agent identity.
    with _L2Env(targets=[_model_target()]) as env:
        res = env.pass_(confirm=True)
        eq(
            res["execution"]["refused_reason"],
            ctl.REFUSED_UNSUPPORTED_ACTION,
            "a target with no authorized agent identity refuses fail-closed",
        )
        eq(len(env.dispatches), 0, "unresolvable target dispatches nothing")
        eq(
            [e for e in ctl.attention_envelopes(env.root)],
            [],
            "a resolvable-but-unsupported target is not treated as a capability wall",
        )


def run_l2_telemetry_counters() -> None:
    """§4: refusals are counted by reason on the existing status surface."""

    with _L2Env() as env:
        env.pass_(confirm=False)
        env.pass_(confirm=False)
        st = _j(ctl.hermes_controller_status(env.root))
        eq(
            st["executions"]["refused_by_reason"].get(ctl.REFUSED_CONFIRM_REQUIRED),
            2,
            "status counts refusals by stable reason",
        )
        eq(st["executions"]["executed"], 0, "no execution was counted")
        eq(st["executions"]["placement_escalations"], 0, "no placement escalation counted")
        tele = env.telemetry()
        check(
            all(not t["executed_idempotency_key"] for t in tele),
            "refusals never write the execution ledger key",
        )
        check(
            all(
                t["executed_refused_reason"] == ctl.REFUSED_CONFIRM_REQUIRED
                for t in tele
            ),
            "refusals record their reason on the pass telemetry row",
        )


def run() -> int:
    run_lease_machinery()
    run_conflation()
    run_shadow_pass_zero_side_effect()
    run_fail_closed()
    run_l2_disabled_identity()
    run_l2_gate_matrix()
    run_l2_dry_run_preview()
    run_l2_executed_path()
    run_l2_idempotent_replay()
    run_l2_crash_reconciliation()
    run_l2_failed_dispatch_fail_closed()
    run_l2_prohibition_matrix()
    run_l2_no_capable_target()
    run_l2_telemetry_counters()
    print(f"\n{len(passed)} passed, {len(failures)} failed")
    if failures:
        print("FAILED:", *failures, sep="\n  - ")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())


def test_controller_loop() -> None:
    """Shadow reconciler matrix under pytest."""
    assert run() == 0, "controller shadow loop matrix reported failures"
