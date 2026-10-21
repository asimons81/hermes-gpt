"""v0.16 registration: owner-inbox ack, one-call launch, and attention relay status.

- ``hermes_owner_inbox_ack`` is always registered; it is gated internally.
- ``hermes_mission_launch`` is registered only with the Autopilot machine gate.
- ``hermes_attention_delivery_status`` is registered only with
  ``HERMES_GPT_ATTENTION_DELIVERY=1`` and never sends anything.
No v0.16 tool exposes ``hermes_root`` as an MCP input.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import operator_attention_delivery as attention
import operator_policy as op
import server
import tool_profiles
from test_server import clear_gate_envs, tool_names, tools_by_name

ACK = "hermes_owner_inbox_ack"
LAUNCH = "hermes_mission_launch"
STATUS = "hermes_attention_delivery_status"
AUTOPILOT_GATE = server.op_autopilot.AUTOPILOT_ENV
ATTENTION_ENVS = (
    attention.ENABLE_ENV,
    attention.TARGETS_ENV,
    attention.HERMES_CLI_ENV,
    attention.IMSG_CLI_ENV,
    attention.LINK_BASE_ENV,
    attention.MAX_PER_RUN_ENV,
)


def _surface(monkeypatch, *, profile=None, gates=()):
    clear_gate_envs(monkeypatch)
    for name in ATTENTION_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(server.op_finance.ENABLE_FINANCE_ENV, "1")
    monkeypatch.setenv("HERMES_HOME", str(Path(server.__file__).resolve().parent))
    if profile is not None:
        monkeypatch.setenv(tool_profiles.TOOL_PROFILE_ENV, profile)
    for name in gates:
        monkeypatch.setenv(name, "1")
    return tools_by_name(server.build_server())


def _text(result) -> str:
    content = result[0] if isinstance(result, tuple) else result.content
    return content[0].text


def test_ack_registered_by_default_and_launch_and_status_absent(monkeypatch):
    names = _surface(monkeypatch)
    assert ACK in names
    assert LAUNCH not in names
    assert STATUS not in names
    assert "hermes_owner_inbox" in names


def test_autopilot_gate_registers_launch_next_to_autopilot_tools(monkeypatch):
    names = _surface(monkeypatch, gates=(AUTOPILOT_GATE,))
    assert LAUNCH in names
    assert {"hermes_autopilot_start", "hermes_autopilot_status", "hermes_autopilot_stop"} <= set(names)
    assert STATUS not in names


def test_delivery_gate_registers_status_only(monkeypatch):
    names = _surface(monkeypatch, gates=(attention.ENABLE_ENV,))
    assert STATUS in names
    assert LAUNCH not in names


def test_delivery_status_call_spawns_no_subprocess(monkeypatch, tmp_path):
    # A real, executable stand-in for the imsg CLI: status must not run it.
    fake_cli = tmp_path / "imsg"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR)

    def _forbidden(*args, **kwargs):
        raise AssertionError("attention status must not spawn a process")

    _surface(monkeypatch, gates=(attention.ENABLE_ENV,))
    monkeypatch.setenv(attention.TARGETS_ENV, "imessage:+15551234567")
    monkeypatch.setenv(attention.IMSG_CLI_ENV, str(fake_cli))
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    result = asyncio.run(server.build_server().call_tool(STATUS, {}))
    payload = json.loads(_text(result))
    assert payload["enabled"] is True
    assert payload["read_only"] is True
    assert "+15551234567" not in json.dumps(payload)  # masked in status output


def test_ack_refuses_without_operator_and_writes_nothing(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    try:
        payload = json.loads(server.hermes_owner_inbox_ack(["0" * 24]))
    finally:
        op.set_audit_log_override(None)
    assert payload["success"] is False
    assert payload["code"] == "OWNER_INBOX_ACK_DENIED"
    assert not (tmp_path / "missions" / "attention_acks.jsonl").exists()


def test_ack_dry_run_by_default_under_workspace_direct(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    try:
        payload = json.loads(server.hermes_owner_inbox_ack(["0" * 24]))
    finally:
        op.set_audit_log_override(None)
    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["changed"] is False
    assert payload["unknown"] == ["0" * 24]
    assert not (tmp_path / "missions" / "attention_acks.jsonl").exists()


def test_launch_wrapper_defaults_to_dry_run_and_writes_nothing(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(AUTOPILOT_GATE, "1")
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    spec = json.dumps({"schema": "hermes.mission-spec/v1", "mission_id": "msn-v016dry", "title": "t",
                       "objective": "o", "owner_profile": "default", "acceptance_criteria": ["a"],
                       "context_refs": [], "skills": [], "final_approval_required": True})
    try:
        payload = json.loads(server.hermes_mission_launch(spec_json=spec))
    finally:
        op.set_audit_log_override(None)
    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["mission_id"] == "msn-v016dry"  # preview echoes the spec id; nothing is created
    assert [step["outcome"] for step in payload["steps"]] == ["preview", "would_run", "skipped", "would_run"]
    assert not (tmp_path / "missions").exists()  # no Mission store; the audit log is the only write


@pytest.mark.parametrize("profile", [None, "chat-lean"])
def test_chat_lean_lists_new_names_when_their_gates_are_on(monkeypatch, profile):
    names = set(_surface(monkeypatch, profile=profile, gates=(AUTOPILOT_GATE, attention.ENABLE_ENV)))
    assert {ACK, LAUNCH, STATUS, "hermes_owner_inbox"} <= names
    assert tool_profiles.CHAT_LEAN_TOOLS >= {ACK, LAUNCH, STATUS}


def test_chat_lean_keeps_new_names_gated_off(monkeypatch):
    names = set(_surface(monkeypatch, profile="chat-lean"))
    assert ACK in names
    assert LAUNCH not in names and STATUS not in names


def test_no_tool_exposes_hermes_root_with_every_gate_on(monkeypatch):
    tools = _surface(monkeypatch, gates=(AUTOPILOT_GATE, attention.ENABLE_ENV, "HERMES_GPT_ENABLE_JEV", "HERMES_GPT_REVIEW_ASSIST"))
    for name in (ACK, LAUNCH, STATUS, "hermes_owner_inbox"):
        assert name in tools, name
    for name, tool in tools.items():
        props = tool.model_dump(by_alias=True).get("inputSchema", {}).get("properties", {})
        assert "hermes_root" not in props, name


def test_v016_annotations_match_their_contracts(monkeypatch, wire):
    tools = _surface(monkeypatch, gates=(AUTOPILOT_GATE, attention.ENABLE_ENV))
    expected = {
        ACK: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        LAUNCH: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        STATUS: {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    }
    for name, want in expected.items():
        ann = wire(tools[name])["annotations"]
        for key, value in want.items():
            assert ann[key] is value, (name, key)


def test_default_surface_count_is_pinned(monkeypatch):
    assert len(tool_names(_surface_server(monkeypatch))) == 139


def _surface_server(monkeypatch):
    _surface(monkeypatch)
    return server.build_server()
