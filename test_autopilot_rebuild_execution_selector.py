"""Regression (2026-10-21): a never-invoked delegation whose plan node pins an
execution selector must still be RE-DRIVEN — the rebuilt contract has to
reproduce the stored lineage digest, including the fields the validation
manifest intentionally excludes.

Live shape (msn-m3-pi-dev-004, the first real dev campaign on the corrected
hermes_agent architecture): the node pinned
execution={backend: hermes_agent, options: {delegate: pi, sandbox:
workspace-write}}; the dispatch crashed ambiguously before invoking (item-252
shape); ``_rebuild_dispatch_contract`` restored only the objective pointer and
the manifest's OPTION-STRIPPED execution block, so the canonical digest never
matched the stored lineage sha and the worker held ``contract_unavailable``
for 8+ hours. The manifest is a prompt-free subset BY DESIGN (INV-9) — the
rebuild must reconstruct inputs/constraints and the node's normalized
execution selector exactly as ``_build_contract`` emitted them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_delegations as deleg
import operator_mission_runtime as mission
from test_operator_autopilot_scheduler import _j, _mk, _node, _tick, make_env

MID = "msn-rebuild-exec-selector"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _delegation_row(root: Path, mid: str, node_id: str) -> dict:
    import operator_mission_plan as plan
    review = _j(plan.hermes_plan_review(mid, hermes_root=root))
    node = next(n for n in review["nodes"] if n["node_id"] == node_id)
    key = autopilot.dispatch_key(mid, review["version"], node_id, 0, str(node.get("contract_sha256", "")))
    task_id = autopilot._task_id(mid, node_id, key)
    with deleg._connect(deleg._db_path(root), write=False) as db:
        row = db.execute("SELECT * FROM delegations WHERE task_id=?", (task_id,)).fetchone()
    assert row is not None, f"delegation for {node_id} must exist"
    return dict(row)


def test_rebuild_reproduces_stored_lineage_for_execution_pinned_nodes(env):
    root, backend = env
    pinned = _node("n1")
    pinned["execution"] = {"backend": "hermes_agent", "options": {"delegate": "pi", "sandbox": "workspace-write"}}
    _mk(root, [pinned], mid=MID)

    summary = _tick(root, mid=MID)
    assert summary["dispatched"] == ["n1"], summary

    row = _delegation_row(root, MID, "n1")
    review = _j(__import__("operator_mission_plan").hermes_plan_review(MID, hermes_root=root))
    node = next(n for n in review["nodes"] if n["node_id"] == "n1")
    rebuilt = autopilot._rebuild_dispatch_contract(MID, node, autopilot.dispatch_key(
        MID, review["version"], "n1", 0, str(node.get("contract_sha256", ""))), root)
    assert rebuilt is not None, (
        "rebuild must reproduce the stored lineage sha for execution-pinned nodes "
        "(old code: manifest's option-stripped execution block -> digest mismatch -> None)"
    )


def test_never_invoked_execution_pinned_delegation_is_redriven(env):
    root, backend = env
    pinned = _node("n1")
    pinned["execution"] = {"backend": "hermes_agent", "options": {"delegate": "pi", "sandbox": "workspace-write"}}
    _mk(root, [pinned], mid=MID)

    summary = _tick(root, mid=MID)
    assert summary["dispatched"] == ["n1"], summary
    assert len(backend.calls) == 1

    # Strand the delegation in the item-252 crash shape (ambiguous submission
    # died before invoking; STALE ambiguity — zero observations past grace).
    row = _delegation_row(root, MID, "n1")
    old = "2026-10-21T02:22:06.448300+00:00"
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

    # Old code: held contract_unavailable forever (the 8h msn-m3-pi-dev-004
    # wedge). Fixed code: the rebuilt contract reproduces the stored lineage
    # digest and the SAME delegation id is re-driven through L2.
    summary2 = _tick(root, mid=MID)
    assert len(backend.calls) == 2, (
        f"second tick must re-drive the execution-pinned never-invoked submission "
        f"(calls={len(backend.calls)}, summary={summary2})"
    )
    row2 = _delegation_row(root, MID, "n1")
    assert row2["dispatch_phase"] != "reserved", (
        f"re-driven row must have left the reserved phase (got {row2['dispatch_phase']!r})"
    )
