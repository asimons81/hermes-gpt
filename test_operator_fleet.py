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


def test_fleet_list_returns_registry_without_tokens():
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
            {"name": "rza", "url": "http://rza.example:8765", "has_token": True},
            {"name": "nous-girl", "url": "http://nous.example:8765", "has_token": True},
        ],
    }
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_status_uses_fixed_doctor_argv_for_registered_agent():
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
    assert out["capabilities"] == {"message_send": True}
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
            (HERMES, "a2a", "send", "rza", message, "--json"): (
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
    assert calls[-1] == [HERMES, "a2a", "send", "rza", message, "--json"]


def test_fleet_task_rejects_unknown_agent_before_any_remote_command():
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(fleet.hermes_fleet_task(agent="not-a-peer", task_id="task-123", runner=runner, hermes_bin=HERMES))

    assert out["success"] is False
    assert out["code"] == "UNKNOWN_AGENT"
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_task_reads_the_a2a_cli_wrapped_task_shape():
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "task", "task-123", "--agent", "rza", "--json"): (
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
