"""Tests for the browser-facing security boundary (t_7266e74c).

Covers ``ui_security`` redaction properties, account/connection endpoints,
the auth boundary, Mission allowlist semantics, stale-lease helpers, and the
UI mount behavior in ``server.build_asgi_app``. All tests run against a temp
``hermes_root`` fixture — never production data. ``Path.home`` is patched to
the fixture so default store locations resolve inside it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import oauth_auth
import operator_mission as op_mission
import operator_policy as op
import server
import token_store
import ui_api
import ui_security

# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture
def ui_root(tmp_path: Path, monkeypatch):
    """Hermetic Hermes root; Path.home patched so defaults stay inside tmp.

    The root lives at ``tmp_path/.hermes`` so ``_default_hermes_root()`` —
    which route handlers use — resolves to the same fixture location the
    tests write into.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    op_mission._cache_clear()
    root = tmp_path / ".hermes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "model: test-model\nprovider: test-provider\n", encoding="utf-8"
    )
    return root


def _build_app(monkeypatch, *, ui_enabled: bool = True, **env) -> TestClient:
    """Build the ASGI app with the UI mount per test env, return TestClient."""
    if ui_enabled:
        monkeypatch.setenv(ui_security.UI_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(ui_security.UI_ENABLED_ENV, raising=False)
    # Deterministic Mission allowlist unless the test sets it explicitly.
    if "HERMES_GPT_MISSION_ALLOWED_SURFACES" not in env:
        monkeypatch.delenv(op_mission.MISSION_ALLOWED_SURFACES_ENV, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    # Loopback base URL so the MCP transport-security Host check passes.
    return TestClient(app, base_url="http://127.0.0.1")


def _envelope(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text[:300]}"
    return resp.json()


def _save_tokens(root: Path, expires_at: float) -> None:
    """Persist a durable token bundle with a fixed expiry (env key path)."""
    token_store.save_tokens(root, {"chatgpt": {"expires_at": expires_at}})


# Forbidden patterns that must never appear in browser payloads.
FORBIDDEN_PATTERNS = [
    re.compile(r"RAW [A-Z ]*SHOULD NEVER SURFACE"),
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{6,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"Bearer [A-Za-z0-9._\-]{10,}"),
    re.compile(r"hermes_gpt_tokens\.json"),
    re.compile(r"hermes_gpt_token_key"),
    re.compile(r"(^|[\s\"'(=])secrets/"),
    re.compile(r"/secrets/"),
    re.compile(r"/\.ssh/"),
    re.compile(r"/home/"),
    re.compile(r"\.hermes/"),
    re.compile(r"password\s*[:=]\s*[^\s]{4,}"),
    re.compile(r"api[_-]?key\s*[:=]\s*[^\s]{4,}"),
]


def assert_no_forbidden(text: str) -> None:
    """Assert a serialized browser payload contains no forbidden material."""
    for pattern in FORBIDDEN_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"forbidden pattern {pattern.pattern!r} leaked: ...{text[max(0, match.start()-40):match.end()+40]}..."


# ---------------------------------------------------------------------------
# Redaction unit properties
# ---------------------------------------------------------------------------


def test_redact_browser_marks_secret_keys(ui_root):
    payload = {
        "prompt": "RAW PROMPT SHOULD NEVER SURFACE",
        "memory_body": "RAW MEMORY BODY SHOULD NEVER SURFACE",
        "transcript": "RAW TRANSCRIPT SHOULD NEVER SURFACE",
        "request_dump": "RAW REQUEST DUMP SHOULD NEVER SURFACE",
        "credentials": "RAW CREDENTIALS SHOULD NEVER SURFACE",
        "profile_secret": "RAW PROFILE SECRET SHOULD NEVER SURFACE",
        "token": "sk-abc12345678901234567890",
        "access_token": "abc",
        "client_secret": "xyz",
        "content": "RAW CONTENT SHOULD NEVER SURFACE",
        "delta": "RAW DELTA SHOULD NEVER SURFACE",
        "nested": {"secret": "s3cr3t", "ok_field": "fine"},
    }
    out = ui_security.redact_browser(payload)
    assert out["prompt"] == "[REDACTED]"
    assert out["memory_body"] == "[REDACTED]"
    assert out["transcript"] == "[REDACTED]"
    assert out["request_dump"] == "[REDACTED]"
    assert out["credentials"] == "[REDACTED]"
    assert out["profile_secret"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["content"] == "[REDACTED]"
    assert out["delta"] == "[REDACTED]"
    assert out["nested"]["secret"] == "[REDACTED]"
    assert out["nested"]["ok_field"] == "fine"


def test_redact_browser_content_allowed_keeps_chat_content(ui_root):
    payload = {
        "content": "the user's own assistant reply text",
        "delta": "streamed token text",
        "tool_result": "output at /home/tony/.hermes/secrets/x with sk-abc12345678901234567890",
        "summary": "3 matches",
        "tool_calls": [{"name": "hermes_search_files", "arguments": {"path": "/home/tony/repo"}}],
    }
    out = ui_security.redact_browser(payload, content_allowed=True)
    assert out["content"] == "the user's own assistant reply text"
    assert out["delta"] == "streamed token text"
    # Tool output is NOT the user's own conversation text: full redaction.
    assert "/home/" not in out["tool_result"]
    assert "sk-" not in out["tool_result"]
    assert out["tool_result"].startswith("output at [REDACTED")
    assert "sk-" not in json.dumps(out["tool_calls"])


def test_redact_browser_truncates_to_preview_cap(ui_root, monkeypatch):
    monkeypatch.setenv(ui_security.UI_TOOL_PREVIEW_BYTES_ENV, "256")
    long_text = "x" * 100_000
    out = ui_security.redact_browser({"tool_result": long_text, "summary": long_text})
    assert len(out["tool_result"]) <= 256 + len(ui_security.TRUNCATED_MARKER)
    assert ui_security.TRUNCATED_MARKER in out["tool_result"]
    assert ui_security.TRUNCATED_MARKER in out["summary"]


def test_redact_browser_redacts_secrets_paths(ui_root):
    payload = {
        "summary": "read /home/tony/.hermes/secrets/hermes_gpt_tokens.json for creds",
        "refs": ["secrets/hermes_gpt_token_key", "/home/tony/.ssh/id_rsa", "/home/tony/repo/file.py"],
        "data": {"path": "/home/tony/.hermes/profiles/dev/config.yaml"},
    }
    out = ui_security.redact_browser(payload)
    assert "secrets/" not in out["summary"]
    assert "hermes_gpt_tokens.json" not in out["summary"]
    assert "hermes_gpt_token_key" not in " ".join(out["refs"])
    assert "/home/" not in " ".join(out["refs"])
    assert "/home/" not in out["data"]["path"]
    assert "[REDACTED" in out["refs"][0]


def test_redact_browser_redacts_secret_substrings(ui_root):
    payload = {
        "error": "auth failed: Bearer tok_abcdefghijklmnop and sk-proj-abc12345678901234567890 AKIAABCDEFGHIJ",
        "summary": "password=hunter2secret and api_key=deadbeef1234",
    }
    out = ui_security.redact_browser(payload)
    assert "Bearer tok_" not in out["error"]
    assert "sk-proj-" not in out["error"]
    assert "AKIA" not in out["error"]
    assert "hunter2secret" not in out["summary"]
    assert "deadbeef1234" not in out["summary"]


def test_redact_browser_never_silently_drops_and_preserves_envelope(ui_root):
    payload = {"ok": True, "data": {"token": "s3cr3t", "code": "TURN_IN_PROGRESS", "message": "busy"}}
    out = ui_security.redact_browser(payload)
    assert out["ok"] is True
    assert out["data"]["code"] == "TURN_IN_PROGRESS"
    assert out["data"]["message"] == "busy"
    assert out["data"]["token"] == "[REDACTED]"


def test_redact_browser_does_not_mutate_source(ui_root):
    source = {"prompt": "RAW PROMPT SHOULD NEVER SURFACE", "list": ["Bearer abcdefghijklmnopq"]}
    snapshot = json.dumps(source, sort_keys=True)
    ui_security.redact_browser(source)
    assert json.dumps(source, sort_keys=True) == snapshot


def test_redact_log_line(ui_root):
    line = ui_security.redact_log_line("user sent sk-abc12345678901234567890 at /home/tony/.hermes/x")
    assert "sk-" not in line
    assert "/home/" not in line


# ---------------------------------------------------------------------------
# Account status derivation
# ---------------------------------------------------------------------------


def test_account_status_loopback_default_ok(ui_root):
    assert ui_security.account_status(ui_root) == "ok"


def test_account_status_static_bearer_ok(ui_root, monkeypatch):
    monkeypatch.setenv(oauth_auth.AUTH_TOKEN_ENV, "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert ui_security.account_status(ui_root) == "ok"


def test_account_status_oauth_without_store_unauthorized(ui_root, monkeypatch):
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    assert ui_security.account_status(ui_root) == "unauthorized"


def test_account_status_expired(ui_root, monkeypatch):
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    _save_tokens(ui_root, time.time() - 3600)
    assert ui_security.account_status(ui_root) == "expired"


def test_account_status_not_expired(ui_root, monkeypatch):
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    _save_tokens(ui_root, time.time() + 3600)
    assert ui_security.account_status(ui_root) == "ok"


def test_account_status_revoked_corrupt_store(ui_root, monkeypatch):
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    # Structurally valid envelope that cannot decrypt -> corrupt -> revoked.
    secrets_dir = ui_root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / token_store.ENVELOPE_FILENAME).write_text(
        json.dumps({"version": 1, "kid": "env", "ciphertext": "zz", "nonce": "zz"}), encoding="utf-8"
    )
    assert ui_security.account_status(ui_root) == "revoked"


# ---------------------------------------------------------------------------
# /api/me and /api/connection endpoints
# ---------------------------------------------------------------------------


def test_me_endpoint_loopback_ok(ui_root, monkeypatch):
    client = _build_app(monkeypatch)
    body = _envelope(client, "/api/me")
    data = body["data"]
    assert data["accountStatus"] == "ok"
    assert data["operatorLevel"] == "read_only"
    assert data["profile"] == "default"
    assert data["serverVersion"] == "0.7.0"
    assert data["allowedSurfaces"] == sorted(op_mission.MISSION_SURFACES)
    assert data["uiCapabilities"][:3] == ["chat", "flight", "events"]
    assert "approvals" not in data["uiCapabilities"]  # read_only level gate
    assert_no_forbidden(json.dumps(body))


def test_me_respects_operator_level_and_capabilities(ui_root, monkeypatch):
    client = _build_app(
        monkeypatch,
        HERMES_GPT_OPERATOR_ENABLED="1",
        HERMES_GPT_OPERATOR_LEVEL="workspace",
    )
    data = _envelope(client, "/api/me")["data"]
    assert data["operatorLevel"] == "workspace"
    assert "approvals" in data["uiCapabilities"]  # workspace gate satisfied


def test_me_allowlist_semantics(ui_root, monkeypatch):
    # Unset = all surfaces (never described as deny-by-default).
    client = _build_app(monkeypatch)
    assert len(_envelope(client, "/api/me")["data"]["allowedSurfaces"]) == len(op_mission.MISSION_SURFACES)
    # Comma list = only listed.
    client = _build_app(monkeypatch, HERMES_GPT_MISSION_ALLOWED_SURFACES="health,profiles")
    assert _envelope(client, "/api/me")["data"]["allowedSurfaces"] == ["health", "profiles"]
    # Empty = none.
    client = _build_app(monkeypatch, HERMES_GPT_MISSION_ALLOWED_SURFACES="")
    assert _envelope(client, "/api/me")["data"]["allowedSurfaces"] == []


def test_me_account_status_expired_endpoint(ui_root, monkeypatch):
    # Static bearer (auth configured) + expired durable store -> expired.
    token = "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    _save_tokens(ui_root, time.time() - 60)
    client = _build_app(monkeypatch, HERMES_GPT_BEARER_TOKEN=token)
    resp = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["accountStatus"] == "expired"


def test_me_requires_auth_when_configured(ui_root, monkeypatch):
    token = "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    client = _build_app(monkeypatch, HERMES_GPT_BEARER_TOKEN=token)
    unauth = client.get("/api/me")
    assert unauth.status_code == 401
    auth = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert auth.status_code == 200
    assert auth.json()["data"]["accountStatus"] == "ok"


def test_connection_endpoint(ui_root, monkeypatch):
    client = _build_app(monkeypatch)
    data = _envelope(client, "/api/connection")["data"]
    assert data["serverStartupId"]
    assert data["staleLeaseSeconds"] == 600
    assert data["toolPreviewBytes"] == 8192
    assert data["accountStatus"] == "ok"


def test_connection_stale_lease_env(ui_root, monkeypatch):
    client = _build_app(monkeypatch, HERMES_GPT_UI_STALE_LEASE_S="120")
    assert _envelope(client, "/api/connection")["data"]["staleLeaseSeconds"] == 120


def test_stale_lease_helper(ui_root, monkeypatch):
    assert ui_security.is_stale_lease(None) is False
    assert ui_security.is_stale_lease(time.time() - 10) is False
    assert ui_security.is_stale_lease(time.time() - 1000) is True
    monkeypatch.setenv(ui_security.UI_STALE_LEASE_S_ENV, "120")
    assert ui_security.is_stale_lease(time.time() - 200) is True
    assert ui_security.is_stale_lease(time.time() - 100) is False


# ---------------------------------------------------------------------------
# Mount behavior
# ---------------------------------------------------------------------------


def test_ui_disabled_has_no_ui_routes(ui_root, monkeypatch):
    client = _build_app(monkeypatch, ui_enabled=False)
    resp = client.get("/api/me")
    assert resp.status_code != 200
    assert "accountStatus" not in resp.text


def test_ui_mounted_before_mcp_and_mcp_intact(ui_root, monkeypatch):
    client = _build_app(monkeypatch)
    assert _envelope(client, "/api/me")["ok"] is True
    # The MCP streamable manager needs the lifespan context (TestClient enter).
    with client:
        mcp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert mcp.status_code == 200
    assert "result" in mcp.json()


def test_ui_api_composes_without_sibling_modules(ui_root, monkeypatch):
    # ui_chat / ui_ops do not exist in this worktree yet; composition degrades.
    routes = ui_api.routes()
    paths = {getattr(r, "path", "") for r in routes}
    assert "/api/me" in paths
    assert "/api/connection" in paths


def test_ui_static_serves_built_spa(ui_root, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>ui-shell</body></html>", encoding="utf-8")
    monkeypatch.setenv(ui_security.UI_DIR_ENV, str(dist))
    client = _build_app(monkeypatch)
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "ui-shell" in resp.text


# ---------------------------------------------------------------------------
# Property sweep: every /api/* route + SSE payload shape
# ---------------------------------------------------------------------------


def test_property_all_api_get_routes_redacted(ui_root, monkeypatch):
    """Every mounted GET /api/* route returns a redacted body."""
    client = _build_app(monkeypatch)

    def walk(route_list):
        for route in route_list:
            path = getattr(route, "path", "")
            if path.startswith("/api/"):
                if getattr(route, "methods", None) and "GET" in route.methods:
                    resp = client.get(path)
                    if resp.status_code == 200:
                        assert_no_forbidden(resp.text)
            mount = getattr(route, "app", None)
            mount_routes = getattr(mount, "routes", None) if mount is not None else None
            if mount_routes:
                walk(mount_routes)

    app_routes = getattr(client.app, "routes", []) or []
    walk(app_routes)


SSE_PAYLOADS = [
    {"event": "meta", "data": {"session_id": "ses-1", "title": "", "ts": 1786000000, "turn_id": "t-abc"}},
    {"event": "token", "data": {"delta": "streamed assistant text, chat content"}},
    {"event": "reasoning", "data": {"delta": "thinking text"}},
    {"event": "tool_start", "data": {"call_id": "call_1", "name": "hermes_search_files", "brief": "search files"}},
    {
        "event": "tool_end",
        "data": {
            "call_id": "call_1",
            "name": "hermes_search_files",
            "status": "ok",
            "summary": "found /home/tony/.hermes/secrets/hermes_gpt_tokens.json",
            "duration_ms": 412,
        },
    },
    {"event": "message_complete", "data": {"message_id": 1234, "role": "assistant"}},
    {"event": "error", "data": {"code": "MODEL_UNAVAILABLE", "message": "Bearer abcdefghijklmnop failed"}},
    {"event": "done", "data": {"turn_id": "t-abc", "message_id": 1234, "finish_reason": "end_turn"}},
]


@pytest.mark.parametrize("sse_event", SSE_PAYLOADS, ids=lambda e: e["event"])
def test_property_sse_payloads_redacted(ui_root, sse_event):
    event = sse_event["event"]
    content_keys = event in ("token", "reasoning")
    out = ui_security.redact_browser(sse_event, content_allowed=content_keys)
    assert_no_forbidden(json.dumps(out))
    if event in ("token", "reasoning"):
        # The user's own streamed text survives; secrets never do.
        assert out["data"]["delta"]
    else:
        assert out["data"].get("summary", "no-summary") != "found /home/tony/.hermes/secrets/hermes_gpt_tokens.json"


def test_error_envelope_preserves_gate_codes(ui_root):
    response = ui_security.err("CONFIRM_REQUIRED", "requires dry-run confirm", status_code=409)
    body = json.loads(bytes(response.body))
    assert response.status_code == 409
    assert body["ok"] is False
    assert body["error"]["code"] == "CONFIRM_REQUIRED"
    assert body["error"]["message"] == "requires dry-run confirm"
    assert_no_forbidden(bytes(response.body).decode("utf-8"))
