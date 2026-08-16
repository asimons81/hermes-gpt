"""Browser-facing security boundary for the Hermes ChatGPT UI (v0.7).

This module is the ONLY place browser-bound payloads are serialized for the
conversational UI. It owns:

- ``redact_browser`` — server-side redaction + truncation for every UI JSON
  response and SSE payload (reuses ``operator_policy.redact_output`` and the
  AGENTS.md Mission Control invariants).
- The JSON envelope helpers (``ok`` / ``err``) that compose with redaction so
  a handler cannot accidentally serialize an unredacted payload.
- Authn/authz wiring guidance: the UI reuses ``BearerAuthMiddleware`` from
  ``oauth_auth``; loopback default (no auth); static bearer or OAuth when
  configured. Remote profile remains blocked by the existing server gates.
- Account / capability context (``GET /api/me``): profile, accountStatus
  ``ok|expired|revoked|unauthorized``, operatorLevel, allowedSurfaces,
  uiCapabilities.
- Connection / stale-state helpers: turn-lease staleness, server restart
  detection, expired/revoked recovery state.

Design rules (AGENTS.md invariants preserved):
- Browser receives only data it needs; no raw prompts, memory bodies,
  transcripts, request dumps, credentials, or profile-secret bodies.
- No ``secrets/`` paths or secret-file paths in refs/summary/data.
- Tool-result previews truncated to ``HERMES_GPT_UI_TOOL_PREVIEW_BYTES``.
- Redaction is ``[REDACTED]``-marked, never silently empty.
- Mission allowlist semantics unchanged: unset = all, list = only listed,
  empty = none. The unset state is NOT "deny by default".
- Mutations never weaken gates: every mutation the UI offers still goes
  through the existing gated ``hermes_*`` tool path (read-only default,
  dry-run-first, confirm preserved, audited). This module adds no mutation
  surface of its own.

Import-safe: no hard dependency on Hermes internals. ``operator_mission`` and
``token_store`` are imported lazily inside the functions that need them.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

import operator_policy as op
from versioning import VERSION

# ---------------------------------------------------------------------------
# Env var names (architecture.md §15)
# ---------------------------------------------------------------------------

UI_ENABLED_ENV = "HERMES_GPT_UI_ENABLED"
UI_PROFILE_ENV = "HERMES_GPT_UI_PROFILE"
UI_DIR_ENV = "HERMES_GPT_UI_DIR"
UI_STALE_LEASE_S_ENV = "HERMES_GPT_UI_STALE_LEASE_S"
UI_TOOL_PREVIEW_BYTES_ENV = "HERMES_GPT_UI_TOOL_PREVIEW_BYTES"

DEFAULT_STALE_LEASE_S = 600
DEFAULT_TOOL_PREVIEW_BYTES = 8192
# Content (user's own conversation text) is bounded but effectively
# unlimited — the 8 KiB cap applies to operator/tool previews, not chat text.
CONTENT_CAP_BYTES = 1_048_576

REDACTED_MARKER = "[REDACTED]"
TRUNCATED_MARKER = "…[truncated]"

# Valid account statuses (interface-contracts.md §3).
ACCOUNT_STATUS_OK = "ok"
ACCOUNT_STATUS_EXPIRED = "expired"
ACCOUNT_STATUS_REVOKED = "revoked"
ACCOUNT_STATUS_UNAUTHORIZED = "unauthorized"
ACCOUNT_STATUSES = (
    ACCOUNT_STATUS_OK,
    ACCOUNT_STATUS_EXPIRED,
    ACCOUNT_STATUS_REVOKED,
    ACCOUNT_STATUS_UNAUTHORIZED,
)

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def ui_enabled() -> bool:
    """Return True when the UI mount is enabled (HERMES_GPT_UI_ENABLED=1)."""
    return os.environ.get(UI_ENABLED_ENV) == "1"


def ui_profile() -> str:
    """Return the profile the UI runs as (HERMES_GPT_UI_PROFILE, default)."""
    return os.environ.get(UI_PROFILE_ENV, "").strip() or "default"


def ui_dir() -> Path:
    """Resolve the static build output directory (HERMES_GPT_UI_DIR)."""
    raw = os.environ.get(UI_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / "web" / "dist"


def stale_lease_seconds() -> int:
    """Return the stale turn-lease threshold in seconds (default 600)."""
    raw = os.environ.get(UI_STALE_LEASE_S_ENV, "").strip()
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_STALE_LEASE_S
    except (TypeError, ValueError):
        return DEFAULT_STALE_LEASE_S


def preview_bytes() -> int:
    """Return the per-string / tool-preview cap in bytes (default 8192)."""
    raw = os.environ.get(UI_TOOL_PREVIEW_BYTES_ENV, "").strip()
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_TOOL_PREVIEW_BYTES
    except (TypeError, ValueError):
        return DEFAULT_TOOL_PREVIEW_BYTES


def is_stale_lease(lease_ts: float | None, now: float | None = None) -> bool:
    """Return True when a turn lease timestamp is older than the threshold.

    ``lease_ts`` is seconds since epoch (the shape used by
    ``session_turn_leases``). A missing lease is never stale.
    """
    if lease_ts is None:
        return False
    now_ts = time.time() if now is None else now
    try:
        age = now_ts - float(lease_ts)
    except (TypeError, ValueError):
        return True
    return age > stale_lease_seconds()


def _default_hermes_root() -> Path:
    """Mirror server.py / operator_mission root resolution (never None)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Keys whose values are ALWAYS replaced with the marker, regardless of mode.
# Raw prompts, memory bodies, transcripts, request dumps, credentials, and
# profile-secret bodies never cross the browser boundary (AGENTS.md Mission
# Control invariants). ``content`` is exempted only when a caller explicitly
# opts in with ``content_allowed=True`` (the user's own chat thread).
_SECRET_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "token", "tokens", "access_token", "refresh_token", "id_token",
        "client_secret", "secret", "secrets", "password", "passwd",
        "api_key", "apikey", "authorization", "cookie", "credentials",
        "private_key", "bearer", "auth_token", "session_token",
        "master_key", "key_material", "plaintext", "raw_secret",
        "secret_body", "profile_secret", "memory_body", "transcript",
        "request_dump", "raw_prompt", "task_json", "result_json",
        "prompt",
    }
)

# Keys whose names imply secret material even when not in the exact set.
_SECRET_KEY_SUFFIXES = ("_key", "_token", "_secret", "_password")


def _is_secret_key(key: str) -> bool:
    lower = key.lower()
    if lower in _SECRET_VALUE_KEYS:
        return True
    return any(lower.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


# PII patterns for operator-derived text (mirrors operator_mission._sanitize_error).
# Replacement may be a string or a callable (re.Pattern.sub accepts both).
_PII_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[redacted-email]"),
    (re.compile(r"(?<!\w)(?:\+?\d[\d().\-\s]{6,}\d)(?!\w)"), "[redacted-phone]"),
    (re.compile(r"(?<![\w@])@[A-Za-z0-9_]{1,32}\b"), "[redacted-username]"),
    (
        re.compile(
            r"(?i)\b(name|contact|customer|client|user|owner|assignee)\s*[:=]\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b"
        ),
        lambda m: f"{m.group(1)}=[redacted-name]",
    ),
    (re.compile(r"\b(?:Mr|Mrs|Ms|Dr)\.\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"), "[redacted-name]"),
    (re.compile(r"\b[A-Z][a-z]{1,30}\s+[A-Z][a-z]{1,30}\b"), "[redacted-name]"),
]

# Absolute filesystem paths (POSIX with 2+ segments, Windows drive, UNC,
# home-relative ~/...). Negative lookbehind keeps URLs and single-segment
# names like "/mcp" intact.
_ABS_PATH_RE = re.compile(
    r"(?i)(?<![:\w/])("
    r"(?:/(?:[^/\s]{1,200}/)+[^/\s]{1,200})"
    r"|[a-z]:\\[^\s]{2,}"
    r"|\\\\[^\s]{2,}"
    r"|~[^\s]{2,}"
    r")"
)

# Secret file basenames and directory segments that must never appear in
# browser payloads (AGENTS.md: secret-file paths and secrets/ refs denied).
_SECRET_BASENAMES: frozenset[str] = frozenset(
    {
        ".env", ".env.local", ".env.development", ".env.production",
        "auth.json", "auth.lock", ".anthropic_oauth.json", "google_oauth.json",
        "webhook_subscriptions.json", "bws_cache.json", "mcp-tokens",
        "credentials", ".npmrc", ".pypirc", ".netrc", ".pgpass",
        ".git-credentials", "id_rsa", "id_ed25519", "authorized_keys",
        "hermes_gpt_tokens.json", "hermes_gpt_token_key",
    }
)
_SECRET_SEGMENTS: frozenset[str] = frozenset(
    {"secrets", "mcp-tokens", "pairing", ".ssh", ".aws", ".gnupg", ".kube", ".docker", "vault"}
)


def _looks_like_secret_path(token: str) -> bool:
    """Return True when a whitespace-delimited token references a secret path."""
    cleaned = token.strip().rstrip(".,;:)]}>\"'")
    if "/" not in cleaned and "\\" not in cleaned:
        return False
    parts = [p.lower().strip(".,;:)]}>\"'") for p in cleaned.replace("\\", "/").split("/")]
    leaf = parts[-1]
    if leaf in _SECRET_BASENAMES:
        return True
    if any(segment in _SECRET_SEGMENTS for segment in parts):
        return True
    return any(needle in leaf for needle in op.SECRET_PATH_SUBSTRINGS)


_SECRET_PATH_TOKEN_RE = re.compile(r"(?<![\w/])([^\s\"'()=,]+)(?=[\s\"'()=,]|$)")


def _redact_secret_path_tokens(text: str) -> str:
    return _SECRET_PATH_TOKEN_RE.sub(
        lambda m: "[REDACTED_SECRETS_PATH]" if _looks_like_secret_path(m.group(1)) else m.group(1),
        text,
    )


def _redact_string(text: Any, *, content: bool = False, cap: int) -> str:
    """Redact/truncate a single string for the browser.

    ``content=True`` (the user's own chat thread) skips PII/path mangling —
    only unambiguous secret shapes and a generous bound apply, so a user's own
    message is never corrupted. Operator-derived text (``content=False``) gets
    the full treatment: secrets, PII, absolute paths, and secrets/ refs.
    """
    if not isinstance(text, str):
        text = str(text)
    out = op.redact_output(text)
    if not content:
        for pattern, replacement in _PII_PATTERNS:
            out = pattern.sub(replacement, out)
        out = _ABS_PATH_RE.sub("[REDACTED_PATH]", out)
        out = _redact_secret_path_tokens(out)
    if len(out) > cap:
        out = out[:cap] + TRUNCATED_MARKER
    return out


def redact_browser(
    payload: Any,
    *,
    content_allowed: bool = False,
    preview_bytes_override: int | None = None,
) -> Any:
    """Return a redacted deep copy of a browser-bound payload.

    Recursively walks dicts/lists: secret-key values become ``[REDACTED]``,
    strings are redacted/truncated, everything else is preserved. The source
    payload is never mutated. Pass ``content_allowed=True`` only for payloads
    that are the user's own conversation (chat thread text, SSE token deltas);
    every other browser payload uses the strict default.
    """
    cap = preview_bytes_override or preview_bytes()
    content_cap = preview_bytes_override or CONTENT_CAP_BYTES
    # Keys that carry raw message text in chat payloads. In strict mode they
    # are message bodies and are redacted entirely; a caller that is
    # explicitly serializing the user's own conversation opts in via
    # ``content_allowed=True`` and only those values get content-mode
    # treatment — every other string still gets the full strict redaction.
    content_keys = ("content", "delta")

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                k = str(key).lower()
                if _is_secret_key(key) or (not content_allowed and k in content_keys):
                    # Never silently empty: a dropped value is always marked.
                    out[key] = REDACTED_MARKER
                elif content_allowed and isinstance(value, str) and k in content_keys:
                    out[key] = _redact_string(value, content=True, cap=content_cap)
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            return _redact_string(node, content=False, cap=cap)
        return node

    return walk(payload)


def redact_log_line(text: str) -> str:
    """Redact a line before it is written to any UI-related server log."""
    return _redact_string(text, content=False, cap=4096)


# ---------------------------------------------------------------------------
# JSON envelope (composition-owned by ui_api.py; implemented next to the
# boundary so redaction cannot be skipped by a handler)
# ---------------------------------------------------------------------------


def ok(data: Any, *, content_allowed: bool = False) -> dict[str, Any]:
    """Return the success envelope with data passed through redaction."""
    return {"ok": True, "data": redact_browser(data, content_allowed=content_allowed)}


def error_envelope(code: str, message: str, *, trace_id: str | None = None) -> dict[str, Any]:
    """Return the error envelope dict with the message redacted."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": redact_browser(message),
            **({"trace_id": trace_id} if trace_id else {}),
        },
    }


def err(code: str, message: str, *, status_code: int = 400, trace_id: str | None = None) -> JSONResponse:
    """Return an error JSONResponse (envelope shape per interface-contracts.md §7)."""
    return JSONResponse(error_envelope(code, message, trace_id=trace_id), status_code=status_code)


# ---------------------------------------------------------------------------
# Account / capability context
# ---------------------------------------------------------------------------


def auth_configured() -> bool:
    """Return True when the server requires auth (static bearer or OAuth).

    Loopback default means no auth: the UI is fully usable with
    ``accountStatus=ok``.
    """
    import oauth_auth

    if os.environ.get(oauth_auth.OAUTH_ENABLE_ENV) == "1":
        return True
    return bool(os.environ.get(oauth_auth.AUTH_TOKEN_ENV, "").strip())


def _oauth_configured() -> bool:
    import oauth_auth

    return os.environ.get(oauth_auth.OAUTH_ENABLE_ENV) == "1"


def account_status(hermes_root: Path | None = None) -> str:
    """Derive the four-state account status for the browser.

    - ``ok``: no auth configured (loopback default), or static bearer, or a
      valid durable token store.
    - ``expired``: durable token store present but expired.
    - ``revoked``: durable token store unreadable/corrupt (tokens unusable —
      treated as revoked; a hard revoke deletes the envelope, which surfaces
      as ``unauthorized`` on OAuth deployments).
    - ``unauthorized``: OAuth configured but no usable durable token store —
      the client must re-authenticate.

    Never exposes token material — only presence/expiry/revocation state.
    """
    if not auth_configured():
        return ACCOUNT_STATUS_OK
    root = hermes_root or _default_hermes_root()
    try:
        import token_store

        status = token_store.status(root)
    except Exception:  # noqa: BLE001 — degraded status must not crash /api/me
        status = {"presence": "error"}
    presence = status.get("presence")
    if presence in ("absent", "error"):
        if _oauth_configured():
            return ACCOUNT_STATUS_UNAUTHORIZED
        return ACCOUNT_STATUS_OK
    if presence == "corrupt":
        return ACCOUNT_STATUS_REVOKED
    if status.get("revoked_at"):
        return ACCOUNT_STATUS_REVOKED
    expires_at = status.get("expires_at")
    now = time.time()
    if isinstance(expires_at, (int, float)):
        if expires_at and float(expires_at) < now:
            return ACCOUNT_STATUS_EXPIRED
    elif isinstance(expires_at, str) and expires_at:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if parsed.timestamp() < now:
                return ACCOUNT_STATUS_EXPIRED
        except ValueError:
            pass
    return ACCOUNT_STATUS_OK


def operator_level() -> str:
    """Return the effective operator level (policy snapshot)."""
    try:
        return op.OperatorPolicy().level or "read_only"
    except Exception:  # noqa: BLE001
        return "read_only"


def allowed_surfaces() -> set[str]:
    """Return the Mission Control allowlist result (v0.7 semantics).

    Unset env = all surfaces; comma list = only listed; empty string = none.
    The unset state is NOT "deny by default" (AGENTS.md).
    """
    import operator_mission as op_mission

    return set(op_mission._allowed_surfaces())


def ui_capabilities(level: str, surfaces: set[str], *, account_ok: bool) -> list[str]:
    """Permission-aware capability list for the browser.

    Read-only lanes (chat/flight/events/fleet) stay available when an account
    degrades; the mutating approvals lane drops out unless the account is ok
    AND the level satisfies the workspace gate.
    """
    caps = ["chat", "flight", "events"]
    if "fleet" in surfaces:
        caps.append("fleet")
    if account_ok and "approvals" in surfaces and op.has_level("workspace", level):
        caps.append("approvals")
    return caps


def model_for_profile(profile: str, hermes_root: Path | None) -> str:
    """Best-effort read of the profile's configured model. Never raises."""
    try:
        home = op.resolve_profile_home(profile, hermes_root)
        config = home / "config.yaml"
        if config.is_file():
            for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("model:"):
                    value = stripped[len("model:"):].strip().strip("'\"")
                    if value:
                        return value
    except Exception:  # noqa: BLE001
        pass
    return ""


def me_payload(hermes_root: Path | None = None) -> dict[str, Any]:
    """Build the /api/me payload (interface-contracts.md §3)."""
    root = hermes_root or _default_hermes_root()
    profile = ui_profile()
    level = operator_level()
    surfaces = allowed_surfaces()
    status = account_status(root)
    return {
        "profile": profile,
        "accountStatus": status,
        "operatorLevel": level,
        "allowedSurfaces": sorted(surfaces),
        "uiCapabilities": ui_capabilities(level, surfaces, account_ok=(status == ACCOUNT_STATUS_OK)),
        "model": model_for_profile(profile, root),
        "serverVersion": VERSION,
    }


# ---------------------------------------------------------------------------
# Connection / stale-state helpers
# ---------------------------------------------------------------------------

_STARTUP_ID: str | None = None


def server_startup_id() -> str:
    """Return a stable per-process startup id (restart detection)."""
    global _STARTUP_ID
    if _STARTUP_ID is None:
        _STARTUP_ID = f"{os.getpid()}-{op.new_trace_id()}"
    return _STARTUP_ID


def connection_payload(hermes_root: Path | None = None) -> dict[str, Any]:
    """Build the /api/connection payload for transport-health state."""
    return {
        "serverStartupId": server_startup_id(),
        "serverTime": int(time.time()),
        "uiEnabled": ui_enabled(),
        "staleLeaseSeconds": stale_lease_seconds(),
        "toolPreviewBytes": preview_bytes(),
        "accountStatus": account_status(hermes_root),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def me_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse(ok(me_payload()))


async def connection_endpoint(_request: Request) -> JSONResponse:
    return JSONResponse(ok(connection_payload()))


def ui_security_routes() -> list[BaseRoute]:
    """Routes owned by the security/state card (composition entry point)."""
    return [
        Route("/api/me", me_endpoint, methods=["GET"]),
        Route("/api/connection", connection_endpoint, methods=["GET"]),
    ]
