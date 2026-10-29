"""Tests for the one-call Mission launch (v0.16): ``hermes_mission_launch``.

Uses temp Hermes roots, the same Operator env as ``test_operator_autopilot.py``,
and a stubbed ``subprocess.Popen`` so no real detached worker is ever spawned.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_mission_budget as budget
import operator_mission_launch as launch
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_policy as op

SECRET_MARKER = "SECRET-OBJECTIVE-MARKER-7431"


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
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV, raising=False)
    monkeypatch.delenv(budget.BUDGET_HARD_BLOCK_ENV, raising=False)
    return root


@pytest.fixture
def gate_on(monkeypatch) -> None:
    monkeypatch.setenv(autopilot.AUTOPILOT_ENV, "1")


class _FakeProc:
    pid = 4242


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    """Record worker spawns instead of starting a real detached process."""
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(autopilot.subprocess, "Popen", fake_popen)
    return calls


def _j(value: str) -> dict:
    return json.loads(value)


def _spec(mid: str | None = None, **overrides) -> str:
    raw = {
        "schema": mission.MISSION_SPEC_SCHEMA,
        "title": "Launch test mission",
        "objective": f"Exercise the one-call launch path. {SECRET_MARKER}",
        "owner_profile": "default",
        "acceptance_criteria": ["launch creates a running Autopilot"],
        "context_refs": [],
        "skills": [],
        "final_approval_required": True,
    }
    if mid is not None:
        raw["mission_id"] = mid
    raw.update(overrides)
    return json.dumps(raw)


def _snapshot(root: Path) -> dict[str, str]:
    """Hash every file and directory under the root so any write shows up as a diff."""
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_file():
            state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            state[rel + "/"] = "dir"
    return state


def _audit_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _launch_records(tmp_path: Path) -> list[dict]:
    return [r for r in _audit_lines(tmp_path) if r.get("tool") == launch.TOOL]


def _steps(out: dict) -> dict[str, dict]:
    return {s["step"]: s for s in out["steps"]}


def _mission_exists(mid: str, root: Path) -> bool:
    got = _j(mission.hermes_mission_get(mid, hermes_root=root))
    return bool(got.get("success")) and got.get("found") is not False


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_previews_every_step(hermes_root, gate_on, spawned, tmp_path):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(
        _spec(), budget_json=json.dumps({"quota": 50}),
        autopilot_limits_json=json.dumps({"max_concurrency": 2}),
        dry_run=True, hermes_root=hermes_root,
    ))

    assert out["success"] is True, out
    assert out["dry_run"] is True
    assert out["would_start_autopilot"] is True
    assert out["refusal"] is None
    assert [s["step"] for s in out["steps"]] == list(launch.CHAIN)
    steps = _steps(out)
    assert steps["mission_create"]["outcome"] == "preview"
    assert steps["plan_create"]["outcome"] == "would_run"
    assert steps["budget_set"]["outcome"] == "preview"
    assert steps["autopilot_start"]["outcome"] == "would_run"
    assert out["effective_limits"]["max_concurrency"] == 2
    assert out["effective_limits"]["max_replans"] == 2
    # No id is fabricated for a spec that does not carry one.
    assert out["mission_id"] is None
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_dry_run_is_the_default(hermes_root, gate_on, spawned):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), hermes_root=hermes_root))
    assert out["dry_run"] is True
    assert out["success"] is True
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_dry_run_with_explicit_id_reports_it_and_still_writes_nothing(hermes_root, gate_on, tmp_path):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec("msn-launch-dry"), dry_run=True, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["mission_id"] == "msn-launch-dry"
    assert _snapshot(hermes_root) == before
    assert _mission_exists("msn-launch-dry", hermes_root) is False


def test_dry_run_reports_gate_off_without_refusing_the_preview(hermes_root, spawned):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=True, hermes_root=hermes_root))
    assert out["would_start_autopilot"] is False
    assert out["success"] is False
    assert out["refusal"]["code"] == "AUTOPILOT_GATE_OFF"
    assert out["refusal"]["step"] == "preflight"
    assert out["preflight"]["autopilot_gate"]["ok"] is False
    assert _steps(out)["autopilot_start"]["outcome"] == "refused"
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_dry_run_in_dry_apply_mode_reports_direct_required(hermes_root, gate_on, monkeypatch, spawned):
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=True, hermes_root=hermes_root))
    assert out["refusal"]["code"] == "APPLY_MODE_NOT_DIRECT"
    assert out["would_start_autopilot"] is False
    assert _snapshot(hermes_root) == before


# ---------------------------------------------------------------------------
# Confirm and preflight
# ---------------------------------------------------------------------------


def test_apply_requires_confirm_before_doing_anything(hermes_root, gate_on, spawned, tmp_path):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=False, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["refusal"]["code"] == "CONFIRM_REQUIRED"
    assert out["mission_id"] is None
    assert _snapshot(hermes_root) == before
    assert spawned == []
    records = _launch_records(tmp_path)
    assert len(records) == 1 and records[0]["success"] is False


def test_apply_refused_when_gate_off_creates_nothing(hermes_root, spawned):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["refusal"]["code"] == "AUTOPILOT_GATE_OFF"
    assert out["refusal"]["step"] == "preflight"
    assert out["completed_steps"] == []
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_apply_refused_at_wrong_operator_level_creates_nothing(hermes_root, gate_on, monkeypatch, spawned):
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["refusal"]["code"] == "OPERATOR_LEVEL_INSUFFICIENT"
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_apply_refused_when_operator_disabled_creates_nothing(hermes_root, gate_on, monkeypatch, spawned):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "0")
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["refusal"]["code"] == "OPERATOR_DISABLED"
    assert _snapshot(hermes_root) == before


def test_apply_refused_in_dry_apply_mode_creates_nothing(hermes_root, gate_on, monkeypatch, spawned):
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["refusal"]["code"] == "APPLY_MODE_NOT_DIRECT"
    assert out["autopilot_started"] is False
    assert _snapshot(hermes_root) == before
    assert spawned == []


# ---------------------------------------------------------------------------
# Happy path and optional budget
# ---------------------------------------------------------------------------


def test_apply_creates_mission_plan_budget_and_starts_autopilot(hermes_root, gate_on, spawned, tmp_path):
    out = _j(launch.hermes_mission_launch(
        _spec(), budget_json=json.dumps({"quota": 50, "policy": {"unit": "tokens"}}),
        autopilot_limits_json=json.dumps({"max_concurrency": 2, "max_runtime_seconds": 3600}),
        dry_run=False, confirm=True, hermes_root=hermes_root,
    ))

    assert out["success"] is True, out
    assert out["dry_run"] is False
    assert out["autopilot_started"] is True
    assert out["refusal"] is None
    assert out["completed_steps"] == list(launch.CHAIN)
    assert out["not_run_steps"] == []
    assert [s["step"] for s in out["steps"]] == list(launch.CHAIN)
    assert all(s["outcome"] == "completed" for s in out["steps"])
    mid = out["mission_id"]
    assert mid and mid.startswith("msn-")
    assert out["created"]["mission_id"] == mid
    assert out["created"]["autopilot"]["job_id"]

    got = _j(mission.hermes_mission_get(mid, hermes_root=hermes_root))
    assert got["status"] == "draft"
    assert got["final_approval_required"] is True
    assert _j(plan.hermes_plan_get(mid, hermes_root=hermes_root))["nodes"]
    assert _j(budget.hermes_budget_check(mid, hermes_root=hermes_root))["found"] is True

    run = autopilot._read_run(mid, hermes_root)
    assert run is not None
    assert run["state"] == "running"
    assert run["max_concurrency"] == 2
    assert run["max_runtime_seconds"] == 3600
    assert run["max_replans"] == launch._LIMIT_DEFAULTS["max_replans"]
    assert len(spawned) == 1
    argv = spawned[0]
    assert "--worker" in argv and mid in argv

    records = _launch_records(tmp_path)
    assert len(records) == 1
    assert records[0]["success"] is True and records[0]["dry_run"] is False
    assert records[0]["mission_id"] == mid


def test_apply_without_budget_skips_budget_and_does_not_hold_dispatch(hermes_root, gate_on, spawned):
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["success"] is True, out
    steps = _steps(out)
    assert steps["budget_set"]["outcome"] == "skipped"
    assert "unrestricted" in steps["budget_set"]["detail"]
    mid = out["mission_id"]
    # No account exists, so the Autopilot budget gate reads clear rather than holding.
    assert _j(budget.hermes_budget_check(mid, hermes_root=hermes_root))["found"] is False
    assert autopilot._budget_gate(mid, hermes_root) == ("", {})
    assert autopilot._read_run(mid, hermes_root)["state"] == "running"


def test_rerun_creates_a_new_mission(hermes_root, gate_on, spawned):
    first = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    second = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))
    assert first["success"] and second["success"]
    assert first["mission_id"] != second["mission_id"]
    assert second["note"] == launch.NOTE


# ---------------------------------------------------------------------------
# Partial failure reporting (nothing is rolled back)
# ---------------------------------------------------------------------------


def test_plan_failure_mid_chain_reports_created_mission(hermes_root, gate_on, spawned, monkeypatch):
    def refuse_plan(*args, **kwargs):
        return json.dumps({"success": False, "code": "PLAN_CREATE_REJECTED", "safe_message": "plan store busy",
                           "suggested_action": "Retry plan creation."})

    monkeypatch.setattr(plan, "hermes_plan_create", refuse_plan)
    out = _j(launch.hermes_mission_launch(
        _spec(), budget_json=json.dumps({"quota": 10}), dry_run=False, confirm=True, hermes_root=hermes_root,
    ))

    assert out["success"] is False
    assert out["refusal"]["code"] == "PLAN_CREATE_REJECTED"
    assert out["refusal"]["step"] == "plan_create"
    assert out["completed_steps"] == ["mission_create"]
    assert out["not_run_steps"] == ["plan_create", "budget_set", "autopilot_start"]
    assert out["created"] == {"mission_id": out["mission_id"]}
    assert out["autopilot_started"] is False
    assert spawned == []
    # The Mission exists; nothing was deleted or rolled back.
    assert _mission_exists(out["mission_id"], hermes_root) is True


def test_autopilot_exception_reports_completed_steps_and_ids(hermes_root, gate_on, spawned, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("worker spawn exploded")

    monkeypatch.setattr(autopilot, "hermes_autopilot_start", boom)
    out = _j(launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root))

    assert out["success"] is False
    assert out["refusal"]["code"] == "LAUNCH_STEP_ERROR"
    assert out["completed_steps"] == ["mission_create", "plan_create"]
    assert out["not_run_steps"] == ["autopilot_start"]
    assert _steps(out)["autopilot_start"]["outcome"] == "error"
    assert out["created"]["mission_id"] == out["mission_id"]
    assert "plan" in out["created"]
    assert out["autopilot_started"] is False


# ---------------------------------------------------------------------------
# Underlying validators and the approval boundary
# ---------------------------------------------------------------------------


def test_invalid_spec_is_refused_by_mission_create_validator(hermes_root, gate_on, spawned, tmp_path):
    bad = json.dumps({"schema": mission.MISSION_SPEC_SCHEMA, "objective": "no title here"})
    before = _snapshot(hermes_root)
    dry = _j(launch.hermes_mission_launch(bad, dry_run=True, hermes_root=hermes_root))
    assert dry["success"] is False
    assert dry["refusal"]["step"] == "mission_create"
    assert dry["would_start_autopilot"] is False
    assert _steps(dry)["plan_create"]["outcome"] == "skipped"
    assert _snapshot(hermes_root) == before

    applied = _j(launch.hermes_mission_launch(bad, dry_run=False, confirm=True, hermes_root=hermes_root))
    assert applied["success"] is False
    assert applied["refusal"]["step"] == "mission_create"
    assert applied["mission_id"] is None
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_invalid_budget_is_refused_by_budget_validator_before_any_write(hermes_root, gate_on, spawned):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), budget_json=json.dumps({"quota": -5}),
                                          dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["refusal"]["step"] == "budget_set"
    assert out["created"] == {}
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_refused_preview_with_explicit_id_reports_no_created_id(hermes_root, gate_on, spawned):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec("msn-launch-bad-budget"), budget_json=json.dumps({"quota": -5}),
                                          dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["mission_id"] is None
    assert out["created"] == {}
    assert out["completed_steps"] == []
    assert _steps(out)["mission_create"]["outcome"] == "not_run"
    assert _snapshot(hermes_root) == before
    assert _mission_exists("msn-launch-bad-budget", hermes_root) is False
    assert spawned == []


@pytest.mark.parametrize("limits", [
    {"max_concurrency": 99},
    {"max_replans": -1},
    {"max_attempts_per_node": 0},
    {"max_runtime_seconds": 1},
    {"not_a_limit": 1},
])
def test_invalid_autopilot_limits_refused_before_any_write(hermes_root, gate_on, spawned, limits):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), autopilot_limits_json=json.dumps(limits),
                                          dry_run=False, confirm=True, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["refusal"]["code"] == "AUTOPILOT_LIMITS_INVALID"
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_unknown_top_level_budget_key_is_refused(hermes_root, gate_on):
    out = _j(launch.hermes_mission_launch(_spec(), budget_json=json.dumps({"quota": 5, "extra": 1}),
                                          hermes_root=hermes_root))
    assert out["success"] is False
    assert out["refusal"]["code"] == "INVALID_INPUT"


def test_final_approval_cannot_be_disabled_by_launch(hermes_root, gate_on, spawned):
    before = _snapshot(hermes_root)
    spec = _spec(final_approval_required=False)
    for dry in (True, False):
        out = _j(launch.hermes_mission_launch(spec, dry_run=dry, confirm=True, hermes_root=hermes_root))
        assert out["success"] is False
        assert out["refusal"]["code"] == "APPROVAL_REQUIRED_CANNOT_BE_DISABLED"
    assert _snapshot(hermes_root) == before
    assert spawned == []


def test_mission_create_still_refuses_disabled_approval_at_workspace_level(hermes_root):
    """The underlying gate the launch relies on: a non-Owner cannot create an approval-free Mission."""
    out = _j(mission.hermes_mission_create(_spec("msn-approval-off", final_approval_required=False),
                                           confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False
    assert _mission_exists("msn-approval-off", hermes_root) is False


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_one_launch_audit_record_per_call_without_objective_text(hermes_root, gate_on, spawned, tmp_path):
    launch.hermes_mission_launch(_spec(), dry_run=True, hermes_root=hermes_root)
    launch.hermes_mission_launch(_spec(), dry_run=False, confirm=True, hermes_root=hermes_root)

    records = _launch_records(tmp_path)
    assert len(records) == 2
    assert [r["dry_run"] for r in records] == [True, False]
    applied = records[1]
    assert applied["success"] is True
    assert applied["changed"] is True
    assert applied["mission_id"].startswith("msn-")
    assert [s["step"] for s in applied["steps"]] == list(launch.CHAIN)
    # The launch record carries counts and ids only, never the objective.
    for record in records:
        assert SECRET_MARKER not in json.dumps(record)


def test_launch_result_never_echoes_objective_text(hermes_root, gate_on, spawned):
    for dry in (True, False):
        raw = launch.hermes_mission_launch(_spec(), dry_run=dry, confirm=True, hermes_root=hermes_root)
        assert SECRET_MARKER not in raw

REQUEST = 'launch-test-0123456789abcdef'


def test_keyed_launch_replay_survives_reload(hermes_root, gate_on, spawned):
    first = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert first['success']
    assert first['receipt']['state'] == 'completed'
    second = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert second['replayed'] and second['mission_id'] == first['mission_id']
    assert len(spawned) == 1
    assert SECRET_MARKER.encode() not in launch._receipt_path(hermes_root).read_bytes()
    got = launch.launch_receipt(REQUEST, hermes_root)
    assert got['found'] and got['receipt']['mission_id'] == first['mission_id']


def test_keyed_conflict_is_not_a_second_launch(hermes_root, gate_on, spawned):
    first = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert first['success']
    conflict = _j(launch.hermes_mission_launch(_spec(title='different'), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert conflict['code'] == 'LAUNCH_REQUEST_CONFLICT'
    assert len(spawned) == 1


def test_keyed_preview_and_gate_refusal_write_no_receipt(hermes_root, gate_on, spawned, monkeypatch):
    before = _snapshot(hermes_root)
    out = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, hermes_root=hermes_root))
    assert out['success']
    assert _snapshot(hermes_root) == before
    monkeypatch.delenv(autopilot.AUTOPILOT_ENV)
    out = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert not out['success']
    assert _snapshot(hermes_root) == before
    assert not spawned


def test_crash_reservation_never_reexecutes(hermes_root, gate_on, spawned, monkeypatch):
    real = launch._apply_chain
    def crash(*args, **kwargs):
        raise SystemExit('simulated process death after intent commit')
    monkeypatch.setattr(launch, '_apply_chain', crash)
    with pytest.raises(SystemExit):
        launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root)
    monkeypatch.setattr(launch, '_apply_chain', real)
    replay = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert replay['replayed'] and replay['outcome_unknown']
    assert replay['success'] is False and replay['code'] == 'LAUNCH_OUTCOME_UNKNOWN'
    assert replay['receipt']['state'] == 'in_progress'
    assert not spawned


def test_checkpoint_failure_stops_before_dispatch(hermes_root, gate_on, spawned, monkeypatch):
    def broken(*args):
        raise OSError('disk full')
    monkeypatch.setattr(launch, '_record_receipt', broken)
    out = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out['code'] == 'LAUNCH_OUTCOME_UNKNOWN'
    assert _mission_exists(out['mission_id'], hermes_root)
    assert not spawned
    again = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert again['replayed'] and again['outcome_unknown']


def test_receipt_lookup_requires_read_authority_and_writes_nothing(hermes_root, monkeypatch):
    before = _snapshot(hermes_root)
    assert launch.launch_receipt(REQUEST, hermes_root)['found'] is False
    assert _snapshot(hermes_root) == before
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, '0')
    assert launch.launch_receipt(REQUEST, hermes_root)['code'] == 'OPERATOR_LEVEL_INSUFFICIENT'
    assert _snapshot(hermes_root) == before


def test_concurrent_same_key_admits_only_one_chain(hermes_root, gate_on, spawned, monkeypatch):
    import threading
    entered, resume = threading.Event(), threading.Event()
    real = launch._apply_chain
    def held(*args, **kwargs):
        entered.set()
        assert resume.wait(20)
        return real(*args, **kwargs)
    monkeypatch.setattr(launch, '_apply_chain', held)
    results = []
    def submit():
        results.append(_j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root)))
    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert entered.wait(10)
        second = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
        assert second['replayed'] and second['outcome_unknown']
        assert not spawned
    finally:
        resume.set()
        thread.join(20)
    assert not thread.is_alive()
    assert results[0]['success'] and len(spawned) == 1


def test_receipt_intent_storage_failure_has_no_effects(hermes_root, gate_on, spawned, monkeypatch):
    def denied(*args, **kwargs):
        raise launch.sqlite3.OperationalError('read-only storage')
    monkeypatch.setattr(launch.sqlite3, 'connect', denied)
    result = _j(launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert not result['success'] and not spawned
    assert not launch._receipt_path(hermes_root).exists()


def test_replay_audit_does_not_claim_new_changes(hermes_root, gate_on, spawned, tmp_path):
    for _ in range(2):
        launch.hermes_mission_launch(_spec(), request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root)
    records = _launch_records(tmp_path)
    assert [record['changed'] for record in records] == [True, False]


def test_receipt_reservation_is_shared_between_processes(hermes_root, gate_on, tmp_path):
    """Independent interpreters race the same durable key; exactly one reaches effects."""
    import subprocess
    import sys
    code = r'''
import json, sys
from pathlib import Path
import operator_mission_launch as launch
import operator_policy as op
root = Path(sys.argv[1])
op.set_audit_log_override(root.parent / ('audit-' + sys.argv[2] + '.jsonl'))
def fake_launch(policy, spec, budget, limits, dry, confirm, root, checkpoint=None):
    if not dry:
        with (root / 'effects').open('a') as f:
            f.write('one-effect\n')
    return {'success': True, 'mission_id': json.loads(spec)['mission_id'], 'created': {}, 'steps': [], 'autopilot_started': not dry}
launch._launch = fake_launch
spec = json.dumps({'title': 'Race', 'objective': 'Test durable admission', 'acceptance_criteria': ['One effect'], 'owner_profile': 'default'})
print(launch.hermes_mission_launch(spec, request_id='process-race-0123456789', confirm=True, dry_run=False, hermes_root=root))
'''
    children = [subprocess.Popen([sys.executable, '-c', code, str(hermes_root), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert (hermes_root / 'effects').read_text().splitlines() == ['one-effect']
    assert any(r['success'] for r in results)
    assert launch.launch_receipt('process-race-0123456789', hermes_root)['receipt']['state'] == 'completed'


@pytest.mark.parametrize('requested_id', ['', None, '  ', ' msn-explicit-launch '])
def test_keyed_optional_mission_id_is_normalized_before_reservation(hermes_root, gate_on, spawned, requested_id):
    spec = _spec(mission_id=requested_id)
    first = _j(launch.hermes_mission_launch(spec, request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert first['success']
    assert first['receipt']['mission_id'] == first['mission_id']
    again = _j(launch.hermes_mission_launch(spec, request_id=REQUEST, confirm=True, dry_run=False, hermes_root=hermes_root))
    assert again['replayed'] and again['mission_id'] == first['mission_id']
    assert len(spawned) == 1
