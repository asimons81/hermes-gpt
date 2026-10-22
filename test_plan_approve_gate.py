"""Tests for the Owner gate approval operation (``hermes_plan_approve_gate``, v0.16.1).

One Owner-only call resolves an approval frontier by walking the shortest legal
path to ``completed`` through the existing node transition. These tests cover
the dry run, the Owner gate, every refusal code, the compare-and-swap, a
mid-walk failure, the one audit record per call, and the Autopilot frontier
clearing after a real approval.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_mission_plan as plan
import operator_policy as op
from test_operator_autopilot_scheduler import MID, _j, _mk, _node, _states, _tick, make_env

OWNER_ENV = {
    op.OPERATOR_ENABLED_ENV: "1",
    op.OPERATOR_LEVEL_ENV: "owner",
    op.OPERATOR_APPLY_MODE_ENV: "direct",
    op.OWNER_ACTIVE_ENV: "1",
    op.OWNER_ACK_ENV: op.OWNER_ACK_REQUIRED_VALUE,
}

FORWARD_TO_COMPLETED = ["dispatched", "running", "awaiting_review", "validated", "awaiting_approval", "completed"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    root, backend = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, backend


@pytest.fixture
def owner(monkeypatch):
    for key, value in OWNER_ENV.items():
        monkeypatch.setenv(key, value)


def _gate(node_id: str = "gate", parents=()) -> dict:
    return _node(node_id, parents, kind="approval", owner="owner")


def _approve(root: Path, node_id: str = "gate", **kwargs):
    kwargs.setdefault("hermes_root", root)
    return _j(plan.hermes_plan_approve_gate(MID, node_id, **kwargs))


def _plan_version(root: Path) -> int:
    return int(_j(plan.hermes_plan_get(MID, hermes_root=root))["version"])


def _audit_records(tmp_path: Path, tool: str) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("tool") == tool]


def _walk(root: Path, node_id: str, targets) -> None:
    for target in targets:
        out = _j(plan.hermes_plan_node_transition(MID, node_id, target, confirm=True, dry_run=False, hermes_root=root))
        assert out["success"] is True, out


# ---------------------------------------------------------------------------
# The computed path
# ---------------------------------------------------------------------------


def test_path_from_pending_is_the_six_step_forward_walk():
    assert plan.gate_approval_path("pending") == FORWARD_TO_COMPLETED


def test_path_is_shortest_from_every_forward_state():
    assert plan.gate_approval_path("blockable") == FORWARD_TO_COMPLETED
    assert plan.gate_approval_path("dispatched") == FORWARD_TO_COMPLETED[1:]
    assert plan.gate_approval_path("running") == FORWARD_TO_COMPLETED[2:]
    assert plan.gate_approval_path("validated") == ["awaiting_approval", "completed"]
    assert plan.gate_approval_path("awaiting_approval") == ["completed"]


def test_path_never_uses_paused_or_failed_and_refuses_unreachable_targets():
    for start in plan.GATE_APPROVABLE_STATES:
        assert "paused" not in plan.gate_approval_path(start)
        assert "failed" not in plan.gate_approval_path(start)
    with pytest.raises(ValueError):
        plan.gate_approval_path("failed")


# ---------------------------------------------------------------------------
# Dry run and the Owner gate
# ---------------------------------------------------------------------------


def test_dry_run_lists_the_six_steps_and_writes_nothing(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    version = _plan_version(root)

    out = _approve(root, reason="reviewed")
    assert out["success"] is True and out["dry_run"] is True and out["changed"] is False
    assert [s["to_state"] for s in out["steps"]] == FORWARD_TO_COMPLETED
    assert out["from_state"] == "pending" and out["to_state"] == "completed"
    assert out["applied_steps"] == []
    assert _states(root) == {"gate": "pending"}
    assert _plan_version(root) == version


def test_dry_run_is_the_default_even_with_owner_direct(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _approve(root, confirm=True)  # dry_run defaults to True
    assert out["dry_run"] is True
    assert _states(root) == {"gate": "pending"}


def test_owner_direct_run_reaches_completed_and_clears_the_autopilot_frontier(env, owner, tmp_path):
    root, _ = env
    _mk(root, [_gate(), _node("after", ["gate"])])
    before = _tick(root)
    assert before["frontier"]["waiting"] is True and before["frontier"]["nodes"] == ["gate"]

    out = _approve(root, confirm=True, dry_run=False, reason="cleared by owner")
    assert out["success"] is True and out["changed"] is True and out["dry_run"] is False
    assert [s["to_state"] for s in out["applied_steps"]] == FORWARD_TO_COMPLETED
    assert out["node_state"] == "completed"
    assert _states(root) == {"gate": "completed", "after": "pending"}

    after = _tick(root)
    assert after["dispatched"] == ["after"]
    assert after["frontier"]["active"] is False and after["frontier"]["waiting"] is False


def test_owner_run_on_a_high_impact_node_completes_it_too(env, owner):
    root, _ = env
    _mk(root, [_node("risky", auth="high_impact")])
    out = _approve(root, "risky", confirm=True, dry_run=False)
    assert out["success"] is True and out["node_state"] == "completed"
    assert _states(root) == {"risky": "completed"}


def test_partially_walked_gate_resumes_from_its_current_state(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    _walk(root, "gate", FORWARD_TO_COMPLETED[:2])  # now running
    out = _approve(root, confirm=True, dry_run=False)
    assert out["from_state"] == "running"
    assert [s["to_state"] for s in out["applied_steps"]] == FORWARD_TO_COMPLETED[2:]
    assert _states(root) == {"gate": "completed"}


def test_refused_without_owner_mode_and_nothing_is_written(env):
    root, _ = env  # workspace level: Owner Mode is not active
    _mk(root, [_gate()])
    version = _plan_version(root)

    dry = _approve(root)
    assert dry["success"] is False and dry["code"] == "LEVEL_REQUIRED"
    real = _approve(root, confirm=True, dry_run=False)
    assert real["success"] is False and real["code"] == "LEVEL_REQUIRED"
    assert _states(root) == {"gate": "pending"}
    assert _plan_version(root) == version


def test_direct_write_without_confirm_is_refused(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _approve(root, dry_run=False)
    assert out["success"] is False and out["code"] == "CONFIRM_REQUIRED"
    assert out["dry_run"] is False and out["changed"] is False
    assert _states(root) == {"gate": "pending"}


def test_direct_request_in_dry_run_apply_mode_is_a_labelled_preview(env, owner, monkeypatch):
    # Same rule as the other owner tools: a non-direct apply mode turns the write
    # into a preview. The result says so (dry_run true, changed false).
    root, _ = env
    _mk(root, [_gate()])
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    out = _approve(root, confirm=True, dry_run=False)
    assert out["success"] is True and out["dry_run"] is True and out["changed"] is False
    assert _states(root) == {"gate": "pending"}


# ---------------------------------------------------------------------------
# Refusals (fixed codes, nothing written)
# ---------------------------------------------------------------------------


def test_refuses_a_node_that_is_not_gated(env, owner):
    root, _ = env
    _mk(root, [_node("a")])
    out = _approve(root, "a", confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "NOT_A_GATED_NODE"
    assert _states(root) == {"a": "pending"}


def test_refuses_when_parents_are_not_completed(env, owner):
    root, _ = env
    _mk(root, [_node("a"), _gate("gate", ["a"])])
    out = _approve(root, confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "GATE_NOT_REACHED"
    assert _states(root) == {"a": "pending", "gate": "pending"}


@pytest.mark.parametrize("terminal_or_paused", ["completed", "failed", "paused"])
def test_refuses_completed_failed_and_paused_nodes(env, owner, terminal_or_paused):
    root, _ = env
    _mk(root, [_gate()])
    if terminal_or_paused == "completed":
        _walk(root, "gate", FORWARD_TO_COMPLETED)
    elif terminal_or_paused == "failed":
        _walk(root, "gate", ["failed"])
    else:
        _walk(root, "gate", ["paused"])
    out = _approve(root, confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "NODE_STATE_NOT_APPROVABLE"
    assert _states(root) == {"gate": terminal_or_paused}


def test_refuses_an_unknown_node(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _approve(root, "nope", confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "PLAN_NODE_NOT_FOUND"


def test_refuses_a_missing_plan(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _j(plan.hermes_plan_approve_gate("msn-missing", "gate", confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is False and out["code"] == "PLAN_GATE_APPROVE_REJECTED"
    assert _states(root) == {"gate": "pending"}


def test_refuses_an_invalid_node_id_and_an_overlong_reason(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    bad_node = _approve(root, "Not A Node", confirm=True, dry_run=False)
    assert bad_node["success"] is False and bad_node["code"] == "PLAN_GATE_APPROVE_REJECTED"
    long_reason = _approve(root, reason="x" * 500, confirm=True, dry_run=False)
    assert long_reason["success"] is False and long_reason["code"] == "PLAN_GATE_APPROVE_REJECTED"
    assert _states(root) == {"gate": "pending"}


def test_stale_expected_plan_version_is_refused_before_any_step(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    version = _plan_version(root)
    out = _approve(root, confirm=True, dry_run=False, expected_plan_version=version + 7)
    assert out["success"] is False and out["code"] == "PLAN_VERSION_CONFLICT"
    assert out["expected_plan_version"] == version + 7 and out["actual_plan_version"] == version
    assert _states(root) == {"gate": "pending"}


def test_matching_expected_plan_version_applies(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _approve(root, confirm=True, dry_run=False, expected_plan_version=_plan_version(root))
    assert out["success"] is True and _states(root) == {"gate": "completed"}


def test_a_refused_dry_run_with_stale_version_is_still_refused(env, owner):
    root, _ = env
    _mk(root, [_gate()])
    out = _approve(root, expected_plan_version=_plan_version(root) + 1)
    assert out["success"] is False and out["code"] == "PLAN_VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# A step refused mid-walk: stop, report, no rollback
# ---------------------------------------------------------------------------


def test_step_failure_mid_walk_reports_applied_steps_and_does_not_roll_back(env, owner, monkeypatch):
    root, _ = env
    _mk(root, [_gate()])
    real_transition = plan.hermes_plan_node_transition
    calls = {"n": 0}

    def fail_third(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            return json.dumps({"success": False, "code": "PLAN_NODE_TRANSITION_REJECTED", "safe_message": "refused"})
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(plan, "hermes_plan_node_transition", fail_third)
    out = _approve(root, confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "GATE_STEP_FAILED"
    assert [s["to_state"] for s in out["applied_steps"]] == FORWARD_TO_COMPLETED[:2]
    assert out["failed_step"]["to_state"] == "awaiting_review"
    assert out["failed_step"]["code"] == "PLAN_NODE_TRANSITION_REJECTED"
    assert [s["to_state"] for s in out["not_run_steps"]] == FORWARD_TO_COMPLETED[3:]
    assert out["node_state"] == "running"
    assert out["changed"] is True
    assert _states(root) == {"gate": "running"}


def test_cas_conflict_on_the_first_step_applies_nothing(env, owner, monkeypatch):
    root, _ = env
    _mk(root, [_gate()])
    real_read = plan._gate_read
    reads = {"n": 0}

    def bump_before_write(path, mission_id):
        # A concurrent replacement after the read but before the first step.
        reads["n"] += 1
        view = real_read(path, mission_id)
        if reads["n"] == 1:
            _j(plan.hermes_plan_create(MID, json.dumps({
                "schema": plan.PLAN_SCHEMA, "mission_id": MID, "version": 2, "decomposition": "operator-provided",
                "objective": "Replaced.", "nodes": [_gate().copy()],
            }), confirm=True, dry_run=False, hermes_root=root))
        return view

    monkeypatch.setattr(plan, "_gate_read", bump_before_write)
    out = _approve(root, confirm=True, dry_run=False)
    assert out["success"] is False and out["code"] == "GATE_STEP_FAILED"
    assert out["applied_steps"] == []
    assert out["failed_step"]["code"] == "PLAN_VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# Audit and step reasons
# ---------------------------------------------------------------------------


def test_one_gate_audit_record_per_call_and_step_reasons_carry_the_prefix(env, owner, tmp_path):
    root, _ = env
    _mk(root, [_gate()])
    _approve(root)  # dry run: one record
    _approve(root, reason="signed off", confirm=True, dry_run=False)  # real run: one record
    gate_records = _audit_records(tmp_path, "hermes_plan_approve_gate")
    assert len(gate_records) == 2
    assert [r["dry_run"] for r in gate_records] == [True, False]
    assert all(r["to"] == "completed" for r in gate_records)
    assert gate_records[1]["applied_count"] == 6 and gate_records[1]["reason"] == "signed off"

    step_records = [r for r in _audit_records(tmp_path, "hermes_plan_node_transition") if not r["dry_run"]]
    assert len(step_records) == 6
    assert all(r["reason"].startswith("owner gate approval: signed off") for r in step_records)


def test_refused_call_is_audited_as_a_failure(env, tmp_path):
    root, _ = env
    _mk(root, [_node("a")])
    _approve(root, "a", confirm=True, dry_run=False)
    records = _audit_records(tmp_path, "hermes_plan_approve_gate")
    assert len(records) == 1 and records[0]["success"] is False
    assert records[0]["refusal"] == "LEVEL_REQUIRED"  # workspace only: no Owner Mode


# ---------------------------------------------------------------------------
# Server surface: one more default tool, Owner-only annotations, chat-lean
# ---------------------------------------------------------------------------


def test_server_wrapper_hides_hermes_root_and_is_registered_as_a_write(monkeypatch):
    import asyncio

    import server
    import tool_profiles
    from conftest import wire
    from test_server import clear_gate_envs, tool_names

    clear_gate_envs(monkeypatch)
    monkeypatch.delenv(tool_profiles.TOOL_PROFILE_ENV, raising=False)
    built = server.build_server()
    assert "hermes_plan_approve_gate" in tool_names(built)
    listed = {t.name: wire(t) for t in asyncio.run(built.list_tools())}
    tool = listed["hermes_plan_approve_gate"]
    assert "hermes_root" not in tool["inputSchema"]["properties"]
    assert set(tool["inputSchema"]["properties"]) == {
        "mission_id", "node_id", "reason", "confirm", "dry_run", "expected_plan_version",
    }
    annotations = tool["annotations"]
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is False
    assert annotations["openWorldHint"] is False
    assert "hermes_plan_approve_gate" in tool_profiles.CHAT_LEAN_TOOLS
