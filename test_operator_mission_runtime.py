from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _spec(mid: str = "msn-test") -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Implement v0.9",
            "objective": "Coordinate durable mission work without weakening existing gates.",
            "owner_profile": "default",
            "acceptance_criteria": ["all child work is observed", "final Owner approval"],
            "context_refs": [
                {
                    "kind": "document",
                    "ref": "docs:v0.9-plan",
                    "label": "v0.9 plan",
                    "sha256": "a" * 64,
                }
            ],
            "skills": [
                {"name": "compound-engineering", "version": "1", "ref": "skill:compound-engineering"}
            ],
            "final_approval_required": True,
        }
    )


def _j(value: str) -> dict:
    return json.loads(value)


def _owner(monkeypatch) -> None:
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)


def test_create_get_list_persists_bounded_manifests(hermes_root: Path):
    created = _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert created["success"] is True
    assert created["mission_id"] == "msn-test"
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["success"] is True
    assert got["status"] == "draft"
    assert got["context_refs"][0]["sha256"] == "a" * 64
    assert got["skills"][0]["name"] == "compound-engineering"
    listed = _j(mission.hermes_mission_list(hermes_root=hermes_root))
    assert listed["count"] == 1
    assert listed["missions"][0]["mission_id"] == "msn-test"


def test_schema_is_closed_and_dry_run_does_not_create_state(hermes_root: Path):
    bad = json.loads(_spec())
    bad["raw_context_body"] = "do not persist me"
    out = _j(mission.hermes_mission_create(json.dumps(bad), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False
    plan = _j(mission.hermes_mission_create(_spec("msn-plan"), confirm=False, dry_run=True, hermes_root=hermes_root))
    assert plan["success"] is True and plan["dry_run"] is True
    assert not (hermes_root / "missions" / "missions.db").exists()


def test_attach_and_workflow_reconcile_is_fail_closed(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    attached = _j(
        mission.hermes_mission_attach(
            "msn-test", "workflow", "sw-child", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert attached["success"] is True

    wf_dir = hermes_root / "swarm-workflows"
    wf_dir.mkdir()
    (wf_dir / "sw-child.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    running = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert running["status"] == "running"

    (wf_dir / "sw-child.json").write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
    blocked = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert blocked["status"] == "blocked"

    (wf_dir / "sw-child.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    done = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert done["status"] == "awaiting_approval"
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["attachments"][0]["state"] == "succeeded"


def test_owner_approval_is_required_for_final_completion(hermes_root: Path, monkeypatch):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "evidence", "ev-final", state="succeeded", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]

    denied = _j(mission.hermes_mission_transition("msn-test", "completed", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert denied["success"] is False

    _owner(monkeypatch)
    approved = _j(mission.hermes_mission_approve("msn-test", "approval:g7", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert approved["success"] is True
    assert approved["status"] == "completed"
    assert approved["approval"]["approved_by"] == "owner"


def test_approval_refuses_nonterminal_children(hermes_root: Path, monkeypatch):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-1", state="running", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    _owner(monkeypatch)
    denied = _j(mission.hermes_mission_approve("msn-test", "approval:nope", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert denied["success"] is False


def test_internal_attachment_state_bridge_updates_existing_only(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert mission.record_attachment_state("msn-test", "delegation", "dlg-missing", "running", hermes_root=hermes_root) is False
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-1", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert mission.record_attachment_state("msn-test", "delegation", "dlg-1", "succeeded", evidence_ref="evidence:1", hermes_root=hermes_root) is True
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["attachments"][0]["state"] == "succeeded"
    assert got["attachments"][0]["evidence_ref"] == "evidence:1"
