import json
import os
from pathlib import Path

import operator_codex as oc


def _fake_codex(path: Path, version: str = "0.50.0", *, exit_code: int = 0) -> Path:
    """Create a platform-appropriate fake `codex` command that answers --version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(f"@echo off\r\necho codex {version}\r\nexit /b {exit_code}\r\n", encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\necho 'codex {version}'\nexit {exit_code}\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def enable(monkeypatch, root: Path, *, write: bool = False):
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ENABLED", "1")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_LEVEL", "workspace")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_APPLY_MODE", "direct")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PATHS", str(root))
    monkeypatch.setenv(oc.ENABLE_CODEX_RUNNER_ENV, "1")
    if write:
        monkeypatch.setenv(oc.ALLOW_CODEX_WRITE_ENV, "1")


def test_status_and_dry_run_plan(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    assert oc.hermes_codex_status(tmp_path)["enabled"] is True
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is True and plan["dry_run"] is True
    assert "inspect tests" not in json.dumps(plan)
    assert plan["argv"][-1] == "<prompt>"
    assert plan["execution_mode"] == "normal"


def test_gates_and_fixed_arguments(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    assert oc.hermes_codex_start("change", str(tmp_path), sandbox="workspace-write")["code"] == "WRITE_DISABLED"
    assert oc.hermes_codex_plan("change", str(tmp_path), sandbox="workspace-write", execution_mode="nolo")["code"] == "WRITE_DISABLED"
    nolo_read_only = oc.hermes_codex_plan("inspect", str(tmp_path), execution_mode="nolo")
    assert nolo_read_only["success"] is True
    assert nolo_read_only["argv"][nolo_read_only["argv"].index("-a") + 1] == "never"
    assert oc.hermes_codex_plan("x", str(tmp_path), sandbox="danger-full-access")["code"] == "INVALID_SANDBOX"
    assert oc.hermes_codex_plan("x", str(tmp_path.parent / "outside"))["code"] == "POLICY_REFUSED"


def test_execution_modes_and_nolo_policy(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, write=True)
    exe = _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(exe))

    normal = oc.hermes_codex_plan("inspect", str(tmp_path))
    assert normal["execution_mode"] == "normal"
    assert "--dangerously-bypass-approvals-and-sandbox" not in normal["argv"]
    assert normal["argv"][normal["argv"].index("-s") + 1] == "read-only"

    nolo = oc.hermes_codex_plan("inspect", str(tmp_path), execution_mode="NOLO")
    assert nolo["execution_mode"] == "nolo"
    assert "--dangerously-bypass-approvals-and-sandbox" not in nolo["argv"]
    assert nolo["argv"].index("-a") < nolo["argv"].index("exec")
    assert nolo["argv"][nolo["argv"].index("-s") + 1] == "read-only"
    assert nolo["argv"][nolo["argv"].index("-a") + 1] == "never"

    nolo_write = oc.hermes_codex_plan("change", str(tmp_path), sandbox="workspace-write", execution_mode="nolo")
    assert nolo_write["execution_mode"] == "nolo"
    assert nolo_write["argv"][nolo_write["argv"].index("-s") + 1] == "workspace-write"
    assert nolo_write["argv"][nolo_write["argv"].index("-a") + 1] == "never"

    assert oc.hermes_codex_plan("x", str(tmp_path), execution_mode="anything")["code"] == "INVALID_EXECUTION_MODE"
    assert oc.hermes_codex_plan("x", str(tmp_path.parent / "outside"), execution_mode="nolo")["code"] == "POLICY_REFUSED"
    assert oc.hermes_codex_start("x", str(tmp_path), execution_mode="nolo", confirm=False, dry_run=False)["code"] == "DIRECT_CONFIRMATION_REQUIRED"


def test_nolo_job_metadata_and_audit(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, write=True)
    exe = _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(exe))
    monkeypatch.setattr(oc, "resolve_codex_exe", lambda: {
        "path": str(exe.resolve()),
        "source": "env",
        "version": "codex 0.50.0",
        "reason": None,
        "skipped": [],
    })
    captured = {}

    class FakeProc:
        pid = 12345
        returncode = None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    class ImmediateThread:
        def __init__(self, target, args, daemon=False):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(oc.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(oc.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(oc.op, "audit_record", lambda **kwargs: captured.update(kwargs) or kwargs)

    started = oc.hermes_codex_start("harmless", str(tmp_path), execution_mode="nolo", confirm=True, dry_run=False, hermes_root=tmp_path)
    assert started["success"] is True
    assert started["execution_mode"] == "nolo"
    meta = oc._load(started["job_id"], tmp_path)
    assert meta is not None and meta["execution_mode"] == "nolo"
    assert captured["extra"]["execution_mode"] == "nolo"
    result = oc.hermes_codex_job_result(started["job_id"], hermes_root=tmp_path)
    assert result["execution_mode"] == "nolo"


def test_result_redacts_and_bounds(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    job_id = "a" * 32
    oc._save({"job_id": job_id, "status": "completed", "return_code": 0, "workdir": str(tmp_path), "sandbox": "read-only"}, tmp_path)
    _, output = oc._paths(job_id, tmp_path)
    output.write_text(json.dumps({"thread_id": "t1", "message": "token=secret-token-123456789 " + "x" * 2000, "usage": {"input_tokens": 1}}) + "\n", encoding="utf-8")
    result = oc.hermes_codex_job_result(job_id, 500, tmp_path)
    assert "secret-token" not in result["response"]
    assert result["truncated"] is True and result["thread_id"] == "t1"


def test_metadata_never_contains_prompt(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, write=True)
    _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    prompt = "private task body"
    plan = oc.hermes_codex_start(prompt, str(tmp_path), dry_run=True, hermes_root=tmp_path)
    assert prompt not in json.dumps(plan)
    assert plan["prompt_len"] == len(prompt)


# ---------------------------------------------------------------------------
# Codex executable resolution (HERMES_GPT_CODEX_EXE override + PATH scanning)
# ---------------------------------------------------------------------------

def test_override_exe_is_used_for_plan(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    exe = _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(exe))
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is True
    assert plan["argv"][0] == str(exe)


def test_override_exe_must_exist(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    missing = tmp_path / "missing" / "codex"
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(missing))
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is False
    assert plan["code"] == "CODEX_EXE_UNAVAILABLE"


def test_override_exe_rejects_protected_windows_apps(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    protected = _fake_codex(tmp_path / "Program Files" / "WindowsApps" / "codex.exe")
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(protected))
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is False
    assert plan["code"] == "CODEX_EXE_UNAVAILABLE"
    assert "WindowsApps" in plan["safe_message"]


def test_override_exe_rejects_unlaunchable_probe(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    broken = _fake_codex(tmp_path / "bin" / "codex", exit_code=1)
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(broken))
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is False
    assert plan["code"] == "CODEX_EXE_UNAVAILABLE"


def test_path_scan_skips_protected_windows_apps_candidate(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    protected = _fake_codex(tmp_path / "WindowsApps" / "codex")
    good = _fake_codex(tmp_path / "bin" / "codex")
    # PATH order: WindowsApps first (the failure mode), then the standalone CLI.
    monkeypatch.setenv("PATH", os.pathsep.join([str(protected.parent), str(good.parent)]))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is True
    assert plan["argv"][0] == str(good)


def test_path_scan_skips_unlaunchable_first_candidate(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    broken = _fake_codex(tmp_path / "bin1" / "codex", exit_code=1)
    good = _fake_codex(tmp_path / "bin2" / "codex")
    monkeypatch.setenv("PATH", os.pathsep.join([str(broken.parent), str(good.parent)]))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    plan = oc.hermes_codex_plan("inspect tests", str(tmp_path))
    assert plan["success"] is True
    assert plan["argv"][0] == str(good)


def test_status_reports_resolved_path_and_source(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    exe = _fake_codex(tmp_path / "bin" / "codex")
    monkeypatch.setenv(oc.CODEX_EXE_ENV, str(exe))
    status = oc.hermes_codex_status(tmp_path)
    assert status["codex_available"] is True
    assert status["codex_path"] == str(exe)
    assert status["codex_source"] == "env"


def test_status_reports_protected_candidate_as_unavailable(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    protected = _fake_codex(tmp_path / "WindowsApps" / "codex")
    monkeypatch.setenv("PATH", str(protected.parent))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    status = oc.hermes_codex_status(tmp_path)
    assert status["codex_available"] is False
    assert status["codex_path"] is None
    assert status["codex_reason"]
    assert "WindowsApps" in status["codex_reason"]


def test_status_reports_missing_codex(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nobins"))
    monkeypatch.delenv(oc.CODEX_EXE_ENV, raising=False)
    status = oc.hermes_codex_status(tmp_path)
    assert status["codex_available"] is False
    assert status["codex_path"] is None
    assert status["codex_reason"]
