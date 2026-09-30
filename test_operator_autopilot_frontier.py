"""Tests for the Autopilot Approval Frontier (v0.13 PR4).

The frontier is a derived read: these tests check that it is reported
truthfully, that Autopilot never crosses it, and that resolution is still the
owner's own existing tools (no Autopilot approval path exists).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_mission_plan as plan
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe, _task
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _states,
    _tick,
    make_env,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _gate(node_id: str, parents=()) -> dict:
    return _node(node_id, parents, kind="approval", owner="owner")


def _owner_resolves(root: Path, node_id: str) -> None:
    """The owner walking an approval node through the existing transition tool."""
    for target in ("dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed"):
        out = _j(plan.hermes_plan_node_transition(MID, node_id, target, confirm=True, dry_run=False, hermes_root=root))
        assert out["success"] is True, out


def test_gate_reached_mid_dag_stops_dispatch_past_it_and_reports_waiting(env):
    root, backend = env
    _mk(root, [_node("a"), _gate("gate", ["a"]), _node("c", ["gate"])])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    out = _tick(root)
    assert out["completed"] == ["a"] and out["dispatched"] == []
    assert out["frontier"] == {"active": True, "waiting": True, "reasons": ["owner_gate_node"], "nodes": ["gate"]}
    assert _states(root) == {"a": "completed", "gate": "pending", "c": "pending"}
    for _ in range(3):
        assert _tick(root)["dispatched"] == []
    assert len(backend.calls) == 1  # nothing was ever dispatched past the gate


def test_independent_branches_keep_running_and_waiting_only_when_nothing_else_can_run(env):
    root, backend = env
    _mk(root, [_gate("gate"), _node("x"), _node("after", ["gate"])])
    first = _tick(root)
    assert first["dispatched"] == ["x"]
    assert first["frontier"]["active"] is True and first["frontier"]["waiting"] is False
    _observe(root, _task(backend), state="completed")
    second = _tick(root)
    assert second["completed"] == ["x"]
    assert second["frontier"]["waiting"] is True
    assert _states(root) == {"gate": "pending", "x": "completed", "after": "pending"}


def test_high_impact_node_is_a_frontier_and_is_never_dispatched(env):
    root, backend = env
    _mk(root, [_node("risky", auth="high_impact")])
    out = _tick(root)
    assert out["frontier"]["nodes"] == ["risky"] and out["frontier"]["waiting"] is True
    assert backend.calls == [] and _states(root) == {"risky": "pending"}


def test_owner_resolution_through_existing_tools_lets_autopilot_continue(env):
    root, _backend = env
    _mk(root, [_gate("gate"), _node("after", ["gate"])])
    assert _tick(root)["frontier"]["waiting"] is True
    _owner_resolves(root, "gate")
    out = _tick(root)
    assert out["dispatched"] == ["after"]
    assert out["frontier"]["active"] is False and out["frontier"]["waiting"] is False


def test_owner_parked_gate_node_is_reported_but_never_advanced_by_autopilot(env):
    root, _backend = env
    _mk(root, [_gate("gate")])
    for target in ("dispatched", "running", "awaiting_review"):
        assert _j(plan.hermes_plan_node_transition(
            MID, "gate", target, confirm=True, dry_run=False, hermes_root=root))["success"]
    out = _tick(root)
    assert out["frontier"]["nodes"] == ["gate"] and out["frontier"]["waiting"] is True
    assert out["observed_held"] == {"gate": "owner_gate"} and _states(root) == {"gate": "awaiting_review"}


def test_mission_awaiting_approval_is_a_frontier_and_dispatches_nothing(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    done = _tick(root)
    assert done["completed"] == ["a"] and done["mission_status"] == "awaiting_approval"
    assert done["frontier"]["waiting"] is True  # reported in the very tick that reached it
    assert _j(mission.hermes_mission_get(MID, hermes_root=root))["status"] == "awaiting_approval"
    out = _tick(root)
    assert out["skipped"] == "mission_awaiting_approval"
    assert out["frontier"] == {"active": True, "waiting": True, "reasons": ["mission_awaiting_approval"], "nodes": []}
    assert out["dispatched"] == []


def test_frontier_view_is_a_pure_derivation():
    review = {
        "nodes": [
            {"node_id": "g", "kind": "approval", "capability_req": {}, "state": "pending"},
            {"node_id": "n", "kind": "single", "capability_req": {}, "state": "pending"},
        ],
        "ready_nodes": ["g", "n"],
    }
    assert autopilot._frontier_view(review, "running") == {
        "active": True, "waiting": False, "reasons": ["owner_gate_node"], "nodes": ["g"]}
    assert autopilot._frontier_view(review, "awaiting_approval")["waiting"] is True
    assert autopilot._frontier_view({"nodes": [], "ready_nodes": []}, "running")["active"] is False


def test_there_is_no_autopilot_approval_entrypoint():
    assert not [n for n in dir(autopilot) if "approve" in n.lower() and n.startswith(("hermes_", "autopilot_"))]


# ---------------------------------------------------------------------------
# Live-state reporting must never resurrect a stopped run
# ---------------------------------------------------------------------------


def test_set_live_state_moves_between_live_states_only(env):
    root, _ = env
    autopilot._write_run(MID, root, state="running")
    autopilot._set_live_state(MID, root, "waiting_for_owner")
    assert autopilot._read_run(MID, root)["state"] == "waiting_for_owner"
    autopilot._set_live_state(MID, root, "running")
    assert autopilot._read_run(MID, root)["state"] == "running"
    for terminal in ("stopped", "failed", "completed", "stopping"):
        autopilot._write_run(MID, root, state=terminal)
        autopilot._set_live_state(MID, root, "running")
        autopilot._set_live_state(MID, root, "waiting_for_owner")
        assert autopilot._read_run(MID, root)["state"] == terminal


def test_set_live_state_without_a_run_record_writes_nothing(env):
    root, _ = env
    autopilot._set_live_state("msn-no-run", root, "running")
    assert autopilot._read_run("msn-no-run", root) is None


def test_real_worker_reports_waiting_for_owner_and_can_still_be_stopped(env):
    root, _ = env
    _mk(root, [_gate("gate")])
    started = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
    assert started["success"] is True, started
    try:
        deadline = time.monotonic() + 15.0
        run: dict = {}
        while time.monotonic() < deadline:
            run = _j(autopilot.hermes_autopilot_status(MID, hermes_root=root))["run"]
            if run["state"] == "waiting_for_owner":
                break
            time.sleep(0.1)
        assert run["state"] == "waiting_for_owner", run
        assert run["last_schedule"]["frontier"]["nodes"] == ["gate"]
        assert _states(root) == {"gate": "pending"}
    finally:
        stopped = _j(autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root))
    assert stopped["success"] is True and stopped["state"] == "stopped"
    time.sleep(2.5)  # > one worker tick: a late live-state write must not undo the stop
    assert _j(autopilot.hermes_autopilot_status(MID, hermes_root=root))["run"]["state"] == "stopped"
