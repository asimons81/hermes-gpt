"""Budget D3 hard-block enforcement tests (v0.12 slice-2, Pack A).

Covers ``docs/design/v0.12-budget-enforcement.md`` §5 failure modes and §6
test list for :func:`operator_mission_budget.enforce_budget_breaker`, the
``hermes_budget_check(enforce=True)`` extension, and the controller §2.2
reconcile-pass seam:

- gate matrix (each missing gate → stable refusal reason + ZERO writes);
- full-green path (paused mission + one break row + one spooled INTERRUPT +
  bounded result envelope);
- not-pausable fail-closed (need_attention, proposed_action="attention",
  no dispatch);
- INV-11 anti-TOCTOU (policy flips between gate evaluation and action →
  ``policy_changed``, zero writes);
- idempotent repeat (``already_enforced``, no duplicate break rows, one spool
  entry max);
- ``pause_on_cross=False`` → report-only;
- dry-run preview (``enforce=True`` without ``confirm`` → zero writes);
- disabled flag → byte-identical dry-run semantics, zero writes;
- INV-9 boundedness of the break row + result envelope;
- custom ``breaker_signal`` honored;
- ``enforce=True`` with a non-crossing envelope → ``not_crossing``;
- controller seam: ``budget_enforcement`` null/absent when disabled,
  populated when armed; L0/L1 outputs byte-identical when disabled.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_controller as ctl
import operator_mission_budget as budget
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_policy as op

HARD_POLICY = json.dumps({"hard_block_enabled": True, "pause_on_cross": True})


def _dag(mid: str) -> str:
    """Minimal single-node plan so the controller observation path can read
    plan_nodes."""
    return json.dumps(
        {
            "schema": plan.PLAN_SCHEMA,
            "mission_id": mid,
            "version": 1,
            "decomposition": "operator-provided",
            "objective": "Exercise the enforcement seam.",
            "nodes": [
                {
                    "node_id": "a",
                    "kind": "single",
                    "owner": "hermes-dev",
                    "parents": [],
                    "objective": "Node objective.",
                    "capability_req": {
                        "profile": "hermes-dev",
                        "skills": [],
                        "authorization_class": "reversible_write",
                    },
                    "budget": {"est_minutes": 30, "est_tokens": 50_000},
                    "expected_artifacts": ["contract.json"],
                },
            ],
        }
    )


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(budget.BUDGET_HARD_BLOCK_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    return root


def _j(value: str) -> dict:
    return json.loads(value)


def _spec(mid: str = "msn-b") -> str:
    return json.dumps(
        {
            "schema": mission.MISSION_SPEC_SCHEMA,
            "mission_id": mid,
            "title": "Budget enforcement sandbox",
            "objective": "Exercise the D3 hard-block enforcement path.",
            "owner_profile": "default",
            "acceptance_criteria": ["enforcement green"],
            "context_refs": [],
            "skills": [],
            "final_approval_required": True,
        }
    )


def _make_mission(root: Path, mid: str = "msn-b") -> None:
    out = _j(
        mission.hermes_mission_create(
            _spec(mid), confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out


def _set(root: Path, mid: str = "msn-b", quota: float = 10.0, policy_json: str = "") -> dict:
    out = _j(
        budget.hermes_budget_set(
            mid, quota, policy_json, confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out
    return out


def _cross(root: Path, mid: str = "msn-b", amount: float = 12.0) -> None:
    out = _j(
        budget.hermes_budget_record(
            mid, amount, confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert out["success"] is True, out


def _status(root: Path, mid: str) -> str:
    with sqlite3.connect(mission._db_path(root)) as con:
        return con.execute(
            "SELECT status FROM missions WHERE mission_id=?", (mid,)
        ).fetchone()[0]


def _break_rows(root: Path, mid: str) -> list[tuple]:
    with sqlite3.connect(mission._db_path(root)) as con:
        return con.execute(
            "SELECT seq,event_type,spend_after,quota,status,hard_block,ref,reason_sha256 "
            "FROM budget_events WHERE mission_id=? AND event_type='break' ORDER BY seq",
            (mid,),
        ).fetchall()


def _spool_lines(root: Path) -> list[dict]:
    path = root / "missions" / "controller_attention_spool.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _store_fingerprint(root: Path) -> tuple:
    """Everything the enforcement path could touch, hashed for zero-write proofs.

    Includes the WAL sidecar: budget/mission writes go through WAL, so a
    write can land in ``-wal`` before the main db file changes.
    """
    h = __import__("hashlib").sha256()
    dbp = mission._db_path(root)
    for p in (dbp, dbp.parent / (dbp.name + "-wal")):
        if p.is_file():
            h.update(p.read_bytes())
    spool = root / "missions" / "controller_attention_spool.jsonl"
    if spool.is_file():
        h.update(spool.read_bytes())
    return h.hexdigest()


def _armed(root: Path, mid: str = "msn-b", *, running: bool = True, policy_json: str = HARD_POLICY) -> None:
    """Create a mission whose envelope is crossing with the hard-block armed."""
    _make_mission(root, mid)
    _set(root, mid, quota=10.0, policy_json=policy_json)
    _cross(root, mid)
    if running:
        out = _j(
            mission.hermes_mission_transition(
                mid, "running", confirm=True, dry_run=False, hermes_root=root
            )
        )
        assert out["success"] is True, out


def _full_gates_on(monkeypatch) -> None:
    monkeypatch.setenv(budget.BUDGET_HARD_BLOCK_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


# ---------------------------------------------------------------------------
# Gate matrix: each missing gate → stable refusal + zero writes
# ---------------------------------------------------------------------------


def test_gate_confirm_missing_refuses_with_zero_writes(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=False)
    )
    assert out["enforced"] is False
    assert out["reason"] == "confirm_required"
    assert _store_fingerprint(hermes_root) == before
    assert _status(hermes_root, "msn-b") == "running"


def test_gate_machine_off_refuses(hermes_root, monkeypatch):
    _armed(hermes_root)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(budget.BUDGET_HARD_BLOCK_ENV, raising=False)
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "hard_block_gate_off"
    assert _store_fingerprint(hermes_root) == before


def test_gate_operator_off_refuses(hermes_root, monkeypatch):
    _armed(hermes_root)
    monkeypatch.setenv(budget.BUDGET_HARD_BLOCK_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "0")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "not_enabled"
    assert _store_fingerprint(hermes_root) == before


def test_gate_not_direct_refuses(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "not_direct"
    assert _store_fingerprint(hermes_root) == before


def test_gate_pause_on_cross_false_reports_only(hermes_root, monkeypatch):
    _armed(hermes_root, policy_json=json.dumps({"hard_block_enabled": True, "pause_on_cross": False}))
    _full_gates_on(monkeypatch)
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    # Report-only: crossing + hard_block on, but pause_on_cross false → disabled.
    assert out["enforced"] is False
    assert out["reason"] == "disabled"
    assert _store_fingerprint(hermes_root) == before


def test_gate_hard_block_flag_off_refuses(hermes_root, monkeypatch):
    _armed(hermes_root, policy_json=json.dumps({"hard_block_enabled": False}))
    _full_gates_on(monkeypatch)
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["reason"] == "disabled"
    assert out["enforced"] is False
    assert _store_fingerprint(hermes_root) == before


# ---------------------------------------------------------------------------
# Full-green path
# ---------------------------------------------------------------------------


def test_full_green_enforcement_pauses_signals_records(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["success"] is True
    assert out["enforced"] is True
    assert out["reason"] == "enforced"
    assert out["need_attention"] is False
    assert out["would_pause"] is True
    assert out["breaker_signal"] == "budget_breaker"
    assert out["spend_after"] == 12.0
    assert out["transition"] == {"applied": True, "from": "running", "to": "paused"}
    assert out["spooled"] is True
    # Mission paused, exactly one break row, one spooled INTERRUPT envelope.
    assert _status(hermes_root, "msn-b") == "paused"
    rows = _break_rows(hermes_root, "msn-b")
    assert len(rows) == 1
    assert rows[0][2] == 12.0  # spend_after
    assert rows[0][6] == "budget_breaker"  # ref
    lines = _spool_lines(hermes_root)
    assert len(lines) == 1
    env = lines[0]
    assert env["attention_class"] == "INTERRUPT"
    assert env["metadata"]["row_key"] == "budget_breaker"
    assert env["metadata"]["classification"] == "budget_crossing"
    assert env["metadata"]["proposed_action"] == "pause_mission"
    # The transition audit carries the budget_breaker reason (as its sha).
    import hashlib

    want = hashlib.sha256(b"budget_breaker").hexdigest()
    with sqlite3.connect(mission._db_path(hermes_root)) as con:
        evs = con.execute(
            "SELECT reason_sha256 FROM mission_events WHERE mission_id='msn-b' "
            "AND event_type='mission.transition'"
        ).fetchall()
    assert any(e[0] == want for e in evs)


def test_custom_breaker_signal_honored(hermes_root, monkeypatch):
    _armed(
        hermes_root,
        policy_json=json.dumps(
            {"hard_block_enabled": True, "pause_on_cross": True, "breaker_signal": "spend_wall"}
        ),
    )
    _full_gates_on(monkeypatch)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is True
    assert out["breaker_signal"] == "spend_wall"
    # The signal name lands in the spool title/summary + reason sha of the row.
    raw = json.dumps(_spool_lines(hermes_root))
    assert "spend_wall" not in raw  # INV-9: signal name itself stays bounded-enums
    rows = _break_rows(hermes_root, "msn-b")
    assert len(rows) == 1
    sha = budget._break_reason_sha(
        {"breaker_signal": "spend_wall", "unit": "tokens"}
    )
    assert rows[0][7] == sha


# ---------------------------------------------------------------------------
# Not pausable → fail closed
# ---------------------------------------------------------------------------


def test_not_pausable_fails_closed_with_attention(hermes_root, monkeypatch):
    # draft IS pausable → use awaiting_approval, which MISSION_TRANSITIONS
    # forbids pausing, entered only by reconciliation.
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    with sqlite3.connect(mission._db_path(hermes_root)) as con:
        con.execute("UPDATE missions SET status='awaiting_approval' WHERE mission_id='msn-b'")
        con.commit()
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "not_pausable"
    assert out["need_attention"] is True
    assert out["transition"] is None
    # Fail-closed: signal spooled exactly once with proposed_action=attention.
    lines = _spool_lines(hermes_root)
    assert len(lines) == 1
    assert lines[0]["metadata"]["proposed_action"] == "attention"
    # Break row still recorded (the crossing IS observed) — one entry.
    assert len(_break_rows(hermes_root, "msn-b")) == 1
    # A repeat stays idempotent: no second row, no second spool.
    out2 = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out2["reason"] == "already_enforced"
    assert len(_break_rows(hermes_root, "msn-b")) == 1
    assert len(_spool_lines(hermes_root)) == 1


# ---------------------------------------------------------------------------
# INV-11 anti-TOCTOU
# ---------------------------------------------------------------------------


def test_policy_flip_mid_action_aborts_zero_writes(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    before = _store_fingerprint(hermes_root)

    real_operator_policy = op.OperatorPolicy
    calls = {"n": 0}

    class FlipPolicy:
        def __init__(self):
            calls["n"] += 1
            if calls["n"] >= 2:
                # Flip AFTER the gate evaluation snapshot (call #1) but BEFORE
                # the re-snapshot constructs its policy — the INV-11 re-snap
                # (call #2) must observe the change and abort with zero writes.
                monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
            self._real = real_operator_policy()

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(op, "OperatorPolicy", FlipPolicy)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "policy_changed"
    assert calls["n"] == 2  # re-snapshot provably happened
    assert _store_fingerprint(hermes_root) == before
    assert _status(hermes_root, "msn-b") == "running"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_repeat_enforcement_is_idempotent(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    first = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert first["enforced"] is True
    second = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert second["enforced"] is False
    assert second["reason"] == "already_enforced"
    assert len(_break_rows(hermes_root, "msn-b")) == 1
    assert len(_spool_lines(hermes_root)) == 1
    assert _status(hermes_root, "msn-b") == "paused"


# ---------------------------------------------------------------------------
# budget_check enforce extension
# ---------------------------------------------------------------------------


def test_budget_check_enforce_true_confirm_missing_is_preview(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    before = _store_fingerprint(hermes_root)
    chk = _j(
        budget.hermes_budget_check(
            "msn-b", hermes_root=hermes_root, enforce=True, confirm=False
        )
    )
    assert chk["success"] is True
    assert chk["enforcement"]["enforced"] is False
    assert chk["enforcement"]["reason"] == "confirm_required"
    assert _store_fingerprint(hermes_root) == before


def test_budget_check_enforce_true_executes_on_full_gates(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    chk = _j(
        budget.hermes_budget_check(
            "msn-b", hermes_root=hermes_root, enforce=True, confirm=True
        )
    )
    assert chk["enforcement"]["enforced"] is True
    assert chk["enforcement"]["transition"]["to"] == "paused"
    assert _status(hermes_root, "msn-b") == "paused"


def test_budget_check_enforce_true_non_crossing(hermes_root, monkeypatch):
    _make_mission(hermes_root)
    _set(hermes_root, quota=100.0, policy_json=HARD_POLICY)
    _cross(hermes_root, amount=5.0)  # within envelope
    _full_gates_on(monkeypatch)
    chk = _j(
        budget.hermes_budget_check(
            "msn-b", hermes_root=hermes_root, enforce=True, confirm=True
        )
    )
    assert chk["enforcement"]["enforced"] is False
    assert chk["enforcement"]["reason"] == "not_crossing"
    assert _status(hermes_root, "msn-b") == "draft"


def test_budget_check_default_is_byte_identical(hermes_root):
    # enforce=False (default) must not carry any enforcement keys, and the
    # payload must be identical pre/post capability existing.
    _make_mission(hermes_root)
    _set(hermes_root, quota=10.0, policy_json=HARD_POLICY)
    _cross(hermes_root)
    a = budget.hermes_budget_check("msn-b", hermes_root=hermes_root)
    b = budget.hermes_budget_check("msn-b", hermes_root=hermes_root)
    assert a == b
    assert "enforcement" not in _j(a)


# ---------------------------------------------------------------------------
# Disabled → zero writes; INV-9 boundedness
# ---------------------------------------------------------------------------


def test_disabled_flag_zero_writes_anywhere(hermes_root):
    # Machine gate off (default): even with policy armed + confirm, zero writes.
    _armed(hermes_root)
    before = _store_fingerprint(hermes_root)
    out = _j(
        budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    )
    assert out["enforced"] is False
    assert out["reason"] == "hard_block_gate_off"
    assert _store_fingerprint(hermes_root) == before
    assert _status(hermes_root, "msn-b") == "running"
    assert _break_rows(hermes_root, "msn-b") == []
    assert _spool_lines(hermes_root) == []


def test_inv9_break_row_and_envelope_are_bounded(hermes_root, monkeypatch):
    _armed(hermes_root)
    _full_gates_on(monkeypatch)
    raw = budget.enforce_budget_breaker("msn-b", hermes_root=hermes_root, confirm=True)
    out = _j(raw)
    # Result envelope: bounded keys only — no free text beyond fixed enums.
    assert set(out) == {
        "success",
        "mission_id",
        "enforced",
        "reason",
        "need_attention",
        "would_pause",
        "breaker_signal",
        "spend_after",
        "transition",
        "spooled",
    }
    assert "SECRET" not in raw
    # Break row: bounded columns only.
    rows = _break_rows(hermes_root, "msn-b")
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "break"  # event_type
    assert row[2] == 12.0  # spend_after
    assert row[3] == 10.0  # quota
    assert row[6] == "budget_breaker"  # ref
    assert len(row[7]) == 64  # reason_sha256
    assert row[5] == 1  # hard_block
    # The spool envelope family is fleet-attention-envelope/v1-shaped and
    # carries no raw text beyond the fixed summary template.
    env = _spool_lines(hermes_root)[0]
    assert env["schema_version"] == 1
    assert env["metadata"]["row_key"] == "budget_breaker"


# ---------------------------------------------------------------------------
# Controller §2.2 seam
# ---------------------------------------------------------------------------


def _setup_controller_mission(root: Path) -> None:
    _make_mission(root)
    p = _j(
        plan.hermes_plan_create(
            "msn-b", _dag("msn-b"), confirm=True, dry_run=False, hermes_root=root
        )
    )
    assert p["success"] is True, p


def test_controller_seam_disabled_keeps_output_byte_identical(hermes_root):
    _setup_controller_mission(root=hermes_root)
    _set(hermes_root, quota=10.0, policy_json=HARD_POLICY)
    _cross(hermes_root)
    out = _j(
        mission.hermes_mission_transition(
            "msn-b", "running", confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert out["success"] is True
    # Machine gate off (fixture default) → pass output carries NO
    # budget_enforcement key and the mission is untouched.
    result = ctl.reconcile_pass(
        "msn-b", ctl.TRIGGER_MANUAL, hermes_root=hermes_root
    )
    assert "budget_enforcement" not in result
    assert result["would_execute"] is False
    assert _status(hermes_root, "msn-b") == "running"
    assert _break_rows(hermes_root, "msn-b") == []
    assert _spool_lines(hermes_root) == []


def test_controller_seam_armed_enforces_on_pass(hermes_root, monkeypatch):
    _setup_controller_mission(root=hermes_root)
    _set(hermes_root, quota=10.0, policy_json=HARD_POLICY)
    _cross(hermes_root)
    out = _j(
        mission.hermes_mission_transition(
            "msn-b", "running", confirm=True, dry_run=False, hermes_root=hermes_root
        )
    )
    assert out["success"] is True
    _full_gates_on(monkeypatch)
    result = ctl.reconcile_pass(
        "msn-b", ctl.TRIGGER_MANUAL, hermes_root=hermes_root
    )
    assert result["budget_enforcement"]["enforced"] is True
    assert result["budget_enforcement"]["transition"]["to"] == "paused"
    assert _status(hermes_root, "msn-b") == "paused"
    assert len(_break_rows(hermes_root, "msn-b")) == 1
    # actions_taken records the enforcement for telemetry.
    actions = {a["action"] for a in result["actions_taken"]}
    assert "budget_enforce" in actions
