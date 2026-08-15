from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import urllib.parse

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from oauth_auth import (
    ACCESS_TOKEN_TTL_SECONDS,
    AUTH_TOKEN_ENV,
    BearerAuthMiddleware,
    DefaultMcpAcceptMiddleware,
    MAX_ACCESS_TOKENS,
    MAX_AUTH_CODES,
    MAX_REFRESH_TOKENS,
    OAUTH_CLIENT_ID_ENV,
    OAUTH_CLIENT_SECRET_ENV,
    OAUTH_ENABLE_ENV,
    OAUTH_ISSUER_ENV,
    OAUTH_REDIRECT_URI_ENV,
    OAUTH_SCOPE_ENV,
    OAuthConfig,
    OAuthError,
    OAuthState,
    authorization_metadata,
    authorize,
    config_from_env,
    protected_resource_metadata,
    token,
    validate_bearer_token,
)


CLIENT_ID = "chatgpt-client"
CLIENT_SECRET = "test-client-secret-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
REDIRECT_URI = "https://chatgpt.com/connector/oauth/callback"
ISSUER = "https://mcp.example.com"
RESOURCE = f"{ISSUER}/mcp"
STATIC_BEARER = "test-static-bearer-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@pytest.fixture
def oauth_state() -> OAuthState:
    return OAuthState(
        OAuthConfig(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uris=(REDIRECT_URI,),
            scope="hermes",
        )
    )


@pytest.fixture
def oauth_client(oauth_state: OAuthState) -> TestClient:
    async def resource_endpoint(request):
        return protected_resource_metadata(request, oauth_state)

    async def metadata_endpoint(request):
        return authorization_metadata(request, oauth_state)

    async def authorize_endpoint(request):
        return authorize(request, oauth_state)

    async def token_endpoint(request):
        return await token(request, oauth_state)

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-protected-resource", resource_endpoint),
            Route("/.well-known/oauth-protected-resource/mcp", resource_endpoint),
            Route("/.well-known/oauth-authorization-server", metadata_endpoint),
            Route("/oauth/authorize", authorize_endpoint),
            Route("/oauth/token", token_endpoint, methods=["POST"]),
        ]
    )
    return TestClient(app)


def authorize_code(
    client: TestClient,
    *,
    scope: str = "openid hermes offline_access",
    code_challenge: str | None = None,
    resource: str = RESOURCE,
) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": scope,
        "state": "state-value",
        "resource": resource,
    }
    if code_challenge:
        params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302
    location = urllib.parse.urlparse(response.headers["location"])
    return urllib.parse.parse_qs(location.query)["code"][0]


def exchange_code(
    client: TestClient,
    code: str,
    *,
    secret: str = CLIENT_SECRET,
    code_verifier: str | None = None,
):
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    return client.post("/oauth/token", data=data)


def refresh(client: TestClient, refresh_token: str, *, secret: str = CLIENT_SECRET, scope: str | None = None):
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": secret,
        "refresh_token": refresh_token,
    }
    if scope is not None:
        data["scope"] = scope
    return client.post("/oauth/token", data=data)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_config_requires_a_confidential_client_secret():
    with pytest.raises(ValueError, match="43 to 128"):
        OAuthConfig(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret="short",
            redirect_uris=(REDIRECT_URI,),
            scope="hermes",
        )


@pytest.mark.parametrize(
    ("issuer", "redirect_uri"),
    [
        ("https://mcp.example.com/subpath", REDIRECT_URI),
        ("https://user:pass@mcp.example.com", REDIRECT_URI),
        (ISSUER, "https://user:pass@chatgpt.com/connector/oauth/callback"),
    ],
)
def test_config_rejects_subpath_issuers_and_url_userinfo(issuer: str, redirect_uri: str):
    with pytest.raises(ValueError):
        OAuthConfig(
            issuer=issuer,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uris=(redirect_uri,),
        )


def test_environment_config_is_disabled_by_default_and_requires_complete_confidential_client(monkeypatch):
    for name in (
        OAUTH_ENABLE_ENV,
        OAUTH_ISSUER_ENV,
        OAUTH_CLIENT_ID_ENV,
        OAUTH_CLIENT_SECRET_ENV,
        OAUTH_REDIRECT_URI_ENV,
        OAUTH_SCOPE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    assert config_from_env() is None

    monkeypatch.setenv(OAUTH_ENABLE_ENV, "1")
    monkeypatch.setenv(OAUTH_ISSUER_ENV, ISSUER)
    monkeypatch.setenv(OAUTH_CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(OAUTH_REDIRECT_URI_ENV, REDIRECT_URI)
    with pytest.raises(ValueError, match=OAUTH_CLIENT_SECRET_ENV):
        config_from_env()

    monkeypatch.setenv(OAUTH_CLIENT_SECRET_ENV, CLIENT_SECRET)
    configured = config_from_env()
    assert configured is not None
    assert configured.client_id == CLIENT_ID
    assert configured.redirect_uris == (REDIRECT_URI,)


def test_discovery_truthfully_advertises_implemented_capabilities(oauth_client: TestClient):
    metadata = oauth_client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["client_secret_post", "client_secret_basic"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert set(metadata["scopes_supported"]) == {"hermes", "openid", "offline_access"}
    assert "none" not in metadata["token_endpoint_auth_methods_supported"]
    assert "jwks_uri" not in metadata
    assert "id_token_signing_alg_values_supported" not in metadata


def test_protected_resource_metadata_matches_authorization_server(oauth_client: TestClient):
    resource = oauth_client.get("/.well-known/oauth-protected-resource").json()
    path_derived_resource = oauth_client.get("/.well-known/oauth-protected-resource/mcp").json()
    authorization = oauth_client.get("/.well-known/oauth-authorization-server").json()
    assert resource["resource"] == RESOURCE
    assert resource["authorization_servers"] == [ISSUER]
    assert resource["scopes_supported"] == authorization["scopes_supported"]
    assert path_derived_resource == resource


def test_code_is_useless_without_client_authentication(oauth_client: TestClient):
    code = authorize_code(oauth_client)
    response = exchange_code(oauth_client, code, secret="")
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_wrong_client_secret_does_not_consume_authorization_code(oauth_client: TestClient):
    code = authorize_code(oauth_client)
    assert exchange_code(oauth_client, code, secret="wrong-secret-that-is-still-long-enough").status_code == 401
    assert exchange_code(oauth_client, code).status_code == 200


def test_signed_authorization_code_rejects_tampering_and_replay(oauth_client: TestClient):
    code = authorize_code(oauth_client)
    replacement = "A" if code[-1] != "A" else "B"
    tampered = exchange_code(oauth_client, code[:-1] + replacement)
    assert tampered.status_code == 400
    assert tampered.json()["error"] == "invalid_grant"

    assert exchange_code(oauth_client, code).status_code == 200
    replay = exchange_code(oauth_client, code)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_signed_authorization_code_rejects_noncanonical_base64url_spelling(
    oauth_client: TestClient,
):
    code = authorize_code(oauth_client)
    payload, signature = code.split(".", 1)
    padding = "=" * (-len(signature) % 4)
    decoded = base64.urlsafe_b64decode(signature + padding)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alternate = next(
        candidate
        for candidate in alphabet
        if candidate != signature[-1]
        and base64.urlsafe_b64decode(signature[:-1] + candidate + padding) == decoded
    )
    response = exchange_code(oauth_client, f"{payload}.{signature[:-1]}{alternate}")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_process_restart_invalidates_signed_authorization_codes(oauth_state: OAuthState):
    code = oauth_state.issue_authorization_code(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope="hermes",
        resource=RESOURCE,
        code_challenge="",
    )
    restarted = OAuthState(oauth_state.config)
    with pytest.raises(OAuthError) as exc_info:
        restarted.exchange_authorization_code(
            code=code,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="",
        )
    assert exc_info.value.error == "invalid_grant"


def test_token_endpoint_rejects_oversized_form_body(oauth_client: TestClient):
    response = oauth_client.post(
        "/oauth/token",
        content=b"grant_type=authorization_code&padding=" + (b"x" * 17000),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_token_endpoint_stops_reading_chunked_body_at_limit(oauth_state: OAuthState):
    chunks = [b"x" * 10000, b"y" * 10000, b"z" * 10000]
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        chunk = chunks[receive_calls]
        receive_calls += 1
        return {"type": "http.request", "body": chunk, "more_body": receive_calls < len(chunks)}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/oauth/token",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )
    response = asyncio.run(token(request, oauth_state))
    assert response.status_code == 400
    assert receive_calls == 2


def test_authorization_code_exchange_preserves_legacy_chatgpt_request_without_pkce(oauth_client: TestClient):
    code = authorize_code(oauth_client, scope="openid hermes")
    response = exchange_code(oauth_client, code)
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == ACCESS_TOKEN_TTL_SECONDS
    assert body["scope"] == "openid hermes"
    assert "refresh_token" not in body


def test_pkce_s256_is_enforced_when_the_client_supplies_a_challenge(oauth_client: TestClient):
    verifier = "a" * 64
    code = authorize_code(oauth_client, code_challenge=s256(verifier))
    missing = exchange_code(oauth_client, code)
    assert missing.status_code == 400
    assert missing.json()["error"] == "invalid_grant"
    wrong = exchange_code(oauth_client, code, code_verifier="b" * 64)
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    assert exchange_code(oauth_client, code, code_verifier=verifier).status_code == 200


def test_invalid_pkce_method_and_resource_redirect_errors_with_state(oauth_client: TestClient):
    common = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "hermes",
        "state": "opaque-state",
    }
    bad_method = oauth_client.get(
        "/oauth/authorize",
        params={**common, "code_challenge": "x" * 43, "code_challenge_method": "plain"},
        follow_redirects=False,
    )
    assert bad_method.status_code == 302
    method_error = urllib.parse.parse_qs(urllib.parse.urlparse(bad_method.headers["location"]).query)
    assert method_error["error"] == ["invalid_request"]
    assert method_error["state"] == ["opaque-state"]

    bad_resource = oauth_client.get(
        "/oauth/authorize",
        params={**common, "resource": "https://attacker.example/mcp"},
        follow_redirects=False,
    )
    assert bad_resource.status_code == 302
    resource_error = urllib.parse.parse_qs(urllib.parse.urlparse(bad_resource.headers["location"]).query)
    assert resource_error["error"] == ["invalid_target"]
    assert resource_error["state"] == ["opaque-state"]


def test_resource_scope_is_required_for_authorization_and_refresh(oauth_client: TestClient):
    denied = oauth_client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": "openid offline_access",
        },
        follow_redirects=False,
    )
    denied_query = urllib.parse.parse_qs(urllib.parse.urlparse(denied.headers["location"]).query)
    assert denied_query["error"] == ["invalid_scope"]

    issued = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    narrowed_too_far = refresh(oauth_client, issued["refresh_token"], scope="openid offline_access")
    assert narrowed_too_far.status_code == 400
    assert narrowed_too_far.json()["error"] == "invalid_scope"


def test_offline_access_issues_rotating_client_bound_refresh_token(oauth_client: TestClient):
    initial = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    assert initial["refresh_token"]
    refreshed_response = refresh(oauth_client, initial["refresh_token"])
    assert refreshed_response.status_code == 200
    refreshed = refreshed_response.json()
    assert refreshed["access_token"] != initial["access_token"]
    assert refreshed["refresh_token"] != initial["refresh_token"]
    replay = refresh(oauth_client, initial["refresh_token"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_wrong_refresh_client_secret_does_not_consume_token(oauth_client: TestClient):
    initial = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    denied = refresh(oauth_client, initial["refresh_token"], secret="wrong-secret-that-is-still-long-enough")
    assert denied.status_code == 401
    assert refresh(oauth_client, initial["refresh_token"]).status_code == 200


def test_refresh_expiry_unknown_and_scope_escalation_fail_closed(oauth_client: TestClient, oauth_state: OAuthState):
    unknown = refresh(oauth_client, "unknown-refresh-token")
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "invalid_grant"

    initial = exchange_code(
        oauth_client,
        authorize_code(oauth_client, scope="hermes offline_access"),
    ).json()
    escalated = refresh(oauth_client, initial["refresh_token"], scope="openid hermes offline_access")
    assert escalated.status_code == 400
    assert escalated.json()["error"] == "invalid_scope"

    oauth_state.refresh_tokens[initial["refresh_token"]]["expires_at"] = time.time() - 1
    expired = refresh(oauth_client, initial["refresh_token"])
    assert expired.status_code == 400
    assert expired.json()["error"] == "invalid_grant"


def test_authorization_code_replay_access_and_refresh_stores_are_bounded(oauth_state: OAuthState):
    assert oauth_state.max_auth_codes == MAX_AUTH_CODES
    assert oauth_state.max_access_tokens == MAX_ACCESS_TOKENS
    assert oauth_state.max_refresh_tokens == MAX_REFRESH_TOKENS

    code = oauth_state.issue_authorization_code(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope="hermes",
        resource=RESOURCE,
        code_challenge="",
    )
    oauth_state.used_auth_codes.update(
        {f"nonce-{index}": {"expires_at": time.time() + 60} for index in range(MAX_AUTH_CODES)}
    )
    with pytest.raises(OAuthError, match="capacity") as exc_info:
        oauth_state.exchange_authorization_code(
            code=code,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="",
        )
    assert exc_info.value.error == "temporarily_unavailable"


def test_public_authorization_requests_do_not_consume_replay_cache(oauth_state: OAuthState):
    latest = ""
    for _index in range(MAX_AUTH_CODES * 2):
        latest = oauth_state.issue_authorization_code(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            scope="hermes",
            resource=RESOURCE,
            code_challenge="",
        )
    assert oauth_state.used_auth_codes == {}
    assert oauth_state.exchange_authorization_code(
        code=latest,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_verifier="",
    )["access_token"]


def test_access_store_exact_capacity_fails_closed_without_consuming_code(oauth_state: OAuthState):
    code = oauth_state.issue_authorization_code(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope="hermes",
        resource=RESOURCE,
        code_challenge="",
    )
    oauth_state.access_tokens.update(
        {f"access-{index}": {"expires_at": time.time() + 60} for index in range(MAX_ACCESS_TOKENS)}
    )
    with pytest.raises(OAuthError, match="capacity") as exc_info:
        oauth_state.exchange_authorization_code(
            code=code,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="",
        )
    assert exc_info.value.error == "temporarily_unavailable"
    oauth_state.access_tokens.pop("access-0")
    assert oauth_state.exchange_authorization_code(
        code=code,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_verifier="",
    )["access_token"]


def test_refresh_store_exact_capacity_fails_closed_without_consuming_code(oauth_state: OAuthState):
    code = oauth_state.issue_authorization_code(
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        scope="hermes offline_access",
        resource=RESOURCE,
        code_challenge="",
    )
    oauth_state.refresh_tokens.update(
        {f"refresh-{index}": {"expires_at": time.time() + 60} for index in range(MAX_REFRESH_TOKENS)}
    )
    with pytest.raises(OAuthError, match="capacity") as exc_info:
        oauth_state.exchange_authorization_code(
            code=code,
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="",
        )
    assert exc_info.value.error == "temporarily_unavailable"
    oauth_state.refresh_tokens.pop("refresh-0")
    assert oauth_state.exchange_authorization_code(
        code=code,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_verifier="",
    )["refresh_token"]


def test_cleanup_removes_only_expired_credentials(oauth_client: TestClient, oauth_state: OAuthState):
    expired = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    active = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    oauth_state.access_tokens[expired["access_token"]]["expires_at"] = time.time() - 1
    oauth_state.refresh_tokens[expired["refresh_token"]]["expires_at"] = time.time() - 1
    oauth_state.cleanup()
    assert expired["access_token"] not in oauth_state.access_tokens
    assert expired["refresh_token"] not in oauth_state.refresh_tokens
    assert active["access_token"] in oauth_state.access_tokens
    assert active["refresh_token"] in oauth_state.refresh_tokens


def test_credentials_are_absent_from_metadata_and_errors(oauth_client: TestClient):
    issued = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    observed = str(oauth_client.get("/.well-known/oauth-authorization-server").json())
    observed += str(refresh(oauth_client, issued["refresh_token"], secret="wrong-secret-that-is-still-long-enough").json())
    assert CLIENT_SECRET not in observed
    assert issued["access_token"] not in observed
    assert issued["refresh_token"] not in observed


def test_bearer_middleware_preserves_static_token_compatibility(monkeypatch: pytest.MonkeyPatch, oauth_state: OAuthState):
    async def endpoint(_request):
        return JSONResponse({"ok": True})

    monkeypatch.setenv(AUTH_TOKEN_ENV, STATIC_BEARER)
    app = BearerAuthMiddleware(Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])]), oauth_state)
    client = TestClient(app)
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": f"Bearer {STATIC_BEARER}"}).status_code == 200


def test_refreshed_access_token_passes_bearer_middleware(oauth_client: TestClient, oauth_state: OAuthState):
    issued = exchange_code(oauth_client, authorize_code(oauth_client)).json()
    refreshed = refresh(oauth_client, issued["refresh_token"]).json()
    assert validate_bearer_token(refreshed["access_token"], oauth_state, static_token="")


def test_accept_middleware_normalizes_only_missing_or_wildcard_accept_header():
    async def endpoint(request):
        return JSONResponse({"accept": request.headers.get("accept")})

    app = DefaultMcpAcceptMiddleware(Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])]))
    client = TestClient(app)
    missing = client.post("/mcp", headers={"Accept": ""}).json()["accept"]
    wildcard = client.post("/mcp", headers={"Accept": "*/*"}).json()["accept"]
    explicit = client.post("/mcp", headers={"Accept": "application/json"}).json()["accept"]
    assert missing == "application/json, text/event-stream"
    assert wildcard == "application/json, text/event-stream"
    assert explicit == "application/json"
