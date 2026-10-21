"""Connector tool profiles (HERMES_GPT_TOOL_PROFILE).

Profiles can only remove tools from the default registration. These tests pin
the unset/full equivalence, the allowlists, the operator exclusions, fail-closed
parsing, and that env gates still decide gated tools.
"""

import asyncio
import json
from pathlib import Path

import pytest

import server
import tool_profiles
from test_server import clear_gate_envs
from test_tool_inventory import DEFAULT_TOOL_INVENTORY


SURFACE_ENV = {server.op_finance.ENABLE_FINANCE_ENV: "1"}
AUTOPILOT_TOOLS = {"hermes_autopilot_start", "hermes_autopilot_status", "hermes_autopilot_stop", "hermes_mission_launch"}
RAW_HOST_TOOLS = {
    "hermes_read_file",
    "hermes_write_file",
    "hermes_patch",
    "hermes_search_files",
    "hermes_run_command",
    "hermes_memory",
}


def _set_surface(monkeypatch, profile=None, *extra_gates):
    clear_gate_envs(monkeypatch)
    for name, value in SURFACE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HERMES_HOME", str(Path(server.__file__).resolve().parent))
    if profile is not None:
        monkeypatch.setenv(tool_profiles.TOOL_PROFILE_ENV, profile)
    for name in extra_gates:
        monkeypatch.setenv(name, "1")


def _tools(monkeypatch, profile=None, *extra_gates):
    _set_surface(monkeypatch, profile, *extra_gates)
    return asyncio.run(server.build_server().list_tools())


def _names(monkeypatch, profile=None, *extra_gates):
    return [tool.name for tool in _tools(monkeypatch, profile, *extra_gates)]


def _canonical(tools) -> str:
    return json.dumps([tool.model_dump(by_alias=True) for tool in tools], sort_keys=True)


def test_unset_profile_matches_default_inventory(monkeypatch):
    names = _names(monkeypatch)
    assert sorted(names) == DEFAULT_TOOL_INVENTORY
    assert len(names) == len(set(names))


def test_explicit_full_profile_is_identical_to_unset(monkeypatch):
    unset = _tools(monkeypatch)
    full = _tools(monkeypatch, "full")
    assert [t.name for t in full] == [t.name for t in unset]
    assert _canonical(full) == _canonical(unset)


def test_empty_profile_value_selects_full(monkeypatch):
    unset = _tools(monkeypatch)
    empty = _tools(monkeypatch, "   ")
    assert _canonical(empty) == _canonical(unset)


def test_chat_lean_exposes_only_allowlisted_tools(monkeypatch):
    names = _names(monkeypatch, "chat-lean")

    assert set(names) <= tool_profiles.CHAT_LEAN_TOOLS
    assert len(names) == len(set(names))
    # With every gate off, chat-lean is exactly the allowlisted part of the inventory.
    assert sorted(names) == sorted(set(DEFAULT_TOOL_INVENTORY) & tool_profiles.CHAT_LEAN_TOOLS)
    assert 20 <= len(names) <= 40


def test_chat_lean_keeps_tool_metadata_identical_to_full(monkeypatch):
    full_by_name = {t.name: t for t in _tools(monkeypatch)}
    for tool in _tools(monkeypatch, "chat-lean"):
        assert _canonical([tool]) == _canonical([full_by_name[tool.name]])


def test_chat_lean_removes_write_and_host_primitives_even_when_gated_on(monkeypatch):
    names = set(_names(monkeypatch, "chat-lean", server.ENABLE_WRITE_ENV, server.ENABLE_TERMINAL_ENV))
    assert not (names & RAW_HOST_TOOLS)
    assert "hermes_write_file" not in names and "hermes_run_command" not in names


def test_chat_lean_autopilot_follows_its_own_gate(monkeypatch):
    gated_off = set(_names(monkeypatch, "chat-lean"))
    assert not (AUTOPILOT_TOOLS & gated_off)

    gated_on = set(_names(monkeypatch, "chat-lean", server.op_autopilot.AUTOPILOT_ENV))
    assert AUTOPILOT_TOOLS <= gated_on
    assert gated_on - gated_off == AUTOPILOT_TOOLS


def test_operator_profile_is_full_minus_raw_host_primitives(monkeypatch):
    base = set(_names(monkeypatch))
    assert set(_names(monkeypatch, "operator")) == base - tool_profiles.OPERATOR_EXCLUDED_TOOLS
    assert set(tool_profiles.OPERATOR_EXCLUDED_TOOLS) == RAW_HOST_TOOLS


def test_operator_profile_excludes_gated_host_primitives_when_enabled(monkeypatch):
    gates = (server.ENABLE_WRITE_ENV, server.ENABLE_TERMINAL_ENV)
    full = set(_names(monkeypatch, None, *gates))
    operator = set(_names(monkeypatch, "operator", *gates))
    assert full - operator == (RAW_HOST_TOOLS & full)
    assert "hermes_write_file" in full and "hermes_write_file" not in operator
    assert "hermes_run_command" in full and "hermes_run_command" not in operator


@pytest.mark.parametrize("bad", ["chat_lean", "Chat-Lean", "lean", "all", "full,operator"])
def test_invalid_profile_fails_closed_at_build(monkeypatch, bad):
    _set_surface(monkeypatch, bad)
    with pytest.raises(ValueError, match=tool_profiles.TOOL_PROFILE_ENV):
        server.build_server()


def test_resolve_profile_accepts_only_known_names():
    assert tool_profiles.resolve_profile(None) == "full"
    assert tool_profiles.resolve_profile("") == "full"
    assert tool_profiles.resolve_profile("operator") == "operator"
    assert tool_profiles.resolve_profile(" chat-lean ") == "chat-lean"
    with pytest.raises(ValueError):
        tool_profiles.resolve_profile("unknown")
