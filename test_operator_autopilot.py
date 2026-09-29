"""Tests for the durable Autopilot runtime (v0.13 PR1).

Mirrors ``test_operator_mission_plan.py``: operator policy is forced to
``workspace + direct`` so writes proceed; the AUTOPILOT_ENV machine gate is
set per-test so the default-off behavior is exercised explicitly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_job_supervisor as job_supervisor
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_policy as op


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV, raising=False)
    return root


def _j(value: str) -> dict:
    return json.loads(value)


def _spec(mid: str) -> str:
    return json.dumps({
        "schema": mission.MISSION_SPEC_SCHEMA,
        "mission_id": mid,
        "title": "Autopilot test mission",
        "objective": "Exercise the durable Autopilot runtime without weakening existing gates.",
        "owner_profile": "default",
        "acceptance_criteria": ["autopilot runtime skeleton"],
        "context_refs": [],
        "skills": [],
        "final_approval_required": True,
    })


def _make_mission(root: Path, mid: str) -> None:
    out = _j(mission.hermes_mission_create(_spec(mid), confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _plan_dag(mid: str) -> str:
    return json.dumps({
        "schema": plan.PLAN_SCHEMA,
        "mission_id": mid,
        "version": 1,
        "decomposition": "operator-provided",
        "objective": "Raw mission objective text.",
        "nodes": [
            {
                "node_id": "a",
                "kind": "single",
                "owner": "hermes-researcher",
                "parents": [],
                "objective": "Raw node objective A.",
                "capability_req": {"profile": "hermes-researcher", "skills": [], "authorization_class": "reversible_write"},
                "budget": {"est_minutes": 30, "est_tokens": 50_000},
                "expected_artifacts": ["work-contract.json"],
            },
        ],
    })


def _make_plan(root: Path, mid: str) -> None:
    created = _j(plan.hermes_plan_create(mid, _plan_dag(mid), confirm=True, dry_run=False, hermes_root=root))
    assert created["success"] is True, created


def _wait_for_state(root: Path, mid: str, states: set[str], *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _j(autopilot.hermes_autopilot_status(mid, hermes_root=root))
        if last.get("found") and last["run"].get("state") in states:
            return last
        time.sleep(0.1)
    raise AssertionError(f"never reached {states}, last={last}")


def _wait_for_worker_running(root: Path, mid: str, *, timeout: float = 5.0) -> dict:
    """Poll until the worker has self-registered its verified process identity.

    The worker records its own identity as the first thing it does (see
    operator_autopilot._worker) precisely to avoid a race reading /proc this
    soon after spawn; until then job_supervisor truthfully reports "queued".
    """
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        status = _j(autopilot.hermes_autopilot_status(mid, hermes_root=root))
        if status.get("found") and status["worker"].get("status") == "running":
            return status
        time.sleep(0.05)
    raise AssertionError(f"worker never reached running, last={status}")


# ---------------------------------------------------------------------------
# Validation / gating (no subprocess spawned)
# ---------------------------------------------------------------------------


def test_start_dry_run_previews_without_writing(hermes_root):
    _make_mission(hermes_root, "msn-ap-dry")
    _make_plan(hermes_root, "msn-ap-dry")
    out = _j(autopilot.hermes_autopilot_start("msn-ap-dry", hermes_root=hermes_root))
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["would_start"] is True
    assert autopilot._read_run("msn-ap-dry", hermes_root) is None


def test_start_direct_requires_confirm(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    _make_mission(hermes_root, "msn-ap-confirm")
    _make_plan(hermes_root, "msn-ap-confirm")
    out = _j(autopilot.hermes_autopilot_start("msn-ap-confirm", confirm=False, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False
    assert autopilot._read_run("msn-ap-confirm", hermes_root) is None


def test_start_direct_requires_machine_gate(hermes_root):
    _make_mission(hermes_root, "msn-ap-gate")
    _make_plan(hermes_root, "msn-ap-gate")
    out = _j(autopilot.hermes_autopilot_start("msn-ap-gate", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False
    assert autopilot.AUTOPILOT_ENV in json.dumps(out)
    assert autopilot._read_run("msn-ap-gate", hermes_root) is None


def test_start_rejects_missing_mission(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    out = _j(autopilot.hermes_autopilot_start("msn-does-not-exist", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False


def test_start_rejects_terminal_mission(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    _make_mission(hermes_root, "msn-ap-terminal")
    cancelled = _j(mission.hermes_mission_transition("msn-ap-terminal", "cancelled", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert cancelled["success"] is True, cancelled
    out = _j(autopilot.hermes_autopilot_start("msn-ap-terminal", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False


def test_start_rejects_missing_plan(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    _make_mission(hermes_root, "msn-ap-noplan")
    out = _j(autopilot.hermes_autopilot_start("msn-ap-noplan", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False


def test_status_reports_not_found_for_unknown_mission(hermes_root):
    out = _j(autopilot.hermes_autopilot_status("msn-never-started", hermes_root=hermes_root))
    assert out["success"] is True
    assert out["found"] is False


def test_stop_is_a_noop_when_nothing_is_running(hermes_root):
    out = _j(autopilot.hermes_autopilot_stop("msn-never-started", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["changed"] is False


# ---------------------------------------------------------------------------
# Real detached worker (subprocess) — the PR1 exit gate
# ---------------------------------------------------------------------------


def test_start_spawns_a_durable_worker_that_survives_and_reconciles(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    mid = "msn-ap-worker"
    _make_mission(hermes_root, mid)
    _make_plan(hermes_root, mid)

    started = _j(autopilot.hermes_autopilot_start(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert started["success"] is True, started
    assert started["run"]["state"] == "running"
    job_id = started["job_id"]

    # A concurrent start call is idempotent: it must not spawn a second worker.
    again = _j(autopilot.hermes_autopilot_start(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert again["success"] is True
    assert again["idempotent"] is True

    status = _wait_for_worker_running(hermes_root, mid)
    assert status["worker"]["process_verification"] == "verified"

    # Mission reaches terminal state; the worker must observe this on its own
    # (no hermes_autopilot_stop call here) and terminalize itself cleanly.
    cancelled = _j(mission.hermes_mission_transition(mid, "cancelled", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert cancelled["success"] is True, cancelled

    final = _wait_for_state(hermes_root, mid, {"completed"})
    assert final["run"]["state"] == "completed"

    job = job_supervisor.get_job(job_id, hermes_root=hermes_root, reconcile=False)
    assert job is not None
    assert job["status"] == "completed"


def test_stop_cancels_the_worker_and_state_survives_a_fresh_status_read(hermes_root, monkeypatch):
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")
    mid = "msn-ap-stop"
    _make_mission(hermes_root, mid)
    _make_plan(hermes_root, mid)

    started = _j(autopilot.hermes_autopilot_start(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert started["success"] is True, started
    _wait_for_worker_running(hermes_root, mid)

    stopped = _j(autopilot.hermes_autopilot_stop(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert stopped["success"] is True, stopped
    assert stopped["changed"] is True
    assert stopped["state"] == "stopped"

    # Simulates "reconnect later": a brand new status read must still be
    # truthful without any in-memory state from the call above.
    status = _j(autopilot.hermes_autopilot_status(mid, hermes_root=hermes_root))
    assert status["run"]["state"] == "stopped"

    restarted = _j(autopilot.hermes_autopilot_start(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert restarted["success"] is True
    assert not restarted.get("idempotent")
    assert restarted["run"]["state"] == "running"

    # Clean up the second worker so the test process doesn't leak a child.
    _wait_for_worker_running(hermes_root, mid)
    cleanup = _j(autopilot.hermes_autopilot_stop(mid, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert cleanup["success"] is True, cleanup
