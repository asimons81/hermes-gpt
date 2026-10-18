"""Item 327 — record_attachment_state must not spam unchanged-state rows.

The delegation reconcile bridge re-drives ``record_attachment_state`` on every
~1s poll while a Mission-attached delegation is alive. An unchanged
(state, evidence_ref, verified) tuple is a heartbeat, not a transition: the
pre-fix bridge wrote an UPDATE + missions.version bump + mission.attachment_state
event row + live-event publish per poll (~15-21k rows per 4h promote-verify
mission; 392,134 of 395,581 rows in the live store were state-unchanged spam).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_mission_runtime as mission
import operator_policy as op


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    return root


def _spec(mid: str = "msn-test") -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Promote verify",
            "objective": "Observe a delegation without write amplification.",
            "owner_profile": "default",
            "acceptance_criteria": ["child work observed"],
            "context_refs": [],
            "skills": [],
            "final_approval_required": True,
        }
    )


def _j(value: str) -> dict:
    return json.loads(value)


def _db(hermes_root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{mission._db_path(hermes_root)}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _attachment_events(hermes_root: Path, mission_id: str = "msn-test") -> int:
    with _db(hermes_root) as db:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM mission_events WHERE mission_id=? AND event_type='mission.attachment_state'",
                (mission_id,),
            ).fetchone()[0]
        )


def _version(hermes_root: Path, mission_id: str = "msn-test") -> int:
    with _db(hermes_root) as db:
        return int(
            db.execute(
                "SELECT version FROM missions WHERE mission_id=?", (mission_id,)
            ).fetchone()[0]
        )


def _attachment(hermes_root: Path, mission_id: str = "msn-test") -> dict:
    with _db(hermes_root) as db:
        row = db.execute(
            "SELECT state,evidence_ref,verified,updated_at FROM attachments WHERE mission_id=? AND kind='delegation' AND ref='dlg-1'",
            (mission_id,),
        ).fetchone()
    return dict(row) if row is not None else {}


def _seeded_attachment(hermes_root: Path) -> None:
    assert _j(
        mission.hermes_mission_create(
            _spec(), confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )["success"]
    assert _j(
        mission.hermes_mission_attach(
            "msn-test",
            "delegation",
            "dlg-1",
            state="pending",
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )["success"]


def test_unchanged_state_poll_writes_exactly_one_event(hermes_root: Path):
    _seeded_attachment(hermes_root)
    kwargs = {
        "evidence_ref": "delegation:dlg-1",
        "verified": False,
        "hermes_root": hermes_root,
    }
    # Two identical polls (the ~1s reconcile-loop shape): the second must be a
    # no-op, not a second event row. OLD code wrote one row per poll (2 here).
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-1", "running", **kwargs
        )
        is True
    )
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-1", "running", **kwargs
        )
        is True
    )
    assert _attachment_events(hermes_root) == 1


def test_unchanged_state_poll_bumps_neither_version_nor_row(hermes_root: Path):
    _seeded_attachment(hermes_root)
    kwargs = {
        "evidence_ref": "delegation:dlg-1",
        "verified": False,
        "hermes_root": hermes_root,
    }
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-1", "running", **kwargs
        )
        is True
    )
    version = _version(hermes_root)
    attachment = _attachment(hermes_root)
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-1", "running", **kwargs
        )
        is True
    )
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-1", "running", **kwargs
        )
        is True
    )
    assert _version(hermes_root) == version
    assert _attachment(hermes_root) == attachment


def test_real_transitions_still_write_one_event_each(hermes_root: Path):
    _seeded_attachment(hermes_root)
    assert (
        mission.record_attachment_state(
            "msn-test",
            "delegation",
            "dlg-1",
            "running",
            evidence_ref="delegation:dlg-1",
            hermes_root=hermes_root,
        )
        is True
    )
    assert (
        mission.record_attachment_state(
            "msn-test",
            "delegation",
            "dlg-1",
            "succeeded",
            evidence_ref="contract:abc",
            verified=True,
            hermes_root=hermes_root,
        )
        is True
    )
    assert _attachment_events(hermes_root) == 2
    final = _attachment(hermes_root)
    assert final["state"] == "succeeded"
    assert final["evidence_ref"] == "contract:abc"
    assert final["verified"] == 1


def test_same_state_new_evidence_still_records(hermes_root: Path):
    _seeded_attachment(hermes_root)
    # The pending reservation re-drive (item 252) preserves an existing
    # contract: evidence; a genuine evidence change on the same state is a
    # real update and must still be written.
    assert (
        mission.record_attachment_state(
            "msn-test",
            "delegation",
            "dlg-1",
            "pending",
            evidence_ref="delegation:dlg-1",
            hermes_root=hermes_root,
        )
        is True
    )
    assert (
        mission.record_attachment_state(
            "msn-test",
            "delegation",
            "dlg-1",
            "pending",
            evidence_ref="contract:def",
            hermes_root=hermes_root,
        )
        is True
    )
    assert _attachment_events(hermes_root) == 2
    assert _attachment(hermes_root)["evidence_ref"] == "contract:def"


def test_succeeded_gate_and_missing_attachment_unregressed(hermes_root: Path):
    _seeded_attachment(hermes_root)
    # Success still demands verified evidence (entry gate, unchanged).
    assert (
        mission.record_attachment_state(
            "msn-test",
            "delegation",
            "dlg-1",
            "succeeded",
            evidence_ref="contract:abc",
            hermes_root=hermes_root,
        )
        is False
    )
    # Missing attachment still fails (bridge updates existing only).
    assert (
        mission.record_attachment_state(
            "msn-test", "delegation", "dlg-missing", "running", hermes_root=hermes_root
        )
        is False
    )
    assert _attachment_events(hermes_root) == 0
