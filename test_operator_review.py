"""Adversarial tests for v0.7 S3: review-accept writer + validator integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_contract as oc
import operator_policy as op
import operator_review as rev


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "review-evidence").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def clean_env(monkeypatch):
    for name in [
        op.OPERATOR_ENABLED_ENV,
        op.OPERATOR_LEVEL_ENV,
        op.OPERATOR_APPLY_MODE_ENV,
        op.OPERATOR_ALLOWED_PROFILES_ENV,
        op.OPERATOR_ALLOWED_PATHS_ENV,
        op.OPERATOR_DENIED_PATHS_ENV,
        op.OWNER_ACK_ENV,
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def audit_override(tmp_path):
    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    yield log
    op.set_audit_log_override(None)


def _owner_env(monkeypatch) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)


def _minimal_contract(
    assigned_agent: str = "worker-a",
    task_id: str = "t_review_1",
    *,
    human_approval: bool = False,
) -> dict:
    auth = {"class": "read_only", "approved": True}
    if human_approval:
        auth["approved_by"] = "Tony"
        auth["approval_reference"] = "t_x"
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": task_id,
        "assigned_agent": assigned_agent,
        "assigned_profile": "worker-a",
        "objective": "Do the work.",
        "allowed_scope": {"workspaces": ["."], "profiles": ["worker-a"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {
            "required": True,
            "reviewer": "reviewer-a",
            "evidence": "",
            "approval_required": False,
        },
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
        "authorization": auth,
    }


def _contract_sha(contract: dict) -> str:
    canonical, _, sha = oc._parse_contract(json.dumps(contract))
    return sha


def test_self_review_rejected_at_write(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="worker-a",  # self-review
        verdict="SATISFIED",
        evidence_refs=["docs/report.md"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["code"] == "INVALID_REVIEW_ACCEPTANCE"
    assert "self-review" in parsed["safe_message"].lower()
    assert rev.read_review_acceptances(hermes_root) == []


def test_verdict_vocabulary_bounded(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="MAYBE",  # outside bounded vocabulary
        evidence_refs=["docs/report.md"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "verdict" in parsed["safe_message"].lower()


def test_satisfied_requires_evidence(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=[],
        approval_reference="",
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "evidence" in parsed["safe_message"].lower()


def test_denied_path_refs_rejected(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["secrets/hermes_gpt_tokens.json"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "denied" in parsed["safe_message"].lower()


def test_accept_requires_owner_level(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")  # below owner
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["docs/report.md"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["code"] == "REVIEW_POLICY_DENIED"


def test_accept_requires_confirm(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["docs/report.md"],
        confirm=False,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["code"] == "CONFIRMATION_REQUIRED"
    assert rev.read_review_acceptances(hermes_root) == []


def test_dry_run_does_not_write(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["docs/report.md"],
        dry_run=True,
        confirm=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert rev.read_review_acceptances(hermes_root) == []


def test_accept_writes_record_and_audits(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["docs/report.md", "docs/evidence.log"],
        approval_reference="t_approval_1",
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["changed"] is True
    assert parsed["record_id"].startswith("rev-")

    records = rev.read_review_acceptances(hermes_root)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema"] == rev.REVIEW_SCHEMA
    assert rec["contract_sha256"] == sha
    assert rec["reviewer"] == "reviewer-a"
    assert rec["assignee"] == "worker-a"
    assert rec["verdict"] == "SATISFIED"
    assert rec["evidence_refs"] == ["docs/report.md", "docs/evidence.log"]
    # No raw body / prompt fields on the record.
    for forbidden in ("prompt", "transcript", "objective", "raw"):
        assert forbidden not in rec

    audit_lines = audit_override.read_text(encoding="utf-8").splitlines()
    accept_audits = [json.loads(l) for l in audit_lines if '"hermes_review_accept"' in l]
    assert any(a.get("success") is True and a.get("changed") is True for a in accept_audits)


def test_validate_satisfied_with_review_evidence(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="SATISFIED",
        evidence_refs=["docs/report.md"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    assert json.loads(out)["success"] is True

    # The review check must PASS with review-evidence; other checks may be
    # INCONCLUSIVE (no observed run) — that is the fail-closed design, not a
    # review failure.
    parsed = oc._check_review(contract, sha, hermes_root)
    assert parsed["status"] == "PASS"
    assert "review-evidence" in parsed["detail"]

    verdict_out = json.loads(oc.hermes_contract_validate(json.dumps(contract), hermes_root=hermes_root))
    by_kind = {ch["kind"]: ch["status"] for ch in verdict_out.get("checks", [])}
    assert by_kind.get("review") == "PASS"


def test_validate_not_satisfied_without_review_evidence(hermes_root, clean_env, audit_override, monkeypatch):
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    # A NOT_SATISFIED record must not satisfy the validator.
    out = rev.hermes_review_accept(
        contract_sha256=sha,
        task_id=contract["task_id"],
        assignee="worker-a",
        reviewer="reviewer-a",
        verdict="NOT_SATISFIED",
        evidence_refs=["docs/failed.md"],
        confirm=True,
        dry_run=False,
        hermes_root=hermes_root,
    )
    assert json.loads(out)["success"] is True

    parsed = oc._check_review(contract, sha, hermes_root)
    assert parsed["status"] != "PASS"


def test_validate_rejects_self_review_record_at_validate_time(hermes_root, clean_env, audit_override, monkeypatch):
    """Even if a record were forged with reviewer == assignee, validate fails."""
    _owner_env(monkeypatch)
    contract = _minimal_contract()
    sha = _contract_sha(contract)

    # Forge a record directly on disk (bypasses the writer's guard).
    record = {
        "record_id": "rev-deadbeefdeadbeef",
        "schema": rev.REVIEW_SCHEMA,
        "contract_sha256": sha,
        "task_id": contract["task_id"],
        "assignee": "worker-a",
        "reviewer": "worker-a",  # self-review forged
        "verdict": "SATISFIED",
        "evidence_refs": ["docs/report.md"],
        "approval_reference": "",
        "created_by": "attacker",
        "created_at": "2026-08-15T00:00:00+00:00",
        "tool_version": "0.7.0",
    }
    path = rev.review_evidence_path(hermes_root)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")

    parsed = oc._check_review(contract, sha, hermes_root)
    assert parsed["status"] != "PASS"
    assert "self-review" not in parsed.get("detail", "")  # must not be a false pass


def test_human_approval_reference_still_accepted(hermes_root, clean_env, audit_override, monkeypatch):
    """Evidence 2 (human approval ref by a distinct approver) is preserved."""
    contract = _minimal_contract(human_approval=True)
    sha = _contract_sha(contract)

    parsed = oc._check_review(contract, sha, hermes_root)
    assert parsed["status"] == "PASS"
    assert "human approval" in parsed["detail"]
