"""Tests for Autopilot Mission limits (v0.13 PR6): budget gate and run limits."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_job_supervisor as job_supervisor
import operator_mission_budget as budget
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _states,
    _tick,
    make_env,
)

HARD_POLICY = json.dumps({"hard_block_enabled": True, "pause_on_cross": True})


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root, backend = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_CAP_SECONDS", 0.0)
    monkeypatch.delenv(budget.BUDGET_HARD_BLOCK_ENV, raising=False)
    return root, backend


def _set_budget(root: Path, quota: float = 10.0, policy_json: str = "") -> None:
    out = _j(budget.hermes_budget_set(MID, quota, policy_json, confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _spend(root: Path, amount: float) -> None:
    out = _j(budget.hermes_budget_record(MID, amount, confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _mission_status(root: Path) -> str:
    return _j(mission.hermes_mission_get(MID, hermes_root=root))["status"]


# ---------------------------------------------------------------------------
# Budget gate
# ---------------------------------------------------------------------------


def test_no_budget_account_means_no_envelope_and_dispatch_proceeds(env):
    root, _backend = env
    _mk(root, [_node("a")])
    out = _tick(root)
    assert out["dispatched"] == ["a"] and out["limit"] == ""


def test_within_budget_dispatches(env):
    root, _backend = env
    _mk(root, [_node("a")])
    _set_budget(root, 10.0)
    _spend(root, 3.0)
    assert _tick(root)["dispatched"] == ["a"]


def test_crossed_budget_stops_new_dispatch_even_with_hard_block_gates_off(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b")])
    _set_budget(root, 10.0)  # hard-block policy is off by default
    _spend(root, 12.0)
    out = _tick(root, max_concurrency=8)
    assert out["dispatched"] == [] and backend.calls == []
    assert out["limit"] == "budget_crossed" and out["budget"]["crosses"] is True
    assert out["held"] == {"a": "budget_crossed"}  # the batch stops at the first held node
    assert _mission_status(root) == "running"  # Autopilot's own gate held; nothing paused the Mission
    assert _states(root) == {"a": "pending", "b": "pending"}


def test_existing_hard_block_pauses_the_mission_and_autopilot_stops(env, monkeypatch):
    root, backend = env
    monkeypatch.setenv(budget.BUDGET_HARD_BLOCK_ENV, "1")
    _mk(root, [_node("a")])
    _set_budget(root, 10.0, HARD_POLICY)
    _spend(root, 12.0)
    out = _tick(root)
    assert out["dispatched"] == [] and out["budget"]["enforced"] is True
    assert _mission_status(root) == "paused"  # the existing D3 path acted, through its own gates
    assert _tick(root)["skipped"] == "mission_paused" and backend.calls == []


def test_budget_is_rechecked_before_each_dispatch(env, monkeypatch):
    root, backend = env
    _mk(root, [_node("a"), _node("b"), _node("c")])
    real = budget.hermes_budget_check
    calls = {"n": 0}

    def cross_after_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*args, **kwargs)
        return json.dumps({"success": True, "found": True, "envelope_status": "crossing",
                           "crosses_envelope": True, "enforcement": {}})

    _set_budget(root, 10.0)
    monkeypatch.setattr(budget, "hermes_budget_check", cross_after_first)
    out = _tick(root, max_concurrency=8)
    assert out["dispatched"] == ["a"] and out["held"] == {"b": "budget_crossed"}
    assert len(backend.calls) == 1


@pytest.mark.parametrize("payload,reason", [
    ("raise", "budget_check_failed"),
    ("not-json", "budget_check_failed"),
    ({"success": False}, "budget_check_failed"),
    ({"success": True, "found": True, "envelope_status": "invalid", "crosses_envelope": False}, "budget_invalid"),
    ({"success": True, "found": True, "envelope_status": "", "crosses_envelope": False}, "budget_invalid"),
])
def test_unreadable_or_invalid_envelope_fails_closed(env, monkeypatch, payload, reason):
    root, backend = env
    _mk(root, [_node("a")])

    def fake(*args, **kwargs):
        if payload == "raise":
            raise OSError("budget store unreadable")
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(budget, "hermes_budget_check", fake)
    out = _tick(root)
    assert out["dispatched"] == [] and out["limit"] == reason and backend.calls == []


def test_crossed_budget_does_not_stop_observing_in_flight_work(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _set_budget(root, 10.0)
    assert _tick(root)["dispatched"] == ["a"]
    _spend(root, 12.0)
    _observe(root, backend.calls[0]["task_id"], state="completed")
    out = _tick(root)
    assert out["completed"] == ["a"]  # observation is not new spend
    assert out["dispatched"] == [] and out["limit"] == "budget_crossed"
    assert _states(root) == {"a": "completed", "b": "pending"}


def test_crossed_budget_blocks_a_retry_dispatch(env):
    root, backend = env
    _mk(root, [_node("a")])
    _set_budget(root, 10.0)
    _tick(root)
    _spend(root, 12.0)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="rate_limit 429")
    out = _tick(root)
    assert list(out["retried"]) == ["a"] and out["dispatched"] == []
    assert _states(root) == {"a": "blockable"} and len(backend.calls) == 1


# ---------------------------------------------------------------------------
# Configuration: max_attempts_per_node, max_runtime_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"max_attempts_per_node": 0}, {"max_attempts_per_node": 11}, {"max_attempts_per_node": "x"},
    {"max_runtime_seconds": 59}, {"max_runtime_seconds": autopilot.MAX_MAX_RUNTIME_SECONDS + 1},
])
def test_start_rejects_out_of_range_limits(env, kwargs):
    root, _ = env
    _mk(root, [_node("a")])
    out = _j(autopilot.hermes_autopilot_start(MID, hermes_root=root, **kwargs))
    assert out["success"] is False
    assert autopilot._read_run(MID, root) is None


def test_limits_are_previewed_recorded_and_part_of_the_config_hash(env):
    root, _ = env
    _mk(root, [_node("a")])
    default = _j(autopilot.hermes_autopilot_start(MID, hermes_root=root))
    custom = _j(autopilot.hermes_autopilot_start(MID, max_attempts_per_node=5, max_runtime_seconds=600,
                                                 hermes_root=root))
    assert default["max_attempts_per_node"] == 3 and default["max_runtime_seconds"] == autopilot.DEFAULT_MAX_RUNTIME_SECONDS
    assert custom["max_attempts_per_node"] == 5 and custom["max_runtime_seconds"] == 600
    assert default["config_sha256"] != custom["config_sha256"]


def test_max_attempts_per_node_is_the_classifier_breaker(env):
    root, backend = env
    autopilot._write_run(MID, root, max_attempts_per_node=1)
    _mk(root, [_node("a")])
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="timeout")
    out = _tick(root)
    assert out["retried"] == {} and out["failed_nodes"] == {"a": "transient:breaker_exhausted need_attention"}
    assert len(backend.calls) == 1


def test_max_attempts_per_node_two_allows_exactly_one_retry(env):
    root, backend = env
    autopilot._write_run(MID, root, max_attempts_per_node=2)
    _mk(root, [_node("a")])
    _tick(root)
    for index in (0, 1):
        _observe(root, backend.calls[index]["task_id"], state="failed", error="timeout")
        out = _tick(root)
    assert len(backend.calls) == 2 and out["failed_nodes"] == {"a": "transient:breaker_exhausted need_attention"}


def test_runtime_exceeded_derivation():
    now = datetime.now(timezone.utc)
    fresh = {"started_at": now.isoformat(), "max_runtime_seconds": 600}
    old = {"started_at": (now - timedelta(seconds=601)).isoformat(), "max_runtime_seconds": 600}
    assert autopilot._runtime_exceeded(fresh) is False
    assert autopilot._runtime_exceeded(old) is True
    assert autopilot._runtime_exceeded({"started_at": "garbage", "max_runtime_seconds": 600}) is True  # fail closed
    assert autopilot._runtime_exceeded({"max_runtime_seconds": 600}) is True


def test_expired_runtime_stops_new_work_but_still_drains_in_flight(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"]), _node("c")])
    assert _tick(root, max_concurrency=1)["dispatched"] == ["a"]
    assert autopilot._in_flight_count(MID, root) == 1
    _observe(root, backend.calls[0]["task_id"], state="completed")
    out = autopilot.schedule_tick(MID, root, max_concurrency=8, allow_dispatch=False)
    assert out["completed"] == ["a"]  # in-flight work is still observed and completed
    assert out["dispatched"] == [] and out["limit"] == "max_runtime_exceeded"
    assert len(backend.calls) == 1 and autopilot._in_flight_count(MID, root) == 0


def test_real_worker_ends_a_run_that_is_out_of_time_with_nothing_in_flight(env):
    root, _ = env
    _mk(root, [_node("a")])  # no fleet peer in the hermetic root: nothing will ever be in flight
    started = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, max_runtime_seconds=60,
                                                  hermes_root=root))
    assert started["success"] is True, started
    job_id = started["job_id"]
    try:
        long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        autopilot._write_run(MID, root, started_at=long_ago)
        deadline = time.monotonic() + 20.0
        run: dict = {}
        while time.monotonic() < deadline:
            run = _j(autopilot.hermes_autopilot_status(MID, hermes_root=root))["run"]
            if run["state"] == "failed":
                break
            time.sleep(0.1)
        assert run["state"] == "failed" and run["last_error"] == "max_runtime_exceeded", run
        job = job_supervisor.get_job(job_id, hermes_root=root, reconcile=False)
        assert job is not None and job["status"] == "timed_out"
    finally:
        autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root)
