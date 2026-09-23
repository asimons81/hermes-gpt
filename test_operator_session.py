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


# ---------------------------------------------------------------------------
# hermes_session_create — validation cases
# ---------------------------------------------------------------------------


class _FakeSessionDB:
    """In-memory stand-in for hermes_state.SessionDB write surface."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created = []
        self.titled = []

    def create_session(self, session_id, source="cli", **kwargs):
        self.created.append((session_id, source))
        return session_id

    def set_session_title(self, session_id, title):
        self.titled.append((session_id, title))
        return True

    def close(self):
        pass


def test_session_create_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(session.ENABLE_SESSION_CONTROL_ENV, raising=False)
    result = session.hermes_session_create("new session prompt", hermes_root=tmp_path)
    assert result["success"] is False
    assert result["code"] == "SESSION_CONTROL_DISABLED"


def test_session_create_requires_nonempty_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_create("", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"
    assert session.hermes_session_create("   ", hermes_root=tmp_path)["code"] == "INVALID_PROMPT"


def test_session_create_prompt_size_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create(
        "x" * (session.MAX_PROMPT_CHARS + 1), hermes_root=tmp_path
    )
    assert result["code"] == "PROMPT_TOO_LARGE"


def test_session_create_rejects_bool_or_junk_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    assert session.hermes_session_create(
        "p", max_job_runtime_seconds=True, hermes_root=tmp_path
    )["code"] == "INVALID_MAX_JOB_RUNTIME_SECONDS"


def test_session_create_rejects_invalid_profile(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create("p", profile="bad:profile", hermes_root=tmp_path)
    assert result["code"] == "INVALID_PROFILE"


def test_session_create_rejects_overlong_title(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    result = session.hermes_session_create(
        "p", title="t" * (session.MAX_SESSION_TITLE_CHARS + 1), hermes_root=tmp_path
    )
    assert result["code"] == "INVALID_TITLE"


# ---------------------------------------------------------------------------
# hermes_session_create — distinct session + async shape
# ---------------------------------------------------------------------------


def test_session_create_builds_new_distinct_session(monkeypatch, tmp_path):
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session.threading, "Thread", _ImmediateThread)
    fake_db = _FakeSessionDB()
    monkeypatch.setattr(session, "SessionDB", lambda **kw: fake_db)
    calls = []

    def fake_popen(argv, **kwargs):
        proc = _FakeProcess(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(session.subprocess, "Popen", fake_popen)
    prompt = "first work of the new session"
    started = session.hermes_session_create(
        prompt,
        max_job_runtime_seconds=99999,
        hermes_root=tmp_path,
        agent_root=tmp_path / "agent",
        profile="project-manager",
        title="My fresh session",
    )
    # async shape
    assert started["success"] is True
    assert set(started) >= {"job_id", "session_id", "profile", "status"}
    assert started["profile"] == "project-manager"
    assert started["status"] == "running"
    # a new session was created in the DB, distinct from any caller-supplied id
    assert len(fake_db.created) == 1
    new_sid, source = fake_db.created[0]
    assert new_sid == started["session_id"]
    assert source == session.SESSION_CREATE_SOURCE
    # distinct session id pattern (ts_uuid6)
    import re as _re
    assert _re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", new_sid)
    # title recorded
    assert fake_db.titled == [(new_sid, "My fresh session")]
    # CLI resumed the newly created session for the first work
    assert len(calls) == 1
    assert calls[0].argv[1:] == ["--resume", new_sid, "--oneshot", prompt]

    # job tracked to completion and readable by wait/result
    waited = session.hermes_session_job_wait(started["job_id"], 5, tmp_path)
    assert waited["success"] is True
    result = session.hermes_session_job_result(started["job_id"], 500, tmp_path)
    assert result["status"] == "completed"
    assert "secret-value" not in result["response"]


def test_session_create_does_not_accept_arbitrary_profile(monkeypatch, tmp_path):
    # profile is restricted (validated) the same way hermes_session_continue is
    monkeypatch.setenv(session.ENABLE_SESSION_CONTROL_ENV, "1")
    monkeypatch.setattr(session, "SessionDB", lambda **kw: _FakeSessionDB())
    result = session.hermes_session_create(
        "p", profile="../../etc/passwd", hermes_root=tmp_path
    )
    assert result["code"] == "INVALID_PROFILE"
