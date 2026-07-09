"""Workspace, gateway, git, and owner-mode tools for hermes-gpt.

Tools:
- ``hermes_gateway_status``        : read_only  — gateway / ticker / adapter health
- ``hermes_gateway_restart``       : workspace  — fixed-argv gateway restart
- ``hermes_workspace_read``        : read_only  — read file with operator path policy
- ``hermes_workspace_patch``       : workspace  — find-and-replace within an allowed path
- ``hermes_workspace_write_file``  : workspace  — write file within an allowed path
- ``hermes_workspace_run_test``    : workspace  — conservative allowlist of test/lint commands
- ``hermes_git_status``            : read_only  — git status in a workdir
- ``hermes_git_diff``              : read_only  — git diff in a workdir
- ``hermes_owner_run_command``     : owner      — arbitrary command (with catastrophic blocks)
- ``hermes_owner_patch``           : owner      — arbitrary file patch (still denies secret paths)
- ``hermes_owner_write_file``      : owner      — arbitrary file write (still denies secret paths)

Safety rules:
- No shell=True anywhere. Ever.
- Workspace path tools require path under allowed_paths AND not denied.
- Owner tools require explicit owner ack AND direct mode AND dry_run=false.
- Owner tools still deny secret paths (no secret override in this PR).
- Workspace run_test only allows a conservative command allowlist. Rejects
  git add/commit/push, rm, del, powershell, curl, wget, bash -c, cmd /c,
  pipes, redirects, semicolons, ampersands, encoded commands.
- Owner run_command blocks obvious catastrophic patterns: rm -rf /, del /s,
  format, powershell -EncodedCommand, curl|bash, wget|bash, git push --force,
  git add -A, anything touching .env/vault/token/ssh paths.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

import operator_policy as op


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


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


def _split_command_argv(command: str) -> list[str]:
    """Split a command string into argv without invoking a shell.

    On Windows, shlex with posix=False preserves surrounding quote characters.
    That makes a quoted single argument like "45 5 * * *" arrive downstream as
    a literal string that still includes the quotes. Strip only matching outer
    quotes after parsing so cron expressions and paths with spaces survive as
    single argv tokens, without changing unquoted Windows paths.
    """
    argv = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        cleaned: list[str] = []
        for arg in argv:
            if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {"'", '"'}:
                cleaned.append(arg[1:-1])
            else:
                cleaned.append(arg)
        return cleaned
    return argv


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


def _gateway_pid_path(profile_home: Path) -> Path:
    return profile_home / "gateway.pid"


def _gateway_state_path(profile_home: Path) -> Path:
    return profile_home / "gateway_state.json"


def _read_gateway_state(state_path: Path) -> dict[str, Any]:
    """Read gateway_state.json safely.

    Never raises. Returns an empty dict if the file is missing, invalid,
    unreadable, or not a JSON object.
    """
    if not state_path.exists():
        return {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _read_gateway_pid_from_pid_file(pid_path: Path) -> int | None:
    """Read gateway.pid as a plain integer.

    Older Hermes Gateway versions store only a raw PID here.
    If the file is absent, empty, or not parseable, return None.
    """
    if not pid_path.exists():
        return None
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except (OSError, ValueError):
        return None


def _read_gateway_pid_from_state(state: dict[str, Any]) -> int | None:
    """Read PID from gateway_state.json.

    Newer Hermes Gateway versions may store the actual PID in:
    {
      "pid": 9818,
      "kind": "hermes-gateway",
      "gateway_state": "running"
    }

    Accept int or numeric string. Otherwise return None.
    """
    value = state.get("pid")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_pid_alive(pid: int | None) -> bool:
    """Best-effort process liveness probe.

    Prefer psutil when available. Fall back to os.kill(pid, 0).
    Never raises.
    """
    if pid is None:
        return False

    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        pass  # Fall through to os.kill fallback

    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _read_ticker_heartbeat(profile_home: Path) -> float | None:
    hb_path = profile_home / "cron" / "ticker_heartbeat"
    if not hb_path.exists():
        return None
    try:
        return hb_path.stat().st_mtime
    except OSError:
        return None


def _gateway_adapters_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sanitized adapter/platform summary.

    Supports both formats:

    Legacy:
    {
      "telegram": {"connected": true}
    }

    Newer:
    {
      "platforms": {
        "telegram": {"connected": true}
      }
    }

    No tokens, URLs, or secret-like values are surfaced.
    """
    adapters: list[dict[str, Any]] = []
    seen: set[str] = set()

    legacy_keys = ("telegram", "discord", "slack", "signal", "whatsapp", "api_server")
    for key in legacy_keys:
        entry = state.get(key)
        if isinstance(entry, dict):
            adapters.append(
                {
                    "name": key,
                    "connected": bool(entry.get("connected", False)),
                }
            )
            seen.add(key)

    platforms = state.get("platforms")
    if isinstance(platforms, dict):
        for key, entry in platforms.items():
            name = str(key)
            if name in seen:
                continue
            if isinstance(entry, dict):
                adapters.append(
                    {
                        "name": name,
                        "connected": bool(entry.get("connected", False)),
                    }
                )
                seen.add(name)

    return adapters


def hermes_gateway_status(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Return Hermes Gateway status.

    Important behavior:
    - First try ~/.hermes/gateway.pid.
    - If gateway.pid is missing or unparsable, fall back to gateway_state.json["pid"].
    - This fixes the case where Hermes Gateway is actually running but the PID
      file is absent/stale while gateway_state.json contains the correct PID.
    """
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)

        pid_path = _gateway_pid_path(profile_home)
        state_path = _gateway_state_path(profile_home)

        state = _read_gateway_state(state_path)

        pid = _read_gateway_pid_from_pid_file(pid_path)
        pid_source = "gateway.pid" if pid is not None else None

        if pid is None:
            pid = _read_gateway_pid_from_state(state)
            pid_source = "gateway_state.json" if pid is not None else None

        running = _is_pid_alive(pid)
        ticker_heartbeat = _read_ticker_heartbeat(profile_home)
        adapters_summary = _gateway_adapters_summary(state)

        result = {
            "success": True,
            "profile": profile,
            "gateway_pid": pid,
            "gateway_pid_source": pid_source,
            "gateway_running": running,
            "gateway_state": state.get("gateway_state"),
            "gateway_kind": state.get("kind"),
            "gateway_updated_at": state.get("updated_at"),
            "gateway_restart_requested": state.get("restart_requested"),
            "gateway_exit_reason": state.get("exit_reason"),
            "gateway_active_agents": state.get("active_agents"),
            "ticker_heartbeat_mtime": ticker_heartbeat,
            "adapters": adapters_summary,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="gateway",
                code="GATEWAY_STATUS_ERROR",
                suggested_action="Check gateway.pid, gateway_state.json, and profile name.",
            ),
            indent=2,
        )


def _hermes_argv(profile: str, sub: list[str]) -> list[str]:
    if profile == "default":
        return ["hermes", *sub]
    return ["hermes", "-p", profile, *sub]


def _gateway_restart_argv(profile: str) -> list[str]:
    return _hermes_argv(profile, ["gateway", "restart"])


def _hermes_gateway_restart_raw(
    profile: str = "default",
    runner=None,
) -> tuple[int, str, str]:
    """Execute the raw gateway restart argv. Returns (rc, stdout, stderr).

    No policy checks; callers must gate mutation themselves.
    """
    run_fn = runner or op.run_argv
    return run_fn(_gateway_restart_argv(profile), timeout=120, workdir=None)


def hermes_gateway_restart(
    profile: str = "default",
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_profile(profile, hermes_root)
        argv = _gateway_restart_argv(profile)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_restart": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
            }
            op.audit_record(
                tool="hermes_gateway_restart",
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
        rc, out, err = _hermes_gateway_restart_raw(profile, runner=runner)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
            "note": (
                "If hermes does not support 'gateway restart', this command "
                "may have failed. Try 'hermes gateway stop' + 'hermes gateway start' "
                "manually."
            ),
        }
        op.audit_record(
            tool="hermes_gateway_restart",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc}",
            profile=profile,
            error=op.redact_output(err) if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_gateway_restart",
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
                layer="gateway",
                code="GATEWAY_RESTART_ERROR",
                suggested_action="Check Hermes CLI availability, profile, and operator level/apply mode.",
            ),
            indent=2,
        )


# ---------------------------------------------------------------------------
# Workspace read / patch / write_file / run_test
# ---------------------------------------------------------------------------


def hermes_workspace_read(
    path: str,
    offset: int = 1,
    limit: int = 500,
) -> str:
    """Read a file. Read-only but applies operator path policy (deny secrets)."""
    try:
        policy = op.OperatorPolicy()
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy."
            )
        # If allowed_paths is set, require the path to be under one of them.
        # If allowed_paths is empty, allow reads anywhere that's not denied
        # (read-only mode is the default and the existing hermes_read_file
        # tool already exists).
        if policy.allowed_paths and not op.path_under_allowed(path, policy.allowed_paths):
            raise PermissionError(
                f"Path {path!r} is not under any allowed path in "
                f"{op.OPERATOR_ALLOWED_PATHS_ENV}."
            )
        p = op._normalize_path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        start = max(1, int(offset)) - 1
        end = start + max(1, int(limit))
        chunk = "".join(lines[start:end])
        result = {
            "success": True,
            "path": str(p),
            "offset": start + 1,
            "limit": end - start,
            "total_lines": len(lines),
            "content": chunk,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_READ_ERROR",
                suggested_action="Check path, allowed_paths, denied-path policy, and that the file exists.",
            ),
            indent=2,
        )


def hermes_workspace_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(path)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        p = op._normalize_path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise ValueError("old_string not found in file.")
        if not replace_all and content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more context "
                "or set replace_all=True."
            )
        if replace_all:
            new_content = content.replace(old_string, new_string)
            match_count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            match_count = 1

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "path": str(p),
                "match_count": match_count,
                "diff": op.unified_diff(content, new_content, label=p.name),
            }
            op.audit_record(
                tool="hermes_workspace_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(p)
        _atomic_write_text(p, new_content)
        result = {
            "success": True,
            "dry_run": False,
            "path": str(p),
            "match_count": match_count,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_workspace_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"patched {p.name} ({match_count} replacement(s))",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_patch",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_PATCH_ERROR",
                suggested_action="Check path, allowed_paths, old_string/new_string, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_workspace_write_file(
    path: str,
    content: str,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(path)
        if content is None:
            raise ValueError("content is required.")

        p = op._normalize_path(path)
        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "path": str(p),
                "exists": p.exists(),
                "content_len": len(content),
            }
            op.audit_record(
                tool="hermes_workspace_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(p) if p.exists() else None
        _atomic_write_text(p, content)
        result = {
            "success": True,
            "dry_run": False,
            "path": str(p),
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_workspace_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"wrote {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_write_file",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_WRITE_ERROR",
                suggested_action="Check path, allowed_paths, denied-path policy, and operator level/apply mode.",
            ),
            indent=2,
        )


# Allowlist for run_test. Each entry is a tuple of (argv_prefix, max_args).
# The prefix must match exactly; the rest is bounded.
_TEST_COMMAND_ALLOWLIST: tuple[tuple[tuple[str, ...], int], ...] = (
    (("pytest",), 8),
    (("python", "-m", "pytest"), 8),
    (("npm", "test"), 4),
    (("npm", "run", "test"), 4),
    (("npm", "run", "lint"), 4),
    (("ruff", "check"), 4),
    (("mypy",), 4),
    (("git", "status"), 4),
    (("git", "diff"), 4),
)

# Substrings that mark a command as dangerous and must be refused.
_DANGEROUS_PATTERNS: tuple[str, ...] = (
    "rm ",
    "del ",
    "format ",
    "powershell",
    "curl ",
    "wget ",
    "bash -c",
    "cmd /c",
    "git add",
    "git commit",
    "git push",
    "|",
    ">",
    "<",
    ";",
    "&",
    "EncodedCommand",
    "||",
    "&&",
    "`",
    "$(",
)


def _is_allowed_test_command(argv: list[str]) -> tuple[bool, str]:
    """Check whether argv matches the test/lint allowlist."""
    if not argv:
        return (False, "Empty command.")
    cmd = " ".join(argv)
    # Reject dangerous substrings first.
    for needle in _DANGEROUS_PATTERNS:
        if needle in cmd:
            return (False, f"Command contains forbidden substring {needle!r}.")
    for prefix, max_extra in _TEST_COMMAND_ALLOWLIST:
        if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
            extra = argv[len(prefix) :]
            if len(extra) > max_extra:
                return (False, f"Too many arguments for {prefix!r}.")
            return (True, "")
    return (False, "Command not in the test/lint allowlist.")


def hermes_workspace_run_test(
    command: str,
    workdir: str | None = None,
    timeout: int = 120,
    dry_run: bool = True,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        if not command or not command.strip():
            raise ValueError("command is required.")

        # Parse with shlex so we never invoke a shell.
        try:
            argv = _split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from exc

        allowed, reason = _is_allowed_test_command(argv)
        if not allowed:
            raise PermissionError(reason)

        # workdir policy: must be under an allowed_path if any are set.
        if workdir:
            if policy.allowed_paths and not op.path_under_allowed(workdir, policy.allowed_paths):
                raise PermissionError(
                    f"workdir {workdir!r} is not under any allowed path."
                )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": argv,
                "shell": False,
                "workdir": workdir,
                "timeout": max(1, min(int(timeout), 600)),
            }
            op.audit_record(
                tool="hermes_workspace_run_test",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=workdir or "",
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=timeout, workdir=workdir)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "argv": argv,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        op.audit_record(
            tool="hermes_workspace_run_test",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=False,  # test runs are not file mutations
            summary=f"rc={rc} argv={argv}",
            path=workdir or "",
            error=op.redact_output(err) if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_workspace_run_test",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="WORKSPACE_RUN_TEST_ERROR",
                suggested_action="Check command allowlist, workdir, and operator level/apply mode.",
            ),
            indent=2,
        )


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def _git(argv: list[str], workdir: str, runner=None) -> tuple[int, str, str]:
    run_fn = runner or op.run_argv
    return run_fn(["git", *argv], timeout=60, workdir=workdir)


def hermes_git_status(workdir: str, runner=None) -> str:
    try:
        policy = op.OperatorPolicy()
        if not workdir:
            raise ValueError("workdir is required.")
        # If allowed_paths is set, workdir must be under one. Otherwise allow
        # any workdir (read-only git status is safe and the existing terminal
        # tool is also unguarded when enabled).
        if policy.allowed_paths and not op.path_under_allowed(workdir, policy.allowed_paths):
            raise PermissionError(
                f"workdir {workdir!r} is not under any allowed path."
            )
        rc, out, err = _git(["status", "--porcelain=v1"], workdir, runner=runner)
        result = {
            "success": rc == 0,
            "workdir": workdir,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="GIT_STATUS_ERROR",
                suggested_action="Check workdir, allowed_paths, and git availability.",
            ),
            indent=2,
        )


def hermes_git_diff(
    workdir: str,
    pathspec: str | None = None,
    stat: bool = False,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        if not workdir:
            raise ValueError("workdir is required.")
        if policy.allowed_paths and not op.path_under_allowed(workdir, policy.allowed_paths):
            raise PermissionError(
                f"workdir {workdir!r} is not under any allowed path."
            )
        argv: list[str] = ["diff"]
        if stat:
            argv.append("--stat")
        if pathspec:
            argv.append("--")
            argv.append(pathspec)
        rc, out, err = _git(argv, workdir, runner=runner)
        result = {
            "success": rc == 0,
            "workdir": workdir,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="workspace",
                code="GIT_DIFF_ERROR",
                suggested_action="Check workdir, allowed_paths, pathspec, and git availability.",
            ),
            indent=2,
        )


# ---------------------------------------------------------------------------
# Owner Mode
# ---------------------------------------------------------------------------

# Catastrophic command patterns that even Owner Mode refuses.
_CATASTROPHIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\s+/(\s|$)"),
    re.compile(r"\brm\s+-rf\s+/\*"),
    re.compile(r"(?i)\bdel\s+/s\b"),
    re.compile(r"(?i)\bformat\b"),
    re.compile(r"(?i)powershell.*-EncodedCommand"),
    re.compile(r"(?i)\b(curl|wget)\b[^|]*\|\s*(bash|sh)"),
    re.compile(r"(?i)\bgit\s+push\b.*--force"),
    re.compile(r"(?i)\bgit\s+push\b.*\s-f\b"),
    re.compile(r"(?i)\bgit\s+add\s+-A\b"),
    re.compile(r"(?i)\bgit\s+add\s+\.\s*$"),
)


def _command_touches_secrets(command: str) -> bool:
    """Heuristic: does the command mention secret/vault/token/.env/ssh paths?"""
    lower = command.lower()
    needles = (
        ".env",
        "vault",
        "mcp-tokens",
        "auth.json",
        ".ssh",
        "id_rsa",
        "id_ed25519",
        "authorized_keys",
        ".aws",
        ".gnupg",
        ".kube",
        "webhook_subscriptions.json",
        "oauth",
        "token",
        "secret",
        "credential",
        "cookie",
        "password",
    )
    return any(n in lower for n in needles)


def hermes_owner_run_command(
    command: str,
    timeout: int = 120,
    workdir: str | None = None,
    dry_run: bool = True,
    runner=None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if not command or not command.strip():
            raise ValueError("command is required.")

        # Catastrophic pattern block.
        for pattern in _CATASTROPHIC_PATTERNS:
            if pattern.search(command):
                raise PermissionError(
                    f"Command blocked by catastrophic-pattern guard: {command!r}"
                )
        if _command_touches_secrets(command):
            raise PermissionError(
                "Command touches secret-like paths (.env / vault / token / ssh). "
                "Owner Mode does not permit secret access. Edit the file directly "
                "on a trusted shell."
            )

        try:
            argv = _split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from exc
        if not argv:
            raise ValueError("Empty command after parse.")

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": argv,
                "shell": False,
                "workdir": workdir,
                "timeout": max(1, min(int(timeout), 600)),
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_run_command",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                extra={"argv": argv, "workdir": workdir or ""},
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=timeout, workdir=workdir)
        result = {
            "success": rc == 0,
            "dry_run": False,
            "owner_mode": True,
            "returncode": rc,
            "argv": argv,
            "stdout": op.redact_output(out),
            "stderr": op.redact_output(err),
        }
        op.audit_record(
            tool="hermes_owner_run_command",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc} argv={argv}",
            error=op.redact_output(err) if rc != 0 else "",
            extra={"workdir": workdir or ""},
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_run_command",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_RUN_COMMAND_ERROR",
                suggested_action="Check owner acknowledgement, command safety, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_owner_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if not old_string:
            raise ValueError("old_string is required.")
        if new_string is None:
            raise ValueError("new_string is required.")

        # Owner mode still denies secret paths.
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env). Owner Mode "
                "does not override this in the current release."
            )

        p = op._normalize_path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            raise ValueError("old_string not found in file.")
        if not replace_all and content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more context "
                "or set replace_all=True."
            )
        if replace_all:
            new_content = content.replace(old_string, new_string)
            match_count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            match_count = 1

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "path": str(p),
                "match_count": match_count,
                "diff": op.unified_diff(content, new_content, label=p.name),
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(p)
        _atomic_write_text(p, new_content)
        result = {
            "success": True,
            "dry_run": False,
            "owner_mode": True,
            "path": str(p),
            "match_count": match_count,
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_owner_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"owner patched {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_patch",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_PATCH_ERROR",
                suggested_action="Check owner acknowledgement, path, old_string/new_string, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_owner_write_file(
    path: str,
    content: str,
    dry_run: bool = True,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        if content is None:
            raise ValueError("content is required.")
        if op.is_denied_path(path):
            raise PermissionError(
                f"Path {path!r} is denied by the operator path safety policy "
                "(secret / credential / vault / token / .env). Owner Mode "
                "does not override this in the current release."
            )

        p = op._normalize_path(path)
        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "path": str(p),
                "exists": p.exists(),
                "content_len": len(content),
                "owner_mode": True,
            }
            op.audit_record(
                tool="hermes_owner_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                path=str(p),
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        backup = _backup_file(p) if p.exists() else None
        _atomic_write_text(p, content)
        result = {
            "success": True,
            "dry_run": False,
            "owner_mode": True,
            "path": str(p),
            "backup": str(backup) if backup else None,
        }
        op.audit_record(
            tool="hermes_owner_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"owner wrote {p.name}",
            path=str(p),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_owner_write_file",
            level="owner",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            path=path,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="owner",
                code="OWNER_WRITE_ERROR",
                suggested_action="Check owner acknowledgement, path, denied-path policy, and operator level/apply mode.",
            ),
            indent=2,
        )
