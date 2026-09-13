"""Tests for the mission-scoped spend envelope (budget_accounts + budget_check).

Mirrors ``test_operator_mission_plan.py``: all runs against a temp ``hermes_root``
(never production data). Operator policy is forced to ``workspace + direct`` so
the envelope set / spend record writes proceed; read surfaces only require
``read_only``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_mission_budget as budget
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


def _spec(mid: str = "msn-budget") -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Coordinate a budgeted slice",
            "objective": "Exercise the mission-scoped spend envelope in dry-run.",
            "owner_profile": "default",
            "acceptance_criteria": ["envelope set", "budget_check surface"],
            "context_refs": [],
            "skills": [],
            "final_approval_required": True,
        }
    )


def _j(value: str) -> dict:
    return json.loads(value)


def _make_mission(root: Path, mid: str = "msn-budget") -> None:
    out = _j(
        mission.hermes_mission_create(
            _spec(mid), confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out


def _set(
    root: Path, mid: str = "msn-budget", quota: float = 1000.0, policy_json: str = ""
) -> dict:
    out = _j(
        budget.hermes_budget_set(
            mid, quota, policy_json, confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out
    return out


# ---------------------------------------------------------------------------
# Envelope set / get
# ---------------------------------------------------------------------------


def test_budget_set_get_persists_envelope(hermes_root):
    _make_mission(hermes_root)
    created = _set(hermes_root, quota=1000.0)
    assert created["unit"] == "tokens"
    assert created["policy"]["hard_block_enabled"] is False
    got = _j(budget.hermes_budget_get("msn-budget", hermes_root=hermes_root))
    assert got["success"] is True
    assert got["found"] is True
    assert got["quota"] == 1000.0
    assert got["spend"] == 0.0
    assert got["envelope"]["status"] == "within"


def test_budget_set_requires_existing_mission(hermes_root):
    out = _j(
        budget.hermes_budget_set(
            "msn-nope", 500.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert out["success"] is False


def test_budget_set_validates_quota(hermes_root):
    _make_mission(hermes_root)
    for bad in (-1, 0, float("nan"), float("inf"), "nope"):
        out = _j(
            budget.hermes_budget_set(
                "msn-budget", bad, confirm=True, dry_run=False, hermes_root=hermes_root
            )
        )
        assert out["success"] is False, f"quota {bad!r} should be rejected"


def test_budget_set_dry_run_writes_nothing(hermes_root):
    _make_mission(hermes_root)
    out = _j(
        budget.hermes_budget_set(
            "msn-budget", 500.0, confirm=False, dry_run=True, hermes_root=hermes_root
        )
    )
    assert out["success"] is True and out["dry_run"] is True
    # Dry-run writes nothing: the budget_accounts table must not exist.
    path = mission._db_path(hermes_root)
    with sqlite3.connect(path) as con:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_accounts'"
        ).fetchone()
        assert table is None


# ---------------------------------------------------------------------------
# budget_check surface
# ---------------------------------------------------------------------------


def test_budget_check_within_envelope(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=1000.0)
    chk = _j(budget.hermes_budget_check("msn-budget", hermes_root=hermes_root))
    assert chk["success"] is True
    assert chk["envelope_status"] == "within"
    assert chk["crosses_envelope"] is False
    assert chk["block"]["hard_block_enabled"] is False
    assert chk["block"]["would_pause"] is False
    assert chk["block"]["enforcement_phase"] == "phase_4_5"


def test_budget_check_reports_crossing(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=10.0)
    _j(
        budget.hermes_budget_record(
            "msn-budget", 12.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    chk = _j(budget.hermes_budget_check("msn-budget", hermes_root=hermes_root))
    assert chk["envelope_status"] == "crossing"
    assert chk["crosses_envelope"] is True
    # Default policy: hard_block OFF -> would not pause (Phase 2 dry-run).
    assert chk["block"]["hard_block_enabled"] is False
    assert chk["block"]["would_pause"] is False


def test_budget_check_hardblock_flag_design(hermes_root):
    _make_mission(hermes_root)
    policy = json.dumps({"hard_block_enabled": True, "pause_on_cross": True})
    _set(hermes_root, quota=10.0, policy_json=policy)
    _j(
        budget.hermes_budget_record(
            "msn-budget", 12.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    chk = _j(budget.hermes_budget_check("msn-budget", hermes_root=hermes_root))
    # D3 on-crossing semantics are designed (would_pause True) but enforcement is
    # Phase 4/5: enforced_now stays False.
    assert chk["block"]["hard_block_enabled"] is True
    assert chk["block"]["would_pause"] is True
    assert chk["block"]["enforced_now"] is False


def test_budget_check_missing_account_returns_not_found(hermes_root):
    _make_mission(hermes_root)
    chk = _j(budget.hermes_budget_check("msn-budget", hermes_root=hermes_root))
    assert chk["success"] is True
    assert chk["found"] is False


# ---------------------------------------------------------------------------
# budget_record
# ---------------------------------------------------------------------------


def test_budget_record_accumulates_and_appends_event(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=100.0)
    r1 = _j(
        budget.hermes_budget_record(
            "msn-budget",
            30.0,
            ref="node:a",
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    assert r1["success"] is True
    assert r1["spend_after"] == 30.0
    assert r1["crosses_envelope"] is False
    r2 = _j(
        budget.hermes_budget_record(
            "msn-budget", 20.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert r2["spend_before"] == 30.0
    assert r2["spend_after"] == 50.0
    got = _j(budget.hermes_budget_get("msn-budget", hermes_root=hermes_root))
    assert got["spend"] == 50.0
    # Two budget_events appended.
    path = mission._db_path(hermes_root)
    with sqlite3.connect(path) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM budget_events WHERE mission_id='msn-budget'"
        ).fetchone()[0]
        assert n == 2


def test_budget_record_never_pauses_mission(hermes_root):
    _make_mission(hermes_root)
    policy = json.dumps({"hard_block_enabled": True})
    _set(hermes_root, quota=10.0, policy_json=policy)
    rec = _j(
        budget.hermes_budget_record(
            "msn-budget", 50.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert rec["success"] is True
    assert rec["crosses_envelope"] is True
    assert rec["block"]["would_pause"] is True
    assert rec["enforcement"] == "not_executed_phase_2"
    # The Mission is untouched: still draft, never paused/blocked/completed.
    md = _j(mission.hermes_mission_get("msn-budget", hermes_root=hermes_root))
    assert md["status"] == "draft"
    assert md["attachments"] == []


def test_budget_record_dry_run_does_not_write(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=100.0)
    out = _j(
        budget.hermes_budget_record(
            "msn-budget", 40.0, confirm=False, dry_run=True, hermes_root=hermes_root
        )
    )
    assert out["success"] is True and out["dry_run"] is True
    assert out["spend_after"] == 40.0
    got = _j(budget.hermes_budget_get("msn-budget", hermes_root=hermes_root))
    assert got["spend"] == 0.0  # unchanged


def test_budget_record_rejects_bad_amount(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=100.0)
    for bad in (-1, float("nan"), float("inf"), "x"):
        out = _j(
            budget.hermes_budget_record(
                "msn-budget", bad, confirm=True, dry_run=False, hermes_root=hermes_root
            )
        )
        assert out["success"] is False, f"amount {bad!r} should be rejected"


def test_budget_record_requires_existing_account(hermes_root):
    _make_mission(hermes_root)
    out = _j(
        budget.hermes_budget_record(
            "msn-budget", 10.0, confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert out["success"] is False  # no envelope set yet


# ---------------------------------------------------------------------------
# INV-8 / INV-9
# ---------------------------------------------------------------------------


def test_inv9_no_secret_content_persisted(hermes_root):
    _make_mission(hermes_root)
    _set(hermes_root, quota=100.0)
    _j(
        budget.hermes_budget_record(
            "msn-budget",
            5.0,
            ref="contract:abc",
            reason="AIzaSySECRETKEY9abcdefghijklmnop",
            confirm=True,
            dry_run=False,
            hermes_root=hermes_root,
        )
    )
    path = mission._db_path(hermes_root)
    with sqlite3.connect(path) as con:
        acct = con.execute(
            "SELECT policy_json FROM budget_accounts WHERE mission_id='msn-budget'"
        ).fetchone()
        assert acct is not None
        raw = acct[0]
        assert "SECRETKEY" not in raw
        # reason is stored only as a hash, never raw.
        ev = con.execute(
            "SELECT reason_sha256 FROM budget_events WHERE mission_id='msn-budget'"
        ).fetchone()
        assert ev is not None
        assert ev[0]  # hash present
        assert len(ev[0]) == 64
    # Public output also carries only a hash for reason.
    got = _j(budget.hermes_budget_get("msn-budget", hermes_root=hermes_root))
    assert "SECRETKEY" not in json.dumps(got)


def test_inv8_envelope_invariants(hermes_root):
    # Envelope predicate: within requires spend < quota.
    assert budget.within_envelope({}, 5, 10) is True
    assert budget.within_envelope({}, 10, 10) is False
    assert budget.within_envelope({}, 11, 10) is False
    # Unbounded / bad envelopes are not "within" (fail-closed).
    assert budget.within_envelope({}, 1, 0) is False
    assert budget.validate_envelope({}, 1, 10) is True
    assert budget.validate_envelope({}, -1, 10) is False
    assert budget.validate_envelope({}, 0, 0) is False


def test_validate_envelope_rejects_bad_policy(hermes_root):
    from operator_mission_budget import _clean_policy

    with pytest.raises(ValueError):
        _clean_policy({"unit": "not-a-unit"})
    with pytest.raises(ValueError):
        _clean_policy({"schema": "wrong"})
    # hard_block_enabled must be boolean.
    with pytest.raises(TypeError):
        _clean_policy({"hard_block_enabled": "yes"})
