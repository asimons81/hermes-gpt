"""v0.14 observed acceptance, durable delivery grace, and bounded recovery."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import operator_autopilot as ap
import operator_contract as contracts
import operator_delegations as deleg
import operator_mission_plan as plan
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _age_failure(root):
    with deleg._connect(deleg._db_path(root), write=True) as db:
        db.execute("UPDATE delegations SET validation_failure_since=?",
                   ((datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat(),))
        db.commit()


def _report_node(**requirements):
    node = _node("report")
    node["expected_artifacts"] = ["report.md"]
    node["artifact_requirements"] = [{"path": "report.md", **requirements}]
    return node


def test_node_acceptance_is_immutable_and_part_of_the_signature(env):
    root, backend = env
    digest = hashlib.sha256(b"Reviewed result").hexdigest()
    node = _report_node(min_bytes=10, max_bytes=100, sha256=digest)
    canonical = plan._canonical_node(node)
    assert canonical["contract_sha256"] != plan._canonical_node(_report_node(min_bytes=11, max_bytes=100, sha256=digest))["contract_sha256"]
    assert plan._canonical_node(canonical) == canonical
    _mk(root, [node])
    _tick(root)
    contract = backend.calls[0]
    assert contract["expected_artifacts"] == [{"path": "report.md", "must_exist": True,
                                             "min_bytes": 10, "max_bytes": 100, "sha256": digest}]
    _observe(root, contract["task_id"], state="completed", artifacts={"report.md": "Reviewed result"})
    assert _tick(root)["completed"] == ["report"]
    rows = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
    assert rows[0]["validation"]["verdict"] == "SATISFIED"
    assert "Raw objective" not in json.dumps(rows)


@pytest.mark.parametrize("requirements", [
    {"path": "other.md"}, {"path": "../report.md"}, {"path": "report.md", "min_bytes": 0},
    {"path": "report.md", "min_bytes": True}, {"path": "report.md", "max_bytes": 0},
    {"path": "report.md", "max_bytes": "10"}, {"path": "report.md", "sha256": "BAD"},
    {"path": "report.md", "run": "arbitrary command"},
])
def test_invalid_requirements_are_rejected_before_dispatch(requirements):
    node = _node("report")
    node["expected_artifacts"] = ["report.md"]
    node["artifact_requirements"] = [requirements]
    with pytest.raises(ValueError):
        plan._canonical_node(node)


def test_legacy_node_contract_digest_does_not_change():
    node = plan._canonical_node(_node("a"))
    assert "artifact_requirements" not in node
    assert plan._canonical_node({**node, "artifact_requirements": []}) == node


@pytest.mark.parametrize("requirements,content,code", [
    ({"min_bytes": 5}, "x", "artifact_too_small"),
    ({"max_bytes": 3}, "large", "artifact_too_large"),
    ({"sha256": hashlib.sha256(b"expected").hexdigest()}, "wrong", "artifact_hash_mismatch"),
])
def test_finished_bad_artifact_enters_bounded_recovery_after_durable_grace(env, requirements, content, code):
    root, backend = env
    _mk(root, [_report_node(**requirements)])
    _tick(root)
    ap._write_run(MID, root, max_replans=1)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={"report.md": content})
    first = _tick(root)
    assert not first["completed"] and not first["replanned"] and len(backend.calls) == 1
    row = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"][0]
    assert row["state"] == "reconciling" and row["validation_failure_since"]
    assert code in next(c for c in row["validation"]["checks"] if c["kind"] == "artifacts")["failure_codes"]
    # A new reconcile call uses the same durable timer, including after restart.
    before = row["validation_failure_since"]
    assert _j(deleg.hermes_delegation_reconcile(row["delegation_id"], apply=True, hermes_root=root))["delegation"]["validation_failure_since"] == before
    _age_failure(root)
    recovered = _tick(root)
    assert list(recovered["replanned"]) == ["report"]
    assert len(backend.calls) == 2 and backend.calls[1]["expected_artifacts"] == backend.calls[0]["expected_artifacts"]
    assert backend.calls[1]["allowed_scope"] != backend.calls[0]["allowed_scope"]
    failed = _j(deleg.hermes_delegation_get(row["delegation_id"], hermes_root=root))["delegation"]
    assert failed["state"] == "failed" and failed["outcome"] == "validation_failed"
    clone = next(n for n in _j(plan.hermes_plan_get(MID, hermes_root=root))["nodes"] if n["node_id"] != "report")
    assert clone["artifact_requirements"] == plan._canonical_node(_report_node(**requirements))["artifact_requirements"]
    replacement_content = "expected" if "sha256" in requirements else "ok" if "max_bytes" in requirements else "valid deliverable"
    _observe(root, backend.calls[1]["task_id"], state="completed", artifacts={"report.md": replacement_content})
    completed = _tick(root)
    assert completed["completed"] == [clone["node_id"]] and completed["mission_status"] == "awaiting_approval"


def test_late_artifact_during_grace_completes_without_duplicate_dispatch(env):
    root, backend = env
    _mk(root, [_report_node(min_bytes=5)])
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={})
    assert not _tick(root)["completed"]
    workspace = Path(backend.calls[0]["allowed_scope"]["workspaces"][0])
    (workspace / "report.md").write_text("delivered", encoding="utf-8")
    assert _tick(root)["completed"] == ["report"] and len(backend.calls) == 1
    row = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"][0]
    assert row["validation_failure_since"] == "" and row["state"] == "succeeded"


def test_exhausted_replan_budget_stops_bad_artifact_without_more_work(env):
    root, backend = env
    _mk(root, [_report_node(min_bytes=5)])
    _tick(root)
    ap._write_run(MID, root, max_replans=0)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={})
    _tick(root)
    _age_failure(root)
    failed = _tick(root)
    assert "report" in failed["failed_nodes"] and _states(root)["report"] == "failed"
    assert len(backend.calls) == 1
    summary = ap.build_summary(MID, ap._read_run(MID, root), root)
    assert summary["workers"][0]["validation"]["verdict"] == "NOT_SATISFIED"


def test_unreadable_artifact_remains_unverified_and_never_auto_redispatches(env, monkeypatch):
    root, backend = env
    _mk(root, [_report_node(sha256=hashlib.sha256(b"ok").hexdigest())])
    _tick(root)
    ap._write_run(MID, root, max_replans=1)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={"report.md": "ok"})
    def denied(path):
        raise PermissionError("do not persist this private error")
    monkeypatch.setattr(contracts, "_artifact_hash", denied)
    _tick(root)
    _age_failure(root)
    assert not _tick(root)["replanned"] and len(backend.calls) == 1
    row = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"][0]
    assert row["state"] == "reconciling" and row["validation_failure_since"] == ""
    assert "private error" not in json.dumps(row)
    summary = ap.build_summary(MID, ap._read_run(MID, root), root)
    assert "validation_pending" in {item["code"] for item in summary["attention"]}


def test_backend_cannot_forge_validation_failure_classification():
    forged = {"delegation": {"state": "failed", "outcome": "validation_failed", "validation_verdict": "NOT_SATISFIED"}}
    assert ap._observation_env(_node("a"), forged, "running", True)["delegation"]["validation_verdict"] == ""


def test_pending_human_review_never_becomes_automatic_artifact_rework(env, monkeypatch):
    root, backend = env
    original = ap._build_contract
    def with_review(*args, **kwargs):
        contract = original(*args, **kwargs)
        contract["review_requirements"] = {"required": True, "reviewer": "default", "approval_required": True}
        contract["authorization"]["approved_by"] = "hermes-researcher"
        return contract
    monkeypatch.setattr(ap, "_build_contract", with_review)
    _mk(root, [_report_node(min_bytes=5)])
    _tick(root)
    ap._write_run(MID, root, max_replans=1)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={})
    _tick(root)
    _age_failure(root)
    assert not _tick(root)["replanned"] and len(backend.calls) == 1
    row = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"][0]
    assert row["validation_failure_since"] == "" and row["state"] == "reconciling"


def test_read_only_preview_does_not_start_or_persist_a_grace_timer(env):
    root, backend = env
    _mk(root, [_report_node(min_bytes=5)])
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="completed", artifacts={})
    row = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"][0]
    preview = _j(deleg.hermes_delegation_reconcile(row["delegation_id"], apply=False, hermes_root=root))
    assert preview["delegation"]["validation_failure_since"]
    stored = _j(deleg.hermes_delegation_get(row["delegation_id"], hermes_root=root))["delegation"]
    assert stored["validation_failure_since"] == "" and stored["validation"] == {}


@pytest.mark.parametrize("remote_digest,provenance,expected", [
    (hashlib.sha256(b"good").hexdigest(), "coordinator_verified_artifact", "PASS"),
    (hashlib.sha256(b"wrong").hexdigest(), "coordinator_verified_artifact", "FAIL"),
    (hashlib.sha256(b"good").hexdigest(), "worker_claim", "FAIL"),
])
def test_remote_hash_checks_require_coordinator_verified_evidence(tmp_path, monkeypatch, remote_digest, provenance, expected):
    contract = {"allowed_scope": {"workspaces": [str(tmp_path)]}, "expected_artifacts": [
        {"path": "report.md", "must_exist": True, "min_bytes": 1, "max_bytes": 10,
         "sha256": hashlib.sha256(b"good").hexdigest()}]}
    monkeypatch.setattr(contracts, "_admitted_artifact_evidence", lambda *args: [
        {"logical_name": "report.md", "size_bytes": 4, "sha256": remote_digest, "provenance": provenance}])
    assert contracts._check_artifacts(contract, "a" * 64, tmp_path)["status"] == expected


def test_hash_read_is_bounded_and_changing_content_is_unverified(tmp_path, monkeypatch):
    artifact = tmp_path / "report.md"
    artifact.write_bytes(b"same")
    read = contracts.os.read
    def changing(fd, size):
        chunk = read(fd, size)
        if chunk:
            artifact.write_bytes(b"else")
        return chunk
    monkeypatch.setattr(contracts.os, "read", changing)
    with pytest.raises(OSError, match="changed"):
        contracts._artifact_hash(artifact)
    monkeypatch.setattr(contracts.os, "read", read)
    monkeypatch.setattr(contracts, "MAX_ARTIFACT_HASH_BYTES", 3)
    with pytest.raises(OSError, match="limit"):
        contracts._artifact_hash(artifact)


def test_validation_projection_excludes_raw_details_and_unknown_fields():
    value = {"verdict": "NOT_SATISFIED", "checks": [{"kind": "artifacts", "status": "FAIL",
        "detail": "private/path/token", "failure_codes": ["artifact_missing", "secret private text"]}],
        "evidence": "raw file body", "objective": "private objective"}
    assert deleg.validation_summary(value) == {"verdict": "NOT_SATISFIED", "checks": [
        {"kind": "artifacts", "status": "FAIL", "failure_codes": ["artifact_missing"]}]}
