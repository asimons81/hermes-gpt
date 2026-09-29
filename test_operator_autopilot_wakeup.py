"""Tests for Autopilot event-driven wakeups (v0.13 PR7).

Live events are notifications, never proof (docs/live-events.md): they may end
a wait early but must never carry information the worker acts on, and a
missing, delayed or broken event store must degrade to the timer, not stall.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_live_events as live_events
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import (
    MID,
    _j,
    _mk,
    _node,
    _states,
    _tick,
    make_env,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _publish(root: Path, mission_id: str = MID, kind: str = "mission.transition", payload: dict | None = None) -> dict:
    return live_events.publish_event(
        topic="mission", kind=kind, subject_type="mission", subject_id=mission_id,
        mission_id=mission_id, source="test", payload=payload or {}, hermes_root=root,
    )


# ---------------------------------------------------------------------------
# Idle poll configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, autopilot.TICK_SECONDS), ("", autopilot.TICK_SECONDS), ("5", 5.0), ("0.1", autopilot.MIN_IDLE_POLL_SECONDS),
    ("9999", autopilot.MAX_IDLE_POLL_SECONDS), ("garbage", autopilot.TICK_SECONDS), ("nan", autopilot.TICK_SECONDS),
    ("-3", autopilot.MIN_IDLE_POLL_SECONDS),
])
def test_idle_poll_seconds_is_clamped_and_fails_to_the_default(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(autopilot.IDLE_POLL_ENV, raising=False)
    else:
        monkeypatch.setenv(autopilot.IDLE_POLL_ENV, raw)
    assert autopilot._idle_poll_seconds() == expected


# ---------------------------------------------------------------------------
# _wait_for_wakeup
# ---------------------------------------------------------------------------


def test_timer_wakeup_honors_the_full_wait_and_leaves_the_cursor(env):
    root, _ = env
    started = time.monotonic()
    cursor, reason = autopilot._wait_for_wakeup(MID, 0, 0.6, root)
    assert reason == "timer" and cursor == 0
    assert time.monotonic() - started >= 0.55


def test_event_wakes_the_wait_early_and_moves_the_cursor_to_the_high_water_mark(env):
    root, _ = env
    timer = threading.Timer(0.3, lambda: _publish(root))
    timer.start()
    started = time.monotonic()
    cursor, reason = autopilot._wait_for_wakeup(MID, 0, 10.0, root)
    timer.join()
    assert reason == "event" and time.monotonic() - started < 3.0
    assert cursor == live_events.high_watermark(root) >= 1


def test_event_for_another_mission_does_not_wake(env):
    root, _ = env
    _publish(root, mission_id="msn-someone-else")
    started = time.monotonic()
    cursor, reason = autopilot._wait_for_wakeup(MID, 0, 0.6, root)
    assert reason == "timer" and cursor == 0 and time.monotonic() - started >= 0.55


def test_abort_check_ends_the_wait_between_slices(env):
    root, _ = env
    flag = {"stop": False}
    threading.Timer(0.3, lambda: flag.update(stop=True)).start()
    started = time.monotonic()
    cursor, reason = autopilot._wait_for_wakeup(MID, 5, 30.0, root, abort_check=lambda: flag["stop"])
    assert (cursor, reason) == (5, "abort")
    assert time.monotonic() - started < autopilot.WAIT_SLICE_SECONDS + 1.5  # not the 30s wait


def test_a_backlog_is_one_wakeup_not_one_per_event(env):
    root, _ = env
    for _i in range(6):
        _publish(root)
    cursor, reason = autopilot._wait_for_wakeup(MID, 0, 5.0, root)
    assert reason == "event" and cursor == live_events.high_watermark(root)
    # Nothing new since: the next wait is a plain timeout, so the backlog is drained.
    assert autopilot._wait_for_wakeup(MID, cursor, 0.5, root) == (cursor, "timer")


@pytest.mark.parametrize("failure", ["raise", "error-json", "garbage"])
def test_a_broken_event_store_degrades_to_the_timer_without_spinning(env, monkeypatch, failure):
    root, _ = env

    def broken(*args, **kwargs):
        if failure == "raise":
            raise OSError("events.db unreadable")
        return json.dumps({"success": False, "code": "LIVE_EVENT_READ_FAILED"}) if failure == "error-json" else "not json"

    monkeypatch.setattr(live_events, "hermes_live_events_since", broken)
    started = time.monotonic()
    cursor, reason = autopilot._wait_for_wakeup(MID, 7, 0.6, root)
    assert (cursor, reason) == (7, "timer")
    assert time.monotonic() - started >= 0.55  # the full wait was honored: no busy loop


# ---------------------------------------------------------------------------
# Events are never proof
# ---------------------------------------------------------------------------


def test_an_event_claiming_success_changes_nothing_the_tick_does(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b", ["a"])])
    _tick(root)
    forged = _publish(root, kind="delegation.reconciled",
                      payload={"state": "succeeded", "validation_verdict": "SATISFIED", "node_id": "a"})
    _cursor, reason = autopilot._wait_for_wakeup(MID, 0, 1.0, root)
    assert reason == "event" and forged["payload"]["state"] == "succeeded"
    out = _tick(root)  # the wakeup's only consequence: a tick that re-reads durable state
    assert out["completed"] == [] and _states(root) == {"a": "dispatched", "b": "pending"}
    _observe(root, backend.calls[0]["task_id"], state="completed")  # real evidence, no event needed
    assert _tick(root)["completed"] == ["a"]


def test_work_advances_with_no_events_at_all(env):
    root, backend = env
    _mk(root, [_node("a")])
    assert live_events.high_watermark(root) >= 0
    _tick(root)
    _observe(root, backend.calls[0]["task_id"], state="completed")
    assert _tick(root)["completed"] == ["a"]  # nothing depends on an event arriving


# ---------------------------------------------------------------------------
# Real detached worker
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    return last


def _run(root: Path) -> dict:
    return _j(autopilot.hermes_autopilot_status(MID, hermes_root=root))["run"]


def test_real_worker_reacts_to_an_owner_action_through_the_event_not_the_timer(env, monkeypatch):
    root, _ = env
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "30")  # the timer alone would take 30s
    _mk(root, [_node("a")])
    started = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
    assert started["success"] is True, started
    try:
        assert _wait_until(lambda: _run(root).get("last_schedule", {}).get("plan_version") is not None, 15.0)
        began = time.monotonic()
        paused = _j(mission.hermes_mission_transition(MID, "paused", confirm=True, dry_run=False, hermes_root=root))
        assert paused["success"] is True, paused
        run = _wait_until(lambda: (r := _run(root)).get("last_schedule", {}).get("skipped") == "mission_paused" and r, 10.0)
        assert run, _run(root)
        assert time.monotonic() - began < 10.0  # far below the 30s idle poll
        assert run["wakeups"]["event"] >= 1 and run["last_wake"] in ("event", "timer")
        assert run["last_event_cursor"] >= 1
    finally:
        autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root)


def test_restart_resumes_from_the_live_event_high_water_mark(env, monkeypatch):
    root, _ = env
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "0.5")
    _mk(root, [_node("a")])
    first = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
    assert first["success"] is True
    try:
        assert _wait_until(lambda: _run(root).get("last_schedule", {}).get("plan_version") is not None, 15.0)
        assert _j(autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root))["state"] == "stopped"
        _publish(root)
        _publish(root)
        mark = live_events.high_watermark(root)
        second = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
        assert second["success"] is True and second["run"]["attempt"] == 2
        assert second["run"]["last_event_cursor"] == mark  # no replay of pre-restart events
        # Let the new worker register its identity so the final stop can signal it.
        assert _wait_until(lambda: _j(autopilot.hermes_autopilot_status(MID, hermes_root=root))["worker"]["status"] == "running", 10.0)
    finally:
        autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root)


def test_event_flood_cannot_spin_the_worker(env, monkeypatch):
    root, _ = env
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "30")
    _mk(root, [_node("a")])
    started = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
    assert started["success"] is True
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            _publish(root)
            time.sleep(0.005)

    try:
        assert _wait_until(lambda: _run(root).get("last_schedule", {}).get("plan_version") is not None, 15.0)
        before = int(_run(root).get("wakeups", {}).get("event", 0))
        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        time.sleep(2.0)
        stop.set()
        thread.join()
        woke = int(_run(root).get("wakeups", {}).get("event", 0)) - before
        # ~200 events/s for 2s: the debounce caps ticks near 2s / MIN_TICK_INTERVAL.
        assert 1 <= woke <= int(2.0 / autopilot.MIN_TICK_INTERVAL_SECONDS) + 6, woke
    finally:
        stop.set()
        autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root)


def test_stop_is_prompt_even_with_a_long_idle_poll(env, monkeypatch):
    # Guards the property, not a loop mechanism: stop signals the verified worker
    # process tree directly, so the idle poll must never delay it.
    root, _ = env
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "60")
    _mk(root, [_node("a")])
    started = _j(autopilot.hermes_autopilot_start(MID, confirm=True, dry_run=False, hermes_root=root))
    assert started["success"] is True
    assert _wait_until(lambda: _run(root).get("last_schedule", {}).get("plan_version") is not None, 15.0)
    pid = _run(root)["pid"]
    began = time.monotonic()
    assert _j(autopilot.hermes_autopilot_stop(MID, confirm=True, dry_run=False, hermes_root=root))["state"] == "stopped"

    def gone() -> bool:
        # The worker is our child and nothing wait()s on it, so an exited worker is a
        # zombie that os.kill(pid, 0) still "finds"; read the process state instead.
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            return True
        state = next((line.split()[1] for line in status.splitlines() if line.startswith("State:")), "")
        return state in ("Z", "X")

    assert _wait_until(gone, 8.0), "worker outlived its stop by far more than one slice"
    assert time.monotonic() - began < 8.0  # a 60s idle poll would have kept it alive for up to a minute


def test_durable_cancel_is_noticed_within_a_slice_even_with_a_long_idle_poll(env, monkeypatch):
    """The window before a fresh worker registers its identity.

    ``request_cancel`` refuses to signal an unverified PID, so all it can do is
    record the cancel durably; only the worker loop can notice. Reproduced
    deterministically and without a subprocess: run the worker in a thread and
    cancel its job record with no signal at all.
    """
    import operator_job_supervisor as jobs

    root, _ = env
    monkeypatch.setenv(autopilot.IDLE_POLL_ENV, "60")
    # An approval gate only: the tick dispatches nothing, so it publishes no live event
    # and the worker parks in the full 60s wait (a dispatch would echo an event and
    # cut the first wait short, letting the worker see the cancel at the top of its loop).
    _mk(root, [_node("g", kind="approval", owner="owner")])
    run = autopilot._write_run(MID, root, max_concurrency=1, state="running")
    job_id = autopilot.job_id_for(MID, 1)
    jobs.register_job(job_id, backend="autopilot", workspace=root, log_path=root / "autopilot" / "t.log",
                      source_record=autopilot._run_path(MID, root), hermes_root=root)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(rc=autopilot._worker(MID, job_id, root)), daemon=True)
    thread.start()
    assert _wait_until(lambda: (autopilot._read_run(MID, root) or {}).get("last_schedule", {}).get("plan_version") is not None, 15.0)
    time.sleep(1.5)  # comfortably past the debounce: the worker is inside its long wait
    assert thread.is_alive()
    began = time.monotonic()
    jobs.terminalize(job_id, "cancelled", hermes_root=root)  # durable cancel, no process signal
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "the worker ignored a durable cancel for a full idle poll"
    assert result["rc"] == 0 and time.monotonic() - began < 6.0
    assert (autopilot._read_run(MID, root) or {})["state"] == "stopped"
    assert run["mission_id"] == MID
