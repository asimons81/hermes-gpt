"""Tests for the AUTO-01 campaign → MissionPlan compiler.

All runs against a temp hermes_root (never production data); the compiler
itself is pure (no store), so most tests need no operator policy at all —
the compiled plan is round-tripped through ``hermes_plan_validate`` and a
real ``hermes_plan_create`` in the final tests to prove the plan is accepted
verbatim by the existing additive plan store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_campaign_compiler as compiler
import operator_mission_plan as plan_store
import operator_mission_runtime as mission
import operator_policy as op


def _campaign(**overrides) -> dict:
    base = {
        "schema": compiler.CAMPAIGN_SCHEMA,
        "campaign_id": "native-rails-migration",
        "mission_id": "msn-native-rails",
        "objective": "Migrate execution to native rails",
        "tracks": [
            {
                "track": "a",
                "profile": "hermes-dev",
                "tasks": [
                    {
                        "task": "OPS-01",
                        "evidence": ["sustained"],
                    }
                ],
            },
            {
                "track": "b",
                "profile": "hermes-dev",
                "tasks": [
                    {
                        "task": "AUTO-01",
                        "evidence": ["implementation", "ci", "merge"],
                        "depends_on": [],
                    }
                ],
            },
        ],
    }
    base.update(overrides)
    return base


def _compile(doc: dict) -> dict:
    return compiler.compile_campaign(json.dumps(doc))


# ---------------------------------------------------------------------------
# Schema enforcement (fails closed)
# ---------------------------------------------------------------------------


def test_rejects_wrong_schema() -> None:
    doc = _campaign(schema="hermes.campaign/v2")
    with pytest.raises(compiler.CampaignSpecError, match="schema"):
        _compile(doc)


def test_rejects_invalid_mission_id() -> None:
    with pytest.raises(compiler.CampaignSpecError, match="mission_id"):
        _compile(_campaign(mission_id="NOT VALID"))


def test_rejects_unknown_evidence_type() -> None:
    doc = _campaign()
    doc["tracks"][0]["tasks"][0]["evidence"] = ["vibes"]
    with pytest.raises(compiler.CampaignSpecError, match="evidence type"):
        _compile(doc)


def test_rejects_approval_as_task_kind() -> None:
    doc = _campaign()
    doc["tracks"][0]["tasks"][0]["kind"] = "approval"
    with pytest.raises(compiler.CampaignSpecError, match="campaign-level"):
        _compile(doc)


def test_rejects_unknown_dependency() -> None:
    doc = _campaign()
    doc["tracks"][1]["tasks"][0]["depends_on"] = ["NOPE-99"]
    with pytest.raises(compiler.CampaignSpecError, match="unknown task"):
        _compile(doc)


def test_rejects_duplicate_task_ids_across_tracks() -> None:
    doc = _campaign()
    dup = dict(doc["tracks"][0]["tasks"][0])
    doc["tracks"][1]["tasks"].append(dup)
    with pytest.raises(compiler.CampaignSpecError, match="duplicate"):
        _compile(doc)


def test_rejects_task_budget_out_of_range() -> None:
    doc = _campaign()
    doc["tracks"][0]["tasks"][0]["budget"] = {"est_minutes": -5}
    with pytest.raises(compiler.CampaignSpecError, match="est_minutes"):
        _compile(doc)


def test_fails_closed_on_node_cap_instead_of_truncating() -> None:
    tasks = [
        {"task": f"T-{i:02d}", "evidence": ["runtime"]} for i in range(24)
    ]
    overflow = [
        {"task": f"T-{i:02d}", "evidence": ["runtime"]} for i in range(24, 30)
    ]
    doc = _campaign(
        tracks=[{"track": "a", "profile": "hermes-dev", "tasks": tasks + overflow}]
    )
    with pytest.raises(compiler.CampaignSpecError, match="tasks"):
        _compile(doc)


# ---------------------------------------------------------------------------
# Deterministic compilation
# ---------------------------------------------------------------------------


def test_compiles_canonical_dag_shape() -> None:
    plan = _compile(_campaign())
    assert plan["schema"] == plan_store.PLAN_SCHEMA
    assert plan["decomposition"] == compiler.DECOMPOSITION_ID
    ids = [n["node_id"] for n in plan["nodes"]]
    # task nodes + per-track validators + campaign validator
    assert ids == [
        "a-ops-01",
        "a-validate",
        "b-auto-01",
        "b-validate",
        "campaign-validate",
    ]
    # track validators parent on their track's task nodes
    by_id = {n["node_id"]: n for n in plan["nodes"]}
    assert by_id["a-validate"]["parents"] == ["a-ops-01"]
    assert by_id["campaign-validate"]["parents"] == ["a-validate", "b-validate"]
    assert by_id["a-ops-01"]["parents"] == []
    # kinds
    assert by_id["a-validate"]["kind"] == "single"


def test_typed_evidence_recorded_as_receipt_artifacts() -> None:
    plan = _compile(_campaign())
    by_id = {n["node_id"]: n for n in plan["nodes"]}
    # task node: one receipt artifact per required evidence class
    assert by_id["a-ops-01"]["expected_artifacts"] == ["evidence-sustained.json"]
    # track validator unions the track's evidence classes
    assert by_id["b-validate"]["expected_artifacts"] == [
        "evidence-ci.json",
        "evidence-implementation.json",
        "evidence-merge.json",
    ]
    # campaign validator is a plain gate
    assert by_id["campaign-validate"]["expected_artifacts"] == ["evidence.json"]


def test_campaign_level_approval_node() -> None:
    plan = _compile(_campaign(approval=True))
    by_id = {n["node_id"]: n for n in plan["nodes"]}
    approval = by_id["campaign-approval"]
    assert approval["kind"] == "approval"
    assert approval["owner"] == "owner"
    assert approval["parents"] == ["campaign-validate"]
    assert approval["expected_artifacts"] == []
    assert approval["capability_req"]["authorization_class"] == "high_impact"


def test_cross_track_dependency_resolves() -> None:
    doc = _campaign()
    doc["tracks"][1]["tasks"][0]["depends_on"] = ["OPS-01"]
    plan = _compile(doc)
    by_id = {n["node_id"]: n for n in plan["nodes"]}
    assert by_id["b-auto-01"]["parents"] == ["a-ops-01"]


def test_deterministic_same_input_same_sha() -> None:
    a = _compile(_campaign())
    b = _compile(_campaign())
    assert a["plan_sha256"] == b["plan_sha256"]
    assert a == b


def test_no_raw_objective_in_output_inv9() -> None:
    plan = _compile(_campaign(objective="secret-ish objective text"))
    assert "objective" not in plan
    dumped = json.dumps(plan)
    assert "secret-ish objective text" not in dumped
    assert plan["objective_sha256"]


def test_node_count_and_sha_populated() -> None:
    plan = _compile(_campaign())
    assert plan["node_count"] == len(plan["nodes"]) == 5
    assert len(plan["plan_sha256"]) == 64


# ---------------------------------------------------------------------------
# Verifiable contract output: the plan store accepts the compiled plan
# ---------------------------------------------------------------------------


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


def _seed_mission(root: Path, mid: str = "msn-native-rails") -> None:
    spec = json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Native rails migration",
            "objective": "Migrate execution to native rails",
            "owner_profile": "default",
        }
    )
    mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=root)


def test_compiled_plan_validates_against_plan_store(hermes_root: Path) -> None:
    plan = _compile(_campaign())
    result = plan_store.hermes_plan_validate(
        json.dumps(plan), hermes_root=hermes_root
    )
    assert '"valid": true' in result or '"valid":true' in result


def test_compiled_plan_is_accepted_verbatim_by_plan_create(hermes_root: Path) -> None:
    _seed_mission(hermes_root)
    plan = _compile(_campaign())
    result = plan_store.hermes_plan_create(
        "msn-native-rails",
        json.dumps(plan),
        confirm=True,
        dry_run=False,
        status="review",
        hermes_root=hermes_root,
    )
    assert "msn-native-rails" in result
    stored = json.loads(
        plan_store.hermes_plan_get("msn-native-rails", hermes_root=hermes_root)
    )
    assert stored["decomposition"] == compiler.DECOMPOSITION_ID
    assert stored["node_count"] == 5
    assert stored["plan_sha256"] == plan["plan_sha256"]
    node_ids = {n["node_id"] for n in stored["nodes"]}
    assert "campaign-validate" in node_ids
    assert all(n["state"] in ("pending", "blockable") for n in stored["nodes"])
