"""Item 252 worker-side regression: a dispatched node whose delegation never
reached a backend (ambiguous dispatch crash-window, zero observations) must be
RE-DRIVEN by the scheduler, not held forever; and an all-terminal-not-all-
completed mission must reconcile its record instead of idling forever.

Live shape (2026-10-11): promote-verify missions msn-promote-verify-4bfba8f2-1
(9h) and msn-promote-verify-f633e95d (4h) both wedged with
observed_held={verify-convergence: no_delegation} while their delegation rows
sat reconciling/reserved with zero backend observations — `_advance_one`
gated on `_reached_backend` BEFORE the reconcile call, so the store-side
stale-invocation recovery was unreachable from the worker and a 'dispatched'
node is never a dispatch candidate again.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_mission_runtime as mission
from test_operator_autopilot_scheduler import _j, _mk, _node, _states, _tick, make_env

MID = "msn-item252-redrive"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _delegation_for(root: Path, mid: str, node_id: str) -> dict:
    review = _j(plan.hermes_plan_review(mid, hermes_root=root))
    node = next(n for n in review["nodes"] if n["node_id"] == node_id)
    key = autopilot.dispatch_key(mid, review["version"], node_id, 0, str(node.get("contract_sha256", "")))
    task_id = autopilot._task_id(mid, node_id, key)
    with deleg._connect(deleg._db_path(root), write=False) as db:
        row = db.execute("SELECT * FROM delegations WHERE task_id=?", (task_id,)).fetchone()
    assert row is not None, f"delegation for {node_id} must exist"
    return dict(row)


def test_never_invoked_delegation_is_redriven(env):
    root, backend = env
    _mk(root, [_node("n1")], mid=MID)

    summary = _tick(root, mid=MID)
    assert summary["dispatched"] == ["n1"], summary
    assert len(backend.calls) == 1

    # Strand the delegation in the item-252 crash shape: ambiguous submission
    # died before invoking — reconciling/reserved, zero observations, and the
    # attachment evidence_ref poisoned to the legacy format (live shape: the
    # dispatched/ambiguous event was HOURS old, i.e. STALE ambiguity).
    row = _delegation_for(root, MID, "n1")
    old = "2026-10-11T06:32:18.000000+00:00"
    with deleg._connect(deleg._db_path(root), write=True) as db:
        db.execute(
            "UPDATE delegations SET state='reconciling', dispatch_phase='reserved', "
            "backend_state='unobserved', backend_ref_json='{}', cancel_requested=0, "
            "cancellation_in_progress=0 WHERE delegation_id=?",
            (row["delegation_id"],),
        )
        db.execute(
            "UPDATE delegation_events SET created_at=? WHERE delegation_id=? "
            "AND event_type IN ('delegation.dispatched','delegation.dispatch_ambiguous')",
            (old, row["delegation_id"]),
        )
        db.commit()
    with mission._connect(mission._db_path(root), write=True) as db:
        db.execute(
            "UPDATE attachments SET evidence_ref=? WHERE mission_id=? AND kind='delegation' AND ref=?",
            (f"delegation:{row['delegation_id']}", MID, row["delegation_id"]),
        )
        db.commit()

    # Old code (base 781e963): tick 2 holds no_delegation forever — proven
    # both-ways. New code: reconcile recovers to pure reserved, the SAME
    # delegation id is re-driven through the L2 surface.
    summary2 = _tick(root, mid=MID)
    assert len(backend.calls) == 2, (
        f"second tick must re-drive the never-invoked submission (calls={len(backend.calls)}, summary={summary2})"
    )
    row2 = _delegation_for(root, MID, "n1")
    assert row2["dispatch_phase"] != "reserved", (
        f"re-driven row must have left the reserved phase (got {row2['dispatch_phase']!r})"
    )


def test_failed_only_mission_reconciles_record(env, monkeypatch):
    """Item 252 worker defect 2: nodes all terminal but NOT all completed —
    the mission record must reconcile (derive failed) instead of the worker
    idling forever (live: msn-promote-verify-f633e95d stayed draft 4h)."""
    root, backend = env
    _mk(root, [_node("n1")], mid=MID)
    summary = _tick(root, mid=MID)
    assert summary["dispatched"] == ["n1"], summary

    # Make the dispatched delegation FAILED via an observed failed backend
    # record (the realistic worker-fold shape): dispatch contract tasks are
    # observed through the fleet/kannban/runner stores; seed one.
    row = _delegation_for(root, MID, "n1")
    task_id = row["task_id"]
    runs_path = root / "runner-jobs"
    runs_path.mkdir(parents=True, exist_ok=True)
    (runs_path / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id, "status": "failed", "state": "failed",
        "outcome": "failed", "error": "simulated backend failure (item 252)",
        "started_at": "2026-10-11T00:00:00Z", "ended_at": "2026-10-11T00:01:00Z",
    }))
    monkeypatch.setattr(
        "operator_runners.observed_runs",
        lambda tid, hermes_root=None: ([json.loads((runs_path / f"{tid}.json").read_text())]
                                       if (runs_path / f"{tid}.json").is_file() else []),
    )

    _tick(root, mid=MID)
    assert _states(root, mid=MID)["n1"] == "failed", _states(root, mid=MID)
    got = _j(mission.hermes_mission_get(MID, hermes_root=root))
    status = (got.get("mission") or got).get("status")
    assert status == "failed", (
        f"all-terminal-not-all-completed mission must reconcile its record (got {status!r})"
    )
