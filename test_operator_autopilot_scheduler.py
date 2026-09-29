"""Tests for the Autopilot parallel DAG scheduler (v0.13 PR2).

The scheduler runs in-process here (``autopilot.schedule_tick``): the real
plan store, placement scoring, Work Contract validation, delegation store and
Mission attachments are all exercised. Only two external seams are faked, the
same two the existing controller/delegation tests fake:

- ``placement.load_manifest_targets`` — the scored target set;
- ``contract_mod.hermes_contract_dispatch`` — the actual remote submission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_contract as contract_mod
import operator_controller as controller
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_placement as placement
import operator_policy as op

MID = "msn-sched"


def _j(value: str) -> dict:
    return json.loads(value)


def _peer(name: str = "rza", profiles=("hermes-researcher",)) -> dict:
    return {
        "entity_id": f"fleet:{name}", "kind": "fleet_peer", "name": name, "enabled": True,
        "reachable": True, "identity_configured": True, "authorization_ceiling": "reversible_write",
        "allowed_profiles": list(profiles), "features": [], "workspaces": [], "backends": [],
        "skills": [], "model": "", "provider": "", "host_role": "worker", "allow_public_actions": False,
    }


class Backend:
    """Counts remote submissions; ``responses`` scripts per-call results."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[dict] = []
        self.on_call = None

    def __call__(self, contract_json, confirm=False, dry_run=True, timeout=30, hermes_root=None):
        contract = json.loads(contract_json)
        if dry_run:
            return json.dumps({"success": True, "dry_run": True, "changed": False})
        self.calls.append(contract)
        if self.on_call:
            self.on_call(len(self.calls))
        if self.responses:
            return json.dumps(self.responses.pop(0))
        return json.dumps({"success": True, "changed": True, "state": "running"})


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    monkeypatch.setattr(placement, "load_manifest_targets", lambda hermes_root=None, **kw: [_peer()])
    backend = Backend()
    monkeypatch.setattr(contract_mod, "hermes_contract_dispatch", backend)
    return root, backend


def _node(node_id: str, parents=(), *, kind="single", owner="hermes-researcher", auth="reversible_write") -> dict:
    node = {
        "node_id": node_id, "kind": kind, "owner": owner, "parents": list(parents),
        "objective": f"Raw objective {node_id}.",
        "capability_req": {"profile": "hermes-researcher", "skills": [], "authorization_class": auth},
        "budget": {"est_minutes": 5, "est_tokens": 1000},
        "expected_artifacts": [] if kind == "approval" else ["work-contract.json"],
    }
    return node


def _mk(root: Path, nodes: list[dict], *, mid: str = MID) -> None:
    spec = json.dumps({
        "schema": mission.MISSION_SPEC_SCHEMA, "mission_id": mid, "title": "Scheduler test",
        "objective": "Exercise the parallel scheduler.", "owner_profile": "default",
        "acceptance_criteria": ["scheduled"], "context_refs": [], "skills": [], "final_approval_required": True,
    })
    assert _j(mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=root))["success"]
    _put_plan(root, nodes, mid=mid)
    t = _j(mission.hermes_mission_transition(mid, "running", confirm=True, dry_run=False, hermes_root=root))
    assert t["changed"] is True, t


def _put_plan(root: Path, nodes: list[dict], *, mid: str = MID) -> dict:
    doc = json.dumps({
        "schema": plan.PLAN_SCHEMA, "mission_id": mid, "version": 1, "decomposition": "operator-provided",
        "objective": "Raw mission objective.", "nodes": nodes,
    })
    out = _j(plan.hermes_plan_create(mid, doc, confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out
    return out


def _states(root: Path, mid: str = MID) -> dict[str, str]:
    review = _j(plan.hermes_plan_review(mid, hermes_root=root))
    return {n["node_id"]: n["state"] for n in review["nodes"]}


def _tick(root: Path, *, max_concurrency: int = 3, mid: str = MID) -> dict:
    return autopilot.schedule_tick(mid, root, max_concurrency=max_concurrency)


# ---------------------------------------------------------------------------
# Plan-version compare-and-swap (operator_mission_plan)
# ---------------------------------------------------------------------------


def test_node_transition_cas_refuses_stale_plan_version(env):
    root, _ = env
    _mk(root, [_node("a")])
    stale = _j(plan.hermes_plan_node_transition(
        MID, "a", "dispatched", confirm=True, dry_run=False, expected_plan_version=99, hermes_root=root))
    assert stale["success"] is False
    assert stale["code"] == "PLAN_VERSION_CONFLICT"
    assert stale["expected_plan_version"] == 99 and stale["actual_plan_version"] == 1
    assert _states(root) == {"a": "pending"}


def test_node_transition_cas_applies_to_dry_run_and_none_is_unchanged(env):
    root, _ = env
    _mk(root, [_node("a")])
    dry = _j(plan.hermes_plan_node_transition(
        MID, "a", "dispatched", dry_run=True, expected_plan_version=7, hermes_root=root))
    assert dry["code"] == "PLAN_VERSION_CONFLICT"
    ok = _j(plan.hermes_plan_node_transition(
        MID, "a", "dispatched", confirm=True, dry_run=False, expected_plan_version=1, hermes_root=root))
    assert ok["success"] is True and ok["changed"] is True
    # Historical (unchecked) callers keep working with no expected version.
    nxt = _j(plan.hermes_plan_node_transition(MID, "a", "running", confirm=True, dry_run=False, hermes_root=root))
    assert nxt["success"] is True


def test_node_transition_cas_detects_replaced_plan(env):
    root, _ = env
    _mk(root, [_node("a")])
    replaced = _put_plan(root, [_node("a")])
    assert replaced["version"] == 2
    out = _j(plan.hermes_plan_node_transition(
        MID, "a", "dispatched", confirm=True, dry_run=False, expected_plan_version=1, hermes_root=root))
    assert out["code"] == "PLAN_VERSION_CONFLICT"
    assert _states(root) == {"a": "pending"}


# ---------------------------------------------------------------------------
# Scheduling behaviour
# ---------------------------------------------------------------------------


def test_fills_free_slots_in_node_id_order_and_respects_max_concurrency(env):
    root, backend = env
    _mk(root, [_node("n4"), _node("n2"), _node("n1"), _node("n3"), _node("z", ["n1"])])
    first = _tick(root, max_concurrency=3)
    assert first["dispatched"] == ["n1", "n2", "n3"]
    assert first["slots"] == 3 and first["in_flight"] == 0
    assert _states(root) == {"n1": "dispatched", "n2": "dispatched", "n3": "dispatched", "n4": "pending", "z": "pending"}
    assert len(backend.calls) == 3
    # Full: nothing more is dispatched, and the backend is not touched.
    second = _tick(root, max_concurrency=3)
    assert second["dispatched"] == [] and second["slots"] == 0 and second["in_flight"] == 3
    assert len(backend.calls) == 3


def test_children_wait_for_completed_parents(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    assert _tick(root)["dispatched"] == ["a"]
    # a is dispatched, not completed: b must not be considered ready.
    again = _tick(root)
    assert again["dispatched"] == [] and _states(root)["b"] == "pending"
    assert len(backend.calls) == 1


def test_repeated_ticks_never_redispatch_the_same_node(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b")])
    for _ in range(4):
        _tick(root, max_concurrency=8)
    assert len(backend.calls) == 2
    assert len({c["task_id"] for c in backend.calls}) == 2


def test_dispatch_goes_through_delegation_store_with_deterministic_task_id(env):
    root, backend = env
    _mk(root, [_node("a")])
    assert _tick(root)["dispatched"] == ["a"]
    contract = backend.calls[0]
    review = _j(plan.hermes_plan_review(MID, hermes_root=root))
    node = review["nodes"][0]
    key = autopilot.dispatch_key(MID, review["version"], "a", node["retries"], node["contract_sha256"])
    assert contract["task_id"] == autopilot._task_id(MID, "a", key)
    assert contract["assigned_agent"] == "rza"
    assert "Raw objective" not in json.dumps(contract)  # INV-9: no raw objective in the contract
    listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))
    assert [d["task_id"] for d in listed["delegations"]] == [contract["task_id"]]


def test_crash_between_dispatch_and_transition_is_adopted_not_redispatched(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    real = plan.hermes_plan_node_transition
    state = {"failed": False}

    def crash_once(*args, **kwargs):
        if not kwargs.get("dry_run", True) and not state["failed"]:
            state["failed"] = True
            raise OSError("simulated crash before the node transition")
        return real(*args, **kwargs)

    monkeypatch.setattr(plan, "hermes_plan_node_transition", crash_once)
    with pytest.raises(OSError):
        _tick(root)
    assert len(backend.calls) == 1 and _states(root) == {"a": "pending"}
    resumed = _tick(root)
    assert resumed["adopted"] == ["a"] and resumed["dispatched"] == []
    assert _states(root) == {"a": "dispatched"}
    assert len(backend.calls) == 1  # no duplicate mutation-capable dispatch


def test_controller_dispatched_node_is_adopted_not_dispatched_twice(env):
    root, backend = env
    _mk(root, [_node("a")])
    review = _j(plan.hermes_plan_review(MID, hermes_root=root))
    node = review["nodes"][0]
    requirement = {"profile": "hermes-researcher", "authorization_class": "reversible_write"}
    foreign = autopilot._build_contract(MID, node, requirement=requirement, agent="rza", key="c" * 64,
                                        attempt=0, hermes_root=root)
    foreign["task_id"] = f"ctl-{MID}-a-{'c' * 16}"  # the controller L2 rung's id shape
    out = _j(deleg.hermes_delegation_dispatch(json.dumps(foreign), mission_id=MID, confirm=True,
                                              dry_run=False, hermes_root=root))
    assert out["success"] is True
    assert len(backend.calls) == 1
    summary = _tick(root)
    assert summary["adopted"] == ["a"] and summary["dispatched"] == []
    assert len(backend.calls) == 1
    assert _states(root) == {"a": "dispatched"}


def test_rejected_dispatch_is_not_adopted_and_is_bounded(env):
    root, backend = env
    _mk(root, [_node("a")])
    backend.responses = [{"success": False, "changed": False, "code": "PEER_DOWN"}] * 10
    for i in range(autopilot.MAX_DISPATCH_FAILURES):
        out = _tick(root)
        assert out["failed"] == {"a": "rejected"}, (i, out)
        assert _states(root) == {"a": "pending"}  # a rejected dispatch never marks the node dispatched
    calls = len(backend.calls)
    assert calls == autopilot.MAX_DISPATCH_FAILURES
    held = _tick(root)
    assert held["held"] == {"a": "dispatch_failed"} and len(backend.calls) == calls


def test_rejected_then_accepted_retry_reuses_one_delegation(env):
    root, backend = env
    _mk(root, [_node("a")])
    backend.responses = [{"success": False, "changed": False, "code": "PEER_DOWN"}]
    assert _tick(root)["failed"] == {"a": "rejected"}
    assert _tick(root)["dispatched"] == ["a"]  # exact retry re-drives the same reserved row
    assert len(backend.calls) == 2
    assert backend.calls[0]["task_id"] == backend.calls[1]["task_id"]
    listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))
    assert len(listed["delegations"]) == 1
    assert _states(root) == {"a": "dispatched"}


def test_backend_ambiguous_result_is_in_flight_and_never_redispatched(env):
    root, backend = env
    _mk(root, [_node("a")])
    backend.responses = [{"success": False, "changed": True, "submission_may_have_succeeded": True}]
    first = _tick(root)
    # The delegation store records a `reconciling` row: the node is truthfully in flight.
    assert first["dispatched"] == ["a"] and _states(root) == {"a": "dispatched"}
    listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))
    assert [d["state"] for d in listed["delegations"]] == ["reconciling"]
    for _ in range(3):
        _tick(root)
    assert len(backend.calls) == 1


def test_local_persistence_failure_after_backend_accept_is_adopted_never_redispatched(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    real_sync = deleg._sync_mission_attachment
    monkeypatch.setattr(deleg, "_sync_mission_attachment", lambda *a, **k: False)
    first = _tick(root)
    assert first["dispatched"] == [] and first["held"] == {"a": "ambiguous_dispatch"}
    assert _states(root) == {"a": "pending"} and len(backend.calls) == 1
    monkeypatch.setattr(deleg, "_sync_mission_attachment", real_sync)
    second = _tick(root)
    assert second["adopted"] == ["a"] and len(backend.calls) == 1


def test_owner_gated_nodes_are_never_dispatched(env):
    root, backend = env
    _mk(root, [
        _node("hi", auth="high_impact"),
        _node("ok"),
        _node("gate", ["ok"], kind="approval", owner="owner"),
    ])
    out = _tick(root, max_concurrency=8)
    assert out["dispatched"] == ["ok"]
    assert out["held"] == {"hi": "owner_gate"}
    assert _states(root)["hi"] == "pending" and _states(root)["gate"] == "pending"
    assert len(backend.calls) == 1


def test_no_capable_target_holds_without_dispatch(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    monkeypatch.setattr(placement, "load_manifest_targets", lambda hermes_root=None, **kw: [])
    out = _tick(root)
    assert out["held"] == {"a": "no_capable_target"} and backend.calls == []


def test_non_peer_target_is_not_dispatchable(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    profile_only = _peer()
    profile_only.update({"entity_id": "profile:hermes-researcher", "kind": "profile", "authorization_ceiling": ""})
    monkeypatch.setattr(placement, "load_manifest_targets", lambda hermes_root=None, **kw: [profile_only])
    out = _tick(root)
    assert out["dispatched"] == [] and backend.calls == []
    assert set(out["held"].values()) <= {"no_dispatchable_target", "no_capable_target"}


# ---------------------------------------------------------------------------
# Gates and the "one scheduler" lease
# ---------------------------------------------------------------------------


def test_machine_gate_off_dispatches_nothing(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV)
    out = _tick(root)
    assert out["skipped"] == "autopilot_gate_off" and backend.calls == []


@pytest.mark.parametrize("status", ["paused", "blocked"])
def test_held_mission_status_dispatches_nothing(env, status):
    root, backend = env
    _mk(root, [_node("a")])
    t = _j(mission.hermes_mission_transition(MID, status, confirm=True, dry_run=False, hermes_root=root))
    assert t["changed"] is True, t
    out = _tick(root)
    assert out["skipped"] == f"mission_{status}" and backend.calls == []


def test_live_controller_pass_blocks_the_tick_and_lease_is_released_after(env):
    root, backend = env
    _mk(root, [_node("a")])
    with controller._connect(controller._db_path(root), write=True) as db:
        held = controller.acquire_lease(db, MID, "reconcile", ttl=60.0, lease_lock="ctl-pass")
        assert held["acquired"] is True
    blocked = _tick(root)
    assert blocked["skipped"] == "controller_pass_active" and backend.calls == []
    with controller._connect(controller._db_path(root), write=True) as db:
        controller.release_lease(db, MID, "ctl-pass")
    assert _tick(root)["dispatched"] == ["a"]
    # The scheduler's own lease must not linger and starve the controller.
    with controller._connect(controller._db_path(root), write=True) as db:
        assert controller.acquire_lease(db, MID, "reconcile", ttl=60.0, lease_lock="ctl-2")["acquired"] is True


def test_lease_is_released_even_when_a_tick_raises(env, monkeypatch):
    root, _ = env
    _mk(root, [_node("a")])
    monkeypatch.setattr(autopilot, "_dispatch_one", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        _tick(root)
    with controller._connect(controller._db_path(root), write=True) as db:
        assert controller.acquire_lease(db, MID, "reconcile", ttl=60.0, lease_lock="ctl-3")["acquired"] is True


# ---------------------------------------------------------------------------
# Plan replaced under the scheduler
# ---------------------------------------------------------------------------


def test_plan_replaced_before_dispatch_aborts_with_no_remote_side_effect(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a"), _node("b")])
    real = contract_mod.hermes_contract_define
    fired = {"n": 0}

    def replace_plan_once(*args, **kwargs):
        if not fired["n"]:
            fired["n"] += 1
            _put_plan(root, [_node("a"), _node("b")])
        return real(*args, **kwargs)

    monkeypatch.setattr(contract_mod, "hermes_contract_define", replace_plan_once)
    out = _tick(root, max_concurrency=8)
    assert out["skipped"] == "plan_version_conflict"
    assert out["dispatched"] == [] and backend.calls == []
    assert _states(root) == {"a": "pending", "b": "pending"}
    # Next tick re-reads the new version and proceeds normally.
    assert _tick(root, max_concurrency=8)["dispatched"] == ["a", "b"]


def test_plan_replaced_during_dispatch_never_marks_new_plan_node_dispatched(env):
    root, backend = env
    _mk(root, [_node("a")])
    backend.on_call = lambda n: _put_plan(root, [_node("a")])
    out = _tick(root)
    assert out["skipped"] == "plan_version_conflict" and out["dispatched"] == []
    assert _states(root) == {"a": "pending"}  # the stale transition was refused by the CAS


# ---------------------------------------------------------------------------
# Controller semantics are untouched
# ---------------------------------------------------------------------------


def test_controller_frontier_still_picks_one_node():
    nodes = [
        {"node_id": "a", "state": "pending", "deps": []},
        {"node_id": "b", "state": "pending", "deps": []},
    ]
    assert controller._frontier(nodes)["node_id"] == "a"
    nodes[1]["state"] = "running"
    assert controller._frontier(nodes)["node_id"] == "b"  # in-flight before new work, unchanged


def test_scheduler_has_no_second_dispatch_entrypoint():
    assert not hasattr(autopilot, "autopilot_dispatch")
    assert not any(n.startswith("hermes_autopilot_dispatch") for n in dir(autopilot))
