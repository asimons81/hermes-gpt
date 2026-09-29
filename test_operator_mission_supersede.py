"""Tests for the Mission ``superseded_by`` attachment marker (v0.13 PR5).

A failed delegation attempt normally fails the whole Mission on reconcile. The
marker lets a bounded Autopilot retry/replan replace such an attempt with a
successor. It must never hide live, successful or unfinished work, and a marker
that does not verify must have no effect.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_delegations as deleg
import operator_mission_runtime as mission
from test_operator_autopilot_advance import _observe
from test_operator_autopilot_scheduler import MID, _j, _mk, _node, _tick, make_env

PREFIX = mission.SUPERSEDED_PREFIX


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    return make_env(tmp_path, monkeypatch)


def _two_attempts(root: Path, backend):
    """Two independent nodes, both dispatched: returns ([task ids], [delegation ids])."""
    _mk(root, [_node("a"), _node("b")])
    assert _tick(root)["dispatched"] == ["a", "b"]
    tasks = [c["task_id"] for c in backend.calls]
    listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
    by_task = {d["task_id"]: d["delegation_id"] for d in listed}
    return tasks, [by_task[t] for t in tasks]


def _reconcile(root: Path, delegation_id: str) -> None:
    assert _j(deleg.hermes_delegation_reconcile(delegation_id, apply=True, hermes_root=root))["success"]


def _observed_refs(root: Path) -> list[str]:
    m = _j(mission.hermes_mission_get(MID, hermes_root=root))
    return [a["ref"] for a in mission._observe_attachments(root, m)]


def _force_relationship(root: Path, ref: str, relationship: str) -> None:
    """Simulate a forged/stale marker written behind the API's back."""
    with sqlite3.connect(mission._db_path(root)) as db:
        db.execute("UPDATE attachments SET relationship=? WHERE mission_id=? AND ref=?", (relationship, MID, ref))


def _fail(root: Path, task: str, delegation_id: str) -> None:
    _observe(root, task, state="failed", error="worker crashed")
    _reconcile(root, delegation_id)


def _status_after_reconcile(root: Path) -> str:
    mission.hermes_mission_reconcile(MID, confirm=True, dry_run=False, hermes_root=root)
    return _j(mission.hermes_mission_get(MID, hermes_root=root))["status"]


def test_unsuperseded_failed_attempt_fails_the_mission_control(env):
    root, backend = env
    (ta, tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    _observe(root, tb, state="completed")
    _reconcile(root, db_)
    assert _status_after_reconcile(root) == "failed"


def test_superseded_failed_attempt_no_longer_fails_the_mission(env):
    root, backend = env
    (ta, tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    _observe(root, tb, state="completed")
    _reconcile(root, db_)
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is True
    assert _observed_refs(root) == [db_]
    assert _status_after_reconcile(root) == "awaiting_approval"  # never completed: final approval still required


def test_live_attempt_can_never_be_superseded(env):
    root, backend = env
    (_ta, _tb), (da, db_) = _two_attempts(root, backend)
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is False
    assert sorted(_observed_refs(root)) == sorted([da, db_])


def test_succeeded_attempt_can_never_be_superseded(env):
    root, backend = env
    (ta, _tb), (da, db_) = _two_attempts(root, backend)
    _observe(root, ta, state="completed")
    _reconcile(root, da)
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is False


def test_supersede_rejects_self_unknown_and_reverse_cycles(env):
    root, backend = env
    (ta, tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    _fail(root, tb, db_)
    assert mission.supersede_delegation_attachment(MID, da, da, hermes_root=root) is False
    assert mission.supersede_delegation_attachment(MID, da, "dlg-does-not-exist", hermes_root=root) is False
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is True
    assert mission.supersede_delegation_attachment(MID, db_, da, hermes_root=root) is False  # would loop back


def test_supersede_is_idempotent_and_refuses_a_different_successor(env):
    root, backend = env
    (ta, _tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is True
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is True
    attachments = {a["ref"]: a for a in _j(mission.hermes_mission_get(MID, hermes_root=root))["attachments"]}
    assert attachments[da]["relationship"] == f"{PREFIX}{db_}"
    assert mission.supersede_delegation_attachment(MID, da, da, hermes_root=root) is False


def test_public_attach_cannot_assert_the_marker(env):
    root, backend = env
    (_ta, _tb), (da, db_) = _two_attempts(root, backend)
    out = _j(mission.hermes_mission_attach(
        MID, "delegation", da, relationship=f"{PREFIX}{db_}", confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is False
    dry = _j(mission.hermes_mission_attach(
        MID, "delegation", da, relationship=f"{PREFIX}{db_}", dry_run=True, hermes_root=root))
    assert dry["success"] is False


def test_forged_marker_on_a_live_attempt_has_no_effect(env):
    root, backend = env
    (_ta, _tb), (da, db_) = _two_attempts(root, backend)
    _force_relationship(root, da, f"{PREFIX}{db_}")
    assert sorted(_observed_refs(root)) == sorted([da, db_])


@pytest.mark.parametrize("successor", ["dlg-not-there", "SELF", "CYCLE"])
def test_dangling_self_or_cyclic_marker_on_a_failed_attempt_is_ignored(env, successor):
    root, backend = env
    (ta, tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    _fail(root, tb, db_)
    target = {"SELF": da, "CYCLE": db_}.get(successor, successor)
    _force_relationship(root, da, f"{PREFIX}{target}")
    if successor == "CYCLE":
        _force_relationship(root, db_, f"{PREFIX}{da}")
    assert da in _observed_refs(root)
    assert _status_after_reconcile(root) == "failed"


def test_chain_of_retries_resolves_to_the_live_end(env):
    root, backend = env
    _mk(root, [_node("a"), _node("b"), _node("c")])
    _tick(root)
    listed = _j(deleg.hermes_delegation_list(mission_id=MID, hermes_root=root))["delegations"]
    by_task = {d["task_id"]: d["delegation_id"] for d in listed}
    tasks = [c["task_id"] for c in backend.calls]
    da, db_, dc = (by_task[t] for t in tasks)
    _fail(root, tasks[0], da)
    _fail(root, tasks[1], db_)
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is True
    assert mission.supersede_delegation_attachment(MID, db_, dc, hermes_root=root) is True
    assert _observed_refs(root) == [dc]  # a -> b -> c, only the live end counts


def test_terminal_mission_cannot_be_modified(env):
    root, backend = env
    (ta, _tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    with sqlite3.connect(mission._db_path(root)) as db:
        db.execute("UPDATE missions SET status='cancelled' WHERE mission_id=?", (MID,))
    assert mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root) is False


def test_marker_json_shape_is_visible_in_mission_get(env):
    root, backend = env
    (ta, _tb), (da, db_) = _two_attempts(root, backend)
    _fail(root, ta, da)
    mission.supersede_delegation_attachment(MID, da, db_, hermes_root=root)
    raw = mission.hermes_mission_get(MID, hermes_root=root)
    assert json.loads(raw)["attachments"]
    assert f"{PREFIX}{db_}" in raw  # the marker is inspectable, not hidden state
