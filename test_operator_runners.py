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


# ---------------------------------------------------------------------------
# PR #18 correctness regression tests
# ---------------------------------------------------------------------------


def test_popen_failure_deletes_request_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spawn failure after envelope writes must delete the request (raw
    objective) and leave only bounded failed metadata."""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    backend = runners.get_backend("pi_rpc")
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")

    def _boom(*args, **kwargs):
        raise RuntimeError("spawn refused")

    monkeypatch.setattr(runners.subprocess, "Popen", _boom)
    raw = _contract(ws, backend="pi_rpc")
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=False, confirm=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_SPAWN_FAILED"
    meta_path, request_path, _ = runners._job_paths(raw["task_id"], root)
    assert not request_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "failed"
    assert meta["outcome"] == "failed"
    assert raw["objective"] not in json.dumps(meta)


def test_fleet_exception_keeps_legacy_contract_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    fleet = runners.get_backend("fleet")
    monkeypatch.setattr(fleet, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fleet peer unreachable")))
    raw = _contract(ws)  # no execution selector -> implicit fleet
    payload = json.loads(contract_mod.hermes_contract_dispatch(json.dumps(raw), dry_run=True, hermes_root=root))
    assert payload["success"] is False
    assert payload["code"] == "CONTRACT_DISPATCH_ERROR"
    assert payload["suggested_action"] == "Check fleet authority manifest, registry, and peer service."


def test_explicit_fleet_selector_uses_runner_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    _enable_workspace(monkeypatch, ws)
    fleet = runners.get_backend("fleet")
    monkeypatch.setattr(fleet, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    raw = _contract(ws, backend="fleet")
    payload = runners.dispatch_contract(raw, confirm=False, dry_run=True, timeout=30, hermes_root=root)
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"


def test_non_fleet_backend_exception_uses_runner_dispatch_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    root.mkdir()
    codex = runners.get_backend("codex")
    monkeypatch.setattr(codex, "dispatch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("codex backend exploded")))
    raw = _contract(ws, backend="codex")
    payload = runners.dispatch_contract(raw, confirm=False, dry_run=True, timeout=30, hermes_root=root)
    assert payload["success"] is False
    assert payload["code"] == "RUNNER_DISPATCH_ERROR"
    assert payload["backend"] == "codex"


def test_codex_observed_runs_uses_normalized_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import operator_codex as op_codex

    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-hermes"))
    backend = runners.CodexBackend()
    # With hermes_root given, observed_runs must use the normalized root
    # (op_codex._root), not hermes_root/'codex-jobs' or ~/.hermes directly.
    normalized = op_codex._root(root)
    normalized.mkdir(parents=True, exist_ok=True)
    normalized.joinpath("job-1.json").write_text(json.dumps({
        "job_id": "job-1", "state": "completed", "outcome": "completed", "task_id": "codex-link-001",
    }), encoding="utf-8")
    runs = backend.observed_runs("codex-link-001", hermes_root=root)
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["scope"] == "runner:codex"
    # And normalization applies without an explicit hermes_root too.
    env_root = op_codex._root(None)
    if env_root != normalized:
        assert backend.observed_runs("codex-link-001") == []


def _fake_backend(monkeypatch):
    backend = runners.PiRpcBackend()
    monkeypatch.setattr(backend, "executable", lambda: "/bin/true")
    return backend


def _make_job(root: Path, task_id: str, *, backend: str = "pi_rpc", state: str = "running") -> Path:
    meta_path = root / f"{task_id}.json"
    request_path = root / f"{task_id}.request.json"
    runners._atomic_json(meta_path, {
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": backend,
        "state": state,
        "outcome": state,
        "workspace": "/tmp",
        "created_at": runners._now(),
        "started_at": runners._now(),
        "ended_at": None,
        "pid": None,
        "returncode": None,
        "error": "",
    })
    runners._atomic_json(request_path, {"backend": backend, "contract": _contract(Path("/tmp"), backend=backend), "timeout": 30})
    return meta_path


def test_cancel_marker_wins_over_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-completed-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path: (0, "done"))
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0, meta.get("error")
    assert meta["state"] == "cancelled"
    assert meta["outcome"] == "cancelled"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_cancel_marker_wins_over_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-failed-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_omx", lambda exe, contract, timeout, log_path: (3, ""))
    runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "cancelled"


def test_cancel_marker_wins_on_exception_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-exc-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    runners._atomic_json(cancel_path, {"task_id": task_id})

    def _raise(name):
        raise LookupError("backend vanished")

    monkeypatch.setattr(runners, "get_backend", _raise)
    assert runners._worker(task_id, root) == 1
    meta = json.loads(meta_path.read_text())
    assert meta["state"] == "cancelled"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_worker_without_cancel_marker_reports_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-ok-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path: (0, "done"))
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0, meta.get("error")
    assert meta["state"] == "completed"
    assert not (root / f"{task_id}.cancel.json").exists()


def test_cancel_arriving_during_terminal_write_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_id = "race-terminal-write-001"
    meta_path, _, _ = runners._job_paths(task_id, tmp_path)
    root = meta_path.parent
    root.mkdir(parents=True, exist_ok=True)
    _make_job(root, task_id)
    cancel_path = root / f"{task_id}.cancel.json"
    monkeypatch.setattr(runners, "get_backend", lambda name: _fake_backend(monkeypatch))
    monkeypatch.setattr(runners, "_worker_pi", lambda exe, contract, timeout, log_path: (0, "done"))
    real_atomic = runners._atomic_json
    injected = {"done": False}

    def _atomic_with_cancel(path, value):
        real_atomic(path, value)
        if path == meta_path and value.get("state") == "completed" and not injected["done"]:
            injected["done"] = True
            real_atomic(cancel_path, {"task_id": task_id, "requested_at": runners._now()})

    monkeypatch.setattr(runners, "_atomic_json", _atomic_with_cancel)
    rc = runners._worker(task_id, root)
    meta = json.loads(meta_path.read_text())
    assert rc == 0
    assert injected["done"] is True
    assert meta["state"] == "cancelled"
    assert meta["outcome"] == "cancelled"
    assert not cancel_path.exists()


class _ExternalBackend:
    name = "external_probe"

    def availability(self, *, hermes_root=None):
        return {"available": True}

    def dispatch(self, *a, **k):
        return {"success": True}

    def observed_runs(self, task_id, *, hermes_root=None):
        return []

    def cancel(self, task_id, *, hermes_root=None):
        return {"success": True}


class _ShadowFleetBackend(_ExternalBackend):
    name = "fleet"


def _fake_entry_points(monkeypatch, candidates):
    class _EP:
        def __init__(self, name, loader):
            self.name = name
            self._loader = loader

        def load(self):
            return self._loader

    class _EPS(dict):
        def select(self, *, group):
            return [_EP(name, loader) for name, loader in candidates.get(group, [])]

    monkeypatch.setattr(runners.importlib.metadata, "entry_points", lambda: _EPS())


def test_plugin_class_entry_point_instantiates(monkeypatch: pytest.MonkeyPatch):
    _fake_entry_points(monkeypatch, {"hermes_gpt.runners": [("external_probe", _ExternalBackend)]})
    loaded = runners.load_entrypoint_backends()
    assert "external_probe" in loaded
    try:
        assert runners.get_backend("external_probe").availability() == {"available": True}
    finally:
        with runners._REGISTRY_LOCK:
            runners._BACKENDS.pop("external_probe", None)


def test_plugin_cannot_shadow_builtin_backend_name(monkeypatch: pytest.MonkeyPatch):
    _fake_entry_points(monkeypatch, {"hermes_gpt.runners": [("fleet", _ShadowFleetBackend)]})
    loaded = runners.load_entrypoint_backends()
    assert loaded == []
    with pytest.raises(LookupError):
        runners.get_backend("__never__")  # registry sanity helper
    # The built-in fleet backend must still be the registered one.
    assert runners.get_backend("fleet").__class__ is runners.FleetBackend
