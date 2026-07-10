"""Safe, idempotent Codex MCP configuration and doctor helpers."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import operator_policy as op_policy
from codex_core import CODEX_TOOLSET_ENV, CODEX_TOOLSETS, ENABLE_CODEX_ENV, ENABLE_MCP_ENV, redact_value


SERVER_NAME = "hermes-gpt"
DEFAULT_STARTUP_TIMEOUT = 30

CORE_EXPECTED_TOOLS = {"hermes_status", "hermes_capabilities", "hermes_plan", "hermes_gateway_diagnostics"}
OPERATOR_EXPECTED_TOOLS = CORE_EXPECTED_TOOLS | {
    "hermes_operator_policy", "hermes_operator_status", "hermes_operator_doctor",
    "hermes_operator_cron_list", "hermes_operator_skill_list",
    "hermes_operator_config_get", "hermes_operator_gateway_status",
}


def expected_tools(toolset: str) -> set[str]:
    return set(OPERATOR_EXPECTED_TOOLS if toolset == "operator" else CORE_EXPECTED_TOOLS)


def config_path(*, project: bool = False, cwd: Path | None = None) -> Path:
    if project:
        start = (cwd or Path.cwd()).resolve()
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=start,
                text=True,
                capture_output=True,
                shell=False,
                timeout=5,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                start = Path(completed.stdout.strip()).resolve()
        except (OSError, subprocess.SubprocessError):
            pass
        return start / ".codex" / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # Python < 3.11
    return tomllib.loads(path.read_text(encoding="utf-8"))


def get_server_entry(path: Path, name: str = SERVER_NAME) -> dict[str, Any] | None:
    try:
        item = read_config(path).get("mcp_servers", {}).get(name)
    except Exception:
        return None
    return item if isinstance(item, dict) else None


def launcher_argv(server_path: Path | None = None) -> list[str]:
    return [sys.executable, str((server_path or Path(__file__).with_name("server.py")).resolve()), "mcp"]


def _toml_quote(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_quote(value) for value in values) + "]"


def _render_server_entry(argv: list[str], name: str = SERVER_NAME, toolset: str = "core") -> str:
    quoted_name = _toml_quote(name)
    command, *args = argv
    return (
        f"[mcp_servers.{quoted_name}]\n"
        f"command = {_toml_quote(command)}\n"
        f"args = {_toml_array(args)}\n"
        f"startup_timeout_sec = {DEFAULT_STARTUP_TIMEOUT}\n\n"
        f"[mcp_servers.{quoted_name}.env]\n"
        f"{ENABLE_CODEX_ENV} = \"1\"\n"
        f"{ENABLE_MCP_ENV} = \"1\"\n"
        f"{CODEX_TOOLSET_ENV} = {_toml_quote(toolset)}\n"
    )


def _is_hermes_entry(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    command = str(entry.get("command", "")).lower()
    return isinstance(args, list) and "mcp" in args and ("hermes-gpt" in command or any("server.py" in str(arg).lower() for arg in args))


def _is_named_table(line: str, name: str) -> bool:
    quoted = re.escape(_toml_quote(name))
    bare = re.escape(name)
    return bool(re.match(rf"^\[mcp_servers\.(?:{quoted}|{bare})(?:\.env)?\]\s*$", line.strip()))


def _drop_server_entry(text: str, name: str = SERVER_NAME) -> tuple[str, bool]:
    """Remove only the named server and its env table, preserving other TOML."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removing = False
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if _is_named_table(line, name):
                removing = True
                removed = True
                continue
            removing = False
        if not removing:
            kept.append(line)
    result = "".join(kept)
    result = re.sub(r"\n{3,}", "\n\n", result).rstrip() + ("\n" if result.strip() else "")
    return result, removed


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{index}")
        index += 1
    shutil.copy2(path, backup)
    return backup


def _write_direct(path: Path, argv: list[str], name: str = SERVER_NAME, toolset: str = "core") -> dict[str, Any]:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    without_old, removed = _drop_server_entry(existing, name)
    updated = without_old.rstrip()
    if updated:
        updated += "\n\n"
    updated += _render_server_entry(argv, name, toolset)
    if updated == existing:
        return {"changed": False, "backup": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(path)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    try:
        read_config(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"changed": True, "backup": str(backup) if backup else None, "replaced_existing_entry": removed}


def _run_codex_add(codex: str, name: str, argv: list[str], toolset: str) -> subprocess.CompletedProcess[str]:
    command = [codex, "mcp", "add", name, "--env", f"{ENABLE_CODEX_ENV}=1", "--env", f"{ENABLE_MCP_ENV}=1", "--env", f"{CODEX_TOOLSET_ENV}={toolset}", "--", *argv]
    return subprocess.run(command, text=True, capture_output=True, shell=False, timeout=30)


def install(*, project: bool = False, cwd: Path | None = None, name: str = SERVER_NAME, server_path: Path | None = None,
            prefer_cli: bool = True, toolset: str = "core", refresh: bool = False) -> dict[str, Any]:
    toolset = toolset.strip().lower()
    if toolset not in CODEX_TOOLSETS:
        return {"ok": False, "changed": False, "code": "INVALID_TOOLSET", "message": "toolset must be core or operator."}
    path = config_path(project=project, cwd=cwd)
    argv = launcher_argv(server_path)
    try:
        read_config(path)
    except Exception:
        return {"ok": False, "changed": False, "code": "MALFORMED_CONFIG", "config_path": str(path), "message": "Codex config is not valid TOML; it was not modified."}
    existing = get_server_entry(path, name)
    if existing:
        if _is_hermes_entry(existing):
            configured = str(existing.get("env", {}).get(CODEX_TOOLSET_ENV, "core")).lower()
            expected_argv = launcher_argv(server_path)
            same = configured == toolset and existing.get("command") == expected_argv[0] and existing.get("args") == expected_argv[1:]
            if same:
                return redact_value({"ok": True, "changed": False, "method": "existing", "toolset": configured, "config_path": str(path), "message": f"{name} is already configured with the requested settings.", "entry": existing})
            if not refresh:
                return redact_value({"ok": False, "changed": False, "code": "REFRESH_REQUIRED", "config_path": str(path), "configured_toolset": configured, "requested_toolset": toolset, "message": "The Hermes GPT entry differs from the requested settings; rerun with --refresh."})
            direct = _write_direct(path, argv, name, toolset)
            return redact_value({"ok": True, "method": "toml-refresh", "toolset": toolset, "config_path": str(path), "launcher": argv, **direct})
        return {"ok": False, "changed": False, "code": "NAME_CONFLICT", "config_path": str(path), "message": f"{name} is owned by a different MCP configuration; no changes were made."}

    codex = shutil.which("codex") if prefer_cli and not project else None
    if codex:
        try:
            completed = _run_codex_add(codex, name, argv, toolset)
            if completed.returncode == 0:
                return redact_value({"ok": True, "changed": True, "method": "codex-cli", "config_path": str(path), "command": [codex, "mcp", "add", name], "output": completed.stdout.strip()})
            cli_error = op_policy.redact_output(completed.stderr.strip() or completed.stdout.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            cli_error = op_policy.redact_output(str(exc))
    else:
        cli_error = "Codex CLI unavailable" if not project else "Project install uses safe TOML editing"

    direct = _write_direct(path, argv, name, toolset)
    return redact_value({"ok": True, "method": "toml-fallback", "toolset": toolset, "config_path": str(path), "launcher": argv, "cli_note": cli_error, **direct})


def uninstall(*, project: bool = False, cwd: Path | None = None, name: str = SERVER_NAME) -> dict[str, Any]:
    path = config_path(project=project, cwd=cwd)
    if not path.exists():
        return {"ok": True, "changed": False, "config_path": str(path), "message": "No Codex config file exists."}
    try:
        read_config(path)
    except Exception:
        return {"ok": False, "changed": False, "code": "MALFORMED_CONFIG", "config_path": str(path), "message": "Codex config is not valid TOML; it was not modified."}
    existing = path.read_text(encoding="utf-8")
    entry = get_server_entry(path, name)
    if entry is not None and not _is_hermes_entry(entry):
        return {"ok": False, "changed": False, "code": "NAME_CONFLICT", "config_path": str(path), "message": f"{name} is not a Hermes GPT entry; no changes were made."}
    updated, removed = _drop_server_entry(existing, name)
    if not removed:
        return {"ok": True, "changed": False, "config_path": str(path), "message": f"No {name} entry exists."}
    backup = _backup(path)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    try:
        if updated.strip():
            read_config(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"ok": True, "changed": True, "config_path": str(path), "backup": str(backup) if backup else None, "removed": name}


def print_config(*, project: bool = False, cwd: Path | None = None, name: str = SERVER_NAME) -> dict[str, Any]:
    path = config_path(project=project, cwd=cwd)
    entry = get_server_entry(path, name)
    toolset = str((entry or {}).get("env", {}).get(CODEX_TOOLSET_ENV, "core")).lower()
    return redact_value({"ok": True, "config_path": str(path), "server_name": name, "entry": entry, "present": entry is not None, "toolset": toolset})


def _kill_proc(proc: subprocess.Popen[str]) -> None:
    """Terminate a subprocess with a short timeout, killing on timeout."""
    try:
        proc.terminate()
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=5)
    except Exception:
        proc.kill()


def _readline_with_timeout(stream: Any, seconds: float = 8.0) -> str:
    result: list[str] = []
    worker = threading.Thread(target=lambda: result.append(stream.readline()), daemon=True)
    worker.start()
    worker.join(seconds)
    if not result:
        raise TimeoutError("No MCP stdio response arrived in time.")
    return result[0]


def _mcp_smoke(argv: list[str], toolset: str = "core") -> dict[str, Any]:
    """Initialize a short-lived local stdio server and issue tools/list."""
    _CI_WINDOWS = os.environ.get("CI") and sys.platform == "win32"
    if _CI_WINDOWS:
        return {"status": "SKIP", "detail": "Subprocess stdio tests are skipped on Windows CI (pipe deadlock)."}
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, CODEX_TOOLSET_ENV: toolset},
    )

    def send(payload: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("MCP stdin is unavailable.")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hermes-gpt-doctor", "version": "1"}}})
        initialized = json.loads(_readline_with_timeout(proc.stdout))
        if initialized.get("result", {}).get("serverInfo", {}).get("name") != "hermes-gpt":
            proc.terminate()
            return {"status": "FAIL", "detail": "The MCP launcher returned an unexpected server identity."}
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = json.loads(_readline_with_timeout(proc.stdout))
        names = {item.get("name") for item in listed.get("result", {}).get("tools", [])}
        expected = expected_tools(toolset)
        return {"status": "PASS" if expected.issubset(names) else "FAIL", "tool_count": len(names), "missing": sorted(expected - names)}
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        return {"status": "FAIL", "detail": op_policy.redact_output(str(exc))}
    finally:
        _kill_proc(proc)


def doctor(*, project: bool = False, cwd: Path | None = None, list_tools: Callable[[], list[str]] | None = None, status: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    path = config_path(project=project, cwd=cwd)
    entry = get_server_entry(path)
    toolset = str((entry or {}).get("env", {}).get(CODEX_TOOLSET_ENV, "core")).lower()
    valid_toolset = toolset in CODEX_TOOLSETS
    codex = shutil.which("codex")
    checks: dict[str, Any] = {
        "codex_binary": {"status": "PASS" if codex else "WARN", "path": codex},
        "codex_config": {"status": "PASS" if path.exists() else "WARN", "path": str(path)},
        "hermes_gpt_entry": {"status": "PASS" if entry and _is_hermes_entry(entry) else "WARN"},
        "toolset": {"status": "PASS" if valid_toolset else "FAIL", "configured": toolset, "available": list(CODEX_TOOLSETS)},
        "env_gates": {"status": "PASS" if os.environ.get(ENABLE_CODEX_ENV) == "1" and os.environ.get(ENABLE_MCP_ENV) == "1" else "WARN", "required": [ENABLE_CODEX_ENV, ENABLE_MCP_ENV]},
    }
    if codex:
        try:
            result = subprocess.run([codex, "--version"], text=True, capture_output=True, shell=False, timeout=10)
            checks["codex_version"] = {"status": "PASS" if result.returncode == 0 else "WARN", "value": op_policy.redact_output(result.stdout.strip())}
        except (OSError, subprocess.SubprocessError) as exc:
            checks["codex_version"] = {"status": "WARN", "detail": op_policy.redact_output(str(exc))}
    if list_tools:
        try:
            tools = list_tools()
            expected = expected_tools(toolset if valid_toolset else "core")
            checks["mcp_tool_registry"] = {"status": "PASS" if expected.issubset(set(tools)) else "FAIL", "tool_count": len(tools), "missing": sorted(expected - set(tools))}
        except Exception:
            checks["mcp_tool_registry"] = {"status": "FAIL", "detail": "Could not list the local MCP tool registry."}
    checks["mcp_stdio_smoke"] = _mcp_smoke(launcher_argv(), toolset if valid_toolset else "core")
    redaction = op_policy.redact_output("token=secret-token-123456789")
    checks["redaction_smoke"] = {"status": "PASS" if "secret-token" not in redaction else "FAIL"}
    if status:
        try:
            snapshot = status()
            checks["gateway"] = {"status": "PASS" if snapshot.get("ok") else "WARN", "gateway": snapshot.get("gateway")}
        except Exception:
            checks["gateway"] = {"status": "WARN", "detail": "Gateway status could not be checked."}
    overall = "PASS" if all(item.get("status") == "PASS" for item in checks.values()) else "WARN"
    return redact_value({"ok": overall == "PASS", "overall": overall, "checks": checks, "suggested_action": "Set the reported gates, run hermes-gpt codex install, then restart Codex." if overall != "PASS" else "Codex MCP integration is ready."})


def main(argv: list[str], *, list_tools: Callable[[], list[str]] | None = None, status: Callable[[], dict[str, Any]] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hermes-gpt codex", description="Install and diagnose the Hermes GPT Codex MCP connector.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("install", "uninstall", "doctor", "print-config"):
        child = subparsers.add_parser(command)
        child.add_argument("--project", action="store_true", help="Use .codex/config.toml in the current project.")
        child.add_argument("--global", dest="project", action="store_false", help="Use ~/.codex/config.toml (default).")
        if command == "install":
            child.add_argument("--toolset", choices=CODEX_TOOLSETS, default="core")
            child.add_argument("--refresh", action="store_true", help="Replace only an existing Hermes GPT entry after creating a backup.")
    subparsers.add_parser("mcp", help="Run the Codex-focused MCP stdio server.")
    args = parser.parse_args(argv)
    if args.command == "mcp":
        raise RuntimeError("The mcp command is dispatched by server.py.")
    operation = {"install": install, "uninstall": uninstall, "doctor": doctor, "print-config": print_config}[args.command]
    kwargs: dict[str, Any] = {"project": args.project}
    if operation is install:
        kwargs.update({"toolset": args.toolset, "refresh": args.refresh})
    if operation is doctor:
        kwargs.update({"list_tools": list_tools, "status": status})
    print(json.dumps(operation(**kwargs), indent=2, default=str))
