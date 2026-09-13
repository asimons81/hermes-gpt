import json
import os
import subprocess
import sys
import threading
import time

import operator_job_supervisor as jobs


def test_process_identity_matches_current_process():
    identity = jobs.process_identity(os.getpid())
    assert identity is not None
    assert identity["start_token"]
    assert identity["cmdline_sha256"]
    assert jobs.verify_process(os.getpid(), identity) is True
    wrong = dict(identity)
    wrong["start_token"] = "definitely-not-this-process"
    assert jobs.verify_process(os.getpid(), wrong) is False


def test_terminalization_is_atomic_and_idempotent(tmp_path):
    jobs.register_job(
        "task-race",
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / "task-race.jsonl",
        source_record=tmp_path / "runner-jobs" / "task-race.json",
        hermes_root=tmp_path,
    )

    results = []

    def finish(state):
        results.append(jobs.terminalize("task-race", state, returncode=0, hermes_root=tmp_path))

    threads = [threading.Thread(target=finish, args=("completed",)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = jobs.get_job("task-race", hermes_root=tmp_path, reconcile=False)
    assert final is not None
    assert final["status"] == "completed"
    assert final["finalization_version"] == 1
    assert all(item["status"] == "completed" for item in results)


def test_detached_worker_terminal_state_survives_launcher_exit(tmp_path):
    """Acceptance: a server-like launcher may die while a detached worker finishes."""
    job_id = "restart-acceptance"
    log_path = tmp_path / "runner-jobs" / f"{job_id}.jsonl"
    source_path = tmp_path / "runner-jobs" / f"{job_id}.json"
    jobs.register_job(
        job_id,
        backend="acceptance",
        workspace=tmp_path,
        log_path=log_path,
        source_record=source_path,
        hermes_root=tmp_path,
    )

    worker_path = tmp_path / "detached_worker.py"
    worker_path.write_text(
        """import os
import sys
import time
from pathlib import Path

import operator_job_supervisor as jobs

root = Path(sys.argv[1])
job_id = sys.argv[2]
jobs.mark_running(job_id, os.getpid(), hermes_root=root)
time.sleep(0.25)
jobs.terminalize(job_id, "completed", returncode=0, summary="done", hermes_root=root)
""",
        encoding="utf-8",
    )
    launcher_path = tmp_path / "launcher.py"
    launcher_path.write_text(
        """import os
import subprocess
import sys

subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
os._exit(0)
""",
        encoding="utf-8",
    )

    child_env = os.environ.copy()
    module_root = os.path.dirname(os.path.abspath(jobs.__file__))
    existing_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (module_root, existing_pythonpath) if part
    )
    parent = subprocess.Popen(
        [sys.executable, str(launcher_path), str(worker_path), str(tmp_path), job_id],
        env=child_env,
    )
    assert parent.wait(timeout=5) == 0

    deadline = time.monotonic() + 5
    observed = None
    while time.monotonic() < deadline:
        observed = jobs.get_job(job_id, hermes_root=tmp_path, reconcile=False)
        if observed and observed.get("status") == "completed":
            break
        time.sleep(0.05)

    assert observed is not None
    assert observed["status"] == "completed"
    assert observed["returncode"] == 0
    assert observed["result_summary"] == "done"


def test_pid_reuse_mismatch_never_signals(monkeypatch, tmp_path):
    jobs.register_job(
        "task-reused",
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / "task-reused.jsonl",
        source_record=tmp_path / "runner-jobs" / "task-reused.json",
        hermes_root=tmp_path,
    )
    record_path = tmp_path / "job-supervisor" / "task-reused.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(
        {
            "status": "running",
            "pid": 424242,
            "process_identity": {
                "platform": "procfs",
                "start_token": "111",
                "cmdline_sha256": "a" * 64,
            },
        }
    )
    jobs._atomic_json(record_path, record)

    monkeypatch.setattr(jobs, "verify_process", lambda pid, expected: False)
    signalled = []
    monkeypatch.setattr(jobs.os, "killpg", lambda *args: signalled.append(args))
    result = jobs.request_cancel("task-reused", hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "JOB_PROCESS_UNVERIFIABLE"
    assert signalled == []
    observed = jobs.get_job("task-reused", hermes_root=tmp_path, reconcile=True)
    assert observed is not None
    assert observed["status"] == "running"
    assert observed["process_verification"] == "mismatch"


def test_cancel_reverifies_identity_immediately_before_signal(monkeypatch, tmp_path):
    job_id = "task-cancel-race"
    jobs.register_job(
        job_id,
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / f"{job_id}.jsonl",
        source_record=tmp_path / "runner-jobs" / f"{job_id}.json",
        hermes_root=tmp_path,
    )
    record_path = tmp_path / "job-supervisor" / f"{job_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(
        {
            "status": "running",
            "pid": 5151,
            "process_identity": {
                "platform": "procfs",
                "start_token": "222",
                "cmdline_sha256": "d" * 64,
            },
        }
    )
    jobs._atomic_json(record_path, record)

    checks = iter([True, False])
    monkeypatch.setattr(jobs, "verify_process", lambda pid, expected: next(checks))
    signalled = []
    monkeypatch.setattr(jobs.os, "killpg", lambda *args: signalled.append(args))

    result = jobs.request_cancel(job_id, hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "JOB_PROCESS_UNVERIFIABLE"
    assert signalled == []
    stored = jobs.get_job(job_id, hermes_root=tmp_path, reconcile=False)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["cancel_requested"] is False
    assert not (tmp_path / "job-supervisor" / f"{job_id}.cancel.json").exists()


def test_cancel_signal_failure_never_publishes_cancelled(monkeypatch, tmp_path):
    job_id = "task-cancel-fail"
    jobs.register_job(
        job_id,
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / f"{job_id}.jsonl",
        source_record=tmp_path / "runner-jobs" / f"{job_id}.json",
        hermes_root=tmp_path,
    )
    record_path = tmp_path / "job-supervisor" / f"{job_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(
        {
            "status": "running",
            "pid": 6161,
            "process_identity": {
                "platform": "procfs",
                "start_token": "333",
                "cmdline_sha256": "e" * 64,
            },
        }
    )
    jobs._atomic_json(record_path, record)

    monkeypatch.setattr(jobs, "verify_process", lambda pid, expected: True)

    def refuse_signal(*args, **kwargs):
        raise PermissionError("no signal permission")

    monkeypatch.setattr(jobs.os, "killpg", refuse_signal)
    result = jobs.request_cancel(job_id, hermes_root=tmp_path)

    assert result["success"] is False
    assert result["code"] == "JOB_CANCEL_SIGNAL_FAILED"
    stored = jobs.get_job(job_id, hermes_root=tmp_path, reconcile=False)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["cancel_requested"] is False
    assert not (tmp_path / "job-supervisor" / f"{job_id}.cancel.json").exists()


def test_log_cursor_has_no_duplicates_or_skips(tmp_path):
    log_path = tmp_path / "runner-jobs" / "task-cursor.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes(b'{"n":1}\r\n{"n":2}\r\n{"n":3}\r\n')
    jobs.register_job(
        "task-cursor",
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=log_path,
        source_record=tmp_path / "runner-jobs" / "task-cursor.json",
        hermes_root=tmp_path,
    )

    first = jobs.status_payload("task-cursor", cursor=0, max_lines=2, hermes_root=tmp_path)
    assert [item["n"] for item in first["log"]["lines"]] == [1, 2]
    second = jobs.status_payload(
        "task-cursor",
        cursor=first["log"]["next_cursor"],
        max_lines=2,
        hermes_root=tmp_path,
    )
    assert [item["n"] for item in second["log"]["lines"]] == [3]
    assert second["log"]["next_cursor"] == log_path.stat().st_size


def test_wait_returns_early_on_terminal_state(tmp_path):
    jobs.register_job(
        "task-wait",
        backend="omx",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / "task-wait.jsonl",
        source_record=tmp_path / "runner-jobs" / "task-wait.json",
        hermes_root=tmp_path,
    )

    def finish():
        time.sleep(0.05)
        jobs.terminalize("task-wait", "completed", returncode=0, hermes_root=tmp_path)

    thread = threading.Thread(target=finish)
    thread.start()
    started = time.monotonic()
    payload = jobs.wait_payload("task-wait", wait_seconds=2, hermes_root=tmp_path)
    elapsed = time.monotonic() - started
    thread.join()

    assert payload["job"]["status"] == "completed"
    assert payload["wait"]["timed_out"] is False
    assert elapsed < 1.5


def test_wait_timeout_returns_running_state(monkeypatch, tmp_path):
    jobs.register_job(
        "task-running",
        backend="omx",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / "task-running.jsonl",
        source_record=tmp_path / "runner-jobs" / "task-running.json",
        hermes_root=tmp_path,
    )
    record_path = tmp_path / "job-supervisor" / "task-running.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(
        {
            "status": "running",
            "pid": 12345,
            "process_identity": {"platform": "procfs", "start_token": "1", "cmdline_sha256": "b" * 64},
        }
    )
    jobs._atomic_json(record_path, record)
    monkeypatch.setattr(jobs, "verify_process", lambda pid, expected: True)

    payload = jobs.wait_payload("task-running", wait_seconds=0, hermes_root=tmp_path)
    assert payload["job"]["status"] == "running"
    assert payload["wait"]["timed_out"] is True


def test_windows_cancel_uses_taskkill_only_after_identity_verification(monkeypatch, tmp_path):
    jobs.register_job(
        "task-win-cancel",
        backend="pi_rpc",
        workspace=tmp_path,
        log_path=tmp_path / "runner-jobs" / "task-win-cancel.jsonl",
        source_record=tmp_path / "runner-jobs" / "task-win-cancel.json",
        hermes_root=tmp_path,
    )
    record_path = tmp_path / "job-supervisor" / "task-win-cancel.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(
        {
            "status": "running",
            "pid": 4343,
            "process_identity": {
                "platform": "windows-cim",
                "start_token": "20260824010101.000000+000",
                "cmdline_sha256": "c" * 64,
            },
        }
    )
    jobs._atomic_json(record_path, record)
    # Exercise the Windows taskkill branch on a Linux CI host without also
    # swapping the host's file-lock implementation to msvcrt.
    monkeypatch.setattr(jobs, "_record_lock", lambda *args, **kwargs: jobs.contextlib.nullcontext())
    monkeypatch.setattr(jobs, "IS_WINDOWS", True)
    monkeypatch.setattr(jobs, "verify_process", lambda pid, expected: True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return jobs.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    result = jobs.request_cancel("task-win-cancel", hermes_root=tmp_path)

    assert result["success"] is True
    assert result["status"] == "cancelled"
    assert calls == [["taskkill", "/PID", "4343", "/T", "/F"]]
    stored = jobs.get_job("task-win-cancel", hermes_root=tmp_path, reconcile=False)
    assert stored is not None
    assert stored["status"] == "cancelled"
