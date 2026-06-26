"""Operator diagnostics and recovery tools for hermes-gpt.

Tools:
- ``hermes_operator_doctor`` : read-only deep health check of operator surfaces.
- ``hermes_operator_snapshot`` : single current-state summary.
- ``hermes_release_doctor`` : release readiness checks.
- ``hermes_operator_recover`` : conservative dry-run-first recovery sequence.

Safety rules:
- Never expose secret values, env values, or raw tokens.
- Never claim a process boundary check unless it verifies something real.
- Never invent a connector re-registration CLI/API.
- Dry-run is the default; apply=True is required for mutations.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import operator_policy as op
import operator_config as op_config
import operator_cron as op_cron
import operator_skills as op_skills
import operator_workspace as op_workspace

# ---------------------------------------------------------------------------
# Release version
# ---------------------------------------------------------------------------

VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Check statuses
# ---------------------------------------------------------------------------

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_UNSUPPORTED = "UNSUPPORTED"

# Heartbeat considered stale after 5 minutes (best-effort).
_STALE_HEARTBEAT_SECONDS = 300

# ---------------------------------------------------------------------------
# Path helpers (mirroring operator modules so we do not depend on privates)
# ---------------------------------------------------------------------------


def _profile_home(profile: str, hermes_root: Path | None) -> Path:
    return op.resolve_profile_home(profile, hermes_root)


def _config_path(profile_home: Path) -> Path:
    return profile_home / "config.yaml"


def _env_path(profile_home: Path) -> Path:
    return profile_home / ".env"


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _jobs_file(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "jobs.json"


def _skills_dir(profile_home: Path) -> Path:
    return profile_home / "skills"


def _gateway_pid_path(profile_home: Path) -> Path:
    return profile_home / "gateway.pid"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"


def _ticker_heartbeat_path(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "ticker_heartbeat"


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------


def _check_result(
    status: str,
    layer: str,
    code: str,
    message: str,
    suggested_action: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a single doctor check result."""
    result: dict[str, Any] = {
        "status": status,
        "layer": layer,
        "code": code,
        "message": message,
        "suggested_action": suggested_action,
    }
    if extra:
        result.update(extra)
    return result


# ---------------------------------------------------------------------------
# Low-level subsystem probes
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int | None) -> bool:
    """Best-effort check whether ``pid`` is alive. Never raises."""
    if pid is None:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False


def _read_config_safe(profile_home: Path) -> dict[str, Any]:
    """Read and parse config.yaml, returning a sanitized summary or raising."""
    path = _config_path(profile_home)
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at {path.name}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to read config.yaml.") from exc
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml root is not a mapping.")
    return op_config._redact_dict(cfg)


def _read_env_keys_safe(env_path: Path) -> tuple[set[str], set[str]]:
    """Return (all_keys, secret_like_keys) from a .env file. Never values."""
    keys: set[str] = set()
    secret_like: set[str] = set()
    if not env_path.exists():
        return (keys, secret_like)
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if not key:
                continue
            keys.add(key)
            if op_config._is_secret_env_name(key):
                secret_like.add(key)
    return (keys, secret_like)


def _read_cron_jobs_safe(profile_home: Path) -> list[dict[str, Any]]:
    """Read cron jobs.json and return the job list, or raise."""
    path = _jobs_file(profile_home)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        raise ValueError("jobs.json is neither a dict nor a list.")
    if not isinstance(jobs, list):
        raise ValueError("jobs.json jobs field is not a list.")
    return [j for j in jobs if isinstance(j, dict)]


def _count_skills_safe(profile_home: Path) -> int:
    """Count SKILL.md files under the profile skills directory."""
    root = _skills_dir(profile_home)
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("SKILL.md"):
        if path.is_file():
            count += 1
    return count


def _read_last_audit_record() -> dict[str, Any] | None:
    """Return the newest audit record, or None if no log exists."""
    log_path = op.audit_log_path()
    if not log_path.exists():
        return None
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _gateway_state_summary(profile_home: Path) -> dict[str, Any]:
    """Best-effort gateway state summary with no tokens."""
    state_path = _gateway_state_path(profile_home)
    summary: dict[str, Any] = {"state_file_exists": state_path.exists()}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            adapters: list[dict[str, Any]] = []
            for key in ("telegram", "discord", "slack", "signal", "whatsapp", "api_server"):
                entry = state.get(key)
                if isinstance(entry, dict):
                    adapters.append(
                        {
                            "name": key,
                            "connected": bool(entry.get("connected", False)),
                        }
                    )
            summary["adapters"] = adapters
        except Exception as exc:
            summary["parse_error"] = f"Could not parse gateway_state.json: {exc.__class__.__name__}"
    return summary


# ---------------------------------------------------------------------------
# Doctor checks
# ---------------------------------------------------------------------------


def _check_operator_runtime() -> dict[str, Any]:
    return _check_result(
        status=STATUS_PASS,
        layer="operator",
        code="RUNTIME_REACHABLE",
        message="Operator diagnostic function is executing in the current process.",
        suggested_action="No action needed.",
        extra={"note": "This check confirms the tool callable ran, not a separate process boundary."},
    )


def _check_gateway_status(profile_home: Path) -> dict[str, Any]:
    pid_path = _gateway_pid_path(profile_home)
    heartbeat_path = _ticker_heartbeat_path(profile_home)

    pid: int | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None

    running = _is_process_alive(pid) if pid is not None else False

    heartbeat_mtime: float | None = None
    heartbeat_stale = False
    if heartbeat_path.exists():
        try:
            heartbeat_mtime = heartbeat_path.stat().st_mtime
            heartbeat_stale = (time.time() - heartbeat_mtime) > _STALE_HEARTBEAT_SECONDS
        except OSError:
            heartbeat_mtime = None

    if pid is None and not heartbeat_path.exists():
        return _check_result(
            status=STATUS_FAIL,
            layer="gateway",
            code="GATEWAY_UNREACHABLE",
            message="Gateway PID file and heartbeat are missing; gateway status cannot be verified.",
            suggested_action="Verify Hermes is running, or run hermes_operator_recover with apply=false to plan a restart.",
        )

    if pid is not None and not running:
        return _check_result(
            status=STATUS_FAIL,
            layer="gateway",
            code="GATEWAY_DEAD_PID",
            message=f"Gateway PID file exists ({pid}) but the process is not alive.",
            suggested_action="Run hermes_operator_recover with apply=true to restart the gateway.",
            extra={"pid": pid, "running": False},
        )

    if heartbeat_stale:
        return _check_result(
            status=STATUS_WARN,
            layer="gateway",
            code="GATEWAY_STALE_HEARTBEAT",
            message="Gateway heartbeat file is older than 5 minutes.",
            suggested_action="Check the gateway process or run hermes_operator_recover with apply=false.",
            extra={"heartbeat_mtime": heartbeat_mtime, "stale_seconds": _STALE_HEARTBEAT_SECONDS},
        )

    extra = {"pid": pid, "running": running}
    if heartbeat_mtime is not None:
        extra["heartbeat_mtime"] = heartbeat_mtime
    extra.update(_gateway_state_summary(profile_home))

    return _check_result(
        status=STATUS_PASS,
        layer="gateway",
        code="GATEWAY_OK",
        message="Gateway appears reachable based on PID and heartbeat.",
        suggested_action="No action needed.",
        extra=extra,
    )


def _check_config_readable(profile_home: Path) -> dict[str, Any]:
    path = _config_path(profile_home)
    if not path.exists():
        return _check_result(
            status=STATUS_WARN,
            layer="config",
            code="CONFIG_MISSING",
            message="config.yaml does not exist for this profile.",
            suggested_action="Create config.yaml if this profile needs custom configuration.",
        )
    try:
        summary = _read_config_safe(profile_home)
        return _check_result(
            status=STATUS_PASS,
            layer="config",
            code="CONFIG_READABLE",
            message="config.yaml exists and parses.",
            suggested_action="No action needed.",
            extra={"top_level_keys": sorted(summary.keys())[:20]},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="config",
            code="CONFIG_UNREADABLE",
            message=f"config.yaml could not be read or parsed: {exc.__class__.__name__}",
            suggested_action="Check config.yaml syntax and permissions.",
        )


def _check_env_readable(profile_home: Path) -> dict[str, Any]:
    env_path = _env_path(profile_home)
    if not env_path.exists():
        return _check_result(
            status=STATUS_WARN,
            layer="env",
            code="ENV_MISSING",
            message="No .env file exists for this profile.",
            suggested_action="Create a .env file only if this profile needs env overrides.",
            extra={"env_exists": False, "key_count": 0, "secret_like_count": 0},
        )
    try:
        keys, secret_like = _read_env_keys_safe(env_path)
        return _check_result(
            status=STATUS_PASS,
            layer="env",
            code="ENV_READABLE",
            message=".env file is readable; values are not exposed.",
            suggested_action="No action needed.",
            extra={
                "env_exists": True,
                "key_count": len(keys),
                "secret_like_count": len(secret_like),
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="env",
            code="ENV_UNREADABLE",
            message=f".env file could not be read: {exc.__class__.__name__}",
            suggested_action="Check .env file permissions.",
        )


def _check_cron_registry(profile_home: Path) -> dict[str, Any]:
    try:
        jobs = _read_cron_jobs_safe(profile_home)
        return _check_result(
            status=STATUS_PASS,
            layer="cron",
            code="CRON_REGISTRY_READABLE",
            message=f"Cron registry readable; {len(jobs)} job(s) found.",
            suggested_action="No action needed.",
            extra={"jobs_count": len(jobs)},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="cron",
            code="CRON_REGISTRY_UNREADABLE",
            message=f"Cron registry could not be read: {exc.__class__.__name__}",
            suggested_action="Inspect cron/jobs.json syntax and permissions.",
        )


def _check_skills_registry(profile_home: Path) -> dict[str, Any]:
    root = _skills_dir(profile_home)
    try:
        count = _count_skills_safe(profile_home)
        exists = root.exists()
        if not exists:
            return _check_result(
                status=STATUS_WARN,
                layer="skills",
                code="SKILLS_DIR_MISSING",
                message="Skills directory does not exist for this profile.",
                suggested_action="Create skills under this profile or verify HERMES_HOME.",
                extra={"skills_dir_exists": False, "count": 0},
            )
        return _check_result(
            status=STATUS_PASS,
            layer="skills",
            code="SKILLS_REGISTRY_READABLE",
            message=f"Skills directory readable; {count} SKILL.md file(s) found.",
            suggested_action="No action needed.",
            extra={"skills_dir_exists": True, "count": count},
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="skills",
            code="SKILLS_REGISTRY_UNREADABLE",
            message=f"Skills directory could not be enumerated: {exc.__class__.__name__}",
            suggested_action="Check skills/ directory permissions.",
        )


def _check_operator_policy(profile: str, hermes_root: Path | None) -> dict[str, Any]:
    try:
        policy = op.OperatorPolicy()
        op.validate_profile_name(profile)
        profile_exists = op.profile_exists(profile, hermes_root)
        allowed = op.profile_is_allowed(profile, policy.allowed_profiles)
        return _check_result(
            status=STATUS_PASS,
            layer="policy",
            code="POLICY_OK",
            message="Operator policy parses and profile is allowed.",
            suggested_action="No action needed.",
            extra={
                "enabled": policy.enabled,
                "level": policy.level,
                "apply_mode": policy.apply_mode,
                "profile_exists": profile_exists,
                "profile_allowed": allowed,
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="policy",
            code="POLICY_INVALID",
            message=f"Operator policy check failed: {exc.__class__.__name__}",
            suggested_action="Review HERMES_GPT_OPERATOR_* environment variables.",
        )


def _check_last_audit_record() -> dict[str, Any]:
    try:
        record = _read_last_audit_record()
        if record is None:
            return _check_result(
                status=STATUS_WARN,
                layer="audit",
                code="AUDIT_LOG_EMPTY",
                message="Audit log exists but has no records yet.",
                suggested_action="No action needed; records will appear as tools are used.",
                extra={"audit_log_path": str(op.audit_log_path())},
            )
        return _check_result(
            status=STATUS_PASS,
            layer="audit",
            code="AUDIT_RECORD_READABLE",
            message="Last audit record is readable JSON.",
            suggested_action="No action needed.",
            extra={
                "audit_log_path": str(op.audit_log_path()),
                "last_record_tool": record.get("tool"),
                "last_record_timestamp": record.get("timestamp"),
            },
        )
    except Exception as exc:
        return _check_result(
            status=STATUS_FAIL,
            layer="audit",
            code="AUDIT_RECORD_UNREADABLE",
            message=f"Last audit record could not be read: {exc.__class__.__name__}",
            suggested_action="Check audit log path and permissions.",
        )


def _check_connector_api_bridge(profile_home: Path) -> dict[str, Any]:
    """Best-effort connector check. Always reports UNSUPPORTED because hermes-gpt
    does not implement a connector re-registration API or health endpoint.
    """
    state_summary = _gateway_state_summary(profile_home)
    return _check_result(
        status=STATUS_UNSUPPORTED,
        layer="connector",
        code="CONNECTOR_REREGISTRATION_UNSUPPORTED",
        message="No supported connector re-registration command or API was found.",
        suggested_action="If the connector is stale, recreate it in ChatGPT / your MCP client, or restart the gateway and reconnect manually.",
        extra={
            "supported": False,
            "action": "manual",
            "gateway_state_summary": state_summary,
        },
    )


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------


def hermes_operator_doctor(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Run a read-only health check across operator surfaces."""
    trace_id = op.new_trace_id()
    try:
        try:
            op.validate_profile_name(profile)
        except ValueError as exc:
            result = op.make_error_envelope(
                layer="policy",
                code="INVALID_PROFILE",
                safe_message=str(exc),
                suggested_action="Provide a valid Hermes profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        if not op.profile_exists(profile, hermes_root):
            result = op.make_error_envelope(
                layer="policy",
                code="PROFILE_NOT_FOUND",
                safe_message=f"Profile {profile!r} does not exist.",
                suggested_action="Verify HERMES_HOME and the profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        profile_home = _profile_home(profile, hermes_root)

        checks: dict[str, Any] = {
            "operator_runtime": _check_operator_runtime(),
            "gateway_status": _check_gateway_status(profile_home),
            "config_readable": _check_config_readable(profile_home),
            "env_readable": _check_env_readable(profile_home),
            "cron_registry": _check_cron_registry(profile_home),
            "skills_registry": _check_skills_registry(profile_home),
            "operator_policy": _check_operator_policy(profile, hermes_root),
            "last_audit_record": _check_last_audit_record(),
            "connector_api_bridge": _check_connector_api_bridge(profile_home),
        }

        failed = [name for name, c in checks.items() if c["status"] == STATUS_FAIL]
        warnings = [name for name, c in checks.items() if c["status"] == STATUS_WARN]
        unsupported = [name for name, c in checks.items() if c["status"] == STATUS_UNSUPPORTED]

        if failed:
            overall = STATUS_FAIL
            recommended = "Run hermes_operator_recover with apply=false to preview recovery steps."
        elif warnings:
            overall = STATUS_WARN
            recommended = "Review warnings, then run hermes_operator_recover if needed."
        elif unsupported:
            overall = STATUS_WARN
            recommended = "Unsupported checks are not failures; review connector notes."
        else:
            overall = STATUS_PASS
            recommended = "No action needed."

        return json.dumps(
            {
                "success": True,
                "ok": overall == STATUS_PASS,
                "profile": profile,
                "overall_status": overall,
                "checks": checks,
                "failed_checks": failed,
                "warnings": warnings,
                "unsupported": unsupported,
                "recommended_action": recommended,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="operator",
            code="DOCTOR_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_doctor again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)


def hermes_operator_snapshot(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Return a single current-state summary of the operator and its surfaces."""
    trace_id = op.new_trace_id()
    try:
        try:
            op.validate_profile_name(profile)
        except ValueError as exc:
            result = op.make_error_envelope(
                layer="policy",
                code="INVALID_PROFILE",
                safe_message=str(exc),
                suggested_action="Provide a valid Hermes profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        profile_exists = op.profile_exists(profile, hermes_root)
        profile_home = _profile_home(profile, hermes_root) if profile_exists else None
        known_issues: list[str] = []

        # Gateway
        gateway: dict[str, Any] = {"running": False}
        if profile_home is not None:
            try:
                pid_path = _gateway_pid_path(profile_home)
                pid = None
                if pid_path.exists():
                    try:
                        pid = int(pid_path.read_text(encoding="utf-8").strip())
                    except (OSError, ValueError):
                        pid = None
                gateway["pid"] = pid
                gateway["running"] = _is_process_alive(pid) if pid is not None else False
                hb_path = _ticker_heartbeat_path(profile_home)
                if hb_path.exists():
                    try:
                        gateway["ticker_heartbeat_mtime"] = hb_path.stat().st_mtime
                    except OSError:
                        pass
            except Exception as exc:
                known_issues.append(f"gateway_probe_failed:{exc.__class__.__name__}")

        # Cron
        cron: dict[str, Any] = {"jobs_count": 0, "enabled_count": 0, "jobs_with_errors": 0}
        if profile_home is not None:
            try:
                jobs = _read_cron_jobs_safe(profile_home)
                cron = {
                    "jobs_count": len(jobs),
                    "enabled_count": sum(1 for j in jobs if j.get("enabled", True)),
                    "jobs_with_errors": sum(1 for j in jobs if j.get("last_error")),
                }
            except Exception as exc:
                known_issues.append(f"cron_probe_failed:{exc.__class__.__name__}")

        # Env
        env: dict[str, Any] = {"env_exists": False, "key_count": 0, "secret_like_count": 0}
        if profile_home is not None:
            try:
                env_path = _env_path(profile_home)
                if env_path.exists():
                    keys, secret_like = _read_env_keys_safe(env_path)
                    env = {
                        "env_exists": True,
                        "key_count": len(keys),
                        "secret_like_count": len(secret_like),
                    }
            except Exception as exc:
                known_issues.append(f"env_probe_failed:{exc.__class__.__name__}")

        # Skills
        skills: dict[str, Any] = {"count": 0}
        if profile_home is not None:
            try:
                skills["count"] = _count_skills_safe(profile_home)
            except Exception as exc:
                known_issues.append(f"skills_probe_failed:{exc.__class__.__name__}")

        # Last audit timestamp
        last_audit_timestamp: str | None = None
        try:
            record = _read_last_audit_record()
            if record:
                last_audit_timestamp = record.get("timestamp")
        except Exception:
            known_issues.append("audit_probe_failed")

        # Repo status
        repo_status = _repo_status(Path(__file__).resolve().parent)

        recommended = (
            "Run hermes_operator_doctor for details."
            if known_issues
            else "No action needed."
        )

        return json.dumps(
            {
                "success": True,
                "version": VERSION,
                "profile": profile,
                "profile_exists": profile_exists,
                "gateway": gateway,
                "cron": cron,
                "env": env,
                "skills": skills,
                "last_audit_timestamp": last_audit_timestamp,
                "repo_status": repo_status,
                "known_issues": known_issues,
                "recommended_next_action": recommended,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="operator",
            code="SNAPSHOT_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_snapshot again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Repo helpers for snapshot and release doctor
# ---------------------------------------------------------------------------


def _repo_status(workdir: Path) -> dict[str, Any]:
    """Best-effort git repo status. No secrets."""
    status: dict[str, Any] = {
        "is_git_repo": False,
        "clean": None,
        "branch": None,
    }
    git_dir = workdir / ".git"
    if not git_dir.exists():
        return status
    status["is_git_repo"] = True
    try:
        rc, out, err = op.run_argv(["git", "status", "--porcelain=v1"], timeout=30, workdir=str(workdir))
        status["clean"] = rc == 0 and not out.strip()
    except Exception:
        status["clean"] = None
    try:
        rc, out, _ = op.run_argv(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=30, workdir=str(workdir))
        if rc == 0:
            status["branch"] = out.strip()
    except Exception:
        status["branch"] = None
    return status


def _find_secret_files(workdir: Path) -> list[str]:
    """Return relative paths of secret-looking files under workdir."""
    secret_names = op.DEFAULT_DENIED_BASENAMES | op.DEFAULT_DENIED_DIR_NAMES
    secret_substrings = op.SECRET_PATH_SUBSTRINGS
    found: list[str] = []
    for root, dirs, files in os.walk(workdir):
        root_path = Path(root)
        # Skip git internals and caches.
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache", "node_modules"}]
        for name in files:
            lower = name.lower()
            if name in secret_names or lower.startswith(".env.") or name == ".env":
                found.append(str(root_path / name))
                continue
            for sub in secret_substrings:
                if sub in lower:
                    found.append(str(root_path / name))
                    break
        for name in dirs:
            if name in secret_names:
                found.append(str(root_path / name))
    # Return paths relative to workdir for readability.
    rels: list[str] = []
    for p in found:
        try:
            rels.append(str(Path(p).relative_to(workdir)))
        except ValueError:
            rels.append(str(p))
    return sorted(set(rels))


def _pyproject_version(workdir: Path) -> str | None:
    path = workdir / "pyproject.toml"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("version"):
                parts = line.split("=", 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _file_contains(workdir: Path, filename: str, needle: str) -> bool:
    path = workdir / filename
    if not path.exists():
        return False
    try:
        return needle in path.read_text(encoding="utf-8")
    except Exception:
        return False


def _previous_git_tag(workdir: Path) -> str | None:
    try:
        rc, out, _ = op.run_argv(["git", "describe", "--tags", "--abbrev=0"], timeout=30, workdir=str(workdir))
        if rc == 0:
            return out.strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Release doctor
# ---------------------------------------------------------------------------


def hermes_release_doctor(
    workdir: str | None = None,
    full_tests: bool = False,
    timeout: int = 180,
    runner=None,
) -> str:
    """Check whether the repo/operator is safe to ship as v0.3.0."""
    trace_id = op.new_trace_id()
    try:
        wd = Path(workdir).expanduser().resolve() if workdir else Path(__file__).resolve().parent
        blocking: list[str] = []
        warnings: list[str] = []

        # Secret-file scan.
        secret_files = _find_secret_files(wd)
        if secret_files:
            blocking.append(f"Secret-like files found in tree: {', '.join(secret_files[:10])}")

        # Git repo / branch / dirty tree.
        repo = _repo_status(wd)
        if not repo["is_git_repo"]:
            warnings.append("Working directory is not a git repo.")
        if repo["branch"] is None:
            warnings.append("Could not detect git branch.")
        if repo["clean"] is False:
            warnings.append("Working tree has uncommitted changes.")

        # pyproject.toml version.
        version = _pyproject_version(wd)
        if version is None:
            blocking.append("Could not read version from pyproject.toml.")
        elif version != VERSION:
            warnings.append(f"pyproject.toml version is {version!r}; expected {VERSION!r}.")

        # CHANGELOG mentions version.
        if not _file_contains(wd, "CHANGELOG.md", VERSION):
            warnings.append(f"CHANGELOG.md does not mention {VERSION}.")

        # README/docs mention reliability tools.
        if not _file_contains(wd, "README.md", "hermes_operator_doctor"):
            warnings.append("README.md does not mention the new diagnostic tools.")
        if not _file_contains(wd, "docs/operator-mode.md", "hermes_operator_recover"):
            warnings.append("docs/operator-mode.md does not mention hermes_operator_recover.")

        # Import / py_compile check.
        try:
            import server  # noqa: F401
        except Exception as exc:
            blocking.append(f"server.py cannot be imported: {exc.__class__.__name__}")

        try:
            op.run_argv([sys.executable, "-m", "py_compile", "server.py"], timeout=60, workdir=str(wd))
        except Exception as exc:
            blocking.append(f"py_compile server.py failed: {exc.__class__.__name__}")

        # Operator apply mode.
        apply_mode = os.environ.get(op.OPERATOR_APPLY_MODE_ENV, "dry_run").strip().lower()
        if apply_mode == "direct":
            warnings.append(f"{op.OPERATOR_APPLY_MODE_ENV}=direct is set; releases should ship dry-run by default.")

        # Version vs previous tag.
        prev_tag = _previous_git_tag(wd)
        if prev_tag and version and prev_tag.endswith(version):
            warnings.append(f"Version {version} matches the previous tag {prev_tag}; consider bumping.")

        # Optional full test run.
        test_result: dict[str, Any] | None = None
        if full_tests:
            run_fn = runner or op.run_argv
            try:
                rc, out, err = run_fn(
                    [sys.executable, "-m", "pytest", "-q"],
                    timeout=max(60, int(timeout)),
                    workdir=str(wd),
                )
                test_result = {"returncode": rc, "success": rc == 0}
                if rc != 0:
                    blocking.append("pytest suite failed.")
            except subprocess.TimeoutExpired:
                blocking.append(f"pytest timed out after {timeout}s.")
                test_result = {"returncode": None, "success": False, "error": "timeout"}
            except Exception as exc:
                blocking.append(f"pytest could not run: {exc.__class__.__name__}")
                test_result = {"returncode": None, "success": False, "error": exc.__class__.__name__}

        if blocking:
            status = "BLOCKED"
            recommended = "Fix blocking issues before tagging a release."
        elif warnings:
            status = "WARN"
            recommended = "Review warnings, then run with full_tests=true before tagging."
        else:
            status = "PASS"
            recommended = "Ready to tag v0.3.0."

        return json.dumps(
            {
                "success": True,
                "status": status,
                "blocking_issues": blocking,
                "non_blocking_issues": warnings,
                "recommended_release_type": "minor",
                "recommended_next_action": recommended,
                "version": version,
                "repo_status": repo,
                "full_tests": full_tests,
                "test_result": test_result,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="release",
            code="RELEASE_DOCTOR_INTERNAL_ERROR",
            suggested_action="Run hermes_release_doctor again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _recover_step_result(
    step: str,
    status: str,
    message: str,
    suggested_action: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "step": step,
        "status": status,
        "message": message,
        "suggested_action": suggested_action,
    }
    if extra:
        result.update(extra)
    return result


def hermes_operator_recover(
    profile: str = "default",
    apply: bool = False,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Conservative recovery sequence. Dry-run by default."""
    trace_id = op.new_trace_id()
    steps: list[dict[str, Any]] = []
    mutations_attempted = 0
    mutations_performed = 0
    warnings: list[str] = []
    failed_steps: list[str] = []

    try:
        try:
            op.validate_profile_name(profile)
        except ValueError as exc:
            result = op.make_error_envelope(
                layer="policy",
                code="INVALID_PROFILE",
                safe_message=str(exc),
                suggested_action="Provide a valid Hermes profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        if not op.profile_exists(profile, hermes_root):
            result = op.make_error_envelope(
                layer="policy",
                code="PROFILE_NOT_FOUND",
                safe_message=f"Profile {profile!r} does not exist.",
                suggested_action="Verify HERMES_HOME and the profile name.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        profile_home = _profile_home(profile, hermes_root)
        policy = op.OperatorPolicy()

        # Pre-flight: if apply=True, require policy allows mutation.
        can_mutate = policy.enabled and policy.apply_mode == "direct" and op.has_level("workspace", policy.level)
        if apply and not can_mutate:
            result = op.make_error_envelope(
                layer="policy",
                code="PERMISSION_DENIED",
                safe_message="apply=true requires operator enabled, apply_mode=direct, and level>=workspace.",
                suggested_action="Set HERMES_GPT_OPERATOR_ENABLED=1, HERMES_GPT_OPERATOR_LEVEL=workspace (or higher), and HERMES_GPT_OPERATOR_APPLY_MODE=direct.",
                trace_id=trace_id,
            )
            return json.dumps(result, indent=2)

        # 1. read_config
        config_path = _config_path(profile_home)
        if not config_path.exists():
            steps.append(
                _recover_step_result(
                    "read_config",
                    STATUS_WARN,
                    "config.yaml does not exist for this profile.",
                    "Create config.yaml if this profile needs custom configuration.",
                )
            )
        else:
            try:
                cfg = _read_config_safe(profile_home)
                steps.append(
                    _recover_step_result(
                        "read_config",
                        STATUS_PASS,
                        "config.yaml is readable and parses.",
                        "No action needed.",
                        extra={"top_level_keys": sorted(cfg.keys())[:20]},
                    )
                )
            except Exception as exc:
                failed_steps.append("read_config")
                steps.append(
                    _recover_step_result(
                        "read_config",
                        STATUS_FAIL,
                        f"config.yaml could not be read: {exc.__class__.__name__}",
                        "Check config.yaml syntax and permissions.",
                    )
                )

        # 2. validate_env
        try:
            env_path = _env_path(profile_home)
            if env_path.exists():
                keys, secret_like = _read_env_keys_safe(env_path)
                steps.append(
                    _recover_step_result(
                        "validate_env",
                        STATUS_PASS,
                        f".env readable; {len(keys)} key(s), {len(secret_like)} secret-like.",
                        "No action needed.",
                        extra={"key_count": len(keys), "secret_like_count": len(secret_like)},
                    )
                )
            else:
                steps.append(
                    _recover_step_result(
                        "validate_env",
                        STATUS_WARN,
                        "No .env file exists for this profile.",
                        "Create one only if this profile needs env overrides.",
                    )
                )
        except Exception as exc:
            failed_steps.append("validate_env")
            steps.append(
                _recover_step_result(
                    "validate_env",
                    STATUS_FAIL,
                    f".env could not be read: {exc.__class__.__name__}",
                    "Check .env permissions.",
                )
            )

        # 3. restart_gateway_if_needed
        gateway_check = _check_gateway_status(profile_home)
        if gateway_check["status"] in (STATUS_FAIL, STATUS_WARN):
            mutations_attempted += 1
            if apply and can_mutate:
                try:
                    rc, out, err = op_workspace._hermes_gateway_restart_raw(profile, runner=runner)
                    if rc == 0:
                        mutations_performed += 1
                        steps.append(
                            _recover_step_result(
                                "restart_gateway_if_needed",
                                STATUS_PASS,
                                "Gateway restart command executed successfully.",
                                "Verify gateway status with hermes_operator_doctor.",
                                extra={"returncode": rc},
                            )
                        )
                    else:
                        failed_steps.append("restart_gateway_if_needed")
                        steps.append(
                            _recover_step_result(
                                "restart_gateway_if_needed",
                                STATUS_FAIL,
                                f"Gateway restart command returned non-zero exit code {rc}.",
                                "Check Hermes logs and restart manually.",
                                extra={"returncode": rc, "stderr": op.redact_output(err)[:500]},
                            )
                        )
                except Exception as exc:
                    failed_steps.append("restart_gateway_if_needed")
                    steps.append(
                        _recover_step_result(
                            "restart_gateway_if_needed",
                            STATUS_FAIL,
                            f"Gateway restart failed: {exc.__class__.__name__}",
                            "Check Hermes installation and PATH.",
                        )
                    )
            else:
                steps.append(
                    _recover_step_result(
                        "restart_gateway_if_needed",
                        STATUS_WARN if not apply else STATUS_FAIL,
                        "Gateway needs restart but apply=false or mutation not allowed.",
                        "Run hermes_operator_recover with apply=true after enabling direct operator mode.",
                        extra={
                            "would_restart": True,
                            "apply": apply,
                            "mutation_allowed": can_mutate,
                        },
                    )
                )
        else:
            steps.append(
                _recover_step_result(
                    "restart_gateway_if_needed",
                    STATUS_PASS,
                    "Gateway status is healthy; no restart needed.",
                    "No action needed.",
                )
            )

        # 4. connector_routes
        steps.append(
            _recover_step_result(
                "connector_routes",
                STATUS_UNSUPPORTED,
                "No supported connector re-registration command or API was found.",
                "If the connector is stale, recreate it in your MCP client or restart the gateway and reconnect manually.",
                extra={"supported": False, "action": "manual"},
            )
        )

        # 5. recheck_cron
        try:
            jobs = _read_cron_jobs_safe(profile_home)
            steps.append(
                _recover_step_result(
                    "recheck_cron",
                    STATUS_PASS,
                    f"Cron registry readable; {len(jobs)} job(s).",
                    "No action needed.",
                    extra={"jobs_count": len(jobs)},
                )
            )
        except Exception as exc:
            failed_steps.append("recheck_cron")
            steps.append(
                _recover_step_result(
                    "recheck_cron",
                    STATUS_FAIL,
                    f"Cron registry could not be read: {exc.__class__.__name__}",
                    "Inspect cron/jobs.json syntax and permissions.",
                )
            )

        # 6. recheck_skill_index
        try:
            count = _count_skills_safe(profile_home)
            steps.append(
                _recover_step_result(
                    "recheck_skill_index",
                    STATUS_PASS,
                    f"Skills directory readable; {count} SKILL.md file(s).",
                    "No action needed.",
                    extra={"count": count},
                )
            )
        except Exception as exc:
            failed_steps.append("recheck_skill_index")
            steps.append(
                _recover_step_result(
                    "recheck_skill_index",
                    STATUS_FAIL,
                    f"Skills directory could not be enumerated: {exc.__class__.__name__}",
                    "Check skills/ directory permissions.",
                )
            )

        # 7. write_audit_record
        try:
            record = op.audit_record(
                tool="hermes_operator_recover",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=not apply,
                success=len(failed_steps) == 0,
                changed=apply and mutations_performed > 0,
                summary=f"recover apply={apply} mutations={mutations_performed} failed={len(failed_steps)}",
                profile=profile,
                extra={"trace_id": trace_id, "failed_steps": failed_steps},
            )
            steps.append(
                _recover_step_result(
                    "write_audit_record",
                    STATUS_PASS,
                    "Audit record written." if not apply else "Audit record written after recovery attempts.",
                    "No action needed.",
                    extra={"audit_log_path": str(op.audit_log_path()), "record_timestamp": record.get("timestamp")},
                )
            )
        except Exception as exc:
            failed_steps.append("write_audit_record")
            steps.append(
                _recover_step_result(
                    "write_audit_record",
                    STATUS_WARN,
                    f"Audit record could not be written: {exc.__class__.__name__}",
                    "Check audit log path and permissions.",
                )
            )

        overall_ok = len(failed_steps) == 0
        if failed_steps:
            recommended = "Review failed steps and run hermes_operator_doctor."
        elif apply:
            recommended = "Recovery applied; run hermes_operator_doctor to verify."
        else:
            recommended = "Dry-run complete; review steps and run with apply=true if appropriate."

        return json.dumps(
            {
                "success": True,
                "ok": overall_ok,
                "apply": apply,
                "profile": profile,
                "steps": steps,
                "mutations_attempted": mutations_attempted,
                "mutations_performed": mutations_performed,
                "warnings": warnings,
                "failed_steps": failed_steps,
                "recommended_next_action": recommended,
                "trace_id": trace_id,
            },
            indent=2,
            default=str,
        )
    except Exception as exc:
        result = op.error_from_exception(
            exc,
            layer="operator",
            code="RECOVER_INTERNAL_ERROR",
            suggested_action="Run hermes_operator_recover again or check server logs.",
            trace_id=trace_id,
        )
        return json.dumps(result, indent=2)
