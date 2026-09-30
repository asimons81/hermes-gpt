"""Tests for the plan-module pieces of Autopilot recovery (v0.13 PR5).

``bump_retries`` records a new attempt with the state change; ``apply_rework_patch``
replaces a failed node by a clone without rewriting history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import operator_mission_plan as plan
from test_operator_autopilot_scheduler import MID, _j, _mk, _node, make_env


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _go(root: Path, node_id: str, *targets: str, **kwargs) -> dict:
    out: dict = {}
    for target in targets:
        out = _j(plan.hermes_plan_node_transition(
            MID, node_id, target, confirm=True, dry_run=False, hermes_root=root, **kwargs))
        assert out["success"] is True, out
    return out


def _view(root: Path) -> dict:
    return _j(plan.hermes_plan_get(MID, hermes_root=root))


def _node_view(root: Path, node_id: str) -> dict:
    return next(n for n in _view(root)["nodes"] if n["node_id"] == node_id)


def _fail(root: Path, node_id: str) -> None:
    _go(root, node_id, "dispatched", "failed")


# ---------------------------------------------------------------------------
# bump_retries
# ---------------------------------------------------------------------------


def test_bump_retries_is_off_by_default_and_counts_when_asked(env):
    root, _ = env
    _mk(root, [_node("a")])
    _go(root, "a", "dispatched")
    assert _node_view(root, "a")["retries"] == 0
    _go(root, "a", "paused")
    _go(root, "a", "blockable", bump_retries=True)
    assert _node_view(root, "a")["retries"] == 1
    _go(root, "a", "dispatched", "paused")
    _go(root, "a", "blockable", bump_retries=True)
    assert _node_view(root, "a")["retries"] == 2


def test_bump_retries_dry_run_changes_nothing(env):
    root, _ = env
    _mk(root, [_node("a")])
    _go(root, "a", "dispatched", "paused")
    _j(plan.hermes_plan_node_transition(MID, "a", "blockable", dry_run=True, bump_retries=True, hermes_root=root))
    assert _node_view(root, "a")["retries"] == 0


# ---------------------------------------------------------------------------
# apply_rework_patch
# ---------------------------------------------------------------------------


def test_rework_replaces_failed_node_with_a_clone_and_reparents_children(env):
    root, _ = env
    _mk(root, [_node("root"), _node("b", ["root"]), _node("c", ["b"]), _node("d", ["b"])])
    _go(root, "root", "dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed")
    _fail(root, "b")
    before = _view(root)
    out = plan.apply_rework_patch(MID, "b", expected_plan_version=before["version"], hermes_root=root)
    assert out["clone_node_id"] == "b-r1" and out["reparented"] == ["c", "d"]
    after = _view(root)
    assert after["version"] == before["version"]  # extended, not replaced
    assert after["plan_sha256"] != before["plan_sha256"]
    states = {n["node_id"]: n["state"] for n in after["nodes"]}
    assert states == {"root": "completed", "b": "failed", "b-r1": "pending", "c": "pending", "d": "pending"}
    parents = {n["node_id"]: n["parents"] for n in after["nodes"]}
    assert parents["b-r1"] == ["root"] and parents["c"] == ["b-r1"] and parents["d"] == ["b-r1"]
    ready = _j(plan.hermes_plan_review(MID, hermes_root=root))["ready_nodes"]
    assert ready == ["b-r1"]


def test_rework_never_rewrites_completed_or_the_failed_node(env):
    root, _ = env
    _mk(root, [_node("root"), _node("b", ["root"]), _node("c", ["b"])])
    _go(root, "root", "dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed")
    _fail(root, "b")
    before = {n["node_id"]: n for n in _view(root)["nodes"]}
    plan.apply_rework_patch(MID, "b", expected_plan_version=_view(root)["version"], hermes_root=root)
    after = {n["node_id"]: n for n in _view(root)["nodes"]}
    for node_id in ("root", "b"):
        assert after[node_id] == before[node_id]
    clone = after["b-r1"]
    assert clone["capability_req"] == before["b"]["capability_req"]
    assert clone["objective_sha256"] == before["b"]["objective_sha256"]


def test_rework_refuses_a_stale_plan_version(env):
    root, _ = env
    _mk(root, [_node("a")])
    _fail(root, "a")
    with pytest.raises(plan.PlanVersionConflict):
        plan.apply_rework_patch(MID, "a", expected_plan_version=99, hermes_root=root)
    assert [n["node_id"] for n in _view(root)["nodes"]] == ["a"]


@pytest.mark.parametrize("prepare,message", [
    (lambda root: None, "failed node"),                                    # node is pending
    (lambda root: _go(root, "a", "dispatched"), "failed node"),            # node is in flight
])
def test_rework_requires_a_failed_node(env, prepare, message):
    root, _ = env
    _mk(root, [_node("a")])
    prepare(root)
    with pytest.raises(ValueError, match=message):
        plan.apply_rework_patch(MID, "a", expected_plan_version=_view(root)["version"], hermes_root=root)


def test_rework_refuses_when_a_child_already_started(env):
    root, _ = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _fail(root, "a")
    _go(root, "b", "dispatched")  # owner or another actor started the child
    with pytest.raises(ValueError, match="already started"):
        plan.apply_rework_patch(MID, "a", expected_plan_version=_view(root)["version"], hermes_root=root)
    assert {n["node_id"] for n in _view(root)["nodes"]} == {"a", "b"}


def test_rework_never_replans_around_a_human_gate(env):
    root, _ = env
    _mk(root, [_node("hi", auth="high_impact"), _node("gate", kind="approval", owner="owner")])
    _go(root, "hi", "failed")
    _go(root, "gate", "failed")
    for node_id in ("hi", "gate"):
        with pytest.raises(ValueError, match="human-gated"):
            plan.apply_rework_patch(MID, node_id, expected_plan_version=_view(root)["version"], hermes_root=root)


def test_rework_keeps_an_approval_child_a_gate_and_the_gate_count(env):
    root, _ = env
    _mk(root, [_node("a"), _node("gate", ["a"], kind="approval", owner="owner")])
    _fail(root, "a")
    plan.apply_rework_patch(MID, "a", expected_plan_version=_view(root)["version"], hermes_root=root)
    nodes = {n["node_id"]: n for n in _view(root)["nodes"]}
    assert nodes["gate"]["kind"] == "approval" and nodes["gate"]["parents"] == ["a-r1"]
    assert sum(1 for n in nodes.values() if n["kind"] == "approval") == 1


def test_rework_clone_ids_do_not_collide(env):
    root, _ = env
    _mk(root, [_node("a"), _node("z", ["a"])])
    _fail(root, "a")
    plan.apply_rework_patch(MID, "a", expected_plan_version=_view(root)["version"], hermes_root=root)
    _fail(root, "a-r1")
    out = plan.apply_rework_patch(MID, "a-r1", expected_plan_version=_view(root)["version"], hermes_root=root)
    assert out["clone_node_id"] == "a-r1-r1"
    assert {n["node_id"] for n in _view(root)["nodes"]} == {"a", "a-r1", "a-r1-r1", "z"}


def test_rework_result_is_a_valid_plan_that_round_trips(env):
    root, _ = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _fail(root, "a")
    plan.apply_rework_patch(MID, "a", expected_plan_version=_view(root)["version"], hermes_root=root)
    assert _j(plan.hermes_plan_review(MID, hermes_root=root))["topological_order"] == ["a", "a-r1", "b"]
    # The stored document re-canonicalizes to itself: it is a well-formed plan, not a hand-edited blob.
    stored = _view(root)
    canonical_json, canonical = plan._canonical_plan({k: v for k, v in stored.items() if k in
                                                      ("schema", "mission_id", "version", "decomposition",
                                                       "objective_len", "objective_sha256", "nodes")})
    assert [n["node_id"] for n in canonical["nodes"]] == [n["node_id"] for n in stored["nodes"]]
    assert plan._plan_sha256(canonical_json) == stored["plan_sha256"]


# ---------------------------------------------------------------------------
# The invariant checker itself (defence in depth)
# ---------------------------------------------------------------------------


def _defs():
    base = plan._canonical_node(_node("a"))
    child = plan._canonical_node(_node("c", ["a"]))
    return [base, child]


def test_invariant_checker_rejects_tampering():
    old = _defs()
    clone = plan._canonical_node({**old[0], "node_id": "a-r1", "contract_sha256": ""})
    states = {"a": "failed", "c": "pending"}
    good_child = plan._canonical_node({**old[1], "parents": ["a-r1"], "contract_sha256": ""})
    plan._assert_rework_invariants(old, [old[0], good_child, clone], states,
                                   failed_id="a", clone_id="a-r1", child_ids={"c"})

    lowered = dict(clone, capability_req={**clone["capability_req"], "authorization_class": "read_only"})
    with pytest.raises(ValueError):
        plan._assert_rework_invariants(old, [old[0], good_child, lowered], states,
                                       failed_id="a", clone_id="a-r1", child_ids={"c"})
    with pytest.raises(ValueError, match="completed"):
        plan._assert_rework_invariants(old, [dict(old[0], budget={"est_minutes": 1, "est_tokens": 1}), good_child, clone],
                                       {"a": "completed", "c": "pending"},
                                       failed_id="a", clone_id="a-r1", child_ids={"c"})
    with pytest.raises(ValueError, match="unrelated"):
        plan._assert_rework_invariants(old, [old[0], dict(good_child, owner="other"), clone], states,
                                       failed_id="a", clone_id="a-r1", child_ids=set())
    with pytest.raises(ValueError, match="only add"):
        plan._assert_rework_invariants(old, [old[0], good_child], states,
                                       failed_id="a", clone_id="a-r1", child_ids={"c"})
