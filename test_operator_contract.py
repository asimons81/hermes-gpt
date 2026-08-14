"""Tests for the Outcome / Work Contracts surface (v0.6 M1).

Mirrors the ``test_operator_mission.py`` style: all tests run against a temp
``hermes_root`` fixture — never production data. ``Path.home`` is patched to
the temp root so mission sources resolve inside the fixture.

The exit criterion (design §7.4 / §11.4 cases 1-6) is the false-"done"
rejection: a worker claiming completion is never trusted; the validator
rejects a false "done" claim (NOT_SATISFIED / INCONCLUSIVE +
``false_done_detected``) based only on **observed** Mission Control state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import operator_policy as op
import operator_contract as contract_mod

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, *, delegations: list[dict] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS async_delegations ("
            " delegation_id TEXT PRIMARY KEY, origin_session TEXT, parent_session_id TEXT,"
            " state TEXT, dispatched_at TEXT, completed_at TEXT, task_json TEXT, result_json TEXT)"
        )
        for d in delegations or []:
            conn.execute(
                "INSERT OR IGNORE INTO async_delegations VALUES (?,?,?,?,?,?,?,?)",
                (
                    d["delegation_id"],
                    d.get("origin_session"),
                    d.get("parent_session_id"),
                    d.get("state", "completed"),
                    d.get("dispatched_at"),
                    d.get("completed_at"),
                    d.get("task_json", "RAW TASK BODY SHOULD NEVER SURFACE"),
                    d.get("result_json", "RAW RESULT BODY SHOULD NEVER SURFACE"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_kanban_board(boards_dir: Path, slug: str, runs: list[dict]) -> None:
    board_dir = boards_dir / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(board_dir / "kanban.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_runs ("
            " id INTEGER PRIMARY KEY, task_id TEXT, assignee TEXT, status TEXT,"
            " outcome TEXT, error TEXT, body TEXT, metadata TEXT, started_at TEXT, ended_at TEXT)"
        )
        for r in runs:
            conn.execute(
                "INSERT INTO task_runs (task_id, assignee, status, outcome, error, body, metadata, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    r["task_id"],
                    r.get("assignee"),
                    r.get("status", "done"),
                    r.get("outcome"),
                    r.get("error"),
                    r.get("body", "RAW BODY SHOULD NEVER SURFACE"),
                    r.get("metadata", "RAW METADATA SHOULD NEVER SURFACE"),
                    r.get("started_at"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    """Build a hermetic Hermes root + workspace with observed run sources."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    contract_mod.mission._cache_clear()

    root = tmp_path / "hermes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("model: test-model\nprovider: test-provider\n", encoding="utf-8")

    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)

    _make_state_db(
        root / "state.db",
        delegations=[
            {
                "delegation_id": "del-completed",
                "origin_session": "ses-0",
                "state": "completed",
                "dispatched_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
                "completed_at": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
            },
            {
                "delegation_id": "del-running",
                "origin_session": "ses-1",
                "state": "running",
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            },
        ],
    )

    boards = root / "kanban" / "boards"
    _make_kanban_board(
        boards,
        "board-a",
        [
            {"task_id": "t-done", "assignee": "hermes-dev", "status": "done", "outcome": "completed"},
            {"task_id": "t-running", "assignee": "hermes-dev", "status": "running", "outcome": None},
        ],
    )

    return root


def _audit_path() -> Path:
    return op.audit_log_path()


def _contract(**overrides) -> dict:
    value = {
        "schema": "hermes.work-contract/v1",
        "task_id": "wc-test-001",
        "assigned_agent": "hermes-dev",
        "assigned_profile": "hermes-dev",
        "objective": "Refactor the mission overview cache",
        "allowed_scope": {
            "workspaces": ["/tmp/ws-does-not-exist"],
            "profiles": ["hermes-dev"],
        },
        "forbidden_actions": [
            {"action": "public_publish", "reason": "no public posts", "class": "HIGH"},
        ],
        "expected_artifacts": [
            {"path": "src/nexusos/cache.py", "must_exist": True, "min_bytes": 1},
        ],
        "tests": [],
        "review_requirements": {
            "required": True,
            "reviewer": "default",
            "evidence": "audit acceptance",
            "approval_required": True,
        },
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": False,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": ["bounded output"],
        "authorization": {
            "class": "reversible_write",
            "approved": True,
            "approved_by": "Tony",
            "approval_reference": "t_xxx",
        },
    }
    value.update(overrides)
    return value


def _contract_for_ws(ws: Path, **overrides) -> dict:
    """A contract whose allowed workspace points at the fixture ws dir."""
    c = _contract(
        allowed_scope={"workspaces": [str(ws)], "profiles": ["hermes-dev"]},
        **overrides,
    )
    return c


def _run_validate(contract: dict, root: Path, runner=None) -> dict:
    out = contract_mod.hermes_contract_validate(
        json.dumps(contract), runner=runner, hermes_root=root
    )
    assert isinstance(out, str)
    return json.loads(out)


def _run_define(contract: dict, root: Path) -> dict:
    out = contract_mod.hermes_contract_define(json.dumps(contract), hermes_root=root)
    assert isinstance(out, str)
    return json.loads(out)


def _run_status(contract: dict, root: Path) -> dict:
    out = contract_mod.hermes_contract_status(json.dumps(contract), hermes_root=root)
    assert isinstance(out, str)
    return json.loads(out)


def _add_review_evidence(contract: dict, *, reviewer: str = "default") -> None:
    """Write an audit acceptance record for a contract by a distinct reviewer."""
    canonical, _, sha = contract_mod._parse_contract(json.dumps(contract))
    op.audit_record(
        tool="hermes_contract_validate",
        level="read_only",
        apply_mode="read_only",
        dry_run=True,
        success=True,
        changed=False,
        summary="acceptance",
        profile=reviewer,
        extra={"contract_sha256": sha, "task_id": contract["task_id"], "verdict": "SATISFIED", "reviewer": reviewer},
    )


def _add_forbidden_evidence(contract: dict, *, profile: str = "hermes-dev") -> None:
    """Write an audit record showing the assignee performed a forbidden action."""
    op.audit_record(
        tool="hermes_workspace_write_file",
        level="workspace",
        apply_mode="direct",
        dry_run=False,
        success=True,
        changed=True,
        summary="wrote public_publish file",
        profile=profile,
        extra={"forbidden_action": "public_publish", "task_id": contract["task_id"]},
    )


def _test_runner(results: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
    """Runner accepting (argv, timeout, workdir) for workspace run_test."""
    results = results or {}

    def runner(argv, *, timeout, workdir=None):
        key = tuple(argv)
        if key in results:
            return results[key]
        return (0, "", "")

    return runner


def _enable_workspace_direct(monkeypatch) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


def _enable_read_only(monkeypatch) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


# ---------------------------------------------------------------------------
# 1. Schema / canonicalization
# ---------------------------------------------------------------------------


def test_define_canonicalizes_contract(hermes_root):
    ws = hermes_root.parent / "ws"
    out = _run_define(_contract_for_ws(ws), hermes_root)
    assert out["success"] is True
    assert out["schema_version"] == "0.6-wc.1"
    assert out["task_id"] == "wc-test-001"
    assert len(out["contract_sha256"]) == 64
    assert out["contract"]["schema"] == "hermes.work-contract/v1"
    assert out["contract"]["assigned_agent"] == "hermes-dev"
    assert out["contract"]["forbidden_actions"][0]["class"] == "HIGH"
    assert out["contract"]["expected_artifacts"][0]["basename"] == "cache.py"


def test_define_sha_is_stable(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(ws)
    sha1 = _run_define(c, hermes_root)["contract_sha256"]
    sha2 = _run_define(c, hermes_root)["contract_sha256"]
    assert sha1 == sha2
    # Different objective -> different sha.
    c2 = _contract_for_ws(ws, objective="A totally different objective text")
    sha3 = _run_define(c2, hermes_root)["contract_sha256"]
    assert sha1 != sha3


# ---------------------------------------------------------------------------
# 2. Invalid contract
# ---------------------------------------------------------------------------


def test_define_rejects_denied_workspace(hermes_root):
    c = _contract(allowed_scope={"workspaces": ["/home/tony/.ssh"], "profiles": ["hermes-dev"]})
    out = _run_define(c, hermes_root)
    assert out["success"] is False
    assert out["code"] == "CONTRACT_DENIED"


def test_define_rejects_denied_artifact(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        expected_artifacts=[{"path": "/home/tony/.ssh/id_rsa", "must_exist": True, "min_bytes": 1}],
    )
    out = _run_define(c, hermes_root)
    assert out["success"] is False
    assert out["code"] == "CONTRACT_DENIED"


def test_define_rejects_non_allowlisted_test(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        tests=[{"name": "evil", "command": "rm -rf /tmp/x", "workdir": str(ws)}],
    )
    out = _run_define(c, hermes_root)
    assert out["success"] is False
    assert out["code"] == "INVALID_CONTRACT"


def test_define_rejects_high_impact_without_approval(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        authorization={"class": "high_impact", "approved": True},
    )
    out = _run_define(c, hermes_root)
    assert out["success"] is False
    # Design §11.4 test 2: high_impact without approval metadata ->
    # INVALID_CONTRACT / PermissionError. Both are acceptable rejections.
    assert out["code"] in ("INVALID_CONTRACT", "CONTRACT_DENIED")


def test_define_rejects_bad_schema(hermes_root):
    out = _run_define({"schema": "wrong/v1", "task_id": "x"}, hermes_root)
    assert out["success"] is False
    assert out["code"] == "INVALID_CONTRACT"


def test_define_rejects_artifact_escape(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        expected_artifacts=[{"path": "../escape.txt", "must_exist": True, "min_bytes": 1}],
    )
    out = _run_define(c, hermes_root)
    assert out["success"] is False


# ---------------------------------------------------------------------------
# 3. SATISFIED happy path
# ---------------------------------------------------------------------------


def test_validate_satisfied_happy_path(hermes_root, monkeypatch):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")

    _enable_workspace_direct(monkeypatch)
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        tests=[{"name": "unit", "command": "python -m pytest -q tests/", "workdir": str(ws)}],
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": True,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
    )
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root, runner=_test_runner())
    assert out["verdict"] == "SATISFIED"
    assert out["satisfied"] is True
    assert out["false_done_detected"] is False
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["run_state"] == "PASS"
    assert by_kind["artifacts"] == "PASS"
    assert by_kind["tests"] == "PASS"
    assert by_kind["review"] == "PASS"
    assert by_kind["forbidden"] == "PASS"
    assert by_kind["authorization"] == "PASS"


def test_validate_satisfied_via_delegation(hermes_root):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(ws, task_id="del-completed")
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "SATISFIED"
    assert out["checks"][0]["status"] == "PASS"


# ---------------------------------------------------------------------------
# 4. False-"done" rejection (S2 exit criterion, design §7.4 cases 1-6)
# ---------------------------------------------------------------------------


def test_false_done_rejected_nonterminal_run(hermes_root):
    """Case 1: worker claims done, observed run is still running."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(ws, task_id="t-running")
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    assert out["false_done_detected"] is True
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["run_state"] == "FAIL"
    assert any("run_state" in r for r in out["rejected_reasons"])


def test_false_done_rejected_missing_artifact(hermes_root):
    """Case 2: run terminal/ok but expected artifact missing on disk."""
    c = _contract_for_ws(hermes_root.parent / "ws", task_id="t-done")
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    assert out["false_done_detected"] is True
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["artifacts"] == "FAIL"
    assert any("artifacts" in r for r in out["rejected_reasons"])


def test_false_done_rejected_failed_test(hermes_root, monkeypatch):
    """Case 3: run ok + artifacts present, but declared test exits non-zero."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    _enable_workspace_direct(monkeypatch)
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        tests=[{"name": "unit", "command": "python -m pytest -q tests/", "workdir": str(ws)}],
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": True,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
    )
    _add_review_evidence(c, reviewer="default")
    runner = _test_runner({("python", "-m", "pytest", "-q", "tests/"): (1, "", "FAILURES")})
    out = _run_validate(c, hermes_root, runner=runner)
    assert out["verdict"] == "NOT_SATISFIED"
    assert out["false_done_detected"] is True
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["tests"] == "FAIL"
    assert any("tests" in r for r in out["rejected_reasons"])


def test_false_done_rejected_self_review(hermes_root):
    """Case 4: review evidence reviewer == assigned_agent (self-review)."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        # No human approval reference: only audit evidence can satisfy review.
        authorization={"class": "reversible_write", "approved": True},
    )
    _add_review_evidence(c, reviewer="hermes-dev")  # reviewer == assignee
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    assert out["false_done_detected"] is True
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["review"] == "FAIL"


def test_false_done_rejected_no_review_evidence(hermes_root):
    """Review required but no evidence by a distinct reviewer or human approval."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        authorization={"class": "reversible_write", "approved": True},
    )
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["review"] == "FAIL"


def test_false_done_rejected_forbidden_action(hermes_root):
    """Case 5: audit shows the assignee took a HIGH-class forbidden action."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(ws, task_id="t-done")
    _add_review_evidence(c, reviewer="default")
    _add_forbidden_evidence(c)
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    assert out["false_done_detected"] is True
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["forbidden"] == "FAIL"
    assert any("forbidden" in r for r in out["rejected_reasons"])


def test_false_done_rejected_no_observed_run_is_inconclusive(hermes_root):
    """Case 6: valid contract, no observed run at all -> INCONCLUSIVE (fail-closed)."""
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="wc-never-dispatched",
        expected_artifacts=[],
        review_requirements={"required": False, "reviewer": "", "evidence": "", "approval_required": False},
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
    )
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "INCONCLUSIVE"
    assert out["false_done_detected"] is True
    assert out["satisfied"] is False
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["run_state"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# 5. Observed-only evidence
# ---------------------------------------------------------------------------


def test_worker_result_is_never_evidence(hermes_root):
    """A worker-supplied 'done' result with no observed run -> INCONCLUSIVE.

    The validator API accepts no completion bundle; only observed state counts.
    """
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="wc-worker-claims",
        expected_artifacts=[],
        review_requirements={"required": False, "reviewer": "", "evidence": "", "approval_required": False},
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
    )
    # The only "completion" is the contract/objective itself; no observed run.
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "INCONCLUSIVE"
    assert out["satisfied"] is False
    assert out["false_done_detected"] is True
    # No parameter on the public tool accepts a result bundle.
    import inspect

    sig = inspect.signature(contract_mod.hermes_contract_validate)
    assert "result" not in sig.parameters
    assert "completion_bundle" not in sig.parameters


# ---------------------------------------------------------------------------
# 6. Forbidden action detection
# ---------------------------------------------------------------------------


def test_forbidden_action_scan_fails_high_severity(hermes_root):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        forbidden_actions=[
            {"action": "secret_access", "reason": "no secrets", "class": "HIGH"},
        ],
    )
    _add_review_evidence(c, reviewer="default")
    op.audit_record(
        tool="hermes_vault_read",
        level="workspace",
        apply_mode="direct",
        dry_run=False,
        success=True,
        changed=True,
        summary="read secret_access entry",
        profile="hermes-dev",
        extra={"task_id": c["task_id"]},
    )
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "NOT_SATISFIED"
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["forbidden"] == "FAIL"


def test_forbidden_action_ignores_unrelated_contract_audit(hermes_root):
    """L1: a concurrent contract's audit event cannot fail this task."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(ws, task_id="t-done")
    _add_review_evidence(c, reviewer="default")
    op.audit_record(
        tool="hermes_workspace_write_file",
        level="workspace",
        apply_mode="direct",
        dry_run=False,
        success=True,
        changed=True,
        summary="wrote public_publish file",
        profile="hermes-dev",
        extra={"forbidden_action": "public_publish", "task_id": "other-contract"},
    )
    out = _run_validate(c, hermes_root)
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert out["verdict"] == "SATISFIED"
    assert by_kind["forbidden"] == "PASS"


def test_run_state_retry_selection_is_order_independent(hermes_root, monkeypatch):
    """L2: the latest retry wins regardless of source-list ordering."""
    c = _contract(task_id="retry-task")
    old_success = {"status": "done", "outcome": "completed", "started_at": "2026-08-13T10:00:00+00:00"}
    latest_failure = {"status": "done", "outcome": "failed", "started_at": "2026-08-13T11:00:00+00:00"}
    monkeypatch.setattr(contract_mod, "_observed_runs", lambda *_: [old_success, latest_failure])
    first = contract_mod._check_run_state(c, hermes_root)
    monkeypatch.setattr(contract_mod, "_observed_runs", lambda *_: [latest_failure, old_success])
    second = contract_mod._check_run_state(c, hermes_root)
    assert first["status"] == second["status"] == "FAIL"
    assert first["detail"] == second["detail"]


# ---------------------------------------------------------------------------
# 7. Test gating (D6)
# ---------------------------------------------------------------------------


def test_tests_unverified_at_read_only(hermes_root, monkeypatch):
    """At read_only, required tests cannot run -> NOT_SATISFIED (fail-closed)."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    _enable_read_only(monkeypatch)
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        tests=[{"name": "unit", "command": "python -m pytest -q tests/", "workdir": str(ws)}],
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": True,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
    )
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["tests"] == "UNVERIFIED"
    assert out["verdict"] == "NOT_SATISFIED"
    assert any("tests" in r for r in out["rejected_reasons"])


def test_tests_run_at_workspace_direct(hermes_root, monkeypatch):
    """At workspace+direct the allowlisted test runs and gates (D6)."""
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    _enable_workspace_direct(monkeypatch)
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        tests=[{"name": "unit", "command": "python -m pytest -q tests/", "workdir": str(ws)}],
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": True,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
    )
    _add_review_evidence(c, reviewer="default")
    runner = _test_runner({("python", "-m", "pytest", "-q", "tests/"): (0, "ok", "")})
    out = _run_validate(c, hermes_root, runner=runner)
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["tests"] == "PASS"
    assert out["verdict"] == "SATISFIED"


def test_test_workdir_must_be_under_allowed_workspace(hermes_root, monkeypatch):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    _enable_workspace_direct(monkeypatch)
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        tests=[{"name": "unit", "command": "python -m pytest -q tests/", "workdir": "/etc"}],
        completion_criteria={
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": True,
            "tests_pass": True,
            "review_satisfied": True,
            "no_forbidden_actions": True,
        },
    )
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root, runner=_test_runner())
    by_kind = {ch["kind"]: ch["status"] for ch in out["checks"]}
    assert by_kind["tests"] == "FAIL"
    assert out["verdict"] == "NOT_SATISFIED"


# ---------------------------------------------------------------------------
# 8. Redaction
# ---------------------------------------------------------------------------


def test_define_redacts_objective_and_secret_shapes(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        objective="SUPER-SECRET-OBJECTIVE-TEXT-NEVER-SURFACE",
        forbidden_actions=[
            {"action": "public_publish", "reason": "token=sk-proj-abcdefghijklmnopqrstuvwxyz123456", "class": "HIGH"}
        ],
        expected_artifacts=[{"path": "src/nexusos/cache.py", "must_exist": True, "min_bytes": 1}],
    )
    raw = contract_mod.hermes_contract_define(json.dumps(c), hermes_root=hermes_root)
    assert "SUPER-SECRET-OBJECTIVE-TEXT-NEVER-SURFACE" not in raw
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in raw
    out = json.loads(raw)
    assert out["contract"]["objective"]["prompt_len"] > 0
    assert len(out["contract"]["objective"]["prompt_sha256"]) == 64
    # Artifact content is never read onto the surface; only basename + size.
    assert "def cached(): pass" not in raw


def test_validate_redacts_objective(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="t-done",
        objective="SUPER-SECRET-OBJECTIVE-TEXT-NEVER-SURFACE",
    )
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    raw = json.dumps(out)
    assert "SUPER-SECRET-OBJECTIVE-TEXT-NEVER-SURFACE" not in raw
    assert out["contract_sha256"] == contract_mod._parse_contract(json.dumps(c))[2]


def test_status_redacts_bodies(hermes_root):
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(ws, task_id="t-done")
    out = _run_status(c, hermes_root)
    raw = json.dumps(out)
    assert "RAW BODY SHOULD NEVER SURFACE" not in raw
    assert "RAW METADATA SHOULD NEVER SURFACE" not in raw
    assert out["observed"]["kanban_runs"][0]["task_id"] == "t-done"


# ---------------------------------------------------------------------------
# 9. Read-only guarantee
# ---------------------------------------------------------------------------


def test_all_sqlite_opens_are_mode_ro(hermes_root, monkeypatch):
    uris: list[str] = []
    real_connect = sqlite3.connect

    def capturing_connect(database, *args, **kwargs):
        if isinstance(database, str) and database.startswith("file:"):
            uris.append(database)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr("operator_mission.sqlite3.connect", capturing_connect)
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(ws, task_id="t-done")
    _run_validate(c, hermes_root)
    _run_status(c, hermes_root)
    assert uris, "expected at least one SQLite open"
    for uri in uris:
        assert "?mode=ro" in uri, f"non-read-only SQLite URI: {uri}"
        assert "mode=rw" not in uri


def test_validate_does_not_modify_state(hermes_root):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")

    before = {
        p.relative_to(hermes_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in hermes_root.rglob("*")
        if p.is_file()
    }
    c = _contract_for_ws(ws, task_id="t-done")
    _add_review_evidence(c, reviewer="default")
    out = _run_validate(c, hermes_root)
    assert out["verdict"] == "SATISFIED"
    after = {
        p.relative_to(hermes_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in hermes_root.rglob("*")
        if p.is_file()
    }
    assert after == before


# ---------------------------------------------------------------------------
# 10. Audit (D9)
# ---------------------------------------------------------------------------


def test_every_contract_call_is_audited(hermes_root):
    ws = hermes_root.parent / "ws"
    cache = ws / "src" / "nexusos" / "cache.py"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("def cached(): pass\n", encoding="utf-8")
    c = _contract_for_ws(ws, task_id="t-done")
    _add_review_evidence(c, reviewer="default")

    _run_define(c, hermes_root)
    _run_validate(c, hermes_root)
    _run_status(c, hermes_root)

    records = op.audit_tail(limit=50)
    tools = [r["tool"] for r in records]
    assert "hermes_contract_define" in tools
    assert "hermes_contract_validate" in tools
    assert "hermes_contract_status" in tools
    for rec in records:
        if rec["tool"].startswith("hermes_contract_"):
            assert rec.get("contract_sha256"), f"missing contract_sha256 in {rec['tool']}"
            assert rec.get("task_id"), f"missing task_id in {rec['tool']}"


# ---------------------------------------------------------------------------
# 11. Dispatch (dry-run-first, confirm + direct gates)
# ---------------------------------------------------------------------------


HERMES = "/test/hermes"
REGISTRY = {"agents": [{"name": "rza", "url": "http://rza.example:8765", "hasToken": True}]}


def _fleet_runner(responses: dict[tuple[str, ...], tuple[int, str, str]], calls: list[list[str]]):
    def runner(argv, *, timeout):
        calls.append(argv)
        return responses.get(tuple(argv), (1, "", "unexpected argv"))

    return runner


def _authority_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "fleet-authority.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "peers": [
                    {
                        "name": "rza",
                        "expected_host_role": "orchestrator",
                        "expected_card_identity": "rza",
                        "allowed_profiles": ["default"],
                        "max_authorization": "high_impact",
                        "allow_public_actions": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dispatch_requires_workspace_level(hermes_root, monkeypatch, tmp_path):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="wc-dispatch-001",
        assigned_agent="rza",
        assigned_profile="default",
        authorization={"class": "reversible_write", "approved": True, "approved_by": "Tony", "approval_reference": "t_x"},
    )
    out = json.loads(
        contract_mod.hermes_contract_dispatch(json.dumps(c), hermes_root=hermes_root)
    )
    assert out["success"] is False
    assert out["code"] == "FLEET_POLICY_DENIED"


def test_dispatch_dry_run_plan(hermes_root, monkeypatch, tmp_path):
    _enable_workspace_direct(monkeypatch)
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="wc-dispatch-002",
        assigned_agent="rza",
        assigned_profile="default",
        authorization={"class": "reversible_write", "approved": True, "approved_by": "Tony", "approval_reference": "t_x"},
    )
    calls: list[list[str]] = []
    runner = _fleet_runner(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )
    out = json.loads(
        contract_mod.hermes_contract_dispatch(
            json.dumps(c),
            dry_run=True,
            runner=runner,
            hermes_bin=HERMES,
            authority_manifest=_authority_manifest(tmp_path),
            hermes_root=hermes_root,
        )
    )
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["contract_task_id"] == "wc-dispatch-002"
    assert out["contract_sha256"] == contract_mod._parse_contract(json.dumps(c))[2]
    # Dry-run must not dispatch.
    assert not any("a2a" in a and "send" in a for a in calls)


def test_dispatch_requires_confirm_for_real_dispatch(hermes_root, monkeypatch, tmp_path):
    _enable_workspace_direct(monkeypatch)
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="wc-dispatch-003",
        assigned_agent="rza",
        assigned_profile="default",
        authorization={"class": "reversible_write", "approved": True, "approved_by": "Tony", "approval_reference": "t_x"},
    )
    calls: list[list[str]] = []
    runner = _fleet_runner(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )
    out = json.loads(
        contract_mod.hermes_contract_dispatch(
            json.dumps(c),
            dry_run=False,
            confirm=False,
            runner=runner,
            hermes_bin=HERMES,
            authority_manifest=_authority_manifest(tmp_path),
            hermes_root=hermes_root,
        )
    )
    assert out["success"] is False
    assert "CONFIRMATION_REQUIRED" in json.dumps(out)
    assert not any("a2a" in a and "send" in a for a in calls)


def test_dispatch_rejects_duplicate_task_id(hermes_root, monkeypatch, tmp_path):
    _enable_workspace_direct(monkeypatch)
    ws = hermes_root.parent / "ws"
    c = _contract_for_ws(
        ws,
        task_id="t-done",  # already observed in the fixture
        assigned_agent="rza",
        assigned_profile="default",
        authorization={"class": "reversible_write", "approved": True, "approved_by": "Tony", "approval_reference": "t_x"},
    )
    out = json.loads(
        contract_mod.hermes_contract_dispatch(
            json.dumps(c),
            dry_run=True,
            runner=_fleet_runner({}, []),
            hermes_bin=HERMES,
            authority_manifest=_authority_manifest(tmp_path),
            hermes_root=hermes_root,
        )
    )
    assert out["success"] is False
    assert out["code"] == "CONTRACT_ALREADY_DISPATCHED"


# ---------------------------------------------------------------------------
# 12. Server registration
# ---------------------------------------------------------------------------


def test_server_registers_contract_tools(monkeypatch):
    import server

    monkeypatch.setattr(server, "require_imports", lambda: None)
    built = server.build_server()
    import asyncio

    names = {t.name for t in asyncio.run(built.list_tools())}
    for tool in (
        "hermes_contract_define",
        "hermes_contract_dispatch",
        "hermes_contract_validate",
        "hermes_contract_status",
    ):
        assert tool in names
