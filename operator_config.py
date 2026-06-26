"""Config and env operator tools for hermes-gpt.

Tools:
- ``hermes_config_get``          : read_only  — read config.yaml (sanitized)
- ``hermes_config_set``          : skills_config — set a safe key in config.yaml
- ``hermes_config_patch``        : skills_config — text patch on config.yaml
- ``hermes_env_status``          : read_only  — show set/unset status only (no values)
- ``hermes_env_set_nonsecret``   : skills_config — set a non-secret env key in .env
- ``hermes_env_copy_nonsecret``  : skills_config — copy a non-secret key across profiles

Safety rules:
- Never expose raw .env contents.
- Never print secret values. ``hermes_env_status`` returns only set/unset + secret_like.
- ``hermes_config_set`` rejects keys whose path contains key/token/secret/etc.
- ``hermes_env_set_nonsecret`` rejects keys whose name contains TOKEN/SECRET/KEY/
  PASSWORD/CREDENTIAL/AUTH/COOKIE/PRIVATE/OAUTH.
- ``.env`` writes preserve comments and existing lines, replace or append,
  and back up the file before writing.
- ``config.yaml`` writes use atomic replace with backup.
- Hermes' own env-var writer denylist (LD_PRELOAD, PYTHONPATH, PATH, EDITOR,
  HERMES_HOME, etc.) is enforced for env_set_nonsecret in addition to the
  secret-name denylist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import operator_policy as op

# ---------------------------------------------------------------------------
# Key safety rules
# ---------------------------------------------------------------------------

# Substrings that mark a config key path or env var name as secret-like.
_SECRET_KEY_SUBSTRINGS = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "auth",
    "cookie",
    "private",
    "oauth",
)

# Env var names that influence subprocess / loader / interpreter behavior.
# Mirrors Hermes' own _ENV_VAR_NAME_DENYLIST so the operator layer does not
# become a bypass for it. These are refused by env_set_nonsecret regardless
# of secret-like naming.
_ENV_NAME_DENYLIST: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_DEBUG",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "SHELL",
        "BROWSER",
        "EDITOR",
        "VISUAL",
        "PAGER",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
        "GIT_SHELL",
        "HERMES_HOME",
        "HERMES_PROFILE",
        "HERMES_CONFIG",
        "HERMES_ENV",
    }
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:token|secret|password|api[_-]?key|passwd|auth)\s*[:=]\s*[\"']?[^\s\"'#;]{4,}"
)


def _is_secret_key(key_path: str) -> bool:
    lower = (key_path or "").lower()
    return any(s in lower for s in _SECRET_KEY_SUBSTRINGS)


def _is_secret_assignment(text: str) -> bool:
    return bool(text) and bool(_SECRET_ASSIGNMENT_RE.search(text))


def _is_secret_env_name(name: str) -> bool:
    upper = (name or "").upper()
    for s in _SECRET_KEY_SUBSTRINGS:
        if s.upper() in upper:
            return True
    return False


def _config_path(profile_home: Path) -> Path:
    return profile_home / "config.yaml"


def _env_path(profile_home: Path) -> Path:
    return profile_home / ".env"


# ---------------------------------------------------------------------------
# Config get / set / patch
# ---------------------------------------------------------------------------


def _safe_config_view(cfg: dict[str, Any], key_path: Optional[str]) -> Any:
    """Return a sanitized view of ``cfg``.

    Without a key_path: return a top-level summary that redacts any
    secret-looking key. With a key_path: walk the dotted path and return
    the value, redacting secret-looking leaves.
    """
    if key_path is None:
        return _redact_dict(cfg)
    parts = [p for p in key_path.split(".") if p]
    cur: Any = cfg
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return _redact_leaf(cur, ".".join(parts))


def _redact_dict(d: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 6:
        return {"_truncated": True}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _is_secret_key(str(k)):
            out[k] = "<redacted>" if v not in (None, "", [], {}) else v
            continue
        if isinstance(v, dict):
            out[k] = _redact_dict(v, depth + 1)
        elif isinstance(v, list):
            out[k] = [_redact_leaf(item, str(k)) for item in v[:20]]
        else:
            out[k] = _redact_leaf(v, str(k))
    return out


def _redact_leaf(value: Any, key: str) -> Any:
    if _is_secret_key(key) and value not in (None, "", [], {}):
        return "<redacted>"
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_leaf(item, key) for item in value[:20]]
    return value


def hermes_config_get(
    profile: str = "default",
    key_path: Optional[str] = None,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        if not path.exists():
            return json.dumps(
                {"success": True, "profile": profile, "exists": False, "value": None},
                indent=2,
            )
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for config tools.") from exc
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        # If a specific key is requested and it's secret-like, refuse even
        # to return the redacted value — the caller does not need to know
        # whether a secret exists at that path. Return None instead.
        if key_path is not None and _is_secret_key(key_path):
            return json.dumps(
                {
                    "success": True,
                    "profile": profile,
                    "key_path": key_path,
                    "value": None,
                    "redacted": True,
                    "note": "Secret-looking key path; value not returned.",
                },
                indent=2,
            )
        view = _safe_config_view(cfg, key_path)
        return json.dumps(
            {
                "success": True,
                "profile": profile,
                "key_path": key_path,
                "value": view,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="config",
                code="CONFIG_READ_ERROR",
                suggested_action="Check that config.yaml exists, is readable, and is valid YAML.",
            ),
            indent=2,
        )


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    try:
        shutil.copy2(path, bak)
        return bak
    except OSError:
        return None


def _set_dotted(cfg: dict[str, Any], key_path: str, value: Any) -> None:
    parts = [p for p in key_path.split(".") if p]
    if not parts:
        raise ValueError("key_path is required.")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def hermes_config_set(
    profile: str = "default",
    key_path: str = "",
    value: Any = None,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        if not key_path:
            raise ValueError("key_path is required.")
        if _is_secret_key(key_path):
            raise PermissionError(
                f"key_path {key_path!r} looks secret-like. Operator config "
                "set refuses to write secret-like keys."
            )

        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for config tools.") from exc

        cfg: dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            if not isinstance(cfg, dict):
                cfg = {}

        # Coerce value: try to interpret as YAML scalar for parity with
        # `hermes config set`, but fall back to string.
        coerced: Any = value
        if isinstance(value, str):
            try:
                coerced = yaml.safe_load(value)
            except yaml.YAMLError:
                coerced = value

        before = _safe_config_view(cfg, key_path)
        new_cfg = json.loads(json.dumps(cfg, default=str))  # deep copy via json
        _set_dotted(new_cfg, key_path, coerced)
        after = _safe_config_view(new_cfg, key_path)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_set": True,
                "profile": profile,
                "key_path": key_path,
                "before": before,
                "after": after,
            }
            op.audit_record(
                tool="hermes_config_set",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                key=key_path,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(path)
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(new_cfg, fh, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "key_path": key_path,
            "before": before,
            "after": after,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_config_set",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"set {key_path}",
            profile=profile,
            key=key_path,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_config_set",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            key=key_path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="config",
                code="CONFIG_WRITE_ERROR",
                suggested_action="Check config.yaml permissions, key path, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_config_patch(
    profile: str = "default",
    old_string: str = "",
    new_string: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        path = _config_path(profile_home)
        if not path.exists():
            raise FileNotFoundError(f"config.yaml not found at {path}.")

        content = path.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise ValueError("old_string not found in config.yaml.")
        if content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more context."
            )
        if _is_secret_key(old_string) or _is_secret_assignment(old_string):
            raise PermissionError("old_string looks secret-like. Refusing.")
        if _is_secret_key(new_string) or _is_secret_assignment(new_string):
            raise PermissionError(
                "new_string looks like it sets a secret-like key. Refusing."
            )

        new_content = content.replace(old_string, new_string, 1)
        diff = op.unified_diff(content, new_content, label="config.yaml")
        if _is_secret_assignment(diff):
            raise PermissionError(
                "Generated diff contains secret-like key assignments. Refusing."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "profile": profile,
                "diff": diff,
            }
            op.audit_record(
                tool="hermes_config_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(path)
        tmp = path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp, path)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_config_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary="patched config.yaml",
            profile=profile,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_config_patch",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="config",
                code="CONFIG_PATCH_ERROR",
                suggested_action="Check config.yaml contents, patch strings, and operator level/apply mode.",
            ),
            indent=2,
        )


# ---------------------------------------------------------------------------
# Env status / set / copy
# ---------------------------------------------------------------------------


def _read_env_keys(env_path: Path) -> set[str]:
    """Return the set of keys defined in a .env file. Never returns values."""
    keys: set[str] = set()
    if not env_path.exists():
        return keys
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key:
                    keys.add(key)
    except OSError:
        return set()
    return keys


def hermes_env_status(
    profile: str = "default",
    keys: list[str] | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Return set/unset status for env keys. Never returns values."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        env_path = _env_path(profile_home)
        existing = _read_env_keys(env_path)

        if not keys:
            # Return summary of all keys (names only).
            summary = [
                {"key": k, "set": True, "secret_like": _is_secret_env_name(k)}
                for k in sorted(existing)
            ]
            return json.dumps(
                {
                    "success": True,
                    "profile": profile,
                    "env_exists": env_path.exists(),
                    "keys": summary,
                },
                indent=2,
            )

        results = []
        for k in keys:
            if not _ENV_NAME_RE.match(k):
                results.append(
                    {"key": k, "set": False, "secret_like": True, "invalid_name": True}
                )
                continue
            results.append(
                {
                    "key": k,
                    "set": k in existing,
                    "secret_like": _is_secret_env_name(k),
                }
            )
        return json.dumps(
            {"success": True, "profile": profile, "keys": results}, indent=2
        )
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_READ_ERROR",
                suggested_action="Check .env file permissions and profile name.",
            ),
            indent=2,
        )


def _validate_env_key_for_write(key: str) -> None:
    if not key:
        raise ValueError("key is required.")
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"Invalid env var name {key!r}.")
    if _is_secret_env_name(key):
        raise PermissionError(
            f"Refusing to set secret-looking env key {key!r}. "
            "Operator env tools only write non-secret keys."
        )
    if key in _ENV_NAME_DENYLIST:
        raise PermissionError(
            f"Refusing to set denylisted env var {key!r}. This name influences "
            "subprocess execution or Hermes runtime location."
        )


def _write_env_key(env_path: Path, key: str, value: str) -> None:
    """Write ``key=value`` to ``env_path``, preserving comments and existing lines.

    Replaces the existing line if the key is already present; otherwise
    appends. The value is written verbatim (no shell escaping) — the .env
    format is plain KEY=VALUE.
    """
    lines: list[str] = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    new_line = f"{key}={value}\n"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            if not replaced:
                out.append(new_line)
                replaced = True
            # Skip duplicate original lines for the same key.
            continue
        out.append(line)
    if not replaced:
        # Ensure there's a blank line between existing content and the new
        # key when the file isn't empty and doesn't already end with one.
        if out and out[-1].strip() != "":
            out.append("\n")
        out.append(new_line)
    tmp = env_path.with_suffix(".env.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.replace(tmp, env_path)


def hermes_env_set_nonsecret(
    profile: str = "default",
    key: str = "",
    value: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(profile, hermes_root)
        _validate_env_key_for_write(key)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        env_path = _env_path(profile_home)
        existing_keys = _read_env_keys(env_path)
        already_set = key in existing_keys

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_set": True,
                "profile": profile,
                "key": key,
                "target": str(env_path),
                "already_set": already_set,
                "value_len": len(value) if value else 0,
                # Do NOT include the value itself.
            }
            op.audit_record(
                tool="hermes_env_set_nonsecret",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                key=key,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(env_path)
        _write_env_key(env_path, key, value)
        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "key": key,
            "already_set": already_set,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_env_set_nonsecret",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"set env key {key}",
            profile=profile,
            key=key,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_env_set_nonsecret",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            key=key,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_WRITE_ERROR",
                suggested_action="Check .env permissions, key name, and operator level/apply mode.",
            ),
            indent=2,
        )


def _read_env_value(env_path: Path, key: str) -> str | None:
    """Return the raw value for ``key`` from ``env_path``.

    Internal helper. Never expose the returned value to tool output — used
    only by env_copy_nonsecret to copy a value across profiles without
    printing it.
    """
    if not env_path.exists():
        return None
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            existing_key, _, existing_value = stripped.partition("=")
            if existing_key.strip() == key:
                return existing_value.strip()
    return None


def hermes_env_copy_nonsecret(
    source_profile: str,
    target_profile: str,
    key: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills_config")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        _validate_env_key_for_write(key)
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_env = _env_path(source_home)
        target_env = _env_path(target_home)

        value = _read_env_value(source_env, key)
        if value is None:
            raise FileNotFoundError(
                f"Key {key!r} not set in source profile {source_profile!r}."
            )

        target_existing = _read_env_keys(target_env)
        already_set = key in target_existing

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "key": key,
                "target": str(target_env),
                "already_set_in_target": already_set,
                "value_len": len(value),
                # Do NOT include the value itself.
            }
            op.audit_record(
                tool="hermes_env_copy_nonsecret",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                key=key,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(target_env)
        _write_env_key(target_env, key, value)
        result = {
            "success": True,
            "dry_run": False,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "key": key,
            "already_set_in_target": already_set,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_env_copy_nonsecret",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"copied env key {key}",
            source_profile=source_profile,
            target_profile=target_profile,
            key=key,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_env_copy_nonsecret",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            key=key,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="env",
                code="ENV_COPY_ERROR",
                suggested_action="Check source/target profiles, key name, and operator level/apply mode.",
            ),
            indent=2,
        )
