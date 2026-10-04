"""Tests for the Flight Deck Autopilot read model (v0.13 PR8).

Flight Deck is presentation and observation only: the route must be GET-only,
must write nothing (not even the self-healing sync the MCP status tool does),
must stay truthful about a worker that died, and must expose only an explicit
allow-list of fields.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import operator_autopilot as autopilot
import operator_job_supervisor as jobs
import operator_live_events as live_events
import operator_policy as op
import ui_api
import ui_missions

MID = "msn-ui-ap"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    return home


def _client() -> TestClient:
    return TestClient(Starlette(routes=ui_missions.ui_missions_routes()))


def _get(path: str = f"/api/ops/missions/{MID}/autopilot"):
    return _client().get(path)


def _register(root: Path, *, attempt: int = 1, pid: int | None = None, status: str = "running") -> str:
    job_id = autopilot.job_id_for(MID, attempt)
    jobs.register_job(job_id, backend="autopilot", workspace=root, log_path=root / "autopilot" / "x.log",
                      source_record=autopilot._run_path(MID, root), hermes_root=root)
    if pid == os.getpid():
        jobs.mark_running(job_id, pid, hermes_root=root)  # real, verifiable identity
    elif pid is not None:
        record = jobs._load_json(jobs._record_path(job_id, root))
        record.update({"status": status, "pid": pid})
        jobs._atomic_json(jobs._record_path(job_id, root), record)
    return job_id


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
            and "audit" not in p.name and not p.name.endswith(".lock")}


def _run(root: Path, **fields) -> dict:
    return autopilot._write_run(MID, root, state="running", attempt=1, job_id=autopilot.job_id_for(MID, 1), **fields)


# ---------------------------------------------------------------------------
# Routing and authority boundary
# ---------------------------------------------------------------------------


def test_route_is_composed_and_get_only(root):
    assert "/api/ops/missions/{mission_id}/autopilot" in {getattr(r, "path", "") for r in ui_api.routes()}
    assert all(route.methods == {"GET", "HEAD"} for route in ui_missions.ui_missions_routes())
    client = _client()
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(f"/api/ops/missions/{MID}/autopilot").status_code == 405


def test_no_run_is_reported_as_not_found_not_an_error(root):
    body = _get().json()
    assert body["ok"] is True
    data = body["data"]
    assert data["found"] is False and data["read_only"] is True and data["mission_id"] == MID
    assert isinstance(data["live_cursor"], int) and "run" not in data


def test_bad_mission_id_is_a_clean_client_error(root):
    response = _get("/api/ops/missions/not%20a%20valid%20id!/autopilot")
    assert response.status_code == 400 and response.json()["error"]["code"] == "AUTOPILOT_READ_FAILED"


def test_read_access_is_required(root, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "0")
    response = _get()
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTOPILOT_READ_DENIED"


# ---------------------------------------------------------------------------
# Truthful and strictly read-only
# ---------------------------------------------------------------------------


def test_live_worker_is_reported_alive(root):
    _run(root, max_concurrency=3, max_replans=2)
    _register(root, pid=os.getpid())
    data = _get().json()["data"]
    assert data["found"] is True and data["effective_state"] == "running" and data["stale"] is False
    assert data["worker"]["liveness"] == "alive"
    assert data["run"]["state"] == "running" and data["run"]["max_concurrency"] == 3


def test_dead_worker_is_reported_stale_and_nothing_is_written(root):
    _run(root)
    _register(root, pid=2_000_000_000)  # no such process; the record still claims "running"
    before = _snapshot(root)
    for _ in range(3):
        data = _get().json()["data"]
        assert data["effective_state"] == "failed" and data["stale"] is True
        assert data["worker"]["liveness"] == "dead" and data["run"]["state"] == "running"  # stored record shown as found
    assert _snapshot(root) == before  # not even the self-healing sync the MCP status tool performs


def test_cancelled_job_with_stale_running_record_is_reported_stopped_without_writing(root):
    _run(root)
    job_id = _register(root, pid=os.getpid())
    jobs.terminalize(job_id, "cancelled", hermes_root=root)
    before = _snapshot(root)
    data = _get().json()["data"]
    assert data["effective_state"] == "stopped" and data["stale"] is True and data["worker"]["liveness"] == "terminal"
    assert _snapshot(root) == before
    assert autopilot._read_run(MID, root)["state"] == "running"  # left exactly as found


def test_terminal_run_is_reported_as_is(root):
    autopilot._write_run(MID, root, state="completed", attempt=1, job_id=autopilot.job_id_for(MID, 1))
    _register(root, pid=os.getpid())
    data = _get().json()["data"]
    assert data["effective_state"] == "completed" and data["stale"] is False


def test_queued_worker_without_a_pid_is_unregistered_not_dead(root):
    _run(root)
    _register(root)  # registered, worker has not started yet
    data = _get().json()["data"]
    assert data["worker"]["liveness"] == "unregistered" and data["stale"] is False and data["effective_state"] == "running"


def test_the_mcp_status_tool_still_heals_and_the_ui_route_does_not(root):
    _run(root)
    job_id = _register(root, pid=os.getpid())
    jobs.terminalize(job_id, "cancelled", hermes_root=root)
    _get()
    assert autopilot._read_run(MID, root)["state"] == "running"
    healed = json.loads(autopilot.hermes_autopilot_status(MID, hermes_root=root))
    assert healed["run"]["state"] == "stopped"  # the tool's behavior is unchanged


# ---------------------------------------------------------------------------
# Allow-list projection and redaction
# ---------------------------------------------------------------------------


def test_only_allow_listed_fields_reach_the_browser(root):
    _run(root, max_replans=2, replans_used=1, last_error="", last_wake="event", wakeups={"event": 2, "timer": 5},
         last_schedule={"plan_version": 1, "slots": 2, "in_flight": 1, "dispatched": ["a"], "held": {"b": "budget_crossed"},
                        "frontier": {"active": True, "waiting": False, "reasons": [], "nodes": []},
                        "limit": "budget_crossed", "budget": {"status": "crossing"}, "internal_scratch": "nope"},
         recovery={"placements": {"a": "rza"}, "nodes": {"a": {"excluded": ["rza"]}}, "superseded_nodes": {"a": "a-r1"},
                   "pending_supersede": {"a-r1": "dlg-x"}, "replan_pending": {}, "superseded": {}},
         secret_note="do-not-leak", walking={"a": 1}, dispatch_failures={"1:a": 2})
    _register(root, pid=os.getpid())
    run = _get().json()["data"]["run"]
    assert run["replans_used"] == 1 and run["wakeups"] == {"event": 2, "timer": 5}
    assert run["last_schedule"]["held"] == {"b": "budget_crossed"} and run["last_schedule"]["limit"] == "budget_crossed"
    assert run["recovery"] == {"superseded_nodes": {"a": "a-r1"}, "pending_supersede": ["a-r1"], "replan_pending": []}
    text = json.dumps(run)
    for forbidden in ("secret_note", "internal_scratch", "placements", "excluded", "walking", "dispatch_failures",
                      "config_sha256", "pid", "job_id"):
        assert forbidden not in text, forbidden


def test_secret_looking_text_is_redacted_at_the_boundary(root):
    _run(root, last_error="request failed, Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789 api_key=sk-live-0123456789abcdef")
    _register(root, pid=os.getpid())
    text = _get().text
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in text and "sk-live-0123456789abcdef" not in text


# ---------------------------------------------------------------------------
# Cursor ordering
# ---------------------------------------------------------------------------


def test_the_live_event_cursor_is_captured_before_the_durable_read(root, monkeypatch):
    _run(root)
    _register(root, pid=os.getpid())
    before = live_events.high_watermark(root)
    real = autopilot.observe_status

    def event_lands_during_the_read(*args, **kwargs):
        live_events.publish_event(topic="mission", kind="mission.transition", subject_type="mission", subject_id=MID,
                                  mission_id=MID, source="test", payload={}, hermes_root=root)
        return real(*args, **kwargs)

    monkeypatch.setattr(autopilot, "observe_status", event_lands_during_the_read)
    data = _get().json()["data"]
    # The event raced with the snapshot, so it must still be *after* the returned cursor
    # (the browser's next long-poll sees it); advancing the cursor past it would skip it.
    assert data["live_cursor"] == before < live_events.high_watermark(root)


def test_existing_mission_routes_are_untouched(root):
    assert _client().get("/api/ops/missions/msn-missing").status_code == 404


# ---------------------------------------------------------------------------
# Status v2 summary projection (PR9)
# ---------------------------------------------------------------------------


def test_summary_is_projected_through_an_allow_list(root, monkeypatch):
    _run(root)
    _register(root, pid=os.getpid())
    derived = {
        "available": True, "mission_status": "running", "plan_version": 1,
        "progress": {"total": 3, "completed": 1, "percent": 33, "by_state": {"pending": 2}, "ready": 1, "in_flight": 0},
        "workers": [{"node_id": "a", "state": "dispatched", "attempt": 1, "peer": "rza",
                     "delegation_id": "dlg-secretish", "delegation_state": "running"}],
        "frontier": {"active": True, "waiting": True, "reasons": ["owner_gate_node"], "nodes": ["g"]},
        "budget": {"configured": True, "status": "within", "crosses": False, "unit": "usd", "spend": 1.0, "quota": 10.0},
        "recovery": {"retries": 1, "replans_used": 1, "max_replans": 2, "superseded_nodes": 1, "failed_nodes": [],
                     "internal_note": "nope"},
        "limits": {"max_runtime_seconds": 600}, "wake": {"last_wake": "event"},
        "attention": [{"code": "owner_gate_node", "severity": "owner", "nodes": ["g"], "scratch": "nope"}],
        "needs_owner": True, "hidden_extra": "nope",
    }
    monkeypatch.setattr(autopilot, "build_summary", lambda *a, **k: derived)
    summary = _get().json()["data"]["summary"]
    assert summary["needs_owner"] is True and summary["progress"]["percent"] == 33
    assert summary["workers"] == [{"node_id": "a", "state": "dispatched", "attempt": 1, "delegation_state": "running",
                                  "validation": {}, "validation_failure_since": ""}]
    assert summary["attention"] == [{"code": "owner_gate_node", "severity": "owner", "nodes": ["g"]}]
    text = json.dumps(summary)
    for forbidden in ("rza", "dlg-secretish", "hidden_extra", "internal_note", "scratch"):
        assert forbidden not in text, forbidden


def test_an_unavailable_summary_is_reported_as_such(root, monkeypatch):
    _run(root)
    _register(root, pid=os.getpid())

    def broken(*args, **kwargs):
        raise OSError("plan store unreadable")

    monkeypatch.setattr(autopilot, "build_summary", broken)
    data = _get().json()["data"]
    assert data["found"] is True and data["summary"] == {"available": False}
