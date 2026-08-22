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
    assert _j(mission.hermes_mission_attach("msn-test", "evidence", "ev-final", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert mission.record_attachment_state(
        "msn-test",
        "evidence",
        "ev-final",
        "succeeded",
        evidence_ref="evidence:verified-final",
        verified=True,
        hermes_root=hermes_root,
    )
    reconciled = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert reconciled["status"] == "awaiting_approval"

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
    assert mission.record_attachment_state("msn-test", "delegation", "dlg-1", "succeeded", evidence_ref="evidence:1", verified=True, hermes_root=hermes_root) is True
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["attachments"][0]["state"] == "succeeded"
    assert got["attachments"][0]["evidence_ref"] == "evidence:1"


def test_workspace_cannot_weaken_final_approval_boundary(hermes_root: Path):
    raw = json.loads(_spec("msn-no-approval"))
    raw["final_approval_required"] = False
    denied_create = _j(
        mission.hermes_mission_create(
            json.dumps(raw),
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert denied_create["success"] is False

    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    denied_update = _j(
        mission.hermes_mission_update(
            "msn-test",
            json.dumps({"final_approval_required": False}),
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert denied_update["success"] is False
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["final_approval_required"] is True


def test_owner_can_explicitly_create_no_final_approval_mission(hermes_root: Path, monkeypatch):
    _owner(monkeypatch)
    raw = json.loads(_spec("msn-owner-optout"))
    raw["final_approval_required"] = False
    created = _j(mission.hermes_mission_create(json.dumps(raw), confirm=True, dry_run=False, hermes_root=hermes_root))
    assert created["success"] is True


def test_workflow_attachment_rejects_traversal_and_noncanonical_ids(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    bad = _j(
        mission.hermes_mission_attach(
            "msn-test",
            "workflow",
            "sw-child/../../escape",
            state="pending",
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert bad["success"] is False
    assert mission._workflow_state(hermes_root, "sw-child/../../escape") == "unknown"


def test_public_attach_cannot_assert_success_and_bridge_requires_verification(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    denied = _j(
        mission.hermes_mission_attach(
            "msn-test",
            "delegation",
            "dlg-verified",
            state="succeeded",
            evidence_ref="evidence:claimed",
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert denied["success"] is False
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-verified", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert mission.record_attachment_state(
        "msn-test",
        "delegation",
        "dlg-verified",
        "succeeded",
        evidence_ref="evidence:not-enough",
        verified=False,
        hermes_root=hermes_root,
    ) is False
    assert mission.record_attachment_state(
        "msn-test",
        "delegation",
        "dlg-verified",
        "succeeded",
        evidence_ref="evidence:verified",
        verified=True,
        hermes_root=hermes_root,
    ) is True
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    attached = next(item for item in got["attachments"] if item["ref"] == "dlg-verified")
    assert attached["state"] == "succeeded"
    assert bool(attached["verified"]) is True


def test_reconcile_fails_closed_on_unverified_legacy_success(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-legacy", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    with mission._connect(mission._db_path(hermes_root), write=True) as db:
        db.execute(
            "UPDATE attachments SET state='succeeded',verified=0,evidence_ref='legacy:claim' WHERE mission_id=? AND kind='delegation' AND ref='dlg-legacy'",
            ("msn-test",),
        )
        db.commit()
    out = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["status"] == "blocked"
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    attached = next(item for item in got["attachments"] if item["ref"] == "dlg-legacy")
    assert attached["state"] == "blocked"
    assert bool(attached["verified"]) is False


def test_approval_requires_awaiting_approval_and_verified_success(hermes_root: Path, monkeypatch):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-failed", state="failed", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    _owner(monkeypatch)
    denied = _j(mission.hermes_mission_approve("msn-test", "approval:bad", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert denied["success"] is False


@pytest.mark.parametrize("regression", ["blocked", "missing"])
def test_approval_reobserves_child_evidence_and_fails_closed(hermes_root: Path, monkeypatch, regression: str):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "workflow", "sw-child", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    wf_dir = hermes_root / "swarm-workflows"
    wf_dir.mkdir()
    wf_path = wf_dir / "sw-child.json"
    wf_path.write_text(json.dumps({"status": "done"}), encoding="utf-8")
    reconciled = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert reconciled["status"] == "awaiting_approval"
    if regression == "blocked":
        wf_path.write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
    else:
        wf_path.unlink()
    _owner(monkeypatch)
    denied = _j(mission.hermes_mission_approve("msn-test", "approval:stale", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert denied["success"] is False
    got = _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))
    assert got["status"] == "awaiting_approval"
    assert not got["approval"].get("approved")


def test_noop_reconcile_does_not_append_events(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "workflow", "sw-child", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    wf_dir = hermes_root / "swarm-workflows"
    wf_dir.mkdir()
    (wf_dir / "sw-child.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
    first = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert first["changed"] is True
    before = len(_j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))["events"])
    second = _j(mission.hermes_mission_reconcile("msn-test", confirm=True, dry_run=False, hermes_root=hermes_root))
    after = len(_j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))["events"])
    assert second["changed"] is False
    assert after == before


def test_acceptance_and_owner_profile_freeze_after_work_attached(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert _j(mission.hermes_mission_attach("msn-test", "delegation", "dlg-1", state="pending", confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    denied = _j(
        mission.hermes_mission_update(
            "msn-test",
            json.dumps({"acceptance_criteria": ["weakened"]}),
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert denied["success"] is False


@pytest.mark.parametrize("kind", sorted(mission.ATTACHMENT_KINDS))
def test_public_attach_cannot_claim_cancellation_for_any_kind(hermes_root: Path, kind: str):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    ref = "sw-cancelled" if kind == "workflow" else "cancelled-ref"
    out = _j(mission.hermes_mission_attach("msn-test", kind, ref, state="cancelled", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert not out["success"]


def test_parent_cancellation_blocked_by_reserved_delegation(hermes_root: Path):
    assert _j(mission.hermes_mission_create(_spec(), confirm=True, dry_run=False, hermes_root=hermes_root))["success"]
    assert mission.reserve_delegation_attachment("msn-test", "dlg-reserved", evidence_ref="contract:" + "a" * 64, hermes_root=hermes_root)
    out = _j(mission.hermes_mission_transition("msn-test", "cancelled", confirm=True, dry_run=False, hermes_root=hermes_root))
    assert not out["success"]
    assert _j(mission.hermes_mission_get("msn-test", hermes_root=hermes_root))["status"] != "cancelled"
