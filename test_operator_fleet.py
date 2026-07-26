"""Tests for the bounded A2A-backed Hermes GPT fleet control surface."""

from __future__ import annotations

import json

import pytest

import operator_policy as op
import operator_fleet as fleet


HERMES = "/test/hermes"
REGISTRY = {
    "agents": [
        {"name": "rza", "url": "http://rza.example:8765", "hasToken": True},
        {"name": "nous-girl", "url": "http://nous.example:8765", "hasToken": True},
    ]
}


def runner_with(responses: dict[tuple[str, ...], tuple[int, str, str]], calls: list[list[str]]):
    def runner(argv: list[str], *, timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        return responses.get(tuple(argv), (1, "", "unexpected argv"))

    return runner


def enable_read_only(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


def test_fleet_list_requires_operator_mode_and_never_returns_registry_urls(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(fleet.hermes_fleet_list(runner=runner, hermes_bin=HERMES))

    assert out == {
        "success": True,
        "count": 2,
        "agents": [
            {"name": "rza", "has_token": True},
            {"name": "nous-girl", "has_token": True},
        ],
    }
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_status_uses_fixed_doctor_argv_and_safe_summary(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "doctor", "rza", "--timeout", "9", "--json"): (
                0,
                json.dumps({"ok": True, "status": "compatible", "name": "RZA", "capabilities": {"message_send": True}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_status(agent="rza", timeout=9, runner=runner, hermes_bin=HERMES))

    assert out["success"] is True
    assert out["agent"] == "rza"
    assert out["status"] == "compatible"
    assert out["capability_count"] == 1
    assert out["warnings_count"] == 0
    assert out["errors_count"] == 0
    assert calls[-1] == [HERMES, "a2a", "doctor", "rza", "--timeout", "9", "--json"]


def test_fleet_dispatch_dry_run_never_sends_message(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message="review the deployment", dry_run=True, confirm=True,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["plan"] == {"agent": "rza", "message_len": 21}
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_dispatch_requires_confirm_before_remote_execution(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message="review the deployment", dry_run=False, confirm=False,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out["success"] is False
    assert out["code"] == "CONFIRMATION_REQUIRED"
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_dispatch_sends_to_registered_agent_and_redacts_prompt_from_output(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    message = "secret-looking but legitimate task payload"
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "send", "--json", "rza", "--", message): (
                0,
                json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_SUBMITTED"}, "history": [{"parts": [{"text": message}]}]}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message=message, dry_run=False, confirm=True,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out == {
        "success": True,
        "changed": True,
        "agent": "rza",
        "task_id": "task-123",
        "state": "TASK_STATE_SUBMITTED",
    }
    assert message not in json.dumps(out)
    assert calls[-1] == [HERMES, "a2a", "send", "--json", "rza", "--", message]


def test_fleet_task_rejects_unknown_agent_before_any_remote_command(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(fleet.hermes_fleet_task(agent="not-a-peer", task_id="task-123", runner=runner, hermes_bin=HERMES))

    assert out["success"] is False
    assert out["code"] == "UNKNOWN_AGENT"
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_task_reads_the_a2a_cli_wrapped_task_shape(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "task", "--agent", "rza", "--json", "--", "task-123"): (
                0,
                json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-07-26T20:00:00+00:00"}, "artifacts": [{}, {}]}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_task(agent="rza", task_id="task-123", runner=runner, hermes_bin=HERMES))

    assert out == {
        "success": True,
        "agent": "rza",
        "task_id": "task-123",
        "state": "TASK_STATE_COMPLETED",
        "timestamp": "2026-07-26T20:00:00+00:00",
        "artifact_count": 2,
    }


def test_server_registers_the_bounded_fleet_control_tools():
    import asyncio
    import server

    names = {tool.name for tool in asyncio.run(server.build_server().list_tools())}

    assert {
        "hermes_fleet_list",
        "hermes_fleet_status",
        "hermes_fleet_dispatch",
        "hermes_fleet_task",
    } <= names


def test_fleet_read_tools_are_denied_when_operator_mode_is_disabled(monkeypatch):
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_list(runner=runner, hermes_bin=HERMES))

    assert out["code"] == "FLEET_POLICY_DENIED"
    assert calls == []


def test_fleet_dispatch_validates_policy_before_registry_lookup(monkeypatch):
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_dispatch(agent="rza", message="safe task", runner=runner, hermes_bin=HERMES))

    assert out["code"] == "FLEET_POLICY_DENIED"
    assert calls == []


def test_fleet_dispatch_dash_leading_message_is_a_positional_argument(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    message = "--token"
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "send", "--json", "rza", "--", message): (0, json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_SUBMITTED"}}}), ""),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_dispatch(agent="rza", message=message, dry_run=False, confirm=True, runner=runner, hermes_bin=HERMES))

    assert out["success"] is True
    assert calls[-1] == [HERMES, "a2a", "send", "--json", "rza", "--", "--token"]


def test_fleet_task_rejects_dash_leading_task_id_before_registry_lookup(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_task(agent="rza", task_id="-h", runner=runner, hermes_bin=HERMES))

    assert out["code"] == "INVALID_TASK_ID"
    assert calls == []
