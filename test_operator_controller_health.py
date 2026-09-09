"""Standalone coverage matrix — controller telemetry/health + tier routing (§12.2).

Card t_e8468723 (Phase 3 item 8). Complements the parent controller-loop
matrix (test_operator_controller.py, t_ad1e6d07): that file owns lease/
conflation/shadow-pass/fail-closed semantics; THIS file owns

  * §12.2 aggregate health fields (24h window, missions_reconciled_24h,
    active_passes, recoveries, escalations, retry_storms_prevented,
    true stale_lease_reclaims, classification_uncertainty, per_class);
  * GREEN/YELLOW/RED tier derivation + controller-level rollup;
  * RED -> fleet-attention envelope spool (no self-send), envelope INV-9
    containment, dedupe stability, spool readback;
  * INV-10 loudness: stale heartbeat => RED; spool failure raises;
  * heartbeat file/mtime freshness for the watchdog.

Runs standalone with the repo venv python (no pytest dependency), mirroring
the sibling standalone matrices. Invoke: .venv/bin/python test_operator_controller_health.py
"""

import json
import os
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
            "title": "Controller health sandbox",
            "objective": "Exercise the telemetry health + tier routing surface.",
            "owner_profile": "default",
            "acceptance_criteria": ["health green"],
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
            ],
        }
    )


def _setup_mission(root: Path, mid: str = "msn-health") -> None:
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


class _Env:
    """Operator-policy env guard + temp hermes root per test group."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory(prefix="ctl-health-")
        self.root = Path(self.td.name) / "hermes"
        self.root.mkdir()
        op.set_audit_log_override(Path(self.td.name) / "audit.jsonl")
        self.saved = {
            k: os.environ.get(k)
            for k in (
                op.OPERATOR_ENABLED_ENV,
                op.OPERATOR_LEVEL_ENV,
                op.OPERATOR_APPLY_MODE_ENV,
            )
        }
        os.environ[op.OPERATOR_ENABLED_ENV] = "1"
        os.environ[op.OPERATOR_LEVEL_ENV] = "workspace"
        os.environ[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        op.set_audit_log_override(None)
        self.td.cleanup()
        return False


# ---------------------------------------------------------------------------
# 1. Tier derivation matrix (pure function, §12.2 alerting rules)
# ---------------------------------------------------------------------------


def run_tier_matrix() -> None:
    print("tier derivation matrix:")
    # GREEN: steady states
    for cls in (
        fs.CLASS_NONE,
        fs.CLASS_NONE_DISPATCHABLE,
        fs.CLASS_WAITING,
        fs.CLASS_TERMINAL,
    ):
        tier, reasons = ctl.derive_pass_tier(
            mission_status="running",
            classification=cls,
            row_key="wait_recheck",
            need_attention=False,
        )
        eq(tier, ctl.TIER_GREEN, f"class {cls} -> GREEN")
        check(bool(reasons), "GREEN carries a reason")
    # GREEN: none_awaiting_approval / none_completion
    for cls, key in (
        (fs.CLASS_NONE_APPROVAL, "signal_awaiting_approval"),
        (fs.CLASS_NONE_COMPLETION, "signal_completion"),
    ):
        tier, _ = ctl.derive_pass_tier(
            mission_status="awaiting_approval",
            classification=cls,
            row_key=key,
            need_attention=False,
        )
        eq(tier, ctl.TIER_GREEN, f"class {cls} -> GREEN")

    # YELLOW: self-healing / routable classes
    for cls, key in (
        (fs.CLASS_TRANSIENT, "retry_transient_backoff"),
        (fs.CLASS_SEMANTIC, "escalate_semantic"),
        (fs.CLASS_ENVIRONMENT, "recover_environment"),
        (fs.CLASS_AMBIGUOUS, "observe_reconciling"),
    ):
        tier, reasons = ctl.derive_pass_tier(
            mission_status="running",
            classification=cls,
            row_key=key,
            need_attention=False,
        )
        eq(tier, ctl.TIER_YELLOW, f"class {cls} -> YELLOW")
        check(reasons == [f"class:{cls}"], "YELLOW reason names the class")

    # RED: hard walls (row keys)
    for key in (
        "park_authority",
        "park_capability",
        "breaker_exhausted",
        "unknown_fail_closed",
        "fail_closed_evidence",
    ):
        tier, reasons = ctl.derive_pass_tier(
            mission_status="running",
            classification=fs.CLASS_AUTHORITY,
            row_key=key,
            need_attention=True,
        )
        eq(tier, ctl.TIER_RED, f"row {key} -> RED")
        eq(reasons, [f"hard_wall:{key}"], "RED reason names the wall")

    # RED: unknown classification fail-closed loud (no hard-wall row key)
    tier, reasons = ctl.derive_pass_tier(
        mission_status="running",
        classification=fs.CLASS_UNKNOWN,
        row_key="observe_reconciling",
        need_attention=True,
    )
    eq(tier, ctl.TIER_RED, "unknown class without hard-wall row -> RED")
    eq(reasons, ["fail_closed:unknown"], "fail-closed reason")

    # RED: stale blocked reconciling past the threshold
    tier, reasons = ctl.derive_pass_tier(
        mission_status="blocked",
        classification=fs.CLASS_AMBIGUOUS,
        row_key="observe_reconciling",
        need_attention=True,
        stuck_s=ctl.STALE_RECONCILING_SECONDS + 1,
    )
    eq(tier, ctl.TIER_RED, "stale blocked+need_attention -> RED")
    eq(reasons, ["stale_blocked_reconciling"], "stale reason")
    # below threshold stays YELLOW (ambiguous class)
    tier, _ = ctl.derive_pass_tier(
        mission_status="blocked",
        classification=fs.CLASS_AMBIGUOUS,
        row_key="observe_reconciling",
        need_attention=True,
        stuck_s=ctl.STALE_RECONCILING_SECONDS - 1,
    )
    eq(tier, ctl.TIER_YELLOW, "recent blocked ambiguous stays YELLOW")
    # blocked without need_attention is not RED
    tier, _ = ctl.derive_pass_tier(
        mission_status="blocked",
        classification=fs.CLASS_AMBIGUOUS,
        row_key="observe_reconciling",
        need_attention=False,
        stuck_s=ctl.STALE_RECONCILING_SECONDS + 999,
    )
    eq(tier, ctl.TIER_YELLOW, "blocked without need_attention stays YELLOW")

    # Unmapped class defaults YELLOW (count-only), never silent GREEN
    tier, _ = ctl.derive_pass_tier(
        mission_status="running",
        classification="",
        row_key="",
        need_attention=False,
    )
    eq(tier, ctl.TIER_YELLOW, "unmapped/empty class -> YELLOW (never GREEN)")

    # Determinism
    a = ctl.derive_pass_tier(
        mission_status="blocked",
        classification=fs.CLASS_UNKNOWN,
        row_key="observe_reconciling",
        need_attention=True,
        stuck_s=7200.0,
    )
    b = ctl.derive_pass_tier(
        mission_status="blocked",
        classification=fs.CLASS_UNKNOWN,
        row_key="observe_reconciling",
        need_attention=True,
        stuck_s=7200.0,
    )
    eq(a, b, "tier derivation deterministic")


# ---------------------------------------------------------------------------
# 2. Live loop: GREEN pass -> telemetry row, aggregate health, heartbeat
# ---------------------------------------------------------------------------


def run_green_pass_aggregate() -> None:
    print("green pass + aggregate health:")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-health")
        res = _j(
            ctl.hermes_controller_reconcile(
                "msn-health", ctl.TRIGGER_MANUAL, hermes_root=root
            )
        )
        eq(res["classification"], fs.CLASS_NONE_DISPATCHABLE, "ready child")
        eq(res["pass_result"], ctl.PASS_DISPATCHED, "pass result dispatched")
        eq(res["escalation_tier"], ctl.TIER_GREEN, "ready-child pass is GREEN")
        check("attention_spooled" not in res, "GREEN pass spools nothing")

        # Heartbeat file fresh (watchdog surface)
        hb_path = root / "missions" / "controller_heartbeat.json"
        check(hb_path.is_file(), "heartbeat file exists")
        hb = json.loads(hb_path.read_text())
        check(hb.get("live") is True, "heartbeat payload live")
        import os as _os

        age = _os.stat(hb_path).st_mtime
        check(age > 0, "heartbeat mtime readable")

        # controller_status aggregate (§12.2 fields)
        st = _j(ctl.hermes_controller_status(root))
        eq(st["success"], True, "status success")
        eq(st["controller_live"], True, "controller_live true")
        eq(st["tier"], ctl.TIER_GREEN, "controller tier GREEN")
        eq(st["missions_reconciled_24h"], 1, "one mission reconciled in 24h")
        eq(st["passes_24h"], 1, "one pass in 24h")
        eq(st["active_passes"], 0, "no active pass after release")
        eq(st["attention_spooled"], 0, "nothing spooled on GREEN")
        eq(st["per_class"].get(fs.CLASS_NONE_DISPATCHABLE), 1, "per_class counts")
        eq(st["per_tier"].get(ctl.TIER_GREEN), 1, "per_tier GREEN=1")
        check(
            {
                "recoveries",
                "escalations",
                "retry_storms_prevented",
                "stale_lease_reclaims",
                "classification_uncertainty",
            }.issubset(st),
            "status carries all §12.2 aggregate fields",
        )

        # INV-9: telemetry row + decision carry no raw objective text
        mdb = mission._db_path(root)
        with mission._connect(mdb, write=False) as db:
            blob = " ".join(
                str(r[0])
                for r in db.execute(
                    "SELECT actions_taken_json FROM controller_telemetry"
                ).fetchall()
            )
            blob += " " + " ".join(
                str(r[0])
                for r in db.execute(
                    "SELECT decision_json FROM controller_plan"
                ).fetchall()
            )
        check(
            "Raw mission objective text." not in blob
            and "Raw node objective A." not in blob
            and "sk-" not in blob.lower()
            and "Bearer" not in blob,
            "INV-9 containment on telemetry+plan",
        )

        # Tier column persisted on the telemetry row
        with mission._connect(mdb, write=False) as db:
            row = db.execute(
                "SELECT escalation_tier, lease_reclaimed FROM controller_telemetry "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        eq(row["escalation_tier"], ctl.TIER_GREEN, "tier persisted per pass")
        eq(int(row["lease_reclaimed"]), 0, "fresh lease not counted as reclaim")


# ---------------------------------------------------------------------------
# 3. RED path: fail-closed unknown -> spool + aggregate reflects RED
# ---------------------------------------------------------------------------


def run_red_path_spool() -> None:
    print("RED path spool (fail-closed unknown):")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-red")
        mdb = mission._db_path(root)
        # Force the fail-closed classification the parent matrix proved:
        # node failed with NO observation channel -> unknown_fail_closed.
        with mission._connect(mdb, write=True) as db:
            ctl._init_controller_tables(db)
            db.execute(
                "UPDATE plan_nodes SET state='failed' WHERE mission_id=? AND node_id=?",
                ("msn-red", "a"),
            )
            db.commit()
        res = _j(
            ctl.hermes_controller_reconcile(
                "msn-red", ctl.TRIGGER_PERIODIC, hermes_root=root
            )
        )
        eq(res["classification"], fs.CLASS_UNKNOWN, "fail-closed unknown")
        eq(res["row_key"], "unknown_fail_closed", "fail-closed row key")
        eq(res["escalation_tier"], ctl.TIER_RED, "fail-closed pass is RED")
        eq(res.get("attention_spooled"), True, "RED pass spooled attention")

        # Spool file + envelope readback
        spool = ctl.attention_envelopes(root)
        eq(len(spool), 1, "one envelope spooled")
        env_doc = spool[0]
        eq(env_doc["attention_class"], "INTERRUPT", "envelope is INTERRUPT")
        eq(env_doc["severity"], "P1", "envelope severity P1")
        eq(env_doc["state"], "open", "envelope state open")
        check(
            env_doc["dedupe_key"].startswith("controller:red:msn-red:"),
            "dedupe key scoped to mission+row",
        )
        check(bool(env_doc["event_id"]), "event id present")
        eq(
            env_doc["metadata"]["row_key"],
            "unknown_fail_closed",
            "metadata row_key",
        )
        eq(env_doc["metadata"]["tier"], ctl.TIER_RED, "metadata tier RED")
        eq(env_doc["metadata"]["would_execute"], False, "metadata would_execute")

        # Envelope is stable-shaped for the router schema (spot-check the
        # contract the delivery lane enforces).
        for field in (
            "schema_version",
            "event_id",
            "source",
            "domain",
            "severity",
            "attention_class",
            "state",
            "action_required",
            "approval_required",
            "title",
            "summary",
            "dedupe_key",
            "occurred_at",
        ):
            check(field in env_doc, f"envelope field {field}")
        eq(env_doc["source"]["kind"], "system", "source kind system")
        check(0 < len(env_doc["title"]) <= 200, "title bounded")
        check(0 < len(env_doc["summary"]) <= 4000, "summary bounded")
        check(
            isinstance(env_doc["evidence"], list) and len(env_doc["evidence"]) <= 32,
            "evidence bounded",
        )

        # INV-9 on the envelope
        blob = json.dumps(env_doc)
        check(
            "Raw mission objective text." not in blob
            and "Raw node objective A." not in blob
            and "sk-" not in blob.lower()
            and "Bearer" not in blob,
            "INV-9 containment on envelope",
        )

        # Dedupe key stable across two RED passes on the same mission+row
        with mission._connect(mdb, write=True) as db:
            db.execute(
                "UPDATE controller_pass_lease SET lease_expires='2000-01-01T00:00:00+00:00' "
                "WHERE mission_id=?",
                ("msn-red",),
            )
            db.commit()
        _j(
            ctl.hermes_controller_reconcile(
                "msn-red", ctl.TRIGGER_PERIODIC, hermes_root=root
            )
        )
        spool2 = ctl.attention_envelopes(root)
        eq(len(spool2), 2, "second RED pass appends a second envelope")
        eq(
            spool2[0]["dedupe_key"],
            spool2[1]["dedupe_key"],
            "dedupe key stable for same mission+row",
        )

        # Aggregate reflects RED
        st = _j(ctl.hermes_controller_status(root))
        eq(st["tier"], ctl.TIER_RED, "controller tier RED after RED pass")
        eq(st["attention_spooled"], 2, "spool count in status")
        eq(st["escalations"], 2, "escalations counted")
        eq(st["classification_uncertainty"], 2, "uncertainty counted")
        eq(st["per_tier"].get(ctl.TIER_RED), 2, "per_tier RED counted")
        eq(st["missions_reconciled_24h"], 1, "distinct missions counted once")

        # No-send proof: nothing in the sandbox root resembles delivery state.
        # (The broker lane is external; the controller only spools.)
        delivery_paths = list(root.rglob("delivered*")) + list(root.rglob("broker*"))
        eq(delivery_paths, [], "no delivery artifacts inside controller root")


# ---------------------------------------------------------------------------
# 4. Reclaim accounting: stale lease reclaims counted from telemetry
# ---------------------------------------------------------------------------


def run_reclaim_accounting() -> None:
    print("stale-lease-reclaim accounting:")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-reclaim")
        mdb = mission._db_path(root)
        # Seed a dead (expired) lease; the next pass must reclaim it.
        with mission._connect(mdb, write=True) as db:
            ctl._init_controller_tables(db)
            db.execute(
                "INSERT OR REPLACE INTO controller_pass_lease("
                "mission_id,lease_lock,lease_expires,pass_seq,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "msn-reclaim",
                    "crashed-pid-4242",
                    "2000-01-01T00:00:00+00:00",
                    5,
                    ctl._now(),
                    ctl._now(),
                ),
            )
            db.commit()
        res = _j(
            ctl.hermes_controller_reconcile(
                "msn-reclaim", ctl.TRIGGER_PERIODIC, hermes_root=root
            )
        )
        eq(res["lease_acquired"], True, "stale lease reclaimed")
        eq(res["lease_reclaimed"], True, "pass reports lease_reclaimed")
        st = _j(ctl.hermes_controller_status(root))
        eq(st["stale_lease_reclaims"], 1, "reclaim counted in aggregate")
        # ...and a conflation stale pass (held live lease) is NOT a reclaim.
        with mission._connect(mdb, write=True) as db:
            ctl._init_controller_tables(db)
            acq = ctl.acquire_lease(
                db,
                "msn-reclaim",
                ctl.TRIGGER_LIVE_EVENT,
                ttl=600.0,
                lease_lock="holder-x",
            )
            assert acq["acquired"] is True
        confl = _j(
            ctl.hermes_controller_reconcile(
                "msn-reclaim", ctl.TRIGGER_DEPENDENCY, hermes_root=root
            )
        )
        eq(confl["pass_result"], ctl.PASS_STALE, "held lease conflates")
        eq(confl["lease_reclaimed"], False, "conflation is not a reclaim")
        st2 = _j(ctl.hermes_controller_status(root))
        eq(st2["stale_lease_reclaims"], 1, "conflation did not inflate reclaims")


# ---------------------------------------------------------------------------
# 5. INV-10: stale heartbeat -> RED; spool failure raises loudly
# ---------------------------------------------------------------------------


def run_inv10_loudness() -> None:
    print("INV-10 loudness:")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-loud")
        # Fresh heartbeat: tier green after a pass
        _j(
            ctl.hermes_controller_reconcile(
                "msn-loud", ctl.TRIGGER_MANUAL, hermes_root=root
            )
        )
        st = _j(ctl.hermes_controller_status(root))
        eq(st["tier"], ctl.TIER_GREEN, "fresh heartbeat + green passes")

        # Backdate the heartbeat past the stale window
        hb_path = root / "missions" / "controller_heartbeat.json"
        hb = json.loads(hb_path.read_text())
        hb["ts"] = hb["ts"] - (ctl.HEARTBEAT_STALE_SECONDS + 60)
        hb_path.write_text(json.dumps(hb))
        st2 = _j(ctl.hermes_controller_status(root))
        eq(st2["controller_live"], False, "stale heartbeat reads not-live")
        eq(st2["tier"], ctl.TIER_RED, "dead controller surface is RED")
        check(
            "heartbeat_stale_or_missing" in st2["tier_reasons"],
            "RED reason names the heartbeat",
        )

        # Spool failure must raise (never silently drop a RED signal).
        doc = ctl.build_attention_envelope(
            mission_id="msn-loud",
            node_id="a",
            classification=fs.CLASS_UNKNOWN,
            row_key="unknown_fail_closed",
            proposed_action="fail-closed: blocked + need_attention",
            reasons=[],
            uncertainty="missing_observation:no_run_channel",
            tier_reasons=["hard_wall:unknown_fail_closed"],
            pass_seq=3,
        )
        spool_path = root / "missions" / "controller_attention_spool.jsonl"
        spool_path.parent.mkdir(parents=True, exist_ok=True)
        spool_path.write_text("")  # file exists...
        spool_path.chmod(0o000)  # ...but is unwritable
        try:
            try:
                ctl.spool_attention_envelope(doc, root)
                raised = False
            except OSError:
                raised = True
            check(raised, "spool failure raises OSError (loud)")
        finally:
            spool_path.chmod(0o644)
        # attention_envelopes tolerates a corrupt line (read-only surface)
        spool_path.write_text("{not-json}\n")
        eq(ctl.attention_envelopes(root), [], "corrupt spool line skipped read-only")


# ---------------------------------------------------------------------------
# 6. Migration: pre-tier store upgrades in place (backward compat, AC-8)
# ---------------------------------------------------------------------------


def run_migration_backward_compat() -> None:
    print("schema migration (pre-tier store):")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-mig")
        mdb = mission._db_path(root)
        # Create a parent-card-shaped store first (controller tables exist)...
        _j(
            ctl.hermes_controller_reconcile(
                "msn-mig", ctl.TRIGGER_MANUAL, hermes_root=root
            )
        )
        # ...then simulate the pre-tier schema: drop the two new columns.
        with sqlite3.connect(mdb) as raw:
            raw.executescript(
                "CREATE TABLE controller_telemetry_old AS "
                "SELECT mission_id,trigger_kind,node_id,started_at,duration_ms,"
                "pass_result,classification,row_key,would_execute,lease_acquired,"
                "actions_taken_json,need_attention,created_at "
                "FROM controller_telemetry;"
            )
            raw.execute("DROP TABLE controller_telemetry")
            raw.execute(
                "ALTER TABLE controller_telemetry_old RENAME TO controller_telemetry"
            )
        # A write connection migrates in place...
        with mission._connect(mdb, write=True) as db:
            ctl._init_controller_tables(db)
            cols = {
                r["name"] for r in db.execute("PRAGMA table_info(controller_telemetry)")
            }
        check(
            {"lease_reclaimed", "escalation_tier"}.issubset(cols),
            "migration adds lease_reclaimed + escalation_tier",
        )
        # ...and the read-only status surface works against it.
        st = _j(ctl.hermes_controller_status(root))
        eq(st["success"], True, "status reads migrated store")
        # Parent's suite fixtures still pass against a migrated store.
        res = _j(
            ctl.hermes_controller_reconcile(
                "msn-mig", ctl.TRIGGER_MANUAL, hermes_root=root
            )
        )
        eq(
            res["success"] if "success" in res else res["pass_result"],
            ctl.PASS_DISPATCHED,
            "reconcile works post-migration",
        )


# ---------------------------------------------------------------------------
# 7. run_loop_tick still works with tiers attached (loop-level AC)
# ---------------------------------------------------------------------------


def run_loop_tick_tiers() -> None:
    print("loop tick with tiers:")
    with _Env() as env:
        root = env.root
        _setup_mission(root, "msn-tick")
        ctl.trigger("msn-tick", ctl.TRIGGER_MANUAL, hermes_root=root)
        tick = ctl.run_loop_tick(hermes_root=root)
        eq(tick["missions"], 1, "tick processed one mission")
        r0 = tick["results"][0]
        eq(r0["escalation_tier"], ctl.TIER_GREEN, "tick result carries GREEN tier")
        st = _j(ctl.hermes_controller_status(root))
        eq(st["tier"], ctl.TIER_GREEN, "status GREEN after tick")


def run() -> int:
    run_tier_matrix()
    run_green_pass_aggregate()
    run_red_path_spool()
    run_reclaim_accounting()
    run_inv10_loudness()
    run_migration_backward_compat()
    run_loop_tick_tiers()
    print(f"\n{len(passed)} passed, {len(failures)} failed")
    if failures:
        print("FAILED:", *failures, sep="\n  - ")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())


def test_controller_health() -> None:
    """Controller telemetry/health + tier routing matrix under pytest."""
    assert run() == 0, "controller health matrix reported failures"
