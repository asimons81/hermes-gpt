import json
import subprocess
from pathlib import Path

import operator_session as session


class _ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class _FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None

    def wait(self, timeout=None):
        self.kwargs["stdout"].write("mock Hermes response token=secret-value-123456789")
        self.kwargs["stdout"].flush()
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def test_session_control_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    result = session.hermes_session_continue("session-1", "hello", hermes_root=tmp_path)
    assert result["success"] is False
    assert result["code"] == "SESSION_CONTROL_DISABLED"


def test_mocked_continue_status_and_result(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session.threading, "Thread", _ImmediateThread)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(session.subprocess, "Popen", fake_popen)
    prompt = "private follow-up prompt"
    started = session.hermes_session_continue(
        "20260810_143227_6b0982",
        prompt,
        max_job_runtime_seconds=99999,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
    )
    assert started["success"] is True
    assert len(calls) == 1
    assert Path(calls[0].argv[0]).name.lower() in {"hermes", "hermes.exe"}
    assert calls[0].argv[1:] == ["--resume", "20260810_143227_6b0982", "--oneshot", prompt]
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["env"]["HERMES_PROFILE"] == "project-manager"
    assert calls[0].kwargs["env"]["HERMES_HOME"] == str(tmp_path / "profiles" / "project-manager")

    status = session.hermes_session_job_status(started["job_id"], tmp_path)
    assert status["job"]["status"] == "completed"
    assert status["job"]["timeout"] == session.MAX_JOB_RUNTIME_SECONDS
    assert status["job"]["max_job_runtime_seconds"] == session.MAX_JOB_RUNTIME_SECONDS
    assert status["job"]["profile"] == "project-manager"
    metadata_text = json.dumps(status)
    assert prompt not in metadata_text
    assert status["job"]["prompt_len"] == len(prompt)

    result = session.hermes_session_job_result(started["job_id"], 500, tmp_path)
    assert result["status"] == "completed"
    assert result["return_code"] == 0
    assert "secret-value" not in result["response"]
    assert "[REDACTED]" in result["response"]


def test_job_lookup_and_input_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_job_status("not-a-job", tmp_path)["code"] == "JOB_NOT_FOUND"
    assert session.hermes_session_continue("s", "", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"
    assert session.hermes_session_continue(
        "s", "x" * (session.MAX_PROMPT_CHARS + 1), hermes_root=tmp_path
    )["code"] == "PROMPT_TOO_LARGE"
    assert session.hermes_session_continue("s", "x", max_job_runtime_seconds=True, hermes_root=tmp_path)["code"] == "INVALID_MAX_JOB_RUNTIME_SECONDS"


def test_same_session_cannot_run_concurrently(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setitem(session._active_sessions, "default:session-1", "b" * 32)
    result = session.hermes_session_continue("session-1", "next", hermes_root=tmp_path)
    assert result["code"] == "SESSION_BUSY"


def test_reconcile_marks_unowned_running_job_orphaned(tmp_path):
    job_id = "a" * 32
    session._save({"job_id": job_id, "session_id": "s", "status": "running"}, tmp_path)
    result = session.hermes_session_job_status(job_id, tmp_path)
    assert result["job"]["status"] == "orphaned"
    assert "ownership" in result["job"]["reconciliation"]


def test_job_wait_returns_early_on_terminal_state(tmp_path):
    job_id = "c" * 32
    session._save(
        {"job_id": job_id, "session_id": "s", "profile": "dev", "status": "completed", "return_code": 0},
        tmp_path,
    )
    result = session.hermes_session_job_wait(job_id, wait_seconds=5, hermes_root=tmp_path)
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["return_code"] == 0
    assert result["await"]["timed_out"] is False


def test_job_wait_times_out_on_nonterminal_state(monkeypatch, tmp_path):
    job_id = "d" * 32
    monkeypatch.setattr(session, "_reconcile", lambda *a, **k: None)
    session._save({"job_id": job_id, "session_id": "s", "status": "running"}, tmp_path)
    result = session.hermes_session_job_wait(job_id, wait_seconds=0, hermes_root=tmp_path)
    assert result["success"] is True
    assert result["status"] == "running"
    assert result["await"]["timed_out"] is True


def test_job_wait_clamps_seconds(tmp_path):
    job_id = "e" * 32
    session._save({"job_id": job_id, "session_id": "s", "status": "failed"}, tmp_path)
    result = session.hermes_session_job_wait(job_id, wait_seconds=99999, hermes_root=tmp_path)
    assert result["status"] == "failed"
    assert result["await"]["wait_seconds"] == session.MAX_JOB_WAIT_SECONDS

    result_bad = session.hermes_session_job_wait(job_id, wait_seconds="junk", hermes_root=tmp_path)
    assert result_bad["status"] == "failed"
    assert result_bad["await"]["wait_seconds"] == session.MAX_JOB_WAIT_SECONDS


def test_job_wait_missing_job(tmp_path):
    result = session.hermes_session_job_wait("f" * 32, wait_seconds=0, hermes_root=tmp_path)
    assert result["code"] == "JOB_NOT_FOUND"
