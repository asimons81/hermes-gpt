"""Tests for the Autopilot status v2 summary (v0.13 PR9).

Additive to the PR1 payload, derived from durable state on every call, read-only
(a status call may never pause a Mission or write anything), and fail-soft: the
derived part degrades to ``available: false`` rather than failing status.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_mission_budget as budget
import operator_mission_plan as plan
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _peer,
    _tick,
    make_env,
)

OLD_KEYS = {"success", "schema_version", "tool", "mission_id", "found", "run", "worker"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root, backend = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(autopilot, "RETRY_BACKOFF_CAP_SECONDS", 0.0)
    monkeypatch.delenv(budget.BUDGET_HARD_BLOCK_ENV, raising=False)
    return root, backend


def _status(root: Path) -> dict:
    return json.loads(autopilot.hermes_autopilot_status(MID, hermes_root=root))


def _summary(root: Path) -> dict:
    return _status(root)["summary"]


def _codes(summary: dict) -> set[str]:
    return {item["code"] for item in summary["attention"]}


def _start_record(root: Path, **fields) -> None:
    autopilot._write_run(MID, root, state="running", attempt=1, max_concurrency=3, **fields)


def _complete(root, backend, index):
    _observe(root, backend.calls[index]["task_id"], state="completed")


# ---------------------------------------------------------------------------
# Additive, not breaking
# ---------------------------------------------------------------------------


def test_existing_payload_shape_is_unchanged_and_summary_is_the_only_addition(env):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root)
    payload = _status(root)
    assert set(payload) - {"summary"} == OLD_KEYS
    assert payload["success"] is True and payload["found"] is True and payload["run"]["state"] == "running"
    assert set(payload["worker"]) == {"pid", "status", "process_verification"}


def test_unknown_mission_still_reports_not_found_with_no_summary(env):
    root, _ = env
    payload = json.loads(autopilot.hermes_autopilot_status("msn-never-started", hermes_root=root))
    assert payload["found"] is False and "summary" not in payload


# ---------------------------------------------------------------------------
# Progress, workers, frontier
# ---------------------------------------------------------------------------


def test_progress_counts_follow_the_plan(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"]), _node("c")])
    _start_record(root)
    assert _summary(root)["progress"]["percent"] == 0 and _summary(root)["progress"]["total"] == 3
    _tick(root)
    progress = _summary(root)["progress"]
    assert progress["by_state"]["dispatched"] == 2 and progress["by_state"]["pending"] == 1
    assert progress["in_flight"] == 2 and progress["ready"] == 0
    _complete(root, backend, 0)
    _tick(root)
    progress = _summary(root)["progress"]
    assert progress["completed"] == 1 and progress["percent"] == 33 and progress["total"] == 3


def test_workers_lists_in_flight_nodes_with_their_delegation(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b")])
    _start_record(root)
    _tick(root)
    workers = _summary(root)["workers"]
    assert [w["node_id"] for w in workers] == ["a", "b"]
    assert all(w["state"] == "dispatched" and w["attempt"] == 0 and w["peer"] == "rza" for w in workers)
    assert all(w["delegation_id"] and w["delegation_state"] for w in workers)
    assert len(backend.calls) == 2


def test_frontier_is_derived_from_the_current_plan_not_the_cached_last_tick(env):
    root, _ = env
    _mk(root, [_node("g", kind="approval", owner="owner")])
    _start_record(root, last_schedule={"frontier": {"active": False, "waiting": False, "reasons": [], "nodes": []}})
    summary = _summary(root)
    assert summary["frontier"]["active"] is True and summary["frontier"]["nodes"] == ["g"]  # not the stale cache
    assert "owner_gate_node" in _codes(summary) and summary["needs_owner"] is True


# ---------------------------------------------------------------------------
# Budget (read-only), recovery, limits
# ---------------------------------------------------------------------------


def test_budget_view_states_and_attention(env):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root)
    assert _summary(root)["budget"] == {"configured": False}
    assert _j(budget.hermes_budget_set(MID, 10.0, "", confirm=True, dry_run=False, hermes_root=root))["success"]
    within = _summary(root)
    assert within["budget"]["configured"] is True and within["budget"]["status"] == "within"
    assert "budget_crossed" not in _codes(within)
    _j(budget.hermes_budget_record(MID, 12.0, confirm=True, dry_run=False, hermes_root=root))
    crossed = _summary(root)
    assert crossed["budget"]["crosses"] is True and "budget_crossed" in _codes(crossed) and crossed["needs_owner"] is True


def test_a_status_read_never_enforces_the_budget(env, monkeypatch):
    root, _ = env
    monkeypatch.setenv(budget.BUDGET_HARD_BLOCK_ENV, "1")
    _mk(root, [_node("a")])
    _start_record(root)
    hard = json.dumps({"hard_block_enabled": True, "pause_on_cross": True})
    assert _j(budget.hermes_budget_set(MID, 10.0, hard, confirm=True, dry_run=False, hermes_root=root))["success"]
    _j(budget.hermes_budget_record(MID, 12.0, confirm=True, dry_run=False, hermes_root=root))
    for _ in range(3):
        _status(root)
    status = _j(mission.hermes_mission_get(MID, hermes_root=root))["status"]
    assert status == "running"  # observing must never pause the Mission (only a dispatch attempt may)


def test_unreadable_budget_is_reported_not_hidden(env, monkeypatch):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root)

    def broken(*args, **kwargs):
        raise OSError("budget store unreadable")

    monkeypatch.setattr(budget, "hermes_budget_check", broken)
    summary = _summary(root)
    assert summary["budget"] == {"configured": None, "error": True} and "budget_check_failed" in _codes(summary)


def test_recovery_counters_for_a_retry_and_a_replan(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _start_record(root, max_replans=1)
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="timeout")
    _tick(root)
    recovery = _summary(root)["recovery"]
    assert recovery["retries"] == 1 and recovery["replans_used"] == 0 and recovery["failed_nodes"] == []
    _observe(root, backend.calls[1]["task_id"], state="failed", error="tests_failed assertion")
    _tick(root)
    summary = _summary(root)
    assert summary["recovery"]["replans_used"] == 1 and summary["recovery"]["superseded_nodes"] == 1
    assert summary["recovery"]["failed_nodes"] == []  # the replaced node is history, not an open failure
    assert summary["progress"]["total"] == 2  # a (replaced) is excluded; its clone a-r1 and b are counted


def test_an_unrecovered_failure_needs_the_owner(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _start_record(root)
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="failed", error="worker crashed")
    _tick(root)
    summary = _summary(root)
    assert summary["recovery"]["failed_nodes"] == ["a"]
    assert {"code": "node_failed", "nodes": ["a"], "severity": "owner"} in summary["attention"]
    assert summary["needs_owner"] is True


def test_limits_and_runtime_attention(env):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root, max_runtime_seconds=600)
    limits = _summary(root)["limits"]
    assert limits["max_runtime_seconds"] == 600 and 590 <= limits["runtime_remaining_seconds"] <= 600
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    autopilot._write_run(MID, root, started_at=long_ago)
    expired = _summary(root)
    assert expired["limits"]["runtime_remaining_seconds"] == 0.0 and "runtime_exceeded" in _codes(expired)
    assert expired["needs_owner"] is False  # informational: Autopilot ends the run itself
    autopilot._write_run(MID, root, started_at="garbage")
    assert "runtime_exceeded" in _codes(_summary(root))  # an unknowable age counts as exceeded


def test_silent_worker_is_informational(env):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root)
    old = (datetime.now(timezone.utc) - timedelta(seconds=autopilot.SILENT_TICK_SECONDS + 30)).isoformat()
    autopilot._write_run(MID, root, last_tick_at=old, wakeups={"event": 3, "timer": 9}, last_wake="timer",
                         last_event_cursor=7)
    summary = _summary(root)
    assert "worker_silent" in _codes(summary) and summary["needs_owner"] is False
    assert summary["wake"]["wakeups"] == {"event": 3, "timer": 9} and summary["wake"]["last_event_cursor"] == 7
    assert summary["wake"]["tick_age_seconds"] >= autopilot.SILENT_TICK_SECONDS


def test_mission_awaiting_approval_needs_the_owner(env):
    root, backend = env
    _mk(root, [_node("a")])
    _start_record(root)
    _tick(root)
    _complete(root, backend, 0)
    _tick(root)
    summary = _summary(root)
    assert summary["mission_status"] == "awaiting_approval" and "mission_awaiting_approval" in _codes(summary)
    assert summary["needs_owner"] is True and summary["progress"]["percent"] == 100


# ---------------------------------------------------------------------------
# Read-only and fail-soft
# ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and "audit" not in p.name
        and not p.name.endswith((".lock", "-shm"))
        and not (p.name.endswith("-wal") and p.stat().st_size == 0)
    }  # SQLite read-only connections may create empty WAL bookkeeping files; nonempty WAL data stays covered.


def test_snapshot_keeps_nonempty_wal_data_and_ignores_only_empty_wal(tmp_path: Path):
    db = tmp_path / "missions.db"
    empty_wal = tmp_path / "missions.db-wal"
    nonempty_wal = tmp_path / "delegations.db-wal"
    db.write_bytes(b"database")
    empty_wal.write_bytes(b"")
    nonempty_wal.write_bytes(b"wal frames")

    snapshot = _snapshot(tmp_path)

    assert snapshot == {"missions.db": b"database", "delegations.db-wal": b"wal frames"}


def test_building_the_summary_writes_nothing(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _start_record(root)
    _tick(root)
    run = autopilot._read_run(MID, root)
    before = _snapshot(root)
    for _ in range(3):
        autopilot.build_summary(MID, run, root)
    assert _snapshot(root) == before and len(backend.calls) == 1


def test_a_failing_summary_degrades_but_status_still_answers(env, monkeypatch):
    root, _ = env
    _mk(root, [_node("a")])
    _start_record(root)

    def broken(*args, **kwargs):
        raise OSError("plan store unreadable")

    monkeypatch.setattr(plan, "hermes_plan_review", broken)
    payload = _status(root)
    assert payload["success"] is True and payload["found"] is True and payload["run"]["state"] == "running"
    assert payload["summary"] == {"available": False}


def test_extra_peers_do_not_change_the_summary_shape(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(autopilot.placement, "load_manifest_targets",
                        lambda hermes_root=None, **kw: [_peer("rza"), _peer("rzb")])
    _mk(root, [_node("a")])
    _start_record(root)
    _tick(root)
    assert set(_summary(root)) == {
        "available", "mission_status", "plan_version", "progress", "workers", "frontier", "budget", "recovery",
        "limits", "wake", "attention", "needs_owner"}
