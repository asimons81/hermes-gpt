"""Regression: reconcile_job must terminalize a zombie worker (item 254).

The spawner detaches autopilot workers (start_new_session) and never waitpid()s
them. When a worker is SIGTERM'd or crashes, it becomes a persistent zombie
under the spawner: /proc/<pid>/cmdline reads empty (0 bytes), so
verify_process cannot establish identity (None → "unavailable", refusing
process control) and _pid_exists says True — reconcile_job recorded the job
as forever "unverifiable", which wedged hermes_autopilot_start's idempotent
early-return (run record non-terminal → no respawn) for hours in production.

A zombie has provably exited: procfs state 'Z' is authoritative, the process
can never publish a terminal result again, and classifying it
exited-unfinalized (same as a vanished pid) is the fail-closed correct
outcome.
"""

from __future__ import annotations

from pathlib import Path

import operator_job_supervisor as job_supervisor


def _registered_running_job(tmp_path: Path, monkeypatch) -> tuple[str, dict]:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    job_id = "autopilot:msn-ap-zombietest:1"
    record = job_supervisor.register_job(
        job_id,
        backend="autopilot",
        workspace=tmp_path / "hermes",
        log_path=None,
        source_record=None,
        hermes_root=tmp_path / "hermes",
    )
    record = job_supervisor.mark_running(
        job_id, 4194304, hermes_root=tmp_path / "hermes"
    )
    return job_id, record


def test_reconcile_job_terminalizes_a_zombie_worker(tmp_path, monkeypatch):
    job_id, record = _registered_running_job(tmp_path, monkeypatch)
    assert record["status"] == "running"

    # Zombie reality: pid exists, cmdline is EMPTY, stat carries state 'Z'.
    def fake_identity(pid):
        return None if pid == 4194304 else job_supervisor.process_identity(pid)

    monkeypatch.setattr(job_supervisor, "process_identity", fake_identity)
    monkeypatch.setattr(job_supervisor, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        job_supervisor, "_procfs_state", lambda pid: "Z" if pid == 4194304 else None
    )

    out = job_supervisor.reconcile_job(job_id, hermes_root=tmp_path / "hermes")
    assert out is not None and out["status"] == "failed"
    assert out["process_verification"] == "exited-unfinalized"
    assert "zombie" in out["result_summary"]

    # Idempotent: a second reconcile on the terminal record changes nothing.
    again = job_supervisor.reconcile_job(job_id, hermes_root=tmp_path / "hermes")
    assert again is not None and again["status"] == "failed"
    assert again["finalization_version"] == out["finalization_version"]


def test_reconcile_job_keeps_unverifiable_for_non_zombie_alive_pid(tmp_path, monkeypatch):
    """An identity-less but NON-zombie pid (e.g. mid-execve transient) must
    keep the old fail-closed 'unavailable' classification — the zombie escape
    must not swallow the genuinely-unknown case."""
    job_id, record = _registered_running_job(tmp_path, monkeypatch)
    assert record["status"] == "running"

    monkeypatch.setattr(job_supervisor, "process_identity", lambda pid: None)
    monkeypatch.setattr(job_supervisor, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(job_supervisor, "_procfs_state", lambda pid: "S")

    out = job_supervisor.reconcile_job(job_id, hermes_root=tmp_path / "hermes")
    assert out is not None and out["status"] == "running"
    assert out["process_verification"] == "unavailable"
