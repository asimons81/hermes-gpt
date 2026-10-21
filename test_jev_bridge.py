"""Offline transport, subprocess confinement, and Hermes authorization checks."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from starlette.testclient import TestClient

import jev_bridge as bridge
import oauth_auth
import server
from conftest import wire

INPUT = {"state": "The parcel arrived.", "instructions": "Did the parcel arrive?"}
FAKE_KEY = "mock-typesafe-key-not-valid"
RESULT = {"model": "jev-test", "answer": {"type": "noul", "noul": 0.99}, "usage": {"input_tokens": 10, "output_tokens": 2}}


def run(name="jev_yes_no", args=None):
    return wire(asyncio.run(bridge.call(name, INPUT if args is None else args)))


def code(result):
    assert result["isError"]
    return json.loads(result["content"][0]["text"])["code"]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(bridge.ENABLE_ENV, "1")


@pytest.fixture
def adapter(monkeypatch):
    explicit = os.environ.get("HERMES_GPT_TEST_JEV_ADAPTER_ROOT")
    root = Path(explicit) if explicit else Path(__file__).resolve().parent.parent / "jev-mcp"
    node = shutil.which("node")
    if not node or not (root / "node_modules").is_dir():
        if explicit:
            pytest.fail("Explicit Jev test adapter requires Node and installed dependencies")
        pytest.skip("Pinned Jev adapter/Node not installed; set HERMES_GPT_TEST_JEV_ADAPTER_ROOT")
    monkeypatch.setenv(bridge.ROOT_ENV, str(root.resolve()))
    monkeypatch.setenv(bridge.NODE_ENV, str(Path(node).resolve()))
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)
    return bridge._launch_config()


def track_children(monkeypatch):
    children = []
    original = anyio.open_process

    async def spawn(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(anyio, "open_process", spawn)
    return children


def test_default_off_and_strict_gate_never_launch(monkeypatch):
    def forbidden():
        raise AssertionError("disabled call resolved a subprocess")
    monkeypatch.setattr(bridge, "_launch_config", forbidden)
    baseline = {tool.name for tool in asyncio.run(server.build_server().list_tools())}
    for value in (None, "", "0", "true", "yes"):
        if value is None:
            monkeypatch.delenv(bridge.ENABLE_ENV, raising=False)
        else:
            monkeypatch.setenv(bridge.ENABLE_ENV, value)
        assert code(run()) == "DISABLED"
        assert {tool.name for tool in asyncio.run(server.build_server().list_tools())} == baseline
    assert not set(bridge.TOOL_MAP) & baseline


def test_opt_in_adds_exactly_three_tools_and_revocation_refuses_calls(monkeypatch, enabled):
    enabled_tools = {tool.name: wire(tool) for tool in asyncio.run(server.build_server().list_tools())}
    monkeypatch.delenv(bridge.ENABLE_ENV)
    default = {tool.name for tool in asyncio.run(server.build_server().list_tools())}
    assert set(enabled_tools) - default == set(bridge.TOOL_MAP)
    assert code(run()) == "DISABLED"
    for name in bridge.TOOL_MAP:
        assert enabled_tools[name]["_meta"] == {"securitySchemes": [{"type": "noauth"}]}
        annotations = enabled_tools[name]["annotations"]
        assert annotations["readOnlyHint"] and annotations["openWorldHint"]
        assert not annotations["destructiveHint"] and not annotations["idempotentHint"]


@pytest.mark.parametrize("name,args", [
    ("arbitrary", INPUT), ("jev_yes_no", {**INPUT, "command": "other"}),
    ("jev_yes_no", {**INPUT, "state": ""}),
    ("jev_yes_no", {**INPUT, "state": "x" * 16001}),
    ("jev_yes_no", {**INPUT, "instructions": "x" * 4001}),
    ("jev_yes_no", {**INPUT, "criteria": {"yes": "yes"}}),
    ("jev_choose", {**INPUT, "criteria": {"one": "only"}}),
    ("jev_choose", {**INPUT, "criteria": {"invalid key": "A", "other": "B"}}),
    ("jev_score", {**INPUT, "criteria": ["level"] * 33}),
    ("jev_score", {**INPUT, "criteria": ["a", ""]}),
    ("jev_score", {**INPUT, "criteria": ["字" * 4000] * 32}),
])
def test_invalid_inputs_do_not_resolve_command(monkeypatch, enabled, name, args):
    monkeypatch.setattr(bridge, "_launch_config", lambda: pytest.fail("spawn admission bypassed"))
    assert code(run(name, args)) == "INVALID_INPUT"


def test_missing_config_fails_without_starting_child(monkeypatch, enabled):
    monkeypatch.setattr(anyio, "open_process", lambda *a, **k: pytest.fail("spawned"))
    assert code(run()) == "NOT_CONFIGURED"


def test_pinned_config_and_environment_allowlist(monkeypatch, adapter, tmp_path):
    for name in ("NODE_OPTIONS", "NODE_PATH", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "HERMES_GPT_BEARER_TOKEN", "PYTHONPATH"):
        monkeypatch.setenv(name, "mock-sensitive-parent-value")
    launch = bridge._launch_config()
    assert launch.argv == (adapter.argv[0], str(adapter.cwd / "src" / "cli.js"))
    expected = {"PATH", "TYPESAFE_API_KEY", "TYPESAFE_MODEL"}
    if os.name == "nt" and "SystemRoot" in os.environ:
        expected.add("SystemRoot")
    assert set(launch.env) == expected
    assert launch.env["TYPESAFE_API_KEY"] == FAKE_KEY
    # A managed path alone is insufficient: altered adapter code fails closed.
    changed = tmp_path / "adapter"
    for name in bridge._PINNED_FILES:
        dest = changed / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(adapter.cwd / name, dest)
    (changed / "src" / "cli.js").write_text("throw new Error('changed');")
    monkeypatch.setenv(bridge.ROOT_ENV, str(changed))
    with pytest.raises(bridge.BridgeError) as error:
        bridge._launch_config()
    assert error.value.code == "NOT_CONFIGURED"


def test_real_pinned_node_adapter_with_mocked_provider(monkeypatch, enabled, adapter, tmp_path):
    observed = tmp_path / "observed.json"
    preload = tmp_path / "provider.mjs"
    preload.write_text("""
import {writeFileSync} from 'node:fs';
globalThis.fetch = async (url, init) => {
  if (url !== 'https://api.typesafe.ai/v1/systemone' || init.headers.Authorization !== 'Bearer mock-typesafe-key-not-valid') throw new Error('unexpected request');
  writeFileSync(OBSERVED, JSON.stringify(Object.keys(process.env).sort()));
  const question = JSON.parse(init.body).questions.decision;
  let answer;
  if (question.type === 'noul') answer = {type:'noul',noul:0.99};
  if (question.type === 'choice') answer = {type:'choice',choice:'arrived',confidence:1,probabilities:{arrived:1,other:0}};
  if (question.type === 'score') answer = {type:'score',score:1,confidence:1,probabilities:{'0':0,'1':1},legend:{'0':'Not arrived','1':'Arrived'}};
  return Response.json({model:'jev-test',answers:{decision:answer},usage:{input_tokens:10,output_tokens:2}});
};
""".replace("OBSERVED", json.dumps(str(observed))))
    launch = dataclasses.replace(adapter, argv=(adapter.argv[0], "--import", str(preload), adapter.argv[1]))
    monkeypatch.setattr(bridge, "_launch_config", lambda: launch)
    children = track_children(monkeypatch)
    for name, args, kind in [
        ("jev_yes_no", INPUT, "noul"),
        ("jev_choose", {**INPUT, "criteria": {"arrived": "Arrived", "other": "Other"}}, "choice"),
        ("jev_score", {**INPUT, "criteria": ["Not arrived", "Arrived"]}, "score"),
    ]:
        result = run(name, args)
        assert not result.get("isError"), result
        assert result["structuredContent"]["answer"]["type"] == kind
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        assert FAKE_KEY not in json.dumps(result)
    assert len(children) == 3 and all(child.returncode is not None for child in children)
    # CoreFoundation may add its own encoding hint on macOS at process startup.
    assert set(json.loads(observed.read_text())) - {"__CF_USER_TEXT_ENCODING"} == set(adapter.env)


@pytest.mark.parametrize("raw,expected", [
    ({"isError": True, "content": [{"type": "text", "text": json.dumps({"code": "AUTHENTICATION", "message": FAKE_KEY})}]}, "AUTHENTICATION"),
    ({"isError": True, "content": [{"type": "text", "text": FAKE_KEY}]}, "UNAVAILABLE"),
    ({"structuredContent": {**RESULT, "debug": FAKE_KEY}}, "UNAVAILABLE"),
    ({"structuredContent": {**RESULT, "answer": {"type": "choice"}}}, "UNAVAILABLE"),
    ({"structuredContent": {**RESULT, "model": "x" * bridge.MAX_WIRE_BYTES}}, "UNAVAILABLE"),
])
def test_child_errors_and_invalid_results_are_safe(monkeypatch, enabled, raw, expected):
    monkeypatch.setattr(bridge, "_launch_config", lambda: None)
    async def request(*args):
        return raw
    monkeypatch.setattr(bridge, "_request", request)
    result = run()
    assert code(result) == expected
    assert FAKE_KEY not in json.dumps(result)


@pytest.mark.parametrize("behavior,expected", [
    ("import time; time.sleep(60)", "TIMEOUT"),
    ("print('not-json-with-mock-secret',flush=True)", "UNAVAILABLE"),
    ("import sys; sys.stdout.write('x'*600000); sys.stdout.flush()", "UNAVAILABLE"),
    ("raise SystemExit(2)", "UNAVAILABLE"),
])
def test_failed_child_is_reaped(monkeypatch, enabled, tmp_path, behavior, expected):
    script = tmp_path / "child.py"
    script.write_text(behavior)
    launch = bridge._Launch((sys.executable, str(script)), tmp_path, {})
    monkeypatch.setattr(bridge, "_launch_config", lambda: launch)
    monkeypatch.setattr(bridge, "CALL_TIMEOUT", 0.2 if expected == "TIMEOUT" else 3)
    children = track_children(monkeypatch)
    assert code(run()) == expected
    assert len(children) == 1 and children[0].returncode is not None


def test_cancelled_call_reaps_child_and_releases_slot(monkeypatch, enabled, tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import time; time.sleep(60)")
    monkeypatch.setattr(bridge, "_launch_config", lambda: bridge._Launch((sys.executable, str(script)), tmp_path, {}))
    children = track_children(monkeypatch)
    async def cancel():
        task = asyncio.create_task(bridge.call("jev_yes_no", INPUT))
        while not children:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert children[0].returncode is not None
    assert bridge._SLOTS.acquire(blocking=False)
    bridge._SLOTS.release()


def test_process_concurrency_is_bounded(monkeypatch, enabled):
    monkeypatch.setattr(bridge, "_launch_config", lambda: None)
    for _ in range(4):
        assert bridge._SLOTS.acquire(blocking=False)
    try:
        assert code(run()) == "BUSY"
    finally:
        for _ in range(4):
            bridge._SLOTS.release()


def test_bearer_auth_precedes_forwarding_and_authority_stays_read_only(monkeypatch, enabled):
    token = "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    monkeypatch.setenv("HERMES_GPT_BEARER_TOKEN", token)
    calls = []
    monkeypatch.setattr(bridge, "_launch_config", lambda: None)
    async def request(*args):
        calls.append(args)
        return {"structuredContent": RESULT}
    monkeypatch.setattr(bridge, "_request", request)
    built = server.build_server(http=True)
    tools = {tool.name: wire(tool) for tool in asyncio.run(built.list_tools())}
    for name in bridge.TOOL_MAP:
        assert tools[name]["_meta"] == {"securitySchemes": [{"type": "http", "scheme": "bearer"}]}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hermes_jev_yes_no", "arguments": INPUT}}
    before = dict(os.environ)
    with TestClient(server.build_asgi_app(built, http=True), base_url="http://127.0.0.1:7677") as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            assert client.post("/mcp", headers=headers, json=payload).status_code == 401
        assert not calls
        response = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json=payload)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == RESULT
    assert len(calls) == 1
    assert dict(os.environ) == before
    denied = json.loads(server.hermes_owner_run_command("must-not-execute", dry_run=False))
    assert not denied.get("success", False)


def test_jev_tools_reuse_oauth_metadata(monkeypatch, enabled):
    # build_server installs process-global hooks. Keep this test from changing
    # subsequent token-store tests' standalone OAuth behavior.
    monkeypatch.setattr(oauth_auth, "_persist_hook", oauth_auth._persist_hook)
    monkeypatch.setattr(oauth_auth, "_revocation_hook", oauth_auth._revocation_hook)
    monkeypatch.setenv("HERMES_GPT_OAUTH_ENABLE", "1")
    monkeypatch.setenv("HERMES_GPT_OAUTH_ISSUER", "https://mcp.example.com")
    monkeypatch.setenv("HERMES_GPT_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("HERMES_GPT_OAUTH_CLIENT_SECRET", "test-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    monkeypatch.setenv("HERMES_GPT_OAUTH_REDIRECT_URI", "https://chatgpt.com/connector/oauth/callback")
    built = server.build_server(http=True)
    for tool in asyncio.run(built.list_tools()):
        if tool.name in bridge.TOOL_MAP:
            assert wire(tool)["_meta"] == {"securitySchemes": [{"type": "oauth2", "scopes": ["hermes"]}]}
    with TestClient(server.build_asgi_app(built, http=True), base_url="https://mcp.example.com") as client:
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
