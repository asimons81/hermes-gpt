"""Tests for the AUTO-04 owner-approval work-class policy table.

Pure policy-table tests run against plain node dicts; resolver/surface
tests run against the REAL mission store through the mission_runtime
primitives (the module itself is a caller — no direct sqlite in product
code, so tests read spec_json back the same way
``test_mission_approval_policy`` does). The controller integration tests
mirror the ``test_operator_campaign_controller`` harness.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_campaign_compiler as compiler
import operator_campaign_controller as ctrl
import operator_mission_plan as plan_store
import operator_mission_runtime as mr
import operator_policy as op
import operator_work_class as wc


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)


def _node(kind: str = "single", auth: str = "reversible_write") -> dict:
    return {
        "node_id": "n1",
        "kind": kind,
        "parents": [],
        "capability_req": {"profile": "default", "skills": [], "authorization_class": auth},
    }


def _create(root: Path, mid: str, **extra) -> dict:
    return json.loads(mr.hermes_mission_create(
        json.dumps({"mission_id": mid, "title": "T", "objective": "O", **extra}),
        confirm=True, dry_run=False, hermes_root=root))


def _verified_awaiting(root: Path, mid: str, **extra) -> None:
    """Real-store setup: mission + one verified evidence child + reconcile."""
    assert _create(root, mid, **extra)["success"] is True
    assert json.loads(mr.hermes_mission_attach(
        mid, "evidence", "ev-final", state="pending", confirm=True, dry_run=False,
        hermes_root=root))["success"] is True
    assert mr.record_attachment_state(
        mid, "evidence", "ev-final", "succeeded",
        evidence_ref="evidence:verified-final", verified=True, hermes_root=root,
    )
    out = json.loads(mr.hermes_mission_reconcile(mid, confirm=True, dry_run=False, hermes_root=root))
    assert out["status"] == "awaiting_approval"


def _status(root: Path, mid: str) -> tuple[str, int]:
    got = json.loads(mr.hermes_mission_get(mid, hermes_root=root))
    return str(got["status"]), int(got["version"])


AUTO = {"policy": "auto_when_evidence_verified", "max_wait_seconds": 900}
OWNER = {"policy": "owner"}


# ---------------------------------------------------------------------------
# classify_plan_node (fail closed)
# ---------------------------------------------------------------------------

def test_classify_approval_node_is_owner_gated() -> None:
    out = wc.classify_plan_node(_node(kind=plan_store.KIND_APPROVAL, auth="high_impact"))
    assert out == {"work_class": "owner_gated", "auth_class": "high_impact",
                   "preauthorized_eligible": False, "reason": "approval node"}


def test_classify_high_impact_is_owner_gated() -> None:
    out = wc.classify_plan_node(_node(auth="high_impact"))
    assert out["work_class"] == "owner_gated"
    assert out["preauthorized_eligible"] is False
    assert "high_impact" in out["reason"]


def test_classify_read_only_is_routine() -> None:
    out = wc.classify_plan_node(_node(auth="read_only"))
    assert out == {"work_class": "routine", "auth_class": "read_only",
                   "preauthorized_eligible": True, "reason": "routine work class"}


def test_classify_reversible_write_is_routine() -> None:
    out = wc.classify_plan_node(_node(auth="reversible_write"))
    assert out["work_class"] == "routine"
    assert out["preauthorized_eligible"] is True


def test_classify_missing_auth_is_owner_gated() -> None:
    node = _node()
    node["capability_req"] = {"profile": "default", "skills": []}
    out = wc.classify_plan_node(node)
    assert out["work_class"] == "owner_gated"
    assert out["reason"] == "missing authorization_class"


def test_classify_non_dict_is_owner_gated() -> None:
    out = wc.classify_plan_node(None)
    assert out == {"work_class": "owner_gated", "auth_class": "",
                   "preauthorized_eligible": False, "reason": "malformed node"}


# ---------------------------------------------------------------------------
# mission_approval_policy_for_nodes
# ---------------------------------------------------------------------------

def test_policy_for_nodes_empty_is_owner() -> None:
    assert wc.mission_approval_policy_for_nodes([]) == OWNER


def test_policy_for_nodes_all_routine_is_auto() -> None:
    nodes = [_node(auth="read_only"), _node(auth="reversible_write")]
    assert wc.mission_approval_policy_for_nodes(nodes) == AUTO


def test_policy_for_nodes_with_high_impact_is_owner() -> None:
    nodes = [_node(auth="read_only"), _node(auth="high_impact")]
    assert wc.mission_approval_policy_for_nodes(nodes) == OWNER


def test_policy_for_nodes_with_approval_is_owner() -> None:
    nodes = [_node(), _node(kind=plan_store.KIND_APPROVAL, auth="high_impact")]
    assert wc.mission_approval_policy_for_nodes(nodes) == OWNER


def test_policy_for_nodes_malformed_entry_is_owner() -> None:
    assert wc.mission_approval_policy_for_nodes([_node(), None]) == OWNER


# ---------------------------------------------------------------------------
# campaign_preauthorized
# ---------------------------------------------------------------------------

def _campaign_doc(**extra) -> str:
    doc = {
        "schema": compiler.CAMPAIGN_SCHEMA,
        "campaign_id": "camp-test",
        "mission_id": "msn-wc",
        "objective": "o",
        "tracks": [{"track": "a", "profile": "hermes-dev",
                    "tasks": [{"task": "T1", "evidence": ["implementation"]}]}],
    }
    doc.update(extra)
    return json.dumps(doc)


def test_preauthorized_absent_is_false() -> None:
    assert wc.campaign_preauthorized(_campaign_doc()) is False


def test_preauthorized_false_is_false() -> None:
    assert wc.campaign_preauthorized(_campaign_doc(preauthorized=False)) is False


def test_preauthorized_true_is_true() -> None:
    assert wc.campaign_preauthorized(_campaign_doc(preauthorized=True)) is True


def test_preauthorized_non_bool_raises() -> None:
    with pytest.raises(wc.WorkClassError):
        wc.campaign_preauthorized(_campaign_doc(preauthorized="yes"))


def test_preauthorized_invalid_json_raises() -> None:
    with pytest.raises(wc.WorkClassError):
        wc.campaign_preauthorized("{not json")


def test_work_class_dict_unknown_key_raises() -> None:
    with pytest.raises(wc.WorkClassError):
        wc.campaign_preauthorized(_campaign_doc(work_class={"preauthorized": True, "who": "me"}))


def test_work_class_dict_preauthorized_true_is_true() -> None:
    assert wc.campaign_preauthorized(_campaign_doc(work_class={"preauthorized": True})) is True


# ---------------------------------------------------------------------------
# effective_approval_policy (the conjunction)
# ---------------------------------------------------------------------------

def test_effective_no_declaration_routine_is_owner() -> None:
    assert wc.effective_approval_policy(_campaign_doc(), [_node()]) == OWNER


def test_effective_declaration_routine_is_auto() -> None:
    assert wc.effective_approval_policy(_campaign_doc(preauthorized=True), [_node()]) == AUTO


def test_effective_declaration_high_impact_is_owner() -> None:
    nodes = [_node(), _node(auth="high_impact")]
    assert wc.effective_approval_policy(_campaign_doc(preauthorized=True), nodes) == OWNER


def test_effective_declaration_approval_node_is_owner() -> None:
    nodes = [_node(), _node(kind=plan_store.KIND_APPROVAL, auth="high_impact")]
    assert wc.effective_approval_policy(_campaign_doc(preauthorized=True), nodes) == OWNER


def test_effective_invalid_declaration_propagates() -> None:
    with pytest.raises(wc.WorkClassError):
        wc.effective_approval_policy(_campaign_doc(preauthorized=1), [_node()])


# ---------------------------------------------------------------------------
# resolve_owner_gate (real mission store)
# ---------------------------------------------------------------------------

def test_resolve_owner_policy_mission_refused(hermes_root: Path) -> None:
    _verified_awaiting(hermes_root, "msn-r1")  # default policy: owner
    out = json.loads(wc.resolve_owner_gate("msn-r1", hermes_root=hermes_root))
    assert out["success"] is False
    assert out["reason"] == "owner_gated"
    assert out["surfaced"]["success"] is True
    assert _status(hermes_root, "msn-r1") == ("awaiting_approval", out["surfaced"]["version"])


def test_resolve_auto_policy_unverified_children_refused(hermes_root: Path) -> None:
    assert _create(hermes_root, "msn-r2", approval_policy=AUTO)["success"] is True
    assert json.loads(mr.hermes_mission_attach(
        "msn-r2", "evidence", "ev-1", state="pending", confirm=True, dry_run=False,
        hermes_root=hermes_root))["success"] is True
    json.loads(mr.hermes_mission_reconcile("msn-r2", confirm=True, dry_run=False, hermes_root=hermes_root))
    out = json.loads(wc.resolve_owner_gate("msn-r2", hermes_root=hermes_root))
    assert out["success"] is False
    assert out["reason"] == "evidence_not_verified"
    assert _status(hermes_root, "msn-r2")[0] != "completed"


def test_resolve_auto_policy_verified_without_owner_mode_refused(hermes_root: Path) -> None:
    _verified_awaiting(hermes_root, "msn-r3", approval_policy=AUTO)
    before = _status(hermes_root, "msn-r3")
    out = json.loads(wc.resolve_owner_gate("msn-r3", hermes_root=hermes_root))
    assert out["success"] is False
    assert out["reason"] == "owner_mode_unavailable"
    assert out["surfaced"]["owner_mode_available"] is False
    assert _status(hermes_root, "msn-r3") == before  # zero mutation


def test_resolve_auto_policy_verified_with_owner_mode_approves(
    hermes_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verified_awaiting(hermes_root, "msn-r4", approval_policy=AUTO)
    _owner(monkeypatch)
    out = json.loads(wc.resolve_owner_gate("msn-r4", hermes_root=hermes_root))
    assert out["success"] is True
    assert out["status"] == "completed"
    assert out["work_class"] == {"policy_table": "v1", "resolved_via": "policy_table_preauthorization"}
    assert "work-class-policy" in out["approval"]["approval_reference"]
    assert out["approval"]["approved_by"] == "owner"  # the native owner-only path


def test_resolve_dry_run_with_owner_mode_changes_nothing(
    hermes_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verified_awaiting(hermes_root, "msn-r5", approval_policy=AUTO)
    _owner(monkeypatch)
    before = _status(hermes_root, "msn-r5")
    out = json.loads(wc.resolve_owner_gate("msn-r5", dry_run=True, hermes_root=hermes_root))
    assert out["success"] is True
    assert out.get("dry_run") is True
    assert _status(hermes_root, "msn-r5") == before


# ---------------------------------------------------------------------------
# surface_owner_gate (read-only)
# ---------------------------------------------------------------------------

def test_surface_bundle_keys_present(hermes_root: Path) -> None:
    _verified_awaiting(hermes_root, "msn-s1")
    out = json.loads(wc.surface_owner_gate("msn-s1", hermes_root=hermes_root))
    assert out["success"] is True
    for key in ("mission_id", "status", "final_approval_required", "approval_policy",
                "node_classifications", "children", "owner_mode_available", "next"):
        assert key in out
    assert out["status"] == "awaiting_approval"
    assert out["final_approval_required"] is True
    assert out["children_terminal_with_verified_success"] is True
    assert out["next"] == "owner_approve"  # owner policy: human click path
    assert out["owner_mode_available"] is False


def test_surface_is_read_only(hermes_root: Path) -> None:
    _verified_awaiting(hermes_root, "msn-s2", approval_policy=AUTO)
    before = _status(hermes_root, "msn-s2")
    for _ in range(3):
        assert json.loads(wc.surface_owner_gate("msn-s2", hermes_root=hermes_root))["success"] is True
    assert _status(hermes_root, "msn-s2") == before


# ---------------------------------------------------------------------------
# Controller integration (AUTO-04 sets approval_policy at mint)
# ---------------------------------------------------------------------------

def _spec_db(root: Path, mid: str) -> dict:
    db = sqlite3.connect(root / "missions" / "missions.db")
    row = db.execute("SELECT spec_json FROM missions WHERE mission_id=?", (mid,)).fetchone()
    return json.loads(row[0])


def _wc_segment(seq: str, mid: str, *, preauthorized: bool | None = None) -> dict:
    doc = json.loads(_campaign_doc(mission_id=mid))
    if preauthorized is not None:
        doc["preauthorized"] = preauthorized
    return {"seq": seq, "depends_on": [], "campaign_json": json.dumps(doc)}


def _wc_manifest(*segments: dict) -> str:
    return json.dumps({
        "schema": ctrl.MANIFEST_SCHEMA,
        "campaign_id": "camp-test",
        "segments": list(segments),
    })


def test_controller_mint_preauthorized_routine_campaign_is_auto(hermes_root: Path) -> None:
    out = json.loads(ctrl.campaign_controller_tick(
        _wc_manifest(_wc_segment("s1", "msn-wc1", preauthorized=True)),
        confirm=True, apply=True, hermes_root=hermes_root,
    ))
    assert out["success"] is True
    spec = _spec_db(hermes_root, "msn-wc1")
    assert spec["approval_policy"] == AUTO
    assert spec["final_approval_required"] is True  # never forge approval-free


def test_controller_mint_default_campaign_stays_owner(hermes_root: Path) -> None:
    out = json.loads(ctrl.campaign_controller_tick(
        _wc_manifest(_wc_segment("s1", "msn-wc2")),
        confirm=True, apply=True, hermes_root=hermes_root,
    ))
    assert out["success"] is True
    spec = _spec_db(hermes_root, "msn-wc2")
    assert spec["approval_policy"] == OWNER
    assert spec["final_approval_required"] is True


# ---------------------------------------------------------------------------
# Hard invariants (DO NOT list)
# ---------------------------------------------------------------------------

def test_module_never_fabricates_owner_mode() -> None:
    src = Path(wc.__file__).read_text(encoding="utf-8")
    assert op.OWNER_ACK_REQUIRED_VALUE not in src  # never the ack string
    assert "os.environ" not in src  # never touches any env var
    assert "setenv" not in src


def test_native_high_impact_floor_untouched() -> None:
    import operator_controller as controller

    assert controller.L2_FORBIDDEN_AUTH_CLASSES == frozenset({"high_impact"})
    assert wc.ROUTINE_AUTH_CLASSES == ("read_only", "reversible_write")
