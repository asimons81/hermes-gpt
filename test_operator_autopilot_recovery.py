"""Tests for Autopilot recovery (v0.13 PR5): bounded retry and bounded replan.

Every decision must come from the existing failure classifier, be bounded, and
leave the Mission able to reach ``awaiting_approval`` (never ``failed``) once the
successor attempt succeeds — without ever hiding a live or successful attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_placement as placement
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _peer,
    _states,
    _tick,
    make_env,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root, backend = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_CAP_SECONDS", 0.0)
    return root, backend


def _two_peers(monkeypatch) -> None:
    monkeypatch.setattr(placement, "load_manifest_targets",
                        lambda hermes_root=None, **kw: [_peer("rza"), _peer("rzb")])


def _fail(root: Path, backend, index: int, error: str) -> None:
    _observe(root, backend.calls[index]["task_id"], state="failed", error=error)


def _complete(root: Path, backend, index: int) -> None:
    _observe(root, backend.calls[index]["task_id"], state="completed")


def _set_replans(root: Path, n: int) -> None:
    autopilot._write_run(MID, root, max_replans=n)


def _relationships(root: Path) -> dict[str, str]:
    m = _j(mission.hermes_mission_get(MID, hermes_root=root))
    return {a["ref"]: a["relationship"] for a in m["attachments"] if a["kind"] == "delegation"}


def _mission_status(root: Path) -> str:
    return _j(mission.hermes_mission_get(MID, hermes_root=root))["status"]


def _node_view(root: Path, node_id: str) -> dict:
    return next(n for n in _j(plan.hermes_plan_get(MID, hermes_root=root))["nodes"] if n["node_id"] == node_id)


# ---------------------------------------------------------------------------
# Level A — bounded retry of transient failures
# ---------------------------------------------------------------------------


def test_transient_failure_retries_on_an_alternate_peer_and_the_mission_still_completes(env, monkeypatch):
    root, backend = env
    _two_peers(monkeypatch)
    _mk(root, [_node("a")])
    _tick(root)
    first_peer = backend.calls[0]["assigned_agent"]
    _fail(root, backend, 0, "rate_limit 429 too many requests")
    out = _tick(root)
    assert list(out["retried"]) == ["a"] and out["retried"]["a"].startswith("transient:retry_transient_backoff")
    assert out["dispatched"] == ["a"]  # backoff is zero here; the retry is scheduled in the same tick
    assert backend.calls[1]["assigned_agent"] != first_peer
    assert _node_view(root, "a")["retries"] == 1 and _states(root) == {"a": "dispatched"}
    first_delegation = next(d for d in _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
                            if d["task_id"] == backend.calls[0]["task_id"])["delegation_id"]
    assert _relationships(root)[first_delegation].startswith(mission.SUPERSEDED_PREFIX)

    _complete(root, backend, 1)
    done = _tick(root)
    assert done["completed"] == ["a"] and done["mission_status"] == "awaiting_approval"  # not "failed"


def test_retry_falls_back_to_the_only_capable_peer(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "connection_reset")
    out = _tick(root)
    assert out["dispatched"] == ["a"]
    assert backend.calls[1]["assigned_agent"] == backend.calls[0]["assigned_agent"] == "rza"


def test_backoff_holds_the_retry(env, monkeypatch):
    root, backend = env
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_BASE_SECONDS", 600.0)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_CAP_SECONDS", 900.0)
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "timeout")
    out = _tick(root)
    assert list(out["retried"]) == ["a"] and out["held"] == {"a": "retry_backoff"}
    assert _states(root) == {"a": "blockable"} and len(backend.calls) == 1
    assert _tick(root)["dispatched"] == []


def test_retries_are_bounded_by_the_classifier_breaker(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _tick(root)
    for attempt in range(autopilot.MAX_NODE_ATTEMPTS):
        _fail(root, backend, attempt, "service_unavailable 503")
        out = _tick(root)
    assert len(backend.calls) == autopilot.MAX_NODE_ATTEMPTS
    assert out["failed_nodes"] == {"a": "transient:breaker_exhausted need_attention"}
    assert _states(root) == {"a": "failed", "b": "pending"}
    for _ in range(3):
        assert _tick(root)["dispatched"] == []
    assert len(backend.calls) == autopilot.MAX_NODE_ATTEMPTS


@pytest.mark.parametrize("error,expected_class", [
    ("quota exceeded", "authority"),
    ("no_capable_target", "capability"),
    ("disk_full", "environment"),
    ("worker crashed", "unknown"),  # unflavored: never guess a class
])
def test_only_transient_failures_are_ever_retried(env, error, expected_class):
    root, backend = env
    _set_replans(root, 2)  # replans allowed, and still none of these may trigger one
    _mk(root, [_node("a"), _node("b", ["a"])])
    _tick(root)
    _fail(root, backend, 0, error)
    out = _tick(root)
    assert out["retried"] == {} and out["replanned"] == {}
    assert out["failed_nodes"]["a"].startswith(expected_class) and "need_attention" in out["failed_nodes"]["a"]
    assert _states(root) == {"a": "failed", "b": "pending"} and len(backend.calls) == 1


def test_external_reconcile_during_backoff_preserves_the_mission_and_failed_evidence(env, monkeypatch):
    root, backend = env
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_BASE_SECONDS", 600.0)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_CAP_SECONDS", 900.0)
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "timeout")
    _tick(root)  # retry scheduled but held by backoff: no successor delegation yet
    mission.hermes_mission_reconcile(MID, confirm=True, dry_run=False, hermes_root=root)
    assert _mission_status(root) == "running"
    assert any(a["state"] == "failed" for a in _j(mission.hermes_mission_get(MID, hermes_root=root))["attachments"])


# ---------------------------------------------------------------------------
# Level B — bounded rework replan of semantic failures
# ---------------------------------------------------------------------------


def test_semantic_failure_replans_via_a_rework_clone_and_the_mission_completes(env):
    root, backend = env
    _set_replans(root, 1)
    _mk(root, [_node("a"), _node("c", ["a"])])
    _tick(root)
    _fail(root, backend, 0, "tests_failed: assertion in module")
    out = _tick(root)
    assert out["replanned"] == {"a": "a-r1"} and out["dispatched"] == ["a-r1"]
    assert _states(root) == {"a": "failed", "a-r1": "dispatched", "c": "pending"}
    parents = {n["node_id"]: n["parents"] for n in _j(plan.hermes_plan_get(MID, hermes_root=root))["nodes"]}
    assert parents["c"] == ["a-r1"]
    assert (autopilot._read_run(MID, root) or {})["replans_used"] == 1
    first_delegation = next(d for d in _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
                            if d["task_id"] == backend.calls[0]["task_id"])["delegation_id"]
    assert _relationships(root)[first_delegation].startswith(mission.SUPERSEDED_PREFIX)

    _complete(root, backend, 1)
    assert _tick(root)["completed"] == ["a-r1"]
    _complete(root, backend, 2)
    done = _tick(root)
    assert done["completed"] == ["c"] and done["mission_status"] == "awaiting_approval"
    assert _states(root) == {"a": "failed", "a-r1": "completed", "c": "completed"}  # history preserved


def test_replan_is_off_when_max_replans_is_zero(env):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "tests_failed")
    out = _tick(root)
    assert out["replanned"] == {} and _states(root) == {"a": "failed"}
    assert "semantic_failure" in out["failed_nodes"]["a"]


def test_replans_are_bounded_by_max_replans(env):
    root, backend = env
    _set_replans(root, 1)
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "tests_failed")
    assert _tick(root)["replanned"] == {"a": "a-r1"}
    _fail(root, backend, 1, "tests_failed")  # the clone fails too
    out = _tick(root)
    assert out["replanned"] == {} and "a-r1" in out["failed_nodes"]
    assert _states(root) == {"a": "failed", "a-r1": "failed"}  # no a-r1-r1: the bound held
    assert len(backend.calls) == 2


def test_replan_never_touches_completed_nodes(env):
    root, backend = env
    _set_replans(root, 1)
    _mk(root, [_node("root"), _node("b", ["root"])])
    _tick(root)
    _complete(root, backend, 0)
    _tick(root)  # root completes, b dispatches
    before = _node_view(root, "root")
    _fail(root, backend, 1, "assertionerror")
    _tick(root)
    assert _node_view(root, "root") == before and _states(root)["root"] == "completed"


def test_crash_after_patching_adopts_the_clone_and_never_creates_a_second(env, monkeypatch):
    root, backend = env
    _set_replans(root, 2)
    _mk(root, [_node("a"), _node("c", ["a"])])
    _tick(root)
    _fail(root, backend, 0, "tests_failed")
    real = plan.apply_rework_patch

    def patch_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("simulated process death after the plan was patched")

    monkeypatch.setattr(plan, "apply_rework_patch", patch_then_die)
    with pytest.raises(RuntimeError):
        _tick(root)
    monkeypatch.setattr(plan, "apply_rework_patch", real)
    out = _tick(root)
    assert out["replanned"] == {"a": "a-r1"}
    ids = sorted(n["node_id"] for n in _j(plan.hermes_plan_get(MID, hermes_root=root))["nodes"])
    assert ids == ["a", "a-r1", "c"]  # exactly one clone
    assert (autopilot._read_run(MID, root) or {})["replans_used"] == 1


def test_replan_conflict_keeps_intent_and_writes_nothing(env, monkeypatch):
    root, backend = env
    _set_replans(root, 1)
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "tests_failed")

    def conflict(*args, **kwargs):
        raise plan.PlanVersionConflict(1, 2)

    monkeypatch.setattr(plan, "apply_rework_patch", conflict)
    out = _tick(root)
    assert out["skipped"] == "plan_version_conflict"
    assert {n["node_id"] for n in _j(plan.hermes_plan_get(MID, hermes_root=root))["nodes"]} == {"a"}


def test_supersede_is_retried_until_it_succeeds(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a")])
    _tick(root)
    _fail(root, backend, 0, "timeout")
    real = mission.supersede_delegation_attachment
    monkeypatch.setattr(mission, "supersede_delegation_attachment", lambda *a, **k: False)
    _tick(root)  # retry dispatched, marker could not be written
    recovery = autopilot._load_recovery(autopilot._read_run(MID, root) or {})
    assert list(recovery["pending_supersede"]) == ["a"]
    monkeypatch.setattr(mission, "supersede_delegation_attachment", real)
    _complete(root, backend, 1)
    done = _tick(root)
    assert done["completed"] == ["a"] and done["mission_status"] == "awaiting_approval"
    assert autopilot._load_recovery(autopilot._read_run(MID, root) or {})["pending_supersede"] == {}


# ---------------------------------------------------------------------------
# Small pure pieces
# ---------------------------------------------------------------------------


def test_backoff_is_deterministic_monotonic_and_capped():
    values = [autopilot._backoff_seconds("a", n) for n in (1, 2, 3, 4, 10)]
    assert values == [autopilot._backoff_seconds("a", n) for n in (1, 2, 3, 4, 10)]
    assert values[0] >= autopilot.RETRY_BACKOFF_BASE_SECONDS
    assert values[:3] == sorted(values[:3]) and max(values) <= autopilot.RETRY_BACKOFF_CAP_SECONDS


def test_malformed_observation_fails_closed_as_unknown():
    decision = autopilot._classify_failure("msn-x", {"node_id": "a"}, {"bogus": True})
    assert decision["classification"] == "unknown" and decision["auto_retry"] is False


def test_failed_run_verdict_is_withheld_but_other_verdicts_pass_through():
    node = {"node_id": "a", "state": "dispatched", "retries": 0}
    failed = {"delegation": {"state": "failed", "validation_verdict": "NOT_SATISFIED"}, "observed": {"error": "x"}}
    assert autopilot._observation_env(node, failed, "running", True)["delegation"]["validation_verdict"] == ""
    other = {"delegation": {"state": "reconciling", "validation_verdict": "INCONCLUSIVE"}}
    assert autopilot._observation_env(node, other, "running", True)["delegation"]["validation_verdict"] == "INCONCLUSIVE"
