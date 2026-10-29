import io
import json
import subprocess
from pathlib import Path

import pytest

import operator_session as session


class _ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class _FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.stdout = io.BytesIO((json.dumps({"type": "result", "session_id": argv[3], "exit_code": 0, "text": "mock Hermes response token=secret-value-123456789"}) + "\n").encode())

    def wait(self, timeout=None):
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
        timeout=99999,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
    )
    assert started["success"] is True
    assert len(calls) == 1
    assert Path(calls[0].argv[0]).name.lower() in {"hermes", "hermes.exe"}
    assert calls[0].argv[1:] == ["chat", "--resume", "20260810_143227_6b0982", "-q", prompt, "--format", "stream-json"]
    assert calls[0].kwargs["shell"] is False
    assert calls[0].kwargs["env"]["HERMES_PROFILE"] == "project-manager"
    assert calls[0].kwargs["env"]["HERMES_HOME"] == str(tmp_path / "profiles" / "project-manager")

    status = session.hermes_session_job_status(started["job_id"], tmp_path)
    assert status["job"]["status"] == "completed"
    assert status["job"]["timeout"] == session.MAX_TIMEOUT
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
    assert session.hermes_session_continue("s", "x", timeout=True, hermes_root=tmp_path)["code"] == "INVALID_TIMEOUT"


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


@pytest.fixture(autouse=True)
def isolated_session_processes(monkeypatch):
    monkeypatch.setattr(session, "_active_sessions", {})
    monkeypatch.setattr(session, "_processes", {})
    monkeypatch.setattr(session, "_supports_stream_json", lambda *args: True)


@pytest.mark.parametrize("records,rc,code", [
    ([], 0, "MISSING_TERMINAL_RESULT"),
    ([{"type": "text", "text": "not final"}], 0, "MISSING_TERMINAL_RESULT"),
    ([{"type": "result", "session_id": "wrong", "exit_code": 0, "text": "x"}], 0, "SESSION_MISMATCH"),
    ([{"type": "result", "session_id": "s", "exit_code": True, "text": "x"}], 0, "INVALID_RESULT"),
    ([{"type": "result", "session_id": "s", "exit_code": 0, "text": {}}], 0, "INVALID_RESULT"),
    ([{"type": "result", "session_id": "s", "exit_code": 1, "text": "x"}], 1, "OUTCOME_UNKNOWN"),
    ([{"type": "result", "session_id": "s", "exit_code": 0, "text": "x"}], 1, "OUTCOME_UNKNOWN"),
    ([{"type": "result", "session_id": "s", "exit_code": 0, "text": "x", "error": "read failed"}], 0, "OUTCOME_UNKNOWN"),
    ([{"type": "result", "session_id": "s", "exit_code": 0, "text": "x"}] * 2, 0, "AFTER_TERMINAL_RESULT"),
    ([[]], 0, "INVALID_STREAM"),
])
def test_unknown_result_blocks_resubmission(monkeypatch, tmp_path, records, rc, code):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(session, "_terminate", lambda proc: None)
    def spawn(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        proc.stdout = io.BytesIO(("\n".join(json.dumps(r) for r in records)).encode())
        def wait(timeout=None):
            proc.returncode = rc
        proc.wait = wait
        return proc
    monkeypatch.setattr(session.subprocess, "Popen", spawn)
    job = session.hermes_session_continue("s", "private", hermes_root=tmp_path)
    result = session.hermes_session_job_result(job["job_id"], hermes_root=tmp_path)
    assert result["status"] == "failed"
    assert result["outcome_unknown"] and not result["retry_safe"]
    assert result["result_code"] == code
    assert result["response"] == ""
    session._active_sessions.clear()  # another process/restart still sees the reservation
    assert session.hermes_session_continue("s", "again", hermes_root=tmp_path)["code"] == "SESSION_BUSY"


def test_no_spawn_if_intent_cannot_persist(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session, "_save", lambda *args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kw: pytest.fail("spawned"))
    assert session.hermes_session_continue("s", "private", hermes_root=tmp_path)["code"] == "SESSION_STATE_UNWRITABLE"
    assert not session._reservation("s", "default", tmp_path).exists()


def test_post_spawn_persistence_failure_retains_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    real_save = session._save
    def save(meta, root):
        if meta["status"] == "running":
            raise OSError()
        real_save(meta, root)
    monkeypatch.setattr(session, "_save", save)
    monkeypatch.setattr(session.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(session, "_terminate", lambda proc: None)
    result = session.hermes_session_continue("s", "private", hermes_root=tmp_path)
    assert result["outcome_unknown"] and not result["retry_safe"]
    assert session._reservation("s", "default", tmp_path).exists()
    assert session.hermes_session_job_status(result["job_id"], tmp_path)["job"]["status"] == "orphaned"


def test_unsupported_cli_does_not_submit(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session, "_supports_stream_json", lambda *args: False)
    monkeypatch.setattr(session.subprocess, "Popen", lambda *args, **kw: pytest.fail("spawned"))
    assert session.hermes_session_continue("s", "private", hermes_root=tmp_path)["code"] == "SESSION_CLI_UNSUPPORTED"
    assert not session._root(tmp_path).exists()


@pytest.mark.parametrize("payload,expected", [(b"not json", "INVALID_STREAM"), (b"x" * (session.MAX_LINE_BYTES + 1), "OUTPUT_LIMIT")])
def test_protocol_bounds(monkeypatch, payload, expected):
    proc = _FakeProcess(["hermes", "chat", "--resume", "s"])
    proc.stdout = io.BytesIO(payload)
    killed = []
    monkeypatch.setattr(session, "_terminate", lambda p: killed.append(p))
    result = {}
    session._consume(proc, "s", result)
    assert result == {"error": expected}
    assert killed == [proc]


def test_timeout_is_unknown_and_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, '1')
    monkeypatch.setattr(session.threading, 'Thread', _ImmediateThread)
    monkeypatch.setattr(session, '_terminate', lambda proc: None)
    calls = []
    def spawn(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        def wait(timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout)
        proc.wait = wait
        calls.append(proc)
        return proc
    monkeypatch.setattr(session.subprocess, 'Popen', spawn)
    job = session.hermes_session_continue('s', 'private', hermes_root=tmp_path)
    result = session.hermes_session_job_result(job['job_id'], hermes_root=tmp_path)
    assert result['status'] == 'timed_out'
    assert result['outcome_unknown'] and not result['retry_safe']
    assert result['response'] == ''
    assert len(calls) == 1


def test_stream_tool_bodies_are_not_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, '1')
    monkeypatch.setenv('HERMES_YOLO_MODE', '1')
    monkeypatch.setattr(session.threading, 'Thread', _ImmediateThread)
    def spawn(argv, **kwargs):
        assert 'HERMES_YOLO_MODE' not in kwargs['env']
        assert session._load(next(tmp_path.joinpath('session-jobs').glob('*.json')).stem, tmp_path)['status'] == 'starting'
        proc = _FakeProcess(argv, **kwargs)
        proc.stdout = io.BytesIO(b'{"type":"tool_use","input":{"prompt":"do-not-store-this"}}\n' + proc.stdout.getvalue())
        return proc
    monkeypatch.setattr(session.subprocess, 'Popen', spawn)
    job = session.hermes_session_continue('s', 'do-not-store-this', hermes_root=tmp_path)
    assert session.hermes_session_job_result(job['job_id'], hermes_root=tmp_path)['status'] == 'completed'
    assert all('do-not-store-this' not in p.read_text() for p in session._root(tmp_path).glob('*') if p.is_file())
    assert not session._reservation('s', 'default', tmp_path).exists()


def test_spawn_failure_releases_only_unsubmitted_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, '1')
    def spawn(*args, **kwargs):
        raise OSError('no executable')
    monkeypatch.setattr(session.subprocess, 'Popen', spawn)
    assert session.hermes_session_continue('s', 'private', hermes_root=tmp_path)['code'] == 'HERMES_START_FAILED'
    assert not session._reservation('s', 'default', tmp_path).exists()
