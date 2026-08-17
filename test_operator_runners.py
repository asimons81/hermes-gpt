from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_contract as contract_mod
import operator_policy as op
import operator_runners as runners


def _contract(ws: Path, *, backend: str | None = None, options: dict | None = None) -> dict:
    value = {
        "schema": "hermes.work-contract/v1",
        "task_id": "runner-test-001",
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": "Inspect the workspace and make the requested bounded change.",
        "allowed_scope": {"workspaces": [str(ws)], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {
            "class": "reversible_write",
            "approved": True,
            "approved_by": "owner",
            "approval_reference": "test",
        },
    }
    if backend:
        value["execution"] = {"backend": backend, "options": options or {}}
    return value


def _enable_workspace(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(ws))


def test_builtin_backends_registered():
    names = {item["name"] for item in runners.list_backends()}
    assert {"fleet", "pi_rpc", "omx", "codex"}.issubset(names)


def test_legacy_contract_hash_shape_unchanged_without_execution(tmp_path: Path):
    raw = _contract(tmp_path)
    canonical, parsed, sha = contract_mod._parse_contract(json.dumps(raw))
    assert "execution" not in parsed
    assert "execution" not in json.loads(canonical)
    assert len(sha) == 64
    assert runners.selected_backend(parsed) == "fleet"


def test_execution_is_canonical_and_surface_redacts_option_values(tmp_path: Path):
    raw = _contract(tmp_path, backend="pi_rpc", options={"model": "test/model", "provider": "test-provider"})
    _, parsed, _ = contract_mod._parse_contract(json.dumps(raw))
    assert parsed["execution"]["backend"] == "pi_rpc"
    surface = contract_mod._surface_contract(parsed)
    assert surface["execution"] == {"backend": "pi_rpc", "option_keys": ["model", "provider"]}
    assert "test/model" not in json.dumps(surface)


def test_unknown_backend_returns_structured_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_workspace(monkeypatch, tmp_path)
    raw = _contract(tmp_path, backend="does_not_exist")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=tmp_path / "hermes"))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_BACKEND_UNKNOWN"
    assert payload["backend"] == "does_not_exist"


def test_pi_rpc_dry_run_uses_rpc_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="pi_rpc", options={"model": "x/y"})
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["backend"] == "pi_rpc"
    assert payload["plan"]["protocol"] == "jsonl-rpc"
    assert payload["plan"]["mode"] == "rpc"
    assert payload["plan"]["model"] == "x/y"


def test_omx_dry_run_uses_native_exec_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("omx")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="omx")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is True
    assert payload["backend"] == "omx"
    assert payload["plan"]["mode"] == "exec"
    assert payload["plan"]["sandbox"] == "workspace-write"


def test_runner_job_is_observed_by_contract_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    raw = _contract(ws, backend="pi_rpc")
    _, parsed, _ = contract_mod._parse_contract(json.dumps(raw))
    meta_path, _, _ = runners._job_paths(parsed["task_id"], root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": parsed["task_id"],
        "backend": "pi_rpc",
        "state": "completed",
        "outcome": "completed",
        "created_at": "2026-08-17T00:00:00+00:00",
        "started_at": "2026-08-17T00:00:01+00:00",
        "ended_at": "2026-08-17T00:00:02+00:00",
        "error": "",
    })
    check = contract_mod._check_run_state(parsed, root)
    assert check["status"] == "PASS"
    assert "runner:pi_rpc" in check["detail"]


def test_execution_options_reject_secret_like_keys(tmp_path: Path):
    raw = _contract(tmp_path, backend="pi_rpc", options={"api_key": "do-not-inline"})
    with pytest.raises(ValueError, match="must not carry secrets"):
        contract_mod._parse_contract(json.dumps(raw))


def test_canonical_swarm_accepts_per_stage_execution(tmp_path: Path):
    import operator_swarm as swarm
    import operator_swarm_workflows as workflows

    wf = workflows.canonical_workflow(
        title="Runner workflow",
        workspace=str(tmp_path),
        owners={"implementation": "coder", "codex_review": "reviewer"},
        executions={
            "implementation": {"backend": "pi_rpc", "options": {}},
            "codex_review": {"backend": "omx", "options": {"sandbox": "read-only"}},
        },
    )
    impl = next(stage for stage in wf["stages"] if stage["id"] == "implementation")
    review = next(stage for stage in wf["stages"] if stage["id"] == "codex_review")
    assert impl["execution"]["backend"] == "pi_rpc"
    assert review["execution"]["backend"] == "omx"
    assert review["review_requirements"]["reviewer"] == "reviewer"

    contract = swarm._stage_contract(wf, impl, task_id="runner-stage-001")
    _, parsed, _ = contract_mod._parse_contract(json.dumps(contract))
    assert parsed["execution"]["backend"] == "pi_rpc"


def test_read_only_pi_cannot_enable_write_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="pi_rpc", options={"tools": "read,bash,edit,write"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert "read-only authorization" in payload["safe_message"]


def test_read_only_omx_cannot_request_workspace_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("omx")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    raw = _contract(ws, backend="omx", options={"sandbox": "workspace-write"})
    raw["authorization"] = {"class": "read_only", "approved": True}
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert "read-only authorization" in payload["safe_message"]


def test_runner_cancel_enforces_job_workspace_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    root = tmp_path / "hermes"
    allowed.mkdir()
    other.mkdir()
    root.mkdir()
    _enable_workspace(monkeypatch, allowed)
    task_id = "runner-cancel-scope"
    meta_path, _, _ = runners._job_paths(task_id, root)
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "running",
        "workspace": str(other),
        "pid": None,
    })
    payload = json.loads(runners.hermes_runner_cancel(task_id, backend="pi_rpc", dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_CANCEL_ERROR"
