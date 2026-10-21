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
    assert len(tools) == 138


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
    # 138 default + 3 Jev judgments + 1 review assess.
    assert len(tools) == 142


def test_review_assess_present_under_chat_lean_with_both_gates(monkeypatch):
    tools = _surface(monkeypatch, profile="chat-lean", gates=(JEV_GATE, REVIEW_GATE))
    assert REVIEW_ASSESS in tools


def test_neither_tool_exposes_hermes_root(monkeypatch):
    tools = _surface(monkeypatch, gates=(JEV_GATE, REVIEW_GATE))
    for name in (OWNER_INBOX, REVIEW_ASSESS):
        schema = tools[name].model_dump(by_alias=True)["inputSchema"]
        assert "hermes_root" not in schema["properties"], name


def test_owner_inbox_annotations_are_read_only_and_idempotent(monkeypatch):
    ann = _surface(monkeypatch)[OWNER_INBOX].annotations
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False
    assert ann.idempotentHint is True
    assert ann.openWorldHint is False


def test_review_assess_annotations_match_jev_style(monkeypatch):
    ann = _surface(monkeypatch, gates=(JEV_GATE, REVIEW_GATE))[REVIEW_ASSESS].annotations
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is False
    assert ann.idempotentHint is False
    assert ann.openWorldHint is True


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
