from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

AUTH_CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_AUTH_CODES = 1024
MAX_ACCESS_TOKENS = 4096
MAX_REFRESH_TOKENS = 4096
MAX_TOKEN_REQUEST_BYTES = 16384
AUTH_TOKEN_ENV = "HERMES_GPT_BEARER_TOKEN"
OAUTH_ENABLE_ENV = "HERMES_GPT_OAUTH_ENABLE"
OAUTH_ISSUER_ENV = "HERMES_GPT_OAUTH_ISSUER"
OAUTH_CLIENT_ID_ENV = "HERMES_GPT_OAUTH_CLIENT_ID"
OAUTH_CLIENT_SECRET_ENV = "HERMES_GPT_OAUTH_CLIENT_SECRET"
OAUTH_REDIRECT_URI_ENV = "HERMES_GPT_OAUTH_REDIRECT_URI"
OAUTH_SCOPE_ENV = "HERMES_GPT_OAUTH_SCOPE"
_PKCE_VALUE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CLIENT_SECRET = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


class OAuthError(RuntimeError):
    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    scope: str = "hermes"

    def __post_init__(self) -> None:
        issuer = self.issuer.rstrip("/")
        parsed = urllib.parse.urlparse(issuer)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("OAuth issuer must use HTTPS except on loopback.")
        if (
            not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("OAuth issuer must be an origin URL without path, userinfo, query, or fragment.")
        if not self.client_id.strip():
            raise ValueError("OAuth client_id is required.")
        if not _CLIENT_SECRET.fullmatch(self.client_secret):
            raise ValueError("OAuth client_secret must contain 43 to 128 URL-safe characters.")
        if not self.redirect_uris:
            raise ValueError("At least one OAuth redirect URI is required.")
        for redirect_uri in self.redirect_uris:
            redirect = urllib.parse.urlparse(redirect_uri)
            if (
                redirect.scheme != "https"
                or not redirect.netloc
                or not redirect.hostname
                or redirect.fragment
                or redirect.username is not None
                or redirect.password is not None
            ):
                raise ValueError("OAuth redirect URIs must be absolute HTTPS URLs without userinfo or fragments.")
        if not self.scope.strip() or len(self.scope.split()) != 1:
            raise ValueError("OAuth scope must be one non-empty scope token.")
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "client_id", self.client_id.strip())
        object.__setattr__(self, "redirect_uris", tuple(dict.fromkeys(self.redirect_uris)))
        object.__setattr__(self, "scope", self.scope.strip())

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    @property
    def supported_scopes(self) -> tuple[str, ...]:
        # ChatGPT currently adds `openid` even when its OIDC toggle is disabled.
        # It is accepted as a compatibility scope; this server does not advertise
        # OpenID Provider metadata or issue ID tokens.
        return tuple(dict.fromkeys((self.scope, "openid", "offline_access")))


class OAuthState:
    def __init__(
        self,
        config: OAuthConfig,
        *,
        max_auth_codes: int = MAX_AUTH_CODES,
        max_access_tokens: int = MAX_ACCESS_TOKENS,
        max_refresh_tokens: int = MAX_REFRESH_TOKENS,
    ) -> None:
        self.config = config
        self.max_auth_codes = max_auth_codes
        self.max_access_tokens = max_access_tokens
        self.max_refresh_tokens = max_refresh_tokens
        self._authorization_code_key = secrets.token_bytes(32)
        self.used_auth_codes: dict[str, dict[str, Any]] = {}
        self.access_tokens: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}

    def cleanup(self) -> None:
        now = time.time()
        for store in (self.used_auth_codes, self.access_tokens, self.refresh_tokens):
            for credential, item in list(store.items()):
                if item.get("expires_at", 0) <= now:
                    store.pop(credential, None)

    def normalize_scope(self, scope: str) -> str:
        requested = list(dict.fromkeys(scope.split()))
        if (
            not requested
            or self.config.scope not in requested
            or not set(requested).issubset(self.config.supported_scopes)
        ):
            raise OAuthError("invalid_scope", "Requested scope is not supported.")
        return " ".join(requested)

    def _require_capacity(self, store: dict[str, Any], maximum: int, credential_type: str) -> None:
        self.cleanup()
        if len(store) >= maximum:
            raise OAuthError(
                "temporarily_unavailable",
                f"{credential_type} capacity is temporarily unavailable.",
                status_code=503,
            )

    def issue_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scope: str,
        resource: str,
        code_challenge: str,
    ) -> str:
        payload = {
            "v": 1,
            "nonce": secrets.token_urlsafe(24),
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "resource": resource,
            "code_challenge": code_challenge,
            "expires_at": int(time.time()) + AUTH_CODE_TTL_SECONDS,
        }
        encoded = _base64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = hmac.new(self._authorization_code_key, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{_base64url_encode(signature)}"

    def _decode_authorization_code(self, code: str) -> dict[str, Any]:
        if len(code) > 4096:
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
        encoded, separator, encoded_signature = code.partition(".")
        if not separator:
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
        try:
            supplied_signature = _base64url_decode(encoded_signature)
            expected_signature = hmac.new(
                self._authorization_code_key,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(_base64url_decode(encoded))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.") from exc
        required_types = {
            "v": int,
            "nonce": str,
            "client_id": str,
            "redirect_uri": str,
            "scope": str,
            "resource": str,
            "code_challenge": str,
            "expires_at": int,
        }
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), kind) for key, kind in required_types.items()):
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
        if payload["v"] != 1 or not _NONCE.fullmatch(payload["nonce"]):
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
        return payload

    def _new_access_token(self, *, client_id: str, scope: str, resource: str) -> tuple[str, dict[str, Any]]:
        token_value = secrets.token_urlsafe(48)
        item = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": time.time() + ACCESS_TOKEN_TTL_SECONDS,
        }
        return token_value, item

    def _new_refresh_token(self, *, client_id: str, scope: str) -> tuple[str, dict[str, Any]]:
        token_value = secrets.token_urlsafe(48)
        item = {
            "client_id": client_id,
            "scope": scope,
            "expires_at": time.time() + REFRESH_TOKEN_TTL_SECONDS,
        }
        return token_value, item

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        self.cleanup()
        item = self._decode_authorization_code(code)
        nonce = item["nonce"]
        if nonce in self.used_auth_codes or item.get("expires_at", 0) <= time.time():
            raise OAuthError("invalid_grant", "Invalid, expired, or already used authorization code.")
        if item.get("client_id") != client_id or item.get("redirect_uri") != redirect_uri:
            raise OAuthError("invalid_grant", "Authorization code validation failed.")
        challenge = item.get("code_challenge", "")
        if challenge:
            if not _valid_pkce_verifier(code_verifier) or not hmac.compare_digest(_s256(code_verifier), challenge):
                raise OAuthError("invalid_grant", "Authorization code validation failed.")

        scope = self.normalize_scope(item["scope"])
        self._require_capacity(self.used_auth_codes, self.max_auth_codes, "Authorization-code replay cache")
        self._require_capacity(self.access_tokens, self.max_access_tokens, "Access-token")
        if "offline_access" in scope.split():
            self._require_capacity(self.refresh_tokens, self.max_refresh_tokens, "Refresh-token")

        access_value, access_item = self._new_access_token(
            client_id=client_id,
            scope=scope,
            resource=item["resource"],
        )
        response: dict[str, Any] = {
            "access_token": access_value,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "scope": scope,
        }
        refresh_value = ""
        refresh_item: dict[str, Any] | None = None
        if "offline_access" in scope.split():
            refresh_value, refresh_item = self._new_refresh_token(client_id=client_id, scope=scope)
            response["refresh_token"] = refresh_value

        self.used_auth_codes[nonce] = {"expires_at": item["expires_at"]}
        self.access_tokens[access_value] = access_item
        if refresh_item is not None:
            self.refresh_tokens[refresh_value] = refresh_item
        return response

    def exchange_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        requested_scope: str,
    ) -> dict[str, Any]:
        self.cleanup()
        item = self.refresh_tokens.get(refresh_token)
        if not item or item.get("expires_at", 0) <= time.time():
            self.refresh_tokens.pop(refresh_token, None)
            raise OAuthError("invalid_grant", "Invalid, expired, or already used refresh token.")
        if item.get("client_id") != client_id:
            raise OAuthError("invalid_grant", "Refresh token validation failed.")
        original_scope = self.normalize_scope(item["scope"])
        scope = self.normalize_scope(requested_scope) if requested_scope.strip() else original_scope
        if not set(scope.split()).issubset(original_scope.split()):
            raise OAuthError("invalid_scope", "Requested scope exceeds the originally granted scope.")

        self._require_capacity(self.access_tokens, self.max_access_tokens, "Access-token")
        access_value, access_item = self._new_access_token(
            client_id=client_id,
            scope=scope,
            resource=self.config.resource,
        )
        rotated_value, rotated_item = self._new_refresh_token(client_id=client_id, scope=scope)
        self.refresh_tokens.pop(refresh_token, None)
        self.refresh_tokens[rotated_value] = rotated_item
        self.access_tokens[access_value] = access_item
        return {
            "access_token": access_value,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": rotated_value,
            "scope": scope,
        }

    def validate_access_token(self, token_value: str) -> bool:
        if not token_value:
            return False
        self.cleanup()
        item = self.access_tokens.get(token_value)
        return bool(
            item
            and item.get("expires_at", 0) > time.time()
            and item.get("resource") == self.config.resource
        )


def config_from_env() -> OAuthConfig | None:
    if os.environ.get(OAUTH_ENABLE_ENV) != "1":
        return None
    required = {
        OAUTH_ISSUER_ENV: os.environ.get(OAUTH_ISSUER_ENV, "").strip(),
        OAUTH_CLIENT_ID_ENV: os.environ.get(OAUTH_CLIENT_ID_ENV, "").strip(),
        OAUTH_CLIENT_SECRET_ENV: os.environ.get(OAUTH_CLIENT_SECRET_ENV, ""),
        OAUTH_REDIRECT_URI_ENV: os.environ.get(OAUTH_REDIRECT_URI_ENV, "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"OAuth is enabled but required configuration is missing: {', '.join(missing)}")
    redirects = tuple(
        item.strip()
        for item in required[OAUTH_REDIRECT_URI_ENV].replace("\n", ",").split(",")
        if item.strip()
    )
    return OAuthConfig(
        issuer=required[OAUTH_ISSUER_ENV],
        client_id=required[OAUTH_CLIENT_ID_ENV],
        client_secret=required[OAUTH_CLIENT_SECRET_ENV],
        redirect_uris=redirects,
        scope=os.environ.get(OAUTH_SCOPE_ENV, "hermes").strip() or "hermes",
    )


def static_bearer_from_env() -> str | None:
    token_value = os.environ.get(AUTH_TOKEN_ENV, "")
    if not token_value:
        return None
    if not _CLIENT_SECRET.fullmatch(token_value):
        raise ValueError(f"{AUTH_TOKEN_ENV} must contain 43 to 128 URL-safe characters.")
    return token_value


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _valid_pkce_verifier(verifier: str) -> bool:
    return bool(_PKCE_VALUE.fullmatch(verifier))


def _error_response(exc: OAuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.error == "invalid_client":
        headers["WWW-Authenticate"] = "Basic realm=oauth-token"
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=exc.status_code,
        headers=headers,
    )


def validate_bearer_token(token_value: str, state: OAuthState | None, *, static_token: str | None = None) -> bool:
    expected = (static_bearer_from_env() or "") if static_token is None else static_token
    if expected and token_value and hmac.compare_digest(token_value, expected):
        return True
    return bool(state and state.validate_access_token(token_value))


class DefaultMcpAcceptMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            headers = list(scope.get("headers") or [])
            accept_indexes = [index for index, (key, _value) in enumerate(headers) if key.lower() == b"accept"]
            replacement = b"application/json, text/event-stream"
            if not accept_indexes:
                headers.append((b"accept", replacement))
                scope = {**scope, "headers": headers}
            else:
                index = accept_indexes[-1]
                value = headers[index][1].decode("latin-1").strip()
                if not value or value == "*/*":
                    headers[index] = (b"accept", replacement)
                    scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class BearerAuthMiddleware:
    PUBLIC_PATHS = {
        "/",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
        "/oauth/authorize",
        "/oauth/token",
    }

    def __init__(self, app: ASGIApp, state: OAuthState | None = None, *, static_token: str | None = None) -> None:
        self.app = app
        self.state = state
        self.static_token = static_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        expected_static = (static_bearer_from_env() or "") if self.static_token is None else self.static_token
        if not expected_static and self.state is None:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS" or scope.get("path") in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not validate_bearer_token(supplied, self.state, static_token=expected_static):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def authorization_metadata(_request: Request, state: OAuthState) -> JSONResponse:
    issuer = state.config.issuer
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": list(state.config.supported_scopes),
        }
    )


def protected_resource_metadata(_request: Request, state: OAuthState) -> JSONResponse:
    return JSONResponse(
        {
            "resource": state.config.resource,
            "authorization_servers": [state.config.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(state.config.supported_scopes),
        }
    )


def _redirect_response(redirect_uri: str, values: list[tuple[str, str]]) -> RedirectResponse:
    parsed = urllib.parse.urlparse(redirect_uri)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    location = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(existing + values)))
    return RedirectResponse(location, status_code=302)


def authorize(request: Request, state: OAuthState) -> JSONResponse | RedirectResponse:
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    if client_id != state.config.client_id:
        return _error_response(OAuthError("invalid_client", "Unknown OAuth client.", status_code=401))
    if redirect_uri not in state.config.redirect_uris:
        return _error_response(OAuthError("invalid_request", "redirect_uri is not registered."))
    try:
        if params.get("response_type", "") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
        scope = state.normalize_scope(params.get("scope", "") or state.config.scope)
        resource = params.get("resource", "") or state.config.resource
        if resource != state.config.resource:
            raise OAuthError("invalid_target", "Requested resource is not supported.")
        challenge = params.get("code_challenge", "")
        method = params.get("code_challenge_method", "")
        if challenge and (method != "S256" or not _PKCE_VALUE.fullmatch(challenge)):
            raise OAuthError("invalid_request", "Only a valid S256 PKCE challenge is supported.")
        if method and not challenge:
            raise OAuthError("invalid_request", "code_challenge is required when a method is supplied.")
        code = state.issue_authorization_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            resource=resource,
            code_challenge=challenge,
        )
    except OAuthError as exc:
        error_query = [("error", exc.error), ("error_description", exc.description)]
        if params.get("state"):
            error_query.append(("state", params["state"]))
        return _redirect_response(redirect_uri, error_query)
    query = [("code", code)]
    if params.get("state"):
        query.append(("state", params["state"]))
    return _redirect_response(redirect_uri, query)


def _form_value(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name) or []
    return values[0] if values else ""


def _client_credentials(request: Request, form: dict[str, list[str]]) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1], validate=True).decode("utf-8")
        except Exception as exc:
            raise OAuthError("invalid_client", "Invalid OAuth client credentials.", status_code=401) from exc
        client_id, separator, client_secret = decoded.partition(":")
        if not separator:
            raise OAuthError("invalid_client", "Invalid OAuth client credentials.", status_code=401)
        return urllib.parse.unquote(client_id), urllib.parse.unquote(client_secret)
    return _form_value(form, "client_id"), _form_value(form, "client_secret")


def _authenticate_client(request: Request, form: dict[str, list[str]], state: OAuthState) -> str:
    client_id, client_secret = _client_credentials(request, form)
    if not hmac.compare_digest(client_id, state.config.client_id):
        raise OAuthError("invalid_client", "Invalid OAuth client credentials.", status_code=401)
    if not hmac.compare_digest(client_secret, state.config.client_secret):
        raise OAuthError("invalid_client", "Invalid OAuth client credentials.", status_code=401)
    return client_id


async def token(request: Request, state: OAuthState) -> JSONResponse:
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request", "Token requests must use form encoding.")
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > MAX_TOKEN_REQUEST_BYTES:
                    raise OAuthError("invalid_request", "Token request is too large.")
            except ValueError as exc:
                raise OAuthError("invalid_request", "Invalid Content-Length header.") from exc
        buffered = bytearray()
        async for chunk in request.stream():
            if len(buffered) + len(chunk) > MAX_TOKEN_REQUEST_BYTES:
                raise OAuthError("invalid_request", "Token request is too large.")
            buffered.extend(chunk)
        body = bytes(buffered).decode("utf-8")
        try:
            form = urllib.parse.parse_qs(body, keep_blank_values=True, max_num_fields=32)
        except ValueError as exc:
            raise OAuthError("invalid_request", "Token request form is invalid.") from exc
        grant_type = _form_value(form, "grant_type")
        if not grant_type:
            raise OAuthError("invalid_request", "grant_type is required.")
        if grant_type not in {"authorization_code", "refresh_token"}:
            raise OAuthError("unsupported_grant_type", "The requested grant type is not supported.")
        client_id = _authenticate_client(request, form, state)
        if grant_type == "authorization_code":
            response = state.exchange_authorization_code(
                code=_form_value(form, "code"),
                client_id=client_id,
                redirect_uri=_form_value(form, "redirect_uri"),
                code_verifier=_form_value(form, "code_verifier"),
            )
        else:
            refresh_token = _form_value(form, "refresh_token")
            if not refresh_token:
                raise OAuthError("invalid_request", "refresh_token is required.")
            response = state.exchange_refresh_token(
                refresh_token=refresh_token,
                client_id=client_id,
                requested_scope=_form_value(form, "scope"),
            )
        return JSONResponse(response, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except UnicodeDecodeError:
        return _error_response(OAuthError("invalid_request", "Token request is malformed."))
    except OAuthError as exc:
        return _error_response(exc)
