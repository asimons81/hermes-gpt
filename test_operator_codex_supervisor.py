import os
import subprocess
import sys

import operator_codex as oc
import operator_job_supervisor as jobs


def _enable_codex_policy(monkeypatch, root):
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ENABLED", "1")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_LEVEL", "workspace")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_APPLY_MODE", "direct")
    monkeypatch.setenv("HERMES_GPT_OPERATOR_ALLOWED_PATHS", str(root))
    monkeypatch.setenv(oc.ENABLE_CODEX_RUNNER_ENV, "1")


def test_codex_worker_owns_running_and_terminal_metadata(tmp_path):
    job_id = "b" * 32
    meta_path, output_path = oc._paths(job_id, tmp_path)
    oc._save(
        {
            "job_id": job_id,
            "status": "queued",
            "created_at": oc._now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "return_code": None,
            "cancel_requested": False,
            "workdir": str(tmp_path),
            "sandbox": "read-only",
            "execution_mode": "normal",
        },
        tmp_path,
    )
    oc._save_request(
        job_id,
        {
            "argv": [
                sys.executable,
                "-c",
                "import json; print(json.dumps({'message':'done','usage':{'input_tokens':1}}))",
            ],
            "workdir": str(tmp_path),
            "timeout": 10,
        },
        tmp_path,
    )
    jobs.register_job(
        job_id,
        backend="codex",
        workspace=tmp_path,
        log_path=output_path,
        source_record=meta_path,
        hermes_root=tmp_path,
    )

    rc = oc._worker(job_id, oc._root(tmp_path))

    assert rc == 0
    meta = oc._load(job_id, tmp_path)
    assert meta is not None
    assert meta["status"] == "completed"
    assert meta["return_code"] == 0
    assert meta["pid"] == os.getpid()
    durable = jobs.get_job(job_id, hermes_root=tmp_path, reconcile=False)
    assert durable is not None
    assert durable["status"] == "completed"
    assert durable["returncode"] == 0
    result = oc.hermes_codex_job_result(job_id, hermes_root=tmp_path)
    assert result["status"] == "completed"
    assert result["response"] == "done"
    assert result["usage"] == {"input_tokens": 1}
    assert not oc._request_path(job_id, tmp_path).exists()


def test_codex_cancel_finished_job_is_a_true_noop(monkeypatch, tmp_path):
    _enable_codex_policy(monkeypatch, tmp_path)
    job_id = "c" * 32
    meta_path, output_path = oc._paths(job_id, tmp_path)
    oc._save(
        {
            "job_id": job_id,
            "status": "completed",
            "created_at": oc._now(),
            "started_at": oc._now(),
            "ended_at": oc._now(),
            "pid": None,
            "return_code": 0,
            "cancel_requested": False,
            "workdir": str(tmp_path),
            "sandbox": "read-only",
            "execution_mode": "normal",
        },
        tmp_path,
    )
    jobs.register_job(
        job_id,
        backend="codex",
        workspace=tmp_path,
        log_path=output_path,
        source_record=meta_path,
        status="completed",
        hermes_root=tmp_path,
    )
    jobs.terminalize(job_id, "completed", returncode=0, hermes_root=tmp_path)

    result = oc.hermes_codex_cancel(
        job_id,
        confirm=True,
        dry_run=False,
        hermes_root=tmp_path,
    )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["status"] == "completed"
    meta = oc._load(job_id, tmp_path)
    assert meta is not None
    assert meta["status"] == "completed"
    assert meta["cancel_requested"] is False


def test_windows_codex_timeout_uses_taskkill_tree(monkeypatch):
    calls = []

    class FakeProc:
        pid = 7777

        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    proc = FakeProc()

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    with monkeypatch.context() as patch:
        patch.setattr(oc.os, "name", "nt")
        patch.setattr(oc.subprocess, "run", fake_run)
        oc._terminate(proc)

    assert calls == [["taskkill", "/PID", "7777", "/T", "/F"]]
    assert proc.killed is False
