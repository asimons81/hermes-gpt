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
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operator_controller as ctl
import operator_failure_semantics as fs
import operator_mission_plan as plan
import operator_mission_runtime as mission
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


def run() -> int:
    run_lease_machinery()
    run_conflation()
    run_shadow_pass_zero_side_effect()
    run_fail_closed()
    print(f"\n{len(passed)} passed, {len(failures)} failed")
    if failures:
        print("FAILED:", *failures, sep="\n  - ")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())


def test_controller_loop() -> None:
    """Shadow reconciler matrix under pytest."""
    assert run() == 0, "controller shadow loop matrix reported failures"
