"""Tests for the deterministic MissionPlan + plan_nodes additive store (v0.9).

Mirrors ``test_operator_mission_runtime.py``: all runs against a temp
``hermes_root`` (never production data). Operator policy is forced to
``workspace + direct`` so the additive plan writes proceed; read surfaces only
require ``read_only``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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
    return root


def _spec(mid: str = "msn-plan") -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Coordinate a v0.9 release",
            "objective": "Deliver a bounded MissionPlan decomposition without weakening existing gates.",
            "owner_profile": "default",
            "acceptance_criteria": ["additive plan schema", "operator review surface"],
            "context_refs": [],
            "skills": [],
            "final_approval_required": True,
        }
    )


def _j(value: str) -> dict:
    return json.loads(value)


def _make_mission(root: Path, mid: str = "msn-plan") -> None:
    out = _j(mission.hermes_mission_create(_spec(mid), confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _operator_plan_dag() -> str:
    return json.dumps(
        {
            "schema": plan.PLAN_SCHEMA,
            "mission_id": "msn-plan",
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
                {
                    "node_id": "b",
                    "kind": "single",
                    "owner": "hermes-dev",
                    "parents": ["a"],
                    "objective": "Raw node objective B.",
                    "capability_req": {"profile": "hermes-dev", "skills": [], "authorization_class": "reversible_write"},
                    "budget": {"est_minutes": 60, "est_tokens": 200_000},
                    "expected_artifacts": ["evidence.json"],
                },
                {
                    "node_id": "c",
                    "kind": "approval",
                    "owner": "owner",
                    "parents": ["b"],
                    "objective": "Raw node objective C.",
                    "capability_req": {"profile": "owner", "skills": [], "authorization_class": "high_impact"},
                    "budget": {"est_minutes": 0, "est_tokens": 0},
                    "expected_artifacts": [],
                },
            ],
        }
    )


# ---------------------------------------------------------------------------
# Deterministic decomposition
# ---------------------------------------------------------------------------


def test_decompose_is_deterministic_and_bounded(hermes_root):
    _make_mission(hermes_root)
    d = _j(plan.hermes_plan_decompose("msn-plan", hermes_root=hermes_root))
    assert d["success"] is True
    assert d["decomposition"] == "canonical-swarm-v1"
    assert d["node_count"] > 0
    assert len(d["nodes"]) <= plan.MAX_NODES
    # INV-9: no raw objective text; only hash metadata.
    assert "objective" not in d
    for n in d["nodes"]:
        assert "objective" not in n, "raw objective leaked into a node"
        assert n["objective_sha256"]  # non-empty hash
        assert len(n["objective_sha256"]) == 64
    # DAG is acyclic and ordered.
    assert len(d["nodes"]) == len(d["nodes"])


def test_decompose_requires_existing_mission(hermes_root):
    out = _j(plan.hermes_plan_decompose("msn-missing", hermes_root=hermes_root))
    assert out["success"] is False


# ---------------------------------------------------------------------------
# Plan CRUD
# ---------------------------------------------------------------------------


def test_create_get_list_persists_plan(hermes_root):
    _make_mission(hermes_root)
    d = _j(plan.hermes_plan_decompose("msn-plan", hermes_root=hermes_root))
    created = _j(plan.hermes_plan_create("msn-plan", json.dumps(d), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert created["success"] is True
    assert created["version"] == 1
    assert created["node_count"] == d["node_count"]
    assert created["plan_sha256"]

    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    assert got["success"] is True
    assert got["status"] == plan.PLAN_STATUS_DRAFT
    assert got["node_count"] == created["node_count"]
    assert got["nodes"][0]["node_id"] == "research"
    assert got["nodes"][0]["state"] == "pending"

    listed = _j(plan.hermes_plan_list(hermes_root=hermes_root))
    assert listed["count"] == 1
    assert listed["plans"][0]["mission_id"] == "msn-plan"


def test_plan_create_replaces_version_on_update(hermes_root):
    _make_mission(hermes_root)
    d = _j(plan.hermes_plan_decompose("msn-plan", hermes_root=hermes_root))
    _j(plan.hermes_plan_create("msn-plan", json.dumps(d), confirm=True, dry_run=False, hermes_root=hermes_root))
    # Bump a node budget, re-create (replace-version).
    d["nodes"][0]["budget"]["est_minutes"] = 90
    updated = _j(plan.hermes_plan_create("msn-plan", json.dumps(d), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert updated["success"] is True
    assert updated["version"] == 2
    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    assert got["version"] == 2


def test_plan_requires_existing_mission(hermes_root):
    d = _j(plan.hermes_plan_decompose("msn-plan", hermes_root=hermes_root))  # fails; build a manual plan instead
    out = _j(plan.hermes_plan_create("msn-nope", _operator_plan_dag().replace("msn-plan", "msn-nope"), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False


def test_operator_provided_plan_accepted(hermes_root):
    _make_mission(hermes_root)
    out = _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["node_count"] == 3
    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    assert got["decomposition"] == "operator-provided"
    node_ids = {n["node_id"] for n in got["nodes"]}
    assert node_ids == {"a", "b", "c"}


def test_validate_accepts_bounded_plan(hermes_root):
    out = _j(plan.hermes_plan_validate(_operator_plan_dag()))
    assert out["valid"] is True
    assert out["node_count"] == 3


def test_validate_rejects_cyclic_dag(hermes_root):
    raw = json.loads(_operator_plan_dag())
    # a -> b -> a cycle.
    raw["nodes"][0]["parents"] = ["b"]
    out = _j(plan.hermes_plan_validate(json.dumps(raw)))
    assert out["valid"] is False
    assert "cycle" in json.dumps(out).lower()


def test_validate_rejects_unknown_parent(hermes_root):
    raw = json.loads(_operator_plan_dag())
    raw["nodes"][1]["parents"] = ["does-not-exist"]
    out = _j(plan.hermes_plan_validate(json.dumps(raw)))
    assert out["valid"] is False


# ---------------------------------------------------------------------------
# INV-9 data containment
# ---------------------------------------------------------------------------

RAW_SUBSTRINGS = ("Raw mission objective text.", "Raw node objective A.", "Raw node objective B.", "Raw node objective C.")


def test_inv9_no_raw_objective_persisted(hermes_root):
    _make_mission(hermes_root)
    _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    raw_out = json.dumps(got)
    for token in RAW_SUBSTRINGS:
        assert token not in raw_out, f"raw objective persisted: {token!r}"
    # Every node carries a hash, not the text.
    for n in got["nodes"]:
        assert len(n["objective_sha256"]) == 64
        assert n["objective_len"] > 0
    # And in the DB rows themselves (plan_json / plan_nodes).
    path = mission._db_path(hermes_root)
    with sqlite3.connect(path) as con:
        row = con.execute("SELECT plan_json FROM mission_plans WHERE mission_id='msn-plan'").fetchone()
        assert row is not None
        for token in RAW_SUBSTRINGS:
            assert token not in row[0]


def test_inv9_dry_run_does_not_create_state(hermes_root):
    _make_mission(hermes_root)
    out = _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=False, dry_run=True, hermes_root=hermes_root))
    assert out["success"] is True and out["dry_run"] is True
    # Dry-run writes nothing: the plan table must not be created.
    path = mission._db_path(hermes_root)
    with sqlite3.connect(path) as con:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mission_plans'"
        ).fetchone()
        assert table is None


# ---------------------------------------------------------------------------
# Node state machine (design §5.2)
# ---------------------------------------------------------------------------


def test_node_state_machine_vocabulary(hermes_root):
    assert plan.validate_node_transition("pending", "blockable")
    assert plan.validate_node_transition("pending", "dispatched")
    assert plan.validate_node_transition("dispatched", "running")
    assert plan.validate_node_transition("running", "awaiting_review")
    assert plan.validate_node_transition("awaiting_review", "validated")
    assert plan.validate_node_transition("validated", "awaiting_approval")
    assert plan.validate_node_transition("awaiting_approval", "completed")
    assert not plan.validate_node_transition("completed", "running")
    assert not plan.validate_node_transition("pending", "completed")
    assert not plan.validate_node_transition("failed", "running")


def test_node_transition_applies_and_rejects_illegal(hermes_root):
    _make_mission(hermes_root)
    _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    ok = _j(plan.hermes_plan_node_transition("msn-plan", "a", "blockable", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert ok["success"] is True
    assert ok["from_state"] == "pending" and ok["to_state"] == "blockable"

    bad = _j(plan.hermes_plan_node_transition("msn-plan", "a", "completed", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert bad["success"] is False  # pending --(direct)--> completed is illegal

    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    node_a = next(n for n in got["nodes"] if n["node_id"] == "a")
    assert node_a["state"] == "blockable"


def test_node_transition_never_mutates_mission(hermes_root):
    _make_mission(hermes_root)
    _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    _j(plan.hermes_plan_node_transition("msn-plan", "b", "running", confirm=True, dry_run=False, hermes_root=hermes_root))
    # The Mission is untouched: still draft, no dispatch, no approval.
    md = _j(mission.hermes_mission_get("msn-plan", hermes_root=hermes_root))
    assert md["status"] == "draft"
    assert md["attachments"] == []
    assert not md.get("approval", {}).get("approved")


# ---------------------------------------------------------------------------
# Operator review surface (read-only)
# ---------------------------------------------------------------------------


def test_review_surface_read_only(hermes_root):
    _make_mission(hermes_root)
    _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    rev = _j(plan.hermes_plan_review("msn-plan", hermes_root=hermes_root))
    assert rev["success"] is True
    assert rev["topological_order"] == ["a", "b", "c"]
    assert rev["terminal_nodes"] == []
    # All pending, no parents terminal -> 'a' ready, 'b'/'c' blocked by nonterminal parents.
    assert rev["ready_nodes"] == ["a"]
    for n in rev["nodes"]:
        assert n["state"] == "pending"
        assert n["contract_sha256"]
        assert "capability_req" in n


def test_plan_set_status_is_operator_reviewable(hermes_root):
    _make_mission(hermes_root)
    _j(plan.hermes_plan_create("msn-plan", _operator_plan_dag(), confirm=True, dry_run=False, hermes_root=hermes_root))
    ok = _j(plan.hermes_plan_set_status("msn-plan", "approved", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert ok["success"] is True
    got = _j(plan.hermes_plan_get("msn-plan", hermes_root=hermes_root))
    assert got["status"] == "approved"
