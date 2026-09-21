"""Gemini Spark custom-app compatibility: manual confidential-client OAuth.

Locks the OAuth handshake contract Gemini's custom-app flow depends on when it
runs against a server WITHOUT Dynamic Client Registration (DCR): Google's
documented fallback is that the user manually enters a Client ID/Secret
("Advanced features -> Show more" per Google's help page), after which the
normal browser authorization-code flow runs against the existing single
confidential-client authorization server.

The exact Gemini callback URI is deployment-configured through
``HERMES_GPT_OAUTH_REDIRECT_URI`` and is deliberately NOT hardcoded in this
module; the tests below use a representative
``https://oauth-redirect.googleusercontent.com/r/<app>`` value purely as
CONFIG (a stand-in for whatever the operator registered), so the allowlist
semantics stay the subject under test.

Covered: anonymous discovery (protected-resource metadata on both paths plus
authorization-server metadata with no ``registration_endpoint``), exact
redirect-URI allowlisting, PKCE S256, ``client_secret_post`` token exchange,
refresh rotation with replay rejection, an authenticated ``/mcp`` handshake
(initialize + tools/list + a harmless tools/call), and a sanitized handshake
trace that never retains query strings or credential material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

import oauth_auth
import server
import versioning


ISSUER = "https://mcp.example.com"
RESOURCE = f"{ISSUER}/mcp"
CLIENT_ID = "gemini-spark-acceptance"
CLIENT_SECRET = "test-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# CONFIG value only: the real Gemini callback URI is whatever the deployment
# registered in HERMES_GPT_OAUTH_REDIRECT_URI. Nothing here hardcodes Google's
# production path; the tests assert allowlisting semantics against this value.
REDIRECT_URI = "https://oauth-redirect.googleusercontent.com/r/gemini-acceptance"
REDIRECT_HOST = "oauth-redirect.googleusercontent.com"
SCOPE = "hermes"
SUPPORTED_SCOPES = {"hermes", "openid", "offline_access"}
# 43..128 URL-safe characters, per RFC 7636 / the server's own validation.
PKCE_VERIFIER = "test-verifier-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefg"

PROTECTED_RESOURCE_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)


def s256(verifier: str) -> str:
    """RFC 7636 S256 code challenge for a PKCE verifier (no secrets emitted)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_params(**overrides: Any) -> dict[str, Any]:
    """Baseline authorize query params; ``None`` values are dropped.

    This mirrors the browser hop Gemini performs after the user manually
    pastes the Client ID/Secret: response_type=code against the registered
    redirect URI with S256 PKCE.
    """
    params: dict[str, Any] = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": "state-1",
        "code_challenge": s256(PKCE_VERIFIER),
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def redirect_query(response: Any) -> dict[str, list[str]]:
    """Parsed query of an authorize redirect Location (302 responses only)."""
    location = urllib.parse.urlparse(response.headers["location"])
    return urllib.parse.parse_qs(location.query)


def acquire_code(
    client: Any,
    *,
    scope: str = "hermes offline_access",
    state: str = "gemini-state-123",
) -> str:
    """Drive the authorize hop and return the issued authorization code.

    The code is a live secret: callers must never print it or embed it in an
    assertion message.
    """
    response = client.get(
        "/oauth/authorize",
        params=authorize_params(scope=scope, state=state),
        follow_redirects=False,
    )
    assert response.status_code == 302
    return redirect_query(response)["code"][0]


def exchange_code(client: Any, code: str) -> dict[str, Any]:
    """client_secret_post authorization_code exchange, as Gemini performs it."""
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": PKCE_VERIFIER,
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single confidential client registered for the deployment's callback URI."""
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(oauth_auth.OAUTH_ISSUER_ENV, ISSUER)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_SECRET_ENV, CLIENT_SECRET)
    monkeypatch.setenv(oauth_auth.OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    monkeypatch.setenv(oauth_auth.OAUTH_SCOPE_ENV, SCOPE)


@pytest.fixture
def gemini_client(gemini_env: None) -> Iterator[TestClient]:
    """Real ASGI app + real OAuth state, built exactly like test_server.py."""
    # build_server() installs process-global persistence/revocation hooks when
    # OAuth is enabled. Snapshot and restore them so this module cannot change
    # another test module's OAuth behavior in a shared pytest process.
    previous_persist = oauth_auth._persist_hook
    previous_revocation = oauth_auth._revocation_hook
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    with TestClient(app, base_url=ISSUER) as client:
        yield client
    oauth_auth.set_persist_hook(previous_persist)
    oauth_auth.set_revocation_hook(previous_revocation)


class HandshakeRecorder:
    """Record only (method, path-without-query, status) of a handshake.

    Deliberately lossy: query strings, headers, and bodies are dropped before
    anything is retained, so an authorization code or bearer token riding in a
    Location header or form body can never enter the trace, a dump, or a
    failure message.
    """

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.entries: list[dict[str, Any]] = []

    def _record(self, method: str, path: str, response: Any) -> None:
        self.entries.append(
            {
                "method": method,
                "path": urllib.parse.urlparse(path).path,
                "status": response.status_code,
            }
        )

    def get(self, path: str, **kwargs: Any) -> Any:
        response = self._client.get(path, **kwargs)
        self._record("GET", path, response)
        return response

    def post(self, path: str, **kwargs: Any) -> Any:
        response = self._client.post(path, **kwargs)
        self._record("POST", path, response)
        return response


def test_gemini_discovery_contract(gemini_client: TestClient) -> None:
    """Anonymous discovery serves everything except DCR.

    No Authorization header is sent for any request in this test: discovery
    must be reachable before the user has entered the manual credentials.
    """
    for path in PROTECTED_RESOURCE_PATHS:
        response = gemini_client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["resource"] == RESOURCE, path
        assert body["authorization_servers"] == [ISSUER], path
        assert body["bearer_methods_supported"] == ["header"], path
        assert set(body["scopes_supported"]) == SUPPORTED_SCOPES, path

    metadata = gemini_client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert body["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert body["response_types_supported"] == ["code"]
    assert {"authorization_code", "refresh_token"} <= set(body["grant_types_supported"])
    assert body["token_endpoint_auth_methods_supported"] == [
        "client_secret_post",
        "client_secret_basic",
    ]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert set(body["scopes_supported"]) == SUPPORTED_SCOPES
    # No DCR: Gemini has no registration_endpoint to call, so it falls back to
    # the user-supplied confidential client (Client ID + Secret entered under
    # "Advanced features -> Show more") against this single registered client.
    assert "registration_endpoint" not in body

    # The discovery responses above were served anonymously; the protected
    # surface still challenges without a bearer and points back at the PRM.
    unauthenticated = gemini_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert unauthenticated.status_code == 401
    challenge = unauthenticated.headers["www-authenticate"]
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"' in challenge


@pytest.mark.parametrize(
    "redirect_uri",
    [
        REDIRECT_URI,
        f"{REDIRECT_URI}-extra",
        f"http://{REDIRECT_HOST}/r/gemini-acceptance",
        "https://evil.example.com/callback",
    ],
    ids=["configured", "suffix", "scheme", "foreign-host"],
)
def test_gemini_authorize_exact_redirect_allowlist(
    gemini_client: TestClient, redirect_uri: str
) -> None:
    """Only the exact configured redirect URI may receive the redirect."""
    response = gemini_client.get(
        "/oauth/authorize",
        params=authorize_params(redirect_uri=redirect_uri),
        follow_redirects=False,
    )
    if redirect_uri == REDIRECT_URI:
        assert response.status_code == 302
        assert redirect_query(response).get("code")
        return
    # Rejections are a direct JSON error with no redirect at all: a
    # near-miss or foreign callback must never become a dispatch target.
    assert response.status_code == 400
    assert "location" not in response.headers
    body = response.json()
    assert body["error"] == "invalid_request"
    assert "redirect_uri" in body["error_description"]


def test_gemini_authorize_pkce_and_state_roundtrip(gemini_client: TestClient) -> None:
    """S256 PKCE + state survive the authorize hop to the configured host."""
    response = gemini_client.get(
        "/oauth/authorize",
        params=authorize_params(state="gemini-state-123"),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = urllib.parse.urlparse(response.headers["location"])
    assert location.netloc == REDIRECT_HOST
    query = urllib.parse.parse_qs(location.query)
    assert query["state"] == ["gemini-state-123"]
    assert query.get("code")
    # The code is bearer-grade material in transit: it must not be echoed by
    # any JSON surface. Presence-only assertions; the value is never printed.
    code = query["code"][0]
    discovery_bodies = "".join(
        gemini_client.get(path).text for path in PROTECTED_RESOURCE_PATHS
    ) + gemini_client.get("/.well-known/oauth-authorization-server").text
    assert code not in discovery_bodies, "authorization code leaked into a discovery body"


def test_gemini_manual_confidential_client_end_to_end(gemini_client: TestClient) -> None:
    """Acceptance-shaped flow: authorize -> token -> authenticated /mcp."""
    code = acquire_code(gemini_client)
    credentials = exchange_code(gemini_client, code)
    assert credentials["token_type"] == "Bearer"
    assert credentials["expires_in"] == 3600
    assert credentials["scope"] == "hermes offline_access"
    assert credentials["access_token"]
    assert credentials["refresh_token"]

    headers = {"Authorization": f"Bearer {credentials['access_token']}"}
    initialize = gemini_client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gemini-spark", "version": "test"},
            },
        },
    )
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["version"] == versioning.VERSION

    listing = gemini_client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listing.status_code == 200
    assert "hermes_skill_list" in {
        tool["name"] for tool in listing.json()["result"]["tools"]
    }

    # Harmless read-only call: proves the credential reaches the tool layer.
    # HTTP bodies already use the wire field names (isError/content), so the
    # SDK-model helper conftest.wire() is not needed here; this mirrors
    # test_mcp_sdk_migration.py's direct result inspection.
    call = gemini_client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "hermes_skill_list", "arguments": {}},
        },
    )
    assert call.status_code == 200
    result = call.json()["result"]
    assert result.get("isError") is not True, "hermes_skill_list must not fail the handshake"
    assert result["content"]


def test_gemini_unknown_scope_and_resource_fail_closed(gemini_client: TestClient) -> None:
    """Unsupported scope/resource fail closed; a missing resource uses the default."""
    unsupported_scope = gemini_client.get(
        "/oauth/authorize",
        params=authorize_params(scope="hermes ACCESS_VIEW_MANAGE_MCP_CONTENT"),
        follow_redirects=False,
    )
    assert unsupported_scope.status_code == 302
    query = redirect_query(unsupported_scope)
    assert query["error"] == ["invalid_scope"]
    assert "code" not in query

    foreign_resource = gemini_client.get(
        "/oauth/authorize",
        params=authorize_params(resource="https://elsewhere.example.com/mcp"),
        follow_redirects=False,
    )
    assert foreign_resource.status_code == 302
    query = redirect_query(foreign_resource)
    assert query["error"] == ["invalid_target"]
    assert "code" not in query

    # No resource parameter at all is legitimate: it defaults to this server's
    # own resource, which is what Gemini's connector sends.
    defaulted = gemini_client.get(
        "/oauth/authorize",
        params=authorize_params(resource=None),
        follow_redirects=False,
    )
    assert defaulted.status_code == 302
    query = redirect_query(defaulted)
    assert "error" not in query
    assert query.get("code")


def test_gemini_refresh_rotation_and_replay_rejection(gemini_client: TestClient) -> None:
    """offline_access yields a refresh token that rotates and cannot replay."""
    code = acquire_code(gemini_client)
    credentials = exchange_code(gemini_client, code)
    refresh_token = credentials["refresh_token"]

    rotated = gemini_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    assert rotated.status_code == 200
    rotated_body = rotated.json()
    assert rotated_body["token_type"] == "Bearer"
    assert rotated_body["access_token"]
    assert rotated_body["refresh_token"] != refresh_token, "refresh token must rotate"

    replay = gemini_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_gemini_handshake_trace_is_sanitized(
    gemini_client: TestClient, tmp_path: Any
) -> None:
    """The full handshake leaves a trace with no query strings or secrets."""
    recorder = HandshakeRecorder(gemini_client)
    recorder.get("/.well-known/oauth-protected-resource")
    recorder.get("/.well-known/oauth-authorization-server")
    authorize = recorder.get(
        "/oauth/authorize",
        params=authorize_params(scope="hermes offline_access", state="gemini-state-123"),
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    code = redirect_query(authorize)["code"][0]
    issued = recorder.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": PKCE_VERIFIER,
        },
    )
    assert issued.status_code == 200
    credentials = issued.json()
    recorder.post(
        "/mcp",
        headers={"Authorization": f"Bearer {credentials['access_token']}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gemini-spark", "version": "test"},
            },
        },
    )

    paths = [entry["path"] for entry in recorder.entries]
    for path in paths:
        assert "?" not in path, "trace must strip query strings entirely"
        assert "code=" not in path
        assert "token=" not in path
    assert {entry["method"] for entry in recorder.entries} == {"GET", "POST"}
    assert {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/oauth/authorize",
        "/oauth/token",
        "/mcp",
    } <= set(paths)

    dump = tmp_path / "gemini-handshake-trace.json"
    dump.write_text(json.dumps(recorder.entries, indent=2, sort_keys=True), encoding="utf-8")
    text = dump.read_text(encoding="utf-8")
    for needle in ("access_token", "refresh_token", "code_verifier", "client_secret"):
        assert needle not in text, f"trace dump leaked a credential field: {needle}"
    for secret in (
        code,
        credentials["access_token"],
        credentials["refresh_token"],
        PKCE_VERIFIER,
        CLIENT_SECRET,
    ):
        assert secret not in text, "trace dump leaked credential material"
