"""Operator policy, audit log, path safety, and profile helpers for hermes-gpt.

This module is the foundational layer for the tiered Operator / Owner control
plane. It is import-safe: Hermes internals are loaded lazily and failures
degrade to conservative defaults rather than raising.

Design rules enforced here:
- Default behavior is read-only.
- Mutating tools are disabled by default.
- Dry-run is the default apply mode.
- Direct mutation requires explicit env opt-in.
- Owner Mode requires an additional explicit acknowledgement.
- No secrets are exposed.
- No `.env` raw read/write.
- No vault/token/auth/cookie/SSH access.
- No `shell=True` anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Env var names
# ---------------------------------------------------------------------------

OPERATOR_ENABLED_ENV = "HERMES_GPT_OPERATOR_ENABLED"
OPERATOR_LEVEL_ENV = "HERMES_GPT_OPERATOR_LEVEL"
OPERATOR_APPLY_MODE_ENV = "HERMES_GPT_OPERATOR_APPLY_MODE"
OPERATOR_ALLOWED_PROFILES_ENV = "HERMES_GPT_OPERATOR_ALLOWED_PROFILES"
OPERATOR_ALLOWED_PATHS_ENV = "HERMES_GPT_OPERATOR_ALLOWED_PATHS"
OPERATOR_DENIED_PATHS_ENV = "HERMES_GPT_OPERATOR_DENIED_PATHS"
OWNER_ACK_ENV = "HERMES_GPT_OWNER_ACK"

OWNER_ACK_REQUIRED_VALUE = "I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"

# Default audit log locations (tried in order; first writable wins).
AUDIT_LOG_HERMES_PATH = Path.home() / "AppData" / "Local" / "hermes" / "logs" / "hermes_gpt_operator_audit.jsonl"
AUDIT_LOG_FALLBACK_PATH = Path(__file__).resolve().parent / "logs" / "hermes_gpt_operator_audit.jsonl"

# Override hook for tests: when set, the audit log is written/read from this
# path instead of the production locations. Set via ``set_audit_log_override``.
_audit_log_override: Optional[Path] = None
_audit_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Truthy helper
# ---------------------------------------------------------------------------

_TRUTHY_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSEY_VALUES = {"0", "false", "no", "off", "disabled", "", "0", "false"}


def is_truthy(value: Any) -> bool:
    """Return True if ``value`` is a recognized truthy string.

    Truthy: "1", "true", "yes", "on", "enabled" (case-insensitive).
    Falsey: "0", "false", "no", "off", "disabled", empty/unset, anything else.
    Non-string inputs are coerced via str().
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in _TRUTHY_VALUES


def env_truthy(name: str) -> bool:
    """Read env var ``name`` and apply ``is_truthy``."""
    return is_truthy(os.environ.get(name))


# ---------------------------------------------------------------------------
# Structured error envelope
# ---------------------------------------------------------------------------

# Allowed layer values. New layers should be added here so callers stay
# consistent and tooling can rely on a bounded vocabulary.
_ERROR_LAYERS: frozenset[str] = frozenset(
    {
        "operator",
        "policy",
        "config",
        "env",
        "cron",
        "skills",
        "gateway",
        "workspace",
        "owner",
        "audit",
        "connector",
        "release",
        "system",
    }
)

def new_trace_id() -> str:
    """Return a short random trace id for correlating operator failures."""
    import uuid

    return uuid.uuid4().hex[:16]


def _sanitize_exception_message(exc: Exception) -> str:
    """Return a safe exception message.

    Applies best-effort redaction of secret-looking values (API keys, tokens,
    bearer headers, AWS keys) and absolute filesystem paths while preserving
    human-readable error text such as "denied", "blocked", or "not found".
    The result is capped to avoid accidental dumps.
    """
    raw = str(exc)
    # Redact known secret value patterns.
    redacted = redact_output(raw)
    # Redact absolute filesystem paths.
    redacted = re.sub(
        r"(?i)([a-z]:\\[^\s]*|\\\\[^\s]*|/[^\s]*)",
        "[REDACTED_PATH]",
        redacted,
    )
    # Cap length to avoid accidental dumps.
    return redacted[:500]


def make_error_envelope(
    *,
    layer: str,
    code: str,
    safe_message: str,
    suggested_action: str,
    trace_id: str | None = None,
    legacy_error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe structured error envelope for operator-facing failures.

    Backward compatibility: the envelope still includes ``success: false`` and
    ``error`` so existing callers continue to work.
    """
    if layer not in _ERROR_LAYERS:
        layer = "operator"
    if not code:
        code = "UNKNOWN_ERROR"
    if not safe_message:
        safe_message = "An operator error occurred."
    if not suggested_action:
        suggested_action = "Run hermes_operator_doctor for more details."
    tid = trace_id or new_trace_id()
    error = legacy_error or safe_message
    envelope: dict[str, Any] = {
        "success": False,
        "ok": False,
        "error": error,
        "layer": layer,
        "code": code,
        "safe_message": safe_message,
        "suggested_action": suggested_action,
        "trace_id": tid,
    }
    if extra:
        for key, value in extra.items():
            if isinstance(value, str):
                envelope[key] = value[:500]
            else:
                envelope[key] = value
    return envelope


def error_from_exception(
    exc: Exception,
    *,
    layer: str,
    code: str,
    suggested_action: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an error envelope from an exception, sanitizing the message."""
    safe_message = _sanitize_exception_message(exc)
    return make_error_envelope(
        layer=layer,
        code=code,
        safe_message=safe_message,
        suggested_action=suggested_action,
        trace_id=trace_id,
        legacy_error=safe_message,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Operator levels
# ---------------------------------------------------------------------------

# Ordered from least to most privilege. Higher levels include all lower
# capabilities.
LEVELS = ["read_only", "cron", "skills", "skills_config", "workspace", "owner"]


def level_rank(level: str) -> int:
    """Return the integer rank of a level name. Unknown levels map to -1."""
    try:
        return LEVELS.index(level)
    except ValueError:
        return -1


def has_level(required: str, actual: str) -> bool:
    """Return True if ``actual`` level satisfies ``required`` (>= rank)."""
    return level_rank(actual) >= level_rank(required)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# Default denied path fragments. These are matched as path segments / suffixes
# conservatively. The check is intentionally broad: false positives (refusing
# to write a benign file that happens to look secret-like) are acceptable;
# false negatives (writing to a real secret store) are not.
DEFAULT_DENIED_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".env.staging",
        ".envrc",
        "auth.json",
        "auth.lock",
        ".anthropic_oauth.json",
        "google_oauth.json",
        "webhook_subscriptions.json",
        "bws_cache.json",
        "mcp-tokens",
        "credentials",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".git-credentials",
    }
)

DEFAULT_DENIED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".azure",
        "vault",
        "mcp-tokens",
        "pairing",
        ".config",
    }
)

# Substrings that, when present in a path, mark it as secret-like.
SECRET_PATH_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "credential",
    "oauth",
    "cookie",
    "private",
    "password",
    "passwd",
    ".key",
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
)


def _normalize_path(path: str | os.PathLike[str]) -> Path:
    """Expand ~ and resolve. Never raises; falls back to expanded path."""
    try:
        return Path(os.path.expanduser(str(path))).resolve()
    except Exception:
        try:
            return Path(os.path.expanduser(str(path)))
        except Exception:
            return Path(str(path))


def normalize_hermes_data_root(path: str | os.PathLike[str] | None) -> Path | None:
    """Normalize a Hermes install path to the data root.

    ``.../profiles/<profile>`` -> ``...``
    ``.../hermes-agent`` -> ``...``
    Already-normalized data roots remain unchanged.
    """
    if path is None:
        return None
    raw = Path(os.path.expanduser(str(path)))
    try:
        parts = [part.lower() for part in raw.parts]
    except Exception:
        return raw
    if not parts:
        return raw
    if parts[-1] == "hermes-agent":
        return raw.parent
    if len(parts) >= 2 and parts[-2] == "profiles":
        return raw.parent.parent
    return raw


def is_denied_path(path: str | os.PathLike[str]) -> bool:
    """Return True if ``path`` is a secret / credential / vault / token path.

    Conservative: returns True for any path whose basename matches a known
    secret file, whose parent directory is a known secret directory, whose
    name contains a secret-like substring, or that resolves into a known
    Hermes internal credential area (mcp-tokens, pairing, auth.json under a
    Hermes home).

    Defense-in-depth, not a security boundary (the terminal tool can still
    bypass). But operator tools rely on this as a hard refusal gate.
    """
    if path is None:
        return True

    resolved = _normalize_path(path)
    name = resolved.name.lower()

    # Exact-basename deny.
    if name in DEFAULT_DENIED_BASENAMES:
        return True

    # .env.* glob-style match.
    if name.startswith(".env."):
        return True

    # Any parent directory in the denied dir set.
    try:
        for parent in resolved.parents:
            if parent.name.lower() in DEFAULT_DENIED_DIR_NAMES:
                return True
    except Exception:
        pass

    # Secret-like substring in the final path component.
    lower_name = name
    for needle in SECRET_PATH_SUBSTRINGS:
        if needle in lower_name:
            return True

    # Hermes-internal credential stores: detect by path shape (works even
    # when HERMES_HOME is overridden for tests, because we look at the
    # segment names, not the absolute prefix).
    parts = [p.lower() for p in resolved.parts]
    for segment in ("mcp-tokens", "pairing"):
        if segment in parts:
            return True
    # auth.json / .anthropic_oauth.json / google_oauth.json under any
    # hermes home or profile dir.
    if name in {"auth.json", "auth.lock", ".anthropic_oauth.json", "google_oauth.json", "webhook_subscriptions.json", "bws_cache.json"}:
        return True
    # cache/bws_cache.json shape.
    if name == "bws_cache.json" and "cache" in parts:
        return True

    return False


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

# Profile names must match Hermes' profile id regex.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Reserved names that would create confusing on-disk collisions or conflict
# with Hermes itself. Mirrors Hermes' _RESERVED_NAMES, with the special alias
# ``default`` handled separately (it is the built-in profile).
_RESERVED_PROFILE_NAMES: frozenset[str] = frozenset(
    {"hermes", "test", "tmp", "root", "sudo"}
)


def validate_profile_name(name: str) -> str:
    """Return the canonical profile id, raising ValueError if invalid.

    Mirrors Hermes' normalize_profile_name + validate_profile_name. The
    special alias ``default`` is allowed and normalized to itself.
    """
    if not isinstance(name, str):
        raise ValueError("profile name must be a string")
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    if stripped.casefold() == "default":
        return "default"
    canon = stripped.lower()
    if not _PROFILE_NAME_RE.match(canon):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if canon in _RESERVED_PROFILE_NAMES:
        raise ValueError(
            f"Profile name {name!r} is reserved — it collides with either "
            f"the Hermes installation itself or a common system binary. "
            f"Pick a different name."
        )
    return canon


def parse_allowed_profiles(raw: str | None) -> list[str]:
    """Parse the HERMES_GPT_OPERATOR_ALLOWED_PROFILES env value.

    Returns a list of canonical profile names. ``"*"`` is preserved as a
    sentinel meaning "all existing profiles".
    """
    if not raw:
        return ["default"]
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        return ["default"]
    if "*" in items:
        return ["*"]
    normalized: list[str] = []
    for item in items:
        try:
            normalized.append(validate_profile_name(item))
        except ValueError:
            continue
    return normalized or ["default"]


def profile_is_allowed(profile: str, allowed: list[str], existing_profiles: Iterable[str] | None = None) -> bool:
    """Return True if ``profile`` is in the ``allowed`` set.

    If ``allowed`` is ``["*"]``, every profile is allowed (subject to the
    caller validating that ``profile`` actually exists).
    """
    if not allowed:
        return False
    if allowed == ["*"]:
        return True
    try:
        canon = validate_profile_name(profile)
    except ValueError:
        return False
    return canon in {validate_profile_name(p) for p in allowed}


def list_existing_profiles(hermes_root: Path | None) -> list[str]:
    """List existing profile names under ``hermes_root``. Best-effort.

    Returns ``["default"]`` at minimum. Named profiles are discovered by
    listing ``<root>/profiles/*``.
    """
    names = ["default"]
    if hermes_root is None:
        return names
    profiles_dir = hermes_root / "profiles"
    if not profiles_dir.is_dir():
        return names
    try:
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                canon = validate_profile_name(entry.name)
            except ValueError:
                continue
            if canon != "default" and canon not in names:
                names.append(canon)
    except OSError:
        pass
    return names


def resolve_profile_home(profile: str, hermes_root: Path | None) -> Path:
    """Resolve the HERMES_HOME path for a profile.

    ``default`` -> ``hermes_root``
    ``<name>``  -> ``hermes_root / profiles / <name>``
    """
    canon = validate_profile_name(profile)
    if hermes_root is None:
        raise RuntimeError("Hermes root is not available; cannot resolve profile home")
    hermes_root = normalize_hermes_data_root(hermes_root) or hermes_root
    if canon == "default":
        return hermes_root
    return hermes_root / "profiles" / canon


def profile_exists(profile: str, hermes_root: Path | None) -> bool:
    """Return True if ``profile`` exists on disk under ``hermes_root``."""
    if hermes_root is None:
        return profile == "default"
    hermes_root = normalize_hermes_data_root(hermes_root) or hermes_root
    try:
        home = resolve_profile_home(profile, hermes_root)
    except (ValueError, RuntimeError):
        return False
    return home.is_dir()


# ---------------------------------------------------------------------------
# Allowed / denied path policy
# ---------------------------------------------------------------------------


def parse_path_list(raw: str | None) -> list[Path]:
    """Parse a comma- or newline-separated path list. Returns resolved Paths."""
    if not raw:
        return []
    sep = ","
    if "\n" in raw and "," not in raw:
        sep = "\n"
    out: list[Path] = []
    for item in raw.split(sep):
        text = item.strip()
        if not text:
            continue
        out.append(_normalize_path(text))
    return out


def path_under_allowed(path: str | os.PathLike[str], allowed: list[Path]) -> bool:
    """Return True if ``path`` resolves under one of the ``allowed`` roots."""
    if not allowed:
        return False
    resolved = _normalize_path(path)
    for root in allowed:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Policy snapshot
# ---------------------------------------------------------------------------


class OperatorPolicy:
    """Snapshot of the operator policy at call time.

    Reading env vars at construction time means tests that monkeypatch env
    get a fresh policy each call.
    """

    __slots__ = (
        "enabled",
        "level",
        "apply_mode",
        "allowed_profiles",
        "allowed_paths",
        "denied_paths",
        "owner_ack",
        "owner_mode_ready",
        "mutation_allowed",
    )

    def __init__(self) -> None:
        self.enabled = env_truthy(OPERATOR_ENABLED_ENV)
        raw_level = os.environ.get(OPERATOR_LEVEL_ENV, "read_only").strip().lower()
        if raw_level not in LEVELS:
            raw_level = "read_only"
        self.level = raw_level

        raw_mode = os.environ.get(OPERATOR_APPLY_MODE_ENV, "dry_run").strip().lower()
        if raw_mode not in {"dry_run", "direct"}:
            raw_mode = "dry_run"
        self.apply_mode = raw_mode

        self.allowed_profiles = parse_allowed_profiles(
            os.environ.get(OPERATOR_ALLOWED_PROFILES_ENV)
        )
        self.allowed_paths = parse_path_list(
            os.environ.get(OPERATOR_ALLOWED_PATHS_ENV)
        )
        # Denied paths env adds to the built-in defaults; it cannot remove
        # the defaults. We don't store the env list as paths here because
        # ``is_denied_path`` already covers the built-in conservative set.
        self.denied_paths = parse_path_list(
            os.environ.get(OPERATOR_DENIED_PATHS_ENV)
        )

        self.owner_ack = os.environ.get(OWNER_ACK_ENV, "")

        self.owner_mode_ready = (
            self.enabled
            and self.level == "owner"
            and self.owner_ack == OWNER_ACK_REQUIRED_VALUE
        )

        self.mutation_allowed = (
            self.enabled
            and self.apply_mode == "direct"
            and level_rank(self.level) >= level_rank("cron")
        )

    # --- convenience -------------------------------------------------------

    def effective_dry_run(self, requested_dry_run: bool) -> bool:
        """Effective dry-run is True if either input says dry-run OR policy is
        not in direct apply mode."""
        if requested_dry_run:
            return True
        return self.apply_mode != "direct"

    def require_enabled(self) -> None:
        if not self.enabled:
            raise PermissionError(
                "Operator mode is disabled. Set "
                f"{OPERATOR_ENABLED_ENV}=1 to enable it."
            )

    def require_level(self, required: str) -> None:
        self.require_enabled()
        if not has_level(required, self.level):
            raise PermissionError(
                f"Operator level {self.level!r} does not satisfy required level {required!r}. "
                f"Set {OPERATOR_LEVEL_ENV} to at least {required!r}."
            )

    def require_mutation(self, dry_run: bool) -> None:
        """Gate for any mutating operation. Dry-run is allowed at any enabled
        level that satisfies ``required``. Direct requires direct apply mode."""
        # Level check is caller's responsibility; this method gates the
        # apply-mode axis only.
        if self.effective_dry_run(dry_run):
            return
        # Direct path: require enabled + direct mode.
        if not (self.enabled and self.apply_mode == "direct"):
            raise PermissionError(
                "Direct mutation requires operator mode enabled with "
                f"{OPERATOR_APPLY_MODE_ENV}=direct."
            )

    def require_owner(self, dry_run: bool) -> None:
        """Gate for owner-only operations."""
        self.require_level("owner")
        if not self.owner_mode_ready:
            raise PermissionError(
                "Owner Mode requires "
                f"{OPERATOR_ENABLED_ENV}=1, {OPERATOR_LEVEL_ENV}=owner, and "
                f"{OWNER_ACK_ENV}={OWNER_ACK_REQUIRED_VALUE!r}."
            )
        # Owner direct still requires direct apply mode + dry_run=False.
        if not self.effective_dry_run(dry_run):
            if self.apply_mode != "direct":
                raise PermissionError(
                    "Owner direct mutation requires "
                    f"{OPERATOR_APPLY_MODE_ENV}=direct."
                )

    def require_profile(self, profile: str, hermes_root: Path | None) -> None:
        canon = validate_profile_name(profile)
        if not profile_exists(canon, hermes_root):
            raise FileNotFoundError(
                f"Profile {canon!r} does not exist under "
                f"{hermes_root or '<hermes root unavailable>'}."
            )
        if not profile_is_allowed(canon, self.allowed_profiles):
            raise PermissionError(
                f"Profile {canon!r} is not in the allowed profiles list "
                f"({OPERATOR_ALLOWED_PROFILES_ENV})."
            )

    def require_workspace_path(self, path: str | os.PathLike[str]) -> None:
        """For workspace/owner file tools: path must be under an allowed
        path AND not a denied path. Owner mode does NOT bypass the denied
        check (no secret override in this PR)."""
        if is_denied_path(path):
            raise PermissionError(
                f"Path {str(path)!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env)."
            )
        if not self.allowed_paths:
            raise PermissionError(
                "Workspace writes are disabled because "
                f"{OPERATOR_ALLOWED_PATHS_ENV} is empty. Set it to one or more "
                "workspace root directories."
            )
        if not path_under_allowed(path, self.allowed_paths):
            raise PermissionError(
                f"Path {str(path)!r} is not under any allowed path in "
                f"{OPERATOR_ALLOWED_PATHS_ENV}."
            )

    def to_summary(self) -> dict[str, Any]:
        """Return a JSON-safe summary. Never includes raw env values."""
        return {
            "enabled": self.enabled,
            "level": self.level,
            "apply_mode": self.apply_mode,
            "allowed_profiles": list(self.allowed_profiles),
            "allowed_paths_count": len(self.allowed_paths),
            "allowed_paths_summary": [
                str(p) for p in self.allowed_paths[:8]
            ],
            "denied_paths_count": len(self.denied_paths),
            "denied_paths_summary": [
                str(p) for p in self.denied_paths[:8]
            ],
            "owner_mode_ready": self.owner_mode_ready,
            "mutation_allowed": self.mutation_allowed,
            "available_capability_groups": _capability_groups(self.level),
        }


def _capability_groups(level: str) -> list[str]:
    """Return the capability group names available at ``level``."""
    groups: list[str] = ["read_only"]
    if level_rank(level) >= level_rank("cron"):
        groups.append("cron")
    if level_rank(level) >= level_rank("skills"):
        groups.append("skills")
    if level_rank(level) >= level_rank("skills_config"):
        groups.append("skills_config")
    if level_rank(level) >= level_rank("workspace"):
        groups.append("workspace")
    if level_rank(level) >= level_rank("owner"):
        groups.append("owner")
    return groups


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def set_audit_log_override(path: Path | None) -> None:
    """Set or clear the audit log path override (for tests)."""
    global _audit_log_override
    with _audit_lock:
        _audit_log_override = path


def audit_log_path() -> Path:
    """Return the active audit log path."""
    with _audit_lock:
        if _audit_log_override is not None:
            return _audit_log_override
    # Prefer the Hermes logs dir if it exists / is writable.
    try:
        if AUDIT_LOG_HERMES_PATH.parent.exists():
            return AUDIT_LOG_HERMES_PATH
    except OSError:
        pass
    return AUDIT_LOG_FALLBACK_PATH


def _hash_secret_text(text: str | None) -> tuple[int, str]:
    """Return (length, sha256_hex) for prompt/content fields. Never log raw."""
    if text is None:
        return (0, "")
    data = text.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def audit_record(
    *,
    tool: str,
    level: str,
    apply_mode: str,
    dry_run: bool,
    success: bool,
    changed: bool = False,
    summary: str = "",
    error: str = "",
    profile: str | None = None,
    source_profile: str | None = None,
    target_profile: str | None = None,
    path: str | None = None,
    job_id: str | None = None,
    skill_name: str | None = None,
    prompt: str | None = None,
    content: str | None = None,
    key: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a single audit record to the JSONL log. Returns the record.

    Sensitive inputs (prompt, content) are recorded as length + sha256 only.
    Path is summarized to its basename + length, not the full path, to avoid
    leaking directory structure that might itself contain secret hints.

    The record is also returned so callers can include it in tool output.
    """
    prompt_len, prompt_sha = _hash_secret_text(prompt)
    content_len, content_sha = _hash_secret_text(content)

    path_summary = ""
    if path:
        try:
            resolved = _normalize_path(path)
            path_summary = f"{resolved.name} (<{len(str(resolved))} chars>)"
        except Exception:
            path_summary = f"<path> (<{len(str(path))} chars>)"

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "level": level,
        "apply_mode": apply_mode,
        "dry_run": bool(dry_run),
        "success": bool(success),
        "changed": bool(changed),
        "summary": summary[:500] if summary else "",
        "error": error[:500] if error else "",
        "profile": profile,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "path_summary": path_summary,
        "job_id": job_id,
        "skill_name": skill_name,
        "key": key,
        "prompt_len": prompt_len,
        "prompt_sha256": prompt_sha,
        "content_len": content_len,
        "content_sha256": content_sha,
    }
    if extra:
        # Extra must already be sanitized by the caller; we only truncate
        # string values to avoid accidental giant dumps.
        for k, v in extra.items():
            if isinstance(v, str):
                record[k] = v[:500]
            else:
                record[k] = v

    try:
        log_path = audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with _audit_lock:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        # Audit failure must never break a tool. The record is returned so
        # callers can still surface it inline.
        pass

    return record


def audit_tail(limit: int = 20) -> list[dict[str, Any]]:
    """Read the last ``limit`` audit records. Returns newest-last."""
    log_path = audit_log_path()
    if not log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit <= 0:
        return records
    return records[-limit:]


# ---------------------------------------------------------------------------
# Subprocess helper (shared by cron / gateway / workspace run_test / owner)
# ---------------------------------------------------------------------------


def run_argv(
    argv: list[str],
    *,
    timeout: int = 120,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``argv`` as a subprocess with shell=False (hard rule).

    Returns (returncode, stdout, stderr). Output is truncated to a sane
    bound to avoid filling the audit log or context window.
    """
    import subprocess

    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a non-empty list")

    capped_timeout = max(1, min(int(timeout), 600))
    try:
        proc = subprocess.run(
            argv,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=capped_timeout,
            shell=False,  # hard rule: never shell=True
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return (124, _truncate(out), _truncate(err or f"timed out after {capped_timeout}s"))
    except FileNotFoundError as exc:
        return (127, "", _truncate(str(exc)))

    return (proc.returncode, _truncate(proc.stdout), _truncate(proc.stderr))


def _truncate(text: str, limit: int = 4096) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def redact_output(text: str) -> str:
    """Best-effort redaction of secret-looking substrings in command output."""
    if not text:
        return ""
    # Redact common secret shapes: long hex/base64 strings after key/token-like
    # labels, Bearer tokens, sk-... / sk-proj-... OpenAI keys, AKIA... AWS keys.
    patterns: list[tuple[str, str]] = [
        (r"(?i)\b(sk(?:-proj)?-[A-Za-z0-9_-]{20,})\b", "[REDACTED_OPENAI_KEY]"),
        (r"(?i)\b(AKIA[0-9A-Z.]{6,})\b", "[REDACTED_AWS_KEY]"),
        (r"(?i)\b(AKIA[0-9A-Z]{16})\b", "[REDACTED_AWS_KEY]"),
        (r"(?i)(\bBearer\s+)([A-Za-z0-9._\-]{16,})\b", r"\1[REDACTED]"),
        (r"(?i)(\b(?:token|secret|password|api[_-]?key|passwd)\s*[:=]\s*[\"']?)([^\s\"']{8,})", r"\1[REDACTED]"),
    ]
    out = text
    for pattern, repl in patterns:
        out = re.sub(pattern, repl, out)
    return out


# ---------------------------------------------------------------------------
# Unified diff helper
# ---------------------------------------------------------------------------


def unified_diff(old: str, new: str, label: str = "content") -> str:
    """Return a unified diff string. Empty if no changes."""
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
    )
    return "".join(diff)
