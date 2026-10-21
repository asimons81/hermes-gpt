"""Neutrality proof for the declarative tool registry (``register_tools``).

The golden file ``tests_golden_tool_registry.json`` was captured from the
pre-refactor ``register_tools`` (one ``server.add_tool`` sequence). Every
combination below must still produce the same ordered tool list, and the same
per-tool wire form (``model_dump(by_alias=True, exclude_none=True)``: names,
descriptions, input schemas, annotations, meta).

The same golden must hold under both supported interpreters (``.venv`` on mcp
1.x and ``.venv-sdk2`` on mcp 2.x); the capture recorded identical wire forms
under both. Per-tool digests are keyed by auth mode because the static bearer
combinations change ``meta.securitySchemes`` for every tool.

Regenerate only for a deliberate, reviewed registration change by running
``capture_golden()`` and reviewing the diff of the golden file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

import jev_bridge
import oauth_auth
import operator_attention_delivery
import operator_review_assist
import server
import tool_profiles

GOLDEN_PATH = Path(__file__).resolve().parent / "tests_golden_tool_registry.json"
GOLDEN_SCHEMA = "tool_registry_golden/v1"

# Gate environment variables read during registration.
GATE_ENVS: dict[str, str] = {
    "write": server.ENABLE_WRITE_ENV,
    "memory_write": server.ENABLE_MEMORY_WRITE_ENV,
    "session_search": server.ENABLE_SESSION_SEARCH_ENV,
    "session_control": server.ENABLE_SESSION_CONTROL_ENV,
    "terminal": server.ENABLE_TERMINAL_ENV,
    "vision": server.ENABLE_VISION_ENV,
    "web": server.ENABLE_WEB_ENV,
    "finance": server.op_finance.ENABLE_FINANCE_ENV,
    "jev": jev_bridge.ENABLE_ENV,
    "review_assist": operator_review_assist.REVIEW_ENV,
    "attention_delivery": operator_attention_delivery.ENABLE_ENV,
    "autopilot": server.op_autopilot.AUTOPILOT_ENV,
}

# Operator-mode envs. Registration does not read them; "all on" sets them so
# the golden proves they do not change the registered surface.
OPERATOR_ENVS: dict[str, str] = {
    "HERMES_GPT_OPERATOR_ENABLED": "1",
    "HERMES_GPT_OPERATOR_LEVEL": "owner",
    "HERMES_GPT_OPERATOR_APPLY_MODE": "direct",
    "HERMES_GPT_OWNER_ACK": "1",
}

_BEARER_ENV = oauth_auth.AUTH_TOKEN_ENV
# Fixture only: satisfies the 43-128 URL-safe length rule; never a real credential.
_BEARER_FIXTURE = "A" * 43
_SANDBOX_CLEAR_ENVS: tuple[str, ...] = tuple(
    sorted(
        set(GATE_ENVS.values())
        | set(OPERATOR_ENVS)
        | {
            server.UNSAFE_REMOTE_ENV,
            server.TRUSTED_PROXY_IPS_ENV,
            server.ALLOWED_HOSTS_ENV,
            server.tool_profiles.TOOL_PROFILE_ENV,
            oauth_auth.AUTH_TOKEN_ENV,
            oauth_auth.OAUTH_ENABLE_ENV,
            oauth_auth.OAUTH_ISSUER_ENV,
            oauth_auth.OAUTH_CLIENT_ID_ENV,
            oauth_auth.OAUTH_CLIENT_SECRET_ENV,
            oauth_auth.OAUTH_REDIRECT_URI_ENV,
            oauth_auth.OAUTH_SCOPE_ENV,
            jev_bridge.ROOT_ENV,
            jev_bridge.NODE_ENV,
            "HERMES_PROFILE",
            "HERMES_HOME",
        }
    )
)


def _all_on_env() -> dict[str, str]:
    env = {name: "1" for name in GATE_ENVS.values()}
    env.update(OPERATOR_ENVS)
    return env


def combos() -> dict[str, dict[str, Any]]:
    """Named environment combinations. Each entry: env, finance_marker, auth."""
    all_on = _all_on_env()
    out: dict[str, dict[str, Any]] = {}

    def add(name: str, env: dict[str, str], *, finance_marker: bool = False, auth: str = "noauth") -> None:
        out[name] = {"env": env, "finance_marker": finance_marker, "auth": auth}

    add("none", {})
    for key, env_name in GATE_ENVS.items():
        add(f"only:{key}", {env_name: "1"})
    add(
        "only:session_search+session_control",
        {GATE_ENVS["session_search"]: "1", GATE_ENVS["session_control"]: "1"},
    )
    add("only:jev+review_assist", {GATE_ENVS["jev"]: "1", GATE_ENVS["review_assist"]: "1"})
    for key, env_name in GATE_ENVS.items():
        add(f"all_but:{key}", {k: v for k, v in all_on.items() if k != env_name})
    add("all_on", dict(all_on))
    for profile in tool_profiles.PROFILE_NAMES:
        add(f"profile:{profile}", {tool_profiles.TOOL_PROFILE_ENV: profile})
        add(f"profile:{profile}+all_on", {**all_on, tool_profiles.TOOL_PROFILE_ENV: profile})
    add("finance_marker_file", {}, finance_marker=True)
    add("bearer", {_BEARER_ENV: _BEARER_FIXTURE}, auth="bearer")
    add("bearer+all_on", {**all_on, _BEARER_ENV: _BEARER_FIXTURE}, auth="bearer")
    return out


@contextlib.contextmanager
def _sandbox(env: dict[str, str], *, finance_marker: bool) -> Iterator[None]:
    """Clear the gate surface, apply ``env``, and sandbox HERMES_HOME."""
    saved = {name: os.environ.get(name) for name in _SANDBOX_CLEAR_ENVS}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "hermes"
        root.mkdir()
        if finance_marker:
            marker_dir = root / "profiles" / server.op_finance.FINANCE_PROFILE
            marker_dir.mkdir(parents=True)
            (marker_dir / server.op_finance.FINANCE_ENABLE_MARKER).write_text("", encoding="utf-8")
        try:
            for name in _SANDBOX_CLEAR_ENVS:
                os.environ.pop(name, None)
            os.environ["HERMES_HOME"] = str(root)
            os.environ.update(env)
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def snapshot(env: dict[str, str], *, finance_marker: bool = False) -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(name, digest, wire_dump)`` for each tool in registration order."""
    with _sandbox(env, finance_marker=finance_marker):
        built = server.build_server()
        tools = asyncio.run(built.list_tools())
    rows = []
    for tool in tools:
        dump = tool.model_dump(by_alias=True, exclude_none=True)
        rows.append((tool.name, _sha256(dump), dump))
    return rows


def _sdk_version() -> str:
    return importlib.metadata.version("mcp")


def _combo_digest(rows: list[tuple[str, str, dict[str, Any]]]) -> str:
    return _sha256([[name, digest] for name, digest, _ in rows])


def capture_golden() -> dict[str, Any]:
    """Build the golden document for the running interpreter's MCP SDK."""
    tool_digests: dict[str, dict[str, str]] = {"noauth": {}, "bearer": {}}
    entries: dict[str, Any] = {}
    for name, spec in combos().items():
        rows = snapshot(spec["env"], finance_marker=spec["finance_marker"])
        digests = tool_digests[spec["auth"]]
        for tool_name, digest, _ in rows:
            prior = digests.setdefault(tool_name, digest)
            if prior != digest:
                raise AssertionError(f"{tool_name} changes wire form within auth mode {spec['auth']}")
        entries[name] = {
            "auth": spec["auth"],
            "ordered_names": [row[0] for row in rows],
            "sha256": _combo_digest(rows),
        }
    return {
        "schema": GOLDEN_SCHEMA,
        "captured_under": {f"mcp-{_sdk_version()}": True},
        "tool_digests": tool_digests,
        "combos": entries,
    }


def _load_golden() -> dict[str, Any]:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert document.get("schema") == GOLDEN_SCHEMA, "unexpected golden schema"
    return document


COMBOS = combos()


@pytest.mark.parametrize("combo", sorted(COMBOS))
def test_registration_matches_golden(combo: str) -> None:
    golden = _load_golden()
    spec = COMBOS[combo]
    expected = golden["combos"][combo]
    assert expected["auth"] == spec["auth"]
    rows = snapshot(spec["env"], finance_marker=spec["finance_marker"])

    actual_names = [row[0] for row in rows]
    assert actual_names == expected["ordered_names"], _name_diff(expected["ordered_names"], actual_names)

    known = golden["tool_digests"][spec["auth"]]
    for name, digest, _ in rows:
        assert known.get(name) == digest, f"{name}: wire form differs from the golden ({combo})"
    assert _combo_digest(rows) == expected["sha256"], f"ordered wire digest differs ({combo})"


def _name_diff(expected: list[str], actual: list[str]) -> str:
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    moved = [] if missing or extra else [
        f"position {i}: expected {e}, got {a}"
        for i, (e, a) in enumerate(zip(expected, actual))
        if e != a
    ]
    return f"missing={missing} extra={extra} order={moved[:5]} counts={len(expected)}->{len(actual)}"


def test_golden_covers_every_combo_for_this_sdk() -> None:
    golden = _load_golden()
    assert set(golden["combos"]) == set(COMBOS)
    assert golden["tool_digests"]["noauth"]
    assert golden["tool_digests"]["bearer"]


def test_invalid_tool_profile_still_fails_closed() -> None:
    with _sandbox({tool_profiles.TOOL_PROFILE_ENV: "not-a-profile"}, finance_marker=False):
        with pytest.raises(ValueError):
            server.build_server()


if __name__ == "__main__":
    # Regenerate after an intentional tool-surface change (new tool, changed
    # schema, annotation or docstring), then review the diff:
    #   python test_tool_registry_neutrality.py
    # Run it under both MCP SDK families; the document must be identical.
    fresh = capture_golden()
    if GOLDEN_PATH.exists():
        prior = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        same = {k: v for k, v in prior.items() if k != "captured_under"} == {k: v for k, v in fresh.items() if k != "captured_under"}
        if same:
            fresh["captured_under"] = {**prior.get("captured_under", {}), **fresh["captured_under"]}
    GOLDEN_PATH.write_text(json.dumps(fresh, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN_PATH.name}")
