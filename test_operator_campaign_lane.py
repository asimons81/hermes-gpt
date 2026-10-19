"""Tests for the AUTO-03 durable campaign adoption lane.

The lane is a CALLER: register/unregister exercise the real registry under a
temp hermes_root, and every run test drives the REAL mission/plan/controller
stores through the verbatim ``campaign_controller_tick``. Only the wakeup
tests stub the live-event read surface (the same degradation seams as
test_operator_autopilot_wakeup) to prove timer fallback without spinning.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_campaign_compiler as compiler
import operator_campaign_controller as ctrl
import operator_campaign_lane as lane
import operator_live_events as live_events
import operator_mission_runtime as mission
import operator_policy as op


def _segment(seq: str, mid: str, campaign_id: str = "camp-test", *, depends_on: list[str] | None = None) -> dict:
    return {
        "seq": seq,
        "depends_on": depends_on or [],
        "campaign_json": json.dumps({
            "schema": compiler.CAMPAIGN_SCHEMA,
            "campaign_id": campaign_id,
            "mission_id": mid,
            "objective": f"Segment {seq} work",
            "tracks": [
                {"track": "a", "profile": "hermes-dev",
                 "tasks": [{"task": "T1", "evidence": ["implementation"]}]},
            ],
        }),
    }


def _manifest(*segments: dict, campaign_id: str = "camp-test") -> str:
    return json.dumps({
        "schema": ctrl.MANIFEST_SCHEMA,
        "campaign_id": campaign_id,
        "segments": list(segments),
    })


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV, raising=False)
    return root


def _manifest_file(tmp_path: Path, name: str, manifest: str) -> Path:
    path = tmp_path / name
    path.write_text(manifest, encoding="utf-8")
    return path


def _register(path: Path, root: Path) -> dict:
    return json.loads(lane.register_campaign(
        path, confirm=True, apply=True, hermes_root=root))


def _run(root: Path, **kw) -> dict:
    kw.setdefault("confirm", True)
    kw.setdefault("apply", True)
    return json.loads(lane.campaign_lane_run(hermes_root=root, **kw))


def _lane_state(root: Path) -> dict:
    return json.loads((root / "campaigns" / "lane-state.json").read_text())


def _mission_count(root: Path) -> int:
    return int(json.loads(mission.hermes_mission_list(hermes_root=root))["count"])


def _publish(root: Path, mission_id: str) -> dict:
    return live_events.publish_event(
        topic="mission", kind="mission.transition", subject_type="mission",
        subject_id=mission_id, mission_id=mission_id, source="test",
        payload={}, hermes_root=root,
    )


# ---------------------------------------------------------------------------
# register: validate via ONE dry-run tick, fail closed, idempotent
# ---------------------------------------------------------------------------

def test_register_validates_via_dry_run_tick_then_registers(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    out = _register(path, hermes_root)
    assert out["success"] is True and out["already_registered"] is False
    registry = json.loads((hermes_root / "campaigns" / "lane.json").read_text())
    assert registry["schema"] == lane.REGISTRY_SCHEMA
    assert registry["campaigns"] == [{"campaign_id": "camp-test",
                                      "manifest_path": str(path),
                                      "registered_at": registry["campaigns"][0]["registered_at"]}]
    # the validation tick was a dry run: nothing was minted
    assert _mission_count(hermes_root) == 0


def test_register_fails_closed_on_bad_manifest(hermes_root: Path, tmp_path: Path) -> None:
    not_json = _manifest_file(tmp_path, "bad.json", "{not json")
    out = json.loads(lane.register_campaign(
        not_json, confirm=True, apply=True, hermes_root=hermes_root))
    assert out["success"] is False and "validation tick" in out["error"]
    bad_doc = json.loads(_manifest(_segment("s1", "msn-c1")))
    bad_doc["segments"][0]["campaign_json"] = json.dumps({
        "schema": compiler.CAMPAIGN_SCHEMA, "campaign_id": "camp-test",
        "mission_id": "msn-c1", "objective": "x",
        "tracks": [{"track": "a", "profile": "hermes-dev",
                    "tasks": [{"task": "T1", "evidence": ["bogus-evidence"]}]}],
    })
    out2 = json.loads(lane.register_campaign(
        _manifest_file(tmp_path, "worse.json", json.dumps(bad_doc)),
        confirm=True, apply=True, hermes_root=hermes_root))
    assert out2["success"] is False and "does not compile" in out2["error"]
    # fail closed: no registry artifact exists at all
    assert not (hermes_root / "campaigns" / "lane.json").exists()


def test_register_is_idempotent_by_path_and_campaign_id(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    first = _register(path, hermes_root)
    second = _register(path, hermes_root)
    assert first["success"] and second["success"]
    assert second["already_registered"] is True
    registry = json.loads((hermes_root / "campaigns" / "lane.json").read_text())
    assert len(registry["campaigns"]) == 1


def test_register_refuses_duplicate_campaign_id_from_different_path(
    hermes_root: Path, tmp_path: Path,
) -> None:
    one = _manifest_file(tmp_path, "one.json", _manifest(_segment("s1", "msn-c1")))
    two = _manifest_file(tmp_path, "two.json", _manifest(_segment("s1", "msn-c1")))
    assert _register(one, hermes_root)["success"] is True
    out = _register(two, hermes_root)
    assert out["success"] is False and "different" in out["error"]
    registry = json.loads((hermes_root / "campaigns" / "lane.json").read_text())
    assert len(registry["campaigns"]) == 1  # original registration untouched


def test_unregister_is_containment_only_state_survives(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    _register(path, hermes_root)
    _run(hermes_root, mode="once")  # mint a real mission + controller state
    out = json.loads(lane.unregister_campaign(
        "camp-test", confirm=True, apply=True, hermes_root=hermes_root))
    assert out["success"] is True and [e["campaign_id"] for e in out["removed"]] == ["camp-test"]
    registry = json.loads((hermes_root / "campaigns" / "lane.json").read_text())
    assert registry["campaigns"] == []
    # containment: the controller's state and the mission survive untouched
    assert (hermes_root / "campaigns" / "camp-test" / "state.json").is_file()
    assert _mission_count(hermes_root) == 1
    assert "camp-test" not in _lane_state(hermes_root)["campaigns"]


# ---------------------------------------------------------------------------
# run: read registry first, tick verbatim, isolation per campaign
# ---------------------------------------------------------------------------

def test_once_mode_with_empty_registry_is_a_clean_no_op(hermes_root: Path) -> None:
    out = _run(hermes_root, mode="once")
    assert out["success"] is True and out["ticks"] == 1
    assert out["campaigns"] == [] and out["all_ok"] is True


def test_once_drives_pending_to_minted_against_real_store(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    _register(path, hermes_root)
    out = _run(hermes_root, mode="once")
    assert out["success"] is True and out["all_ok"] is True
    view = out["campaigns"][0]
    assert view["campaign_id"] == "camp-test" and view["ok"] is True
    assert view["actions"].get("mission_create") == 1
    created = json.loads(mission.hermes_mission_get("msn-c1", hermes_root=hermes_root))
    assert created.get("found", True) is not False and created["status"] == "draft"
    assert _lane_state(hermes_root)["campaigns"]["camp-test"]["last_tick"] is not None


def test_restart_never_mints_a_duplicate_mission(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    _register(path, hermes_root)
    first = _run(hermes_root, mode="once")
    # "restart": a fresh process/run re-reads the registry and re-adopts
    second = _run(hermes_root, mode="once")
    assert first["success"] and second["success"]
    assert _mission_count(hermes_root) == 1
    view = second["campaigns"][0]
    assert view["actions"].get("mission_create", 0) == 0
    book = _lane_state(hermes_root)["campaigns"]["camp-test"]
    assert book["ticks"] == 2  # bookkeeping accumulates across runs


def test_lane_state_loss_rebuilds_without_duplicates(hermes_root: Path, tmp_path: Path) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    _register(path, hermes_root)
    _run(hermes_root, mode="once")
    (hermes_root / "campaigns" / "lane-state.json").unlink()  # lane state is NOT truth
    out = _run(hermes_root, mode="once")
    assert out["success"] is True
    assert out["campaigns"][0]["actions"].get("mission_create", 0) == 0
    assert _mission_count(hermes_root) == 1
    book = _lane_state(hermes_root)["campaigns"]["camp-test"]
    assert book["event_cursor"] >= 1  # mission_create published an event
    assert book["ticks"] == 1  # fresh lane bookkeeping, rebuilt


def test_bad_manifest_fails_only_that_campaign(hermes_root: Path, tmp_path: Path) -> None:
    good = _manifest_file(tmp_path, "good.json",
                          _manifest(_segment("s1", "msn-good", "camp-good"), campaign_id="camp-good"))
    bad = _manifest_file(tmp_path, "bad.json",
                         _manifest(_segment("s1", "msn-bad", "camp-bad"), campaign_id="camp-bad"))
    assert _register(good, hermes_root)["success"] is True
    assert _register(bad, hermes_root)["success"] is True
    bad.write_text("{corrupted after registration", encoding="utf-8")
    out = _run(hermes_root, mode="once")
    assert out["success"] is True  # the lane survives a failing campaign
    assert out["all_ok"] is False
    by_id = {c["campaign_id"]: c for c in out["campaigns"]}
    assert by_id["camp-good"]["ok"] is True
    assert by_id["camp-good"]["actions"].get("mission_create") == 1
    assert by_id["camp-bad"]["ok"] is False and by_id["camp-bad"]["error"]
    assert _mission_count(hermes_root) == 1  # only the good campaign minted


def test_corrupt_registry_fails_closed(hermes_root: Path) -> None:
    (hermes_root / "campaigns").mkdir(parents=True)
    (hermes_root / "campaigns" / "lane.json").write_text("{broken", encoding="utf-8")
    out = _run(hermes_root, mode="once")
    assert out["success"] is False and "registry" in out["error"]


# ---------------------------------------------------------------------------
# Event-cursor wakeups (notification never proof; timer always backstop)
# ---------------------------------------------------------------------------

WATCH = [{"campaign_id": "camp-test", "cursor": 0, "mission_ids": ["msn-c1"]}]


def test_event_wakeup_advances_cursor_reason_event(hermes_root: Path) -> None:
    timer = threading.Timer(0.3, lambda: _publish(hermes_root, "msn-c1"))
    timer.start()
    started = time.monotonic()
    cursor, reason = lane._wait_for_lane_wakeup(WATCH, 10.0, hermes_root)
    timer.join()
    assert reason == "event" and time.monotonic() - started < 3.0
    assert cursor == live_events.high_watermark(hermes_root) >= 1
    # the backlog is drained by the jump: the next wait is a plain timeout
    assert lane._wait_for_lane_wakeup(
        [{**WATCH[0], "cursor": cursor}], 0.4, hermes_root) == (cursor, "timer")


def test_event_for_an_unwatched_mission_does_not_wake(hermes_root: Path) -> None:
    _publish(hermes_root, "msn-someone-else")
    started = time.monotonic()
    cursor, reason = lane._wait_for_lane_wakeup(WATCH, 0.6, hermes_root)
    assert reason == "timer" and cursor == 0
    assert time.monotonic() - started >= 0.55


def test_timer_fallback_honors_the_full_wait(hermes_root: Path) -> None:
    started = time.monotonic()
    cursor, reason = lane._wait_for_lane_wakeup(WATCH, 0.6, hermes_root)
    assert reason == "timer" and cursor == 0
    assert time.monotonic() - started >= 0.55


@pytest.mark.parametrize("failure", ["raise", "error-json", "garbage"])
def test_erroring_event_store_degrades_to_timer_without_spinning(
    hermes_root: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    def broken(*args, **kwargs):
        if failure == "raise":
            raise OSError("events.db unreadable")
        if failure == "error-json":
            return json.dumps({"success": False, "error": "boom"})
        return "not json at all"

    monkeypatch.setattr(lane.live_events, "hermes_live_events_since", broken)
    started = time.monotonic()
    cursor, reason = lane._wait_for_lane_wakeup(WATCH, 0.8, hermes_root)
    assert reason == "timer" and cursor == 0
    # the FULL wait is honored (an erroring store returns instantly; the
    # slice pause prevents the spin) — not a busy loop, not a stall.
    assert time.monotonic() - started >= 0.7


def test_serve_mode_wakes_on_event_and_advances_persisted_cursor(
    hermes_root: Path, tmp_path: Path,
) -> None:
    path = _manifest_file(tmp_path, "camp.json", _manifest(_segment("s1", "msn-c1")))
    _register(path, hermes_root)
    _run(hermes_root, mode="once")  # pass 1 mints; cursor snapshots the watermark
    before = _lane_state(hermes_root)["campaigns"]["camp-test"]["event_cursor"]
    timer = threading.Timer(0.3, lambda: _publish(hermes_root, "msn-c1"))
    timer.start()
    out = _run(hermes_root, mode="serve", max_ticks=2, idle_poll=5.0)
    timer.join()
    assert out["success"] is True and out["ticks"] == 2
    assert out["wakeups"]["event"] == 1 and out["wakeups"]["timer"] == 0
    after = _lane_state(hermes_root)["campaigns"]["camp-test"]["event_cursor"]
    assert before < after <= live_events.high_watermark(hermes_root)
    # cursor persisted across runs: the next watch resumes from it, not 0
    assert lane._watch_set(hermes_root)[0]["cursor"] == after
    still = _run(hermes_root, mode="once")  # restart: no duplicate work
    assert still["campaigns"][0]["actions"].get("mission_create", 0) == 0


def test_max_ticks_bounds_serve_mode(hermes_root: Path) -> None:
    # an empty lane: every between-pass wait is the timer backstop, so the
    # bound is measured deterministically (a manifest-bearing variant races
    # its own pass events, which is exactly the never-proof doctrine).
    started = time.monotonic()
    out = _run(hermes_root, mode="serve", max_ticks=3, idle_poll=0.5)
    assert out["success"] is True and out["ticks"] == 3
    assert out["wakeups"]["timer"] == 2  # one bounded wait between each pass
    assert time.monotonic() - started >= 0.9  # the waits were honored, not skipped
