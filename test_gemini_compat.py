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

It also covers the opt-in client-profile registry: with
``HERMES_GPT_OAUTH_GEMINI_ENABLE=1`` and the ``HERMES_GPT_OAUTH_GEMINI_*``
client id/secret/redirect settings, ``gemini_client_from_env()`` registers a
SECOND confidential client (the Gemini Spark profile) alongside the primary
client. These tests lock the isolation contract between the two: each client
redirects only to its own exact-match callback URI, authenticates the token
endpoint only with its own secret, spends only the codes it was issued, and
refreshes only the tokens bound to it; access tokens naming a client that is
not registered fail validation. A deployment that never sets the opt-in env
keeps the single-client behavior above: no second client, and the profile's
client_id plus callback URI stay unknown (fail closed).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
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

# The opt-in client-profile registry tests below model the two-client
# deployment: a ChatGPT-style PRIMARY client registered through the original
# HERMES_GPT_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI settings, plus the Gemini
# Spark profile registered by the GEMINI_* settings. The profile's client_id is
# the manual Client ID a Gemini Spark custom app is configured with, and its
# callback URI is a representative
# ``https://oauth-redirect.googleusercontent.com/r/<app>`` CONFIG value (the
# deployment's own registration stands in for it), so the isolation semantics
# stay the subject under test.
PRIMARY_CLIENT_ID = "chatgpt-primary"
PRIMARY_CLIENT_SECRET = "primary-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PRIMARY_REDIRECT_URI = "https://chatgpt.com/connector/oauth/callback"
GEMINI_CLIENT_ID = "gemini-spark-acceptance"
GEMINI_CLIENT_SECRET = "gemini-spark-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Synthetic callback shape (values anonymized): the real Gemini binding URI is
# deployment-observed and configured, never hardcoded.
GEMINI_REDIRECT_URI = (
    "https://oauth-redirect.googleusercontent.com/r/"
    "user_bound_custom-mcp-123456789012345678901-example_com"
)
GEMINI_PROFILE_ENV_VARS = (
    oauth_auth.GEMINI_ENABLE_ENV,
    oauth_auth.GEMINI_CLIENT_ID_ENV,
    oauth_auth.GEMINI_CLIENT_SECRET_ENV,
    oauth_auth.GEMINI_REDIRECT_URI_ENV,
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


def acquire_code_as(
    client: Any,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str = "hermes offline_access",
    state: str = "gemini-state-123",
) -> str:
    """Authorize hop for one named client; the code is a live secret.

    Callers must never print the returned code or embed it in an assertion.
    """
    response = client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=client_id, redirect_uri=redirect_uri, scope=scope, state=state
        ),
        follow_redirects=False,
    )
    assert response.status_code == 302
    return redirect_query(response)["code"][0]


def post_token(
    client: Any,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> Any:
    """client_secret_post authorization_code exchange with explicit credentials.

    Returns the raw response so tests can assert on rejection statuses.
    """
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": PKCE_VERIFIER,
        },
    )


def post_refresh(
    client: Any, *, client_id: str, client_secret: str, refresh_token: str
) -> Any:
    """client_secret_post refresh grant with explicit client credentials."""
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )


def exchange_as(
    client: Any,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Successful exchange for one named client (JSON body, never printed)."""
    response = post_token(
        client,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    assert response.status_code == 200
    return response.json()


def resign_access_token_for_client(state: Any, token_value: str, client_id: str) -> str:
    """Re-MAC a signed access token under a different ``client_id``.

    Used by the validation tests to build a token whose signature is genuine
    but whose named client is (or is not) registered. The return value is live
    credential material: never printed and never used in an assertion message.
    """
    encoded = token_value[len(oauth_auth.ACCESS_TOKEN_PREFIX) :].split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["client_id"] = client_id
    replacement = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = hmac.new(
        state._access_token_key(), replacement.encode("ascii"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{oauth_auth.ACCESS_TOKEN_PREFIX}{replacement}.{encoded_signature}"


@pytest.fixture
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single confidential client registered for the deployment's callback URI."""
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(oauth_auth.OAUTH_ISSUER_ENV, ISSUER)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_SECRET_ENV, CLIENT_SECRET)
    monkeypatch.setenv(oauth_auth.OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    monkeypatch.setenv(oauth_auth.OAUTH_SCOPE_ENV, SCOPE)


@contextlib.contextmanager
def built_test_client() -> Iterator[TestClient]:
    """Real ASGI app + real OAuth state, built exactly like test_server.py.

    build_server() installs process-global persistence/revocation hooks when
    OAuth is enabled. Snapshot and restore them so this module cannot change
    another test module's OAuth behavior in a shared pytest process.
    """
    previous_persist = oauth_auth._persist_hook
    previous_revocation = oauth_auth._revocation_hook
    built = server.build_server(http=True)
    app = server.build_asgi_app(built, http=True)
    try:
        with TestClient(app, base_url=ISSUER) as client:
            yield client
    finally:
        oauth_auth.set_persist_hook(previous_persist)
        oauth_auth.set_revocation_hook(previous_revocation)


@pytest.fixture
def gemini_client(gemini_env: None) -> Iterator[TestClient]:
    """Single-client deployment: the shape every existing install has."""
    with built_test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def isolated_gemini_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the opt-in profile OFF unless a test enables it itself.

    conftest clears the primary OAuth env vars but predates this opt-in
    profile, so an invoking shell that exported the GEMINI_* vars could
    otherwise register a second client behind a test's back.
    """
    for name in GEMINI_PROFILE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the primary (ChatGPT-style) client; the profile stays disabled."""
    monkeypatch.setenv(oauth_auth.OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(oauth_auth.OAUTH_ISSUER_ENV, ISSUER)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_ID_ENV, PRIMARY_CLIENT_ID)
    monkeypatch.setenv(oauth_auth.OAUTH_CLIENT_SECRET_ENV, PRIMARY_CLIENT_SECRET)
    monkeypatch.setenv(oauth_auth.OAUTH_REDIRECT_URI_ENV, PRIMARY_REDIRECT_URI)
    monkeypatch.setenv(oauth_auth.OAUTH_SCOPE_ENV, SCOPE)


@pytest.fixture
def dual_client_env(primary_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary client plus the opt-in Gemini Spark client profile enabled."""
    monkeypatch.setenv(oauth_auth.GEMINI_ENABLE_ENV, "1")
    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_ID_ENV, GEMINI_CLIENT_ID)
    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_SECRET_ENV, GEMINI_CLIENT_SECRET)
    monkeypatch.setenv(oauth_auth.GEMINI_REDIRECT_URI_ENV, GEMINI_REDIRECT_URI)


@pytest.fixture
def primary_client(primary_env: None) -> Iterator[TestClient]:
    """Deployment with the primary client only (profile disabled)."""
    with built_test_client() as client:
        yield client


@pytest.fixture
def dual_client(dual_client_env: None) -> Iterator[TestClient]:
    """Deployment with both registered clients (profile enabled)."""
    with built_test_client() as client:
        yield client


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


# ---------------------------------------------------------------------------
# Opt-in client-profile registry: the Gemini Spark profile is an ADDITIONAL
# registered confidential client. The tests below lock its isolation from the
# primary client in both directions.
# ---------------------------------------------------------------------------


def test_gemini_profile_disabled_rejects_unknown_client(primary_client: TestClient) -> None:
    """Without the opt-in env the profile is neither a client nor a callback.

    This is the single-client deployment every existing install has: the
    Gemini client_id is simply an unknown client, and its callback URI is
    registered nowhere, so both fail closed before any redirect is issued.
    """
    unknown = primary_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=GEMINI_CLIENT_ID, redirect_uri=GEMINI_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"] == "invalid_client"
    assert "location" not in unknown.headers

    # The disabled profile's callback is not registered for the primary client
    # either: enabling the profile is what registers that URI, per client.
    unregistered_callback = primary_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=PRIMARY_CLIENT_ID, redirect_uri=GEMINI_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert unregistered_callback.status_code == 400
    assert unregistered_callback.json()["error"] == "invalid_request"
    assert "redirect_uri" in unregistered_callback.json()["error_description"]
    assert "location" not in unregistered_callback.headers

    # The primary client's own flow is untouched by the absent profile.
    issued = primary_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=PRIMARY_CLIENT_ID,
            redirect_uri=PRIMARY_REDIRECT_URI,
            state="primary-state",
        ),
        follow_redirects=False,
    )
    assert issued.status_code == 302
    query = redirect_query(issued)
    assert query.get("code")
    assert query["state"] == ["primary-state"]


def test_gemini_profile_registers_isolated_second_client(dual_client: TestClient) -> None:
    """Enabled, both clients exist and redirect only to their own callback.

    The primary client's allowlist and the profile's allowlist are disjoint:
    the *other* client's perfectly valid callback URI is still a rejection with
    no Location header, and an unregistered client id never gets a redirect.
    """
    gemini = dual_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=GEMINI_CLIENT_ID, redirect_uri=GEMINI_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert gemini.status_code == 302
    assert redirect_query(gemini).get("code")

    crossed_to_gemini = dual_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=PRIMARY_CLIENT_ID, redirect_uri=GEMINI_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert crossed_to_gemini.status_code == 400
    assert crossed_to_gemini.json()["error"] == "invalid_request"
    assert "redirect_uri" in crossed_to_gemini.json()["error_description"]
    assert "location" not in crossed_to_gemini.headers

    crossed_to_primary = dual_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id=GEMINI_CLIENT_ID, redirect_uri=PRIMARY_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert crossed_to_primary.status_code == 400
    assert crossed_to_primary.json()["error"] == "invalid_request"
    assert "redirect_uri" in crossed_to_primary.json()["error_description"]
    assert "location" not in crossed_to_primary.headers

    unknown = dual_client.get(
        "/oauth/authorize",
        params=authorize_params(
            client_id="unregistered-client", redirect_uri=GEMINI_REDIRECT_URI
        ),
        follow_redirects=False,
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"] == "invalid_client"
    assert "location" not in unknown.headers


def test_gemini_profile_token_exchange_is_isolated(dual_client: TestClient) -> None:
    """Codes, tokens, and refreshes stay bound to the client that got them."""
    code = acquire_code_as(
        dual_client, client_id=GEMINI_CLIENT_ID, redirect_uri=GEMINI_REDIRECT_URI
    )

    # The primary client's credentials are valid for the primary, but the code
    # carries the gemini client_id: the exchange fails on the binding, and it
    # is refused WITHOUT consuming the code.
    crossed = post_token(
        dual_client,
        client_id=PRIMARY_CLIENT_ID,
        client_secret=PRIMARY_CLIENT_SECRET,
        code=code,
        redirect_uri=GEMINI_REDIRECT_URI,
    )
    assert crossed.status_code == 400
    assert crossed.json()["error"] == "invalid_grant"

    # The profile's client_id with the primary client's secret cannot
    # authenticate at all.
    wrong_secret = post_token(
        dual_client,
        client_id=GEMINI_CLIENT_ID,
        client_secret=PRIMARY_CLIENT_SECRET,
        code=code,
        redirect_uri=GEMINI_REDIRECT_URI,
    )
    assert wrong_secret.status_code == 401
    assert wrong_secret.json()["error"] == "invalid_client"

    # Both rejections above must have left the code exchangeable by its owner.
    credentials = exchange_as(
        dual_client,
        client_id=GEMINI_CLIENT_ID,
        client_secret=GEMINI_CLIENT_SECRET,
        code=code,
        redirect_uri=GEMINI_REDIRECT_URI,
    )
    assert credentials["access_token"]
    assert credentials["refresh_token"]

    # Refresh binding is per client too. The primary credentials authenticate,
    # so the rejection is the token's client binding, and the refresh token
    # survives it to rotate for the client it belongs to.
    crossed_refresh = post_refresh(
        dual_client,
        client_id=PRIMARY_CLIENT_ID,
        client_secret=PRIMARY_CLIENT_SECRET,
        refresh_token=credentials["refresh_token"],
    )
    assert crossed_refresh.status_code == 400
    assert crossed_refresh.json()["error"] == "invalid_grant"

    rotated = post_refresh(
        dual_client,
        client_id=GEMINI_CLIENT_ID,
        client_secret=GEMINI_CLIENT_SECRET,
        refresh_token=credentials["refresh_token"],
    )
    assert rotated.status_code == 200
    rotated_body = rotated.json()
    assert rotated_body["token_type"] == "Bearer"
    assert rotated_body["access_token"]
    assert rotated_body["refresh_token"] != credentials["refresh_token"]


def test_gemini_profile_both_clients_authenticate_mcp(dual_client: TestClient) -> None:
    """Each registered client's own credentials complete an /mcp handshake."""
    for client_id, client_secret, redirect_uri in (
        (PRIMARY_CLIENT_ID, PRIMARY_CLIENT_SECRET, PRIMARY_REDIRECT_URI),
        (GEMINI_CLIENT_ID, GEMINI_CLIENT_SECRET, GEMINI_REDIRECT_URI),
    ):
        code = acquire_code_as(
            dual_client, client_id=client_id, redirect_uri=redirect_uri
        )
        credentials = exchange_as(
            dual_client,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
        headers = {"Authorization": f"Bearer {credentials['access_token']}"}

        initialize = dual_client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": client_id, "version": "test"},
                },
            },
        )
        assert initialize.status_code == 200
        assert initialize.json()["result"]["serverInfo"]["version"] == versioning.VERSION

        listing = dual_client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listing.status_code == 200
        assert "hermes_skill_list" in {
            tool["name"] for tool in listing.json()["result"]["tools"]
        }


def test_gemini_profile_env_validation_fails_closed(
    primary_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off by default; enabled-but-wrong configuration never half-registers."""
    # Disabled: no additional client and nothing to validate.
    assert oauth_auth.gemini_client_from_env() is None
    disabled = oauth_auth.config_from_env()
    assert disabled is not None
    assert [client.client_id for client in disabled.clients] == [PRIMARY_CLIENT_ID]

    # Enabled but incomplete: the error names every missing setting and no
    # configuration is produced (no partially-registered client).
    monkeypatch.setenv(oauth_auth.GEMINI_ENABLE_ENV, "1")
    with pytest.raises(ValueError) as missing:
        oauth_auth.config_from_env()
    message = str(missing.value)
    assert "required configuration is missing" in message
    for name in (
        oauth_auth.GEMINI_CLIENT_ID_ENV,
        oauth_auth.GEMINI_CLIENT_SECRET_ENV,
        oauth_auth.GEMINI_REDIRECT_URI_ENV,
    ):
        assert name in message

    # A secret the primary client would also be refused for is refused here,
    # with the profile named so the operator knows which setting is at fault.
    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_ID_ENV, GEMINI_CLIENT_ID)
    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_SECRET_ENV, "short")
    monkeypatch.setenv(oauth_auth.GEMINI_REDIRECT_URI_ENV, GEMINI_REDIRECT_URI)
    with pytest.raises(ValueError) as weak_secret:
        oauth_auth.config_from_env()
    assert str(weak_secret.value).startswith("Gemini Spark client profile:")
    assert "43 to 128" in str(weak_secret.value)

    # A valid profile registers as a second client, primary first; a client_id
    # that collides with the primary is refused outright.
    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_SECRET_ENV, GEMINI_CLIENT_SECRET)
    configured = oauth_auth.config_from_env()
    assert configured is not None
    assert [client.client_id for client in configured.clients] == [
        PRIMARY_CLIENT_ID,
        GEMINI_CLIENT_ID,
    ]
    assert configured.client_registered(GEMINI_CLIENT_ID)
    gemini_profile = configured.client_for_id(GEMINI_CLIENT_ID)
    assert gemini_profile is not None
    assert gemini_profile.redirect_uris == (GEMINI_REDIRECT_URI,)

    monkeypatch.setenv(oauth_auth.GEMINI_CLIENT_ID_ENV, PRIMARY_CLIENT_ID)
    with pytest.raises(ValueError, match="must be unique"):
        oauth_auth.config_from_env()


def dual_client_config() -> oauth_auth.OAuthConfig:
    """Both clients registered in-process (no server, no env)."""
    return oauth_auth.OAuthConfig(
        issuer=ISSUER,
        client_id=PRIMARY_CLIENT_ID,
        client_secret=PRIMARY_CLIENT_SECRET,
        redirect_uris=(PRIMARY_REDIRECT_URI,),
        scope=SCOPE,
        additional_clients=(
            oauth_auth.OAuthClient(
                client_id=GEMINI_CLIENT_ID,
                client_secret=GEMINI_CLIENT_SECRET,
                redirect_uris=(GEMINI_REDIRECT_URI,),
            ),
        ),
    )


def test_unregistered_client_token_is_rejected(tmp_path: Path) -> None:
    """Tokens naming a client that is not registered do not validate.

    Both clients in this config are registered; ``ghost`` is not. The signed
    in-memory path, the in-memory item branch of the durable validator, and the
    restored durable envelope all consult that registry, while tokens bound to
    a registered client keep working.

    Every validation path consults the registry, including the standalone
    in-memory fast path of ``validate_access_token``: a raw item already
    sitting in ``state.access_tokens`` on a state with no durable root is
    returned only when its ``client_id`` is registered.
    """
    config = dual_client_config()
    far_future = time.time() + 3600

    # In-memory (no durable authority): a genuine signed token for a registered
    # client validates; the same token re-MAC'd under an unregistered client_id
    # has a valid signature and is still refused.
    standalone = oauth_auth.OAuthState(config)
    registered_code = standalone.issue_authorization_code(
        client_id=PRIMARY_CLIENT_ID,
        redirect_uri=PRIMARY_REDIRECT_URI,
        scope=SCOPE,
        resource=config.resource,
        code_challenge="",
    )
    registered_token = standalone.exchange_authorization_code(
        code=registered_code,
        client_id=PRIMARY_CLIENT_ID,
        redirect_uri=PRIMARY_REDIRECT_URI,
        code_verifier="",
    )["access_token"]
    # Bind each verdict to a bool first: a failure message must never carry the
    # token value that pytest's introspection would otherwise echo.
    assert standalone.validate_access_token(registered_token) is True
    ghost_signed = resign_access_token_for_client(standalone, registered_token, "ghost")
    assert standalone.validate_access_token(ghost_signed) is False

    # In-memory items are also checked against the registry on the in-memory
    # branch of the durable validator.
    standalone.access_tokens["ghost-in-memory-item"] = {
        "client_id": "ghost",
        "scope": SCOPE,
        "resource": config.resource,
        "expires_at": far_future,
    }
    standalone.access_tokens["gemini-in-memory-item"] = {
        "client_id": GEMINI_CLIENT_ID,
        "scope": SCOPE,
        "resource": config.resource,
        "expires_at": far_future,
    }
    assert standalone._durable_access_token_valid("ghost-in-memory-item") is False
    assert standalone._durable_access_token_valid("gemini-in-memory-item") is True
    # The standalone fast path of validate_access_token consults the same
    # registry before returning a cached item.
    assert standalone.validate_access_token("ghost-in-memory-item") is False
    assert standalone.validate_access_token("gemini-in-memory-item") is True

    # Durable envelope (the server-mode shape): persist, then validate from a
    # state that only saw the encrypted store.
    hermes_root = tmp_path / "hermes"
    (hermes_root / "secrets").mkdir(parents=True, exist_ok=True)
    durable = oauth_auth.OAuthState(config)
    for value, client_id in (
        ("ghost-durable-item", "ghost"),
        ("primary-durable-item", PRIMARY_CLIENT_ID),
        ("gemini-durable-item", GEMINI_CLIENT_ID),
    ):
        durable.access_tokens[value] = {
            "client_id": client_id,
            "scope": SCOPE,
            "resource": config.resource,
            "expires_at": far_future,
        }
    durable.persist_tokens(hermes_root)
    assert durable.validate_access_token("ghost-durable-item") is False

    restored = oauth_auth.OAuthState(config)
    restored.restore_tokens(hermes_root)
    assert restored.validate_access_token("ghost-durable-item") is False
    assert restored.validate_access_token("primary-durable-item") is True
    assert restored.validate_access_token("gemini-durable-item") is True
