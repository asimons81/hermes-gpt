"""v0.15 registration: the Owner inbox and the advisory review-assist tool.

``hermes_owner_inbox`` is always registered (read-only). ``hermes_review_assess``
is registered only when both HERMES_GPT_ENABLE_JEV=1 and HERMES_GPT_REVIEW_ASSIST=1
are set. Neither tool exposes ``hermes_root`` as an MCP input.
"""

import asyncio
import json
from pathlib import Path

import pytest

import jev_bridge
import operator_policy as op
import operator_review_assist as ora
import server
import tool_profiles
from test_server import clear_gate_envs, tool_names, tools_by_name

OWNER_INBOX = "hermes_owner_inbox"
REVIEW_ASSESS = "hermes_review_assess"
REVIEW_GATE = ora.REVIEW_ENV
JEV_GATE = jev_bridge.ENABLE_ENV


def _result_text(result) -> str:
    """First text block of a call_tool result. SDK 1 returns (content, data); SDK 2 returns CallToolResult."""
    content = result[0] if isinstance(result, tuple) else result.content
    return content[0].text


def _surface(monkeypatch, *, profile=None, gates=()):
    clear_gate_envs(monkeypatch)
    monkeypatch.delenv(JEV_GATE, raising=False)
    monkeypatch.delenv(REVIEW_GATE, raising=False)
    monkeypatch.setenv(server.op_finance.ENABLE_FINANCE_ENV, "1")
    monkeypatch.setenv("HERMES_HOME", str(Path(server.__file__).resolve().parent))
    if profile is not None:
        monkeypatch.setenv(tool_profiles.TOOL_PROFILE_ENV, profile)
    for name in gates:
        monkeypatch.setenv(name, "1")
    return tools_by_name(server.build_server())


def test_owner_inbox_registered_by_default(monkeypatch):
    tools = _surface(monkeypatch)
    assert OWNER_INBOX in tools
    assert REVIEW_ASSESS not in tools
    assert len(tools) == 140  # 139 at v0.16 + hermes_plan_approve_gate (v0.16.1)


def test_owner_inbox_registered_under_chat_lean(monkeypatch):
    tools = _surface(monkeypatch, profile="chat-lean")
    assert OWNER_INBOX in tools


def test_review_assess_absent_by_default(monkeypatch):
    assert REVIEW_ASSESS not in _surface(monkeypatch)


def test_review_assess_absent_with_only_jev_gate(monkeypatch):
    tools = _surface(monkeypatch, gates=(JEV_GATE,))
    assert REVIEW_ASSESS not in tools
    assert OWNER_INBOX in tools


def test_review_assess_absent_with_only_review_gate(monkeypatch):
    tools = _surface(monkeypatch, gates=(REVIEW_GATE,))
    assert REVIEW_ASSESS not in tools


def test_review_assess_present_with_both_gates(monkeypatch):
    tools = _surface(monkeypatch, gates=(JEV_GATE, REVIEW_GATE))
    assert REVIEW_ASSESS in tools
    assert OWNER_INBOX in tools
    # 140 default + 3 Jev judgments + 1 review assess.
    assert len(tools) == 144


def test_review_assess_present_under_chat_lean_with_both_gates(monkeypatch):
    tools = _surface(monkeypatch, profile="chat-lean", gates=(JEV_GATE, REVIEW_GATE))
    assert REVIEW_ASSESS in tools


def test_neither_tool_exposes_hermes_root(monkeypatch, wire):
    tools = _surface(monkeypatch, gates=(JEV_GATE, REVIEW_GATE))
    for name in (OWNER_INBOX, REVIEW_ASSESS):
        schema = wire(tools[name])["inputSchema"]
        assert "hermes_root" not in schema["properties"], name


def test_owner_inbox_annotations_are_read_only_and_idempotent(monkeypatch, wire):
    ann = wire(_surface(monkeypatch)[OWNER_INBOX])["annotations"]
    assert ann["readOnlyHint"] is True
    assert ann["destructiveHint"] is False
    assert ann["idempotentHint"] is True
    assert ann["openWorldHint"] is False


def test_review_assess_annotations_match_jev_style(monkeypatch, wire):
    ann = wire(_surface(monkeypatch, gates=(JEV_GATE, REVIEW_GATE))[REVIEW_ASSESS])["annotations"]
    assert ann["readOnlyHint"] is True
    assert ann["destructiveHint"] is False
    assert ann["idempotentHint"] is False
    assert ann["openWorldHint"] is True


def test_review_assess_wrapper_fails_closed_when_gates_off(monkeypatch):
    clear_gate_envs(monkeypatch)
    monkeypatch.delenv(JEV_GATE, raising=False)
    monkeypatch.delenv(REVIEW_GATE, raising=False)
    payload = json.loads(asyncio.run(server.hermes_review_assess("0" * 64)))
    assert payload["success"] is False
    assert payload["code"] == "DISABLED"


def test_owner_inbox_wrapper_returns_json_envelope(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload = json.loads(server.hermes_owner_inbox(limit=5))
    # Operator mode is off by default: refused as an error envelope, never raised.
    assert payload["success"] is False
    assert payload["code"] == "OWNER_INBOX_DENIED"


def test_owner_inbox_wrapper_rejects_bad_limit_as_envelope(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    payload = json.loads(server.hermes_owner_inbox(limit=0))
    assert payload["success"] is False
    assert payload["code"] == "OWNER_INBOX_INVALID"


def test_owner_inbox_tool_success_path_passes_server_root_by_keyword(monkeypatch, tmp_path):
    # Regression: the wrapper once passed (limit, root) positionally into a module
    # signature whose second parameter is include_acknowledged. Refusal-path tests
    # never reached the call, so this one runs the enabled read-only success path.
    clear_gate_envs(monkeypatch)
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    seen: dict = {}
    real = server.op_owner_inbox.hermes_owner_inbox

    def spy(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return real(*args, **kwargs)

    try:
        monkeypatch.setattr(server.op_owner_inbox, "hermes_owner_inbox", spy)
        result = asyncio.run(server.build_server().call_tool(OWNER_INBOX, {"limit": 5}))
    finally:
        op.set_audit_log_override(None)

    payload = json.loads(_result_text(result))
    assert payload["success"] is True, payload
    assert payload["tool"] == OWNER_INBOX
    assert payload["limit"] == 5
    assert seen["args"] == ()
    assert seen["kwargs"]["limit"] == 5
    assert seen["kwargs"]["include_acknowledged"] is False
    assert seen["kwargs"]["hermes_root"] == server._default_hermes_root()
    assert seen["kwargs"]["hermes_root"] is not None


def test_owner_inbox_include_acknowledged_is_forwarded(monkeypatch, tmp_path):
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    try:
        payload = json.loads(server.hermes_owner_inbox(limit=7, include_acknowledged=True))
    finally:
        op.set_audit_log_override(None)
    assert payload["success"] is True
    assert payload["limit"] == 7
