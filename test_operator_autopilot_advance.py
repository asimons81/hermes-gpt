"""Tests for Autopilot node advancement (v0.13 PR3).

Same harness as the scheduler tests. Observation is driven the way the
delegation tests do it: by writing the runner's durable job record for the
dispatched ``task_id``. The real ``hermes_delegation_reconcile`` and Work
Contract validation run; nothing about completion is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_runners as runners
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


def _observe(root: Path, task_id: str, *, state: str, outcome: str = "", error: str = "", artifacts: dict[str, str] | None = None) -> None:
    meta_path, _, _ = runners._job_paths(task_id, root)
    record = {
        "schema_version": runners.SCHEMA_VERSION, "task_id": task_id, "backend": "pi_rpc",
        "state": state, "outcome": outcome or state, "created_at": "2026-08-21T00:00:00+00:00",
        "started_at": "2026-08-21T00:00:01+00:00", "error": error,
    }
    if state in ("completed", "failed", "cancelled"):
        record["ended_at"] = "2026-08-21T00:00:02+00:00"
    runners._atomic_json(meta_path, record)
    if state == "completed":
        directory = root / "missions" / "artifacts" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in (artifacts if artifacts is not None else {"work-contract.json": "{}"}).items():
            (directory / name).write_text(content)


def _task(backend, index: int = 0) -> str:
    return backend.calls[index]["task_id"]


def _delegation_states(root: Path) -> list[str]:
    return [d["state"] for d in _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]]


def test_observed_running_moves_dispatched_node_to_running(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="running")
    out = _tick(root)
    assert out["advanced"] == ["a"]
    assert _states(root) == {"a": "running"}


def test_observed_satisfied_completes_node_and_unblocks_child_in_the_same_tick(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    assert _tick(root)["dispatched"] == ["a"]
    _observe(root, _task(backend), state="completed")
    out = _tick(root)
    assert out["completed"] == ["a"]
    assert out["dispatched"] == ["b"]  # freed slot + unblocked child used within the same tick
    assert _states(root) == {"a": "completed", "b": "dispatched"}
    assert len(backend.calls) == 2


def test_completed_node_is_never_reobserved_or_redispatched(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    _tick(root)
    for _ in range(3):
        again = _tick(root)
        assert again["completed"] == [] and again["dispatched"] == []
    assert _states(root) == {"a": "completed"} and len(backend.calls) == 1


def test_backend_self_report_of_success_is_not_completion(env):
    root, backend = env
    _mk(root, [_node("a")])
    backend.responses = [{"success": True, "changed": True, "state": "completed"}]
    _tick(root)
    assert _delegation_states(root) == ["reconciling"]
    for _ in range(3):
        out = _tick(root)
        assert out["completed"] == []
    assert _states(root) == {"a": "dispatched"}  # no observed run => fail closed


def test_terminal_observation_that_fails_the_contract_does_not_complete(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="completed", outcome="partial")  # terminal, outcome not in outcome_ok
    out = _tick(root)
    assert out["completed"] == []
    assert _states(root)["a"] != "completed"


def test_observed_failure_fails_the_node_and_children_are_never_dispatched(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _tick(root)
    _observe(root, _task(backend), state="failed", error="worker crashed")
    out = _tick(root)
    # "worker crashed" carries no classifiable flavor: the existing classifier
    # fails closed as unknown and flags it for a human (no retry, no replan).
    assert out["failed_nodes"] == {"a": "unknown:unknown_fail_closed need_attention"}
    assert _states(root) == {"a": "failed", "b": "pending"}
    for _ in range(2):
        assert _tick(root)["dispatched"] == []
    assert len(backend.calls) == 1


def test_parallel_nodes_complete_independently(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b"), _node("c"), _node("j", ["a", "b", "c"])])
    assert _tick(root)["dispatched"] == ["a", "b", "c"]
    by_node = {("a", "b", "c")[i]: backend.calls[i]["task_id"] for i in range(3)}
    _observe(root, by_node["a"], state="completed")
    _observe(root, by_node["b"], state="completed")
    _observe(root, by_node["c"], state="running")
    out = _tick(root)
    assert sorted(out["completed"]) == ["a", "b"] and out["advanced"] == ["c"]
    assert _states(root)["j"] == "pending"  # join waits for the last parent
    _observe(root, by_node["c"], state="completed")
    out = _tick(root)
    assert out["completed"] == ["c"] and out["dispatched"] == ["j"]
    assert len({c["task_id"] for c in backend.calls}) == 4


def test_owner_parked_review_gate_is_not_advanced(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    for target in ("running", "awaiting_review"):
        assert _j(plan.hermes_plan_node_transition(
            MID, "a", target, confirm=True, dry_run=False, hermes_root=root))["success"]
    _observe(root, _task(backend), state="completed")  # evidence is SATISFIED...
    out = _tick(root)
    assert out["completed"] == [] and out["observed_held"] == {"a": "review_gate"}
    assert _states(root) == {"a": "awaiting_review"}  # ...but Autopilot did not park it, so it does not release it


def test_crash_mid_walk_resumes_and_completes_without_redispatch(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    real = plan.hermes_plan_node_transition
    seen = {"n": 0}

    def crash_on_third(*args, **kwargs):
        if not kwargs.get("dry_run", True):
            seen["n"] += 1
            if seen["n"] == 3:
                raise OSError("simulated crash mid-walk")
        return real(*args, **kwargs)

    monkeypatch.setattr(plan, "hermes_plan_node_transition", crash_on_third)
    with pytest.raises(OSError):
        _tick(root)
    assert _states(root)["a"] in ("awaiting_review", "validated")
    monkeypatch.setattr(plan, "hermes_plan_node_transition", real)
    out = _tick(root)
    assert out["completed"] == ["a"] and _states(root) == {"a": "completed"}
    assert len(backend.calls) == 1
    assert (autopilot._read_run(MID, root) or {}).get("walking") == {}


def test_plan_replacement_during_advance_is_refused_without_losing_work(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, _task(backend), state="completed")
    real = deleg.hermes_delegation_reconcile

    def replace_then_reconcile(*args, **kwargs):
        replacement = _j(plan.hermes_plan_create(MID, confirm=True, dry_run=False, hermes_root=root))
        assert replacement["code"] == "PLAN_IN_FLIGHT"
        return real(*args, **kwargs)

    monkeypatch.setattr(deleg, "hermes_delegation_reconcile", replace_then_reconcile)
    out = _tick(root)
    assert out["completed"] == ["a"]
    assert _states(root) == {"a": "completed"}


@pytest.mark.parametrize("result", [
    {"success": True, "delegation": {"state": "succeeded", "validation_verdict": "SATISFIED", "contract_sha256": "x" * 64}},
    {"success": True, "evidence_ref": "contract:" + "x" * 64,
     "delegation": {"state": "succeeded", "validation_verdict": "INCONCLUSIVE", "contract_sha256": "x" * 64}},
    {"success": True, "evidence_ref": "contract:" + "y" * 64,
     "delegation": {"state": "succeeded", "validation_verdict": "SATISFIED", "contract_sha256": "x" * 64}},
    {"success": True, "evidence_ref": "contract:" + "x" * 64,
     "delegation": {"state": "reconciling", "validation_verdict": "SATISFIED", "contract_sha256": "x" * 64}},
    {"success": False, "evidence_ref": "contract:" + "x" * 64,
     "delegation": {"state": "succeeded", "validation_verdict": "SATISFIED", "contract_sha256": "x" * 64}},
    {},
])
def test_completion_evidence_gate_fails_closed(result):
    assert autopilot._verified_success(result) is False


def test_completion_evidence_gate_accepts_only_the_full_proof():
    sha = "x" * 64
    assert autopilot._verified_success({
        "success": True, "evidence_ref": f"contract:{sha}",
        "delegation": {"state": "succeeded", "validation_verdict": "SATISFIED", "contract_sha256": sha},
    }) is True
