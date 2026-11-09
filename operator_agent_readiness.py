"""Read-only source/CLI metadata and prior authorized preflight observations.

Readiness never executes installed Agent code. Authorized session control
uses the bounded help helper and records only its closed result in memory.
The source root comes from the running server; source text is never imported.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import operator_policy as op
from versioning import VERSION

PROBE_TIMEOUT_SECONDS = 10
MAX_PROBE_BYTES = 65_536
_preflight_lock = threading.Lock()
_preflights: dict[tuple[Any, ...], dict[str, Any]] = {}
_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    "PYTHONUTF8", "HERMES_HOME", "HERMES_PROFILE",
})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def probe_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Do not forward provider credentials or approval bypasses to help."""
    values = os.environ if env is None else env
    return {key: value for key, value in values.items() if key.upper() in _ENV_KEYS}


def resolve_cli(agent_root: Path | None = None) -> tuple[str, str]:
    """The same source-venv-first selection used for session continuation."""
    if agent_root is not None:
        candidate = Path(agent_root) / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "hermes.exe" if os.name == "nt" else "hermes"
        )
        if candidate.is_file():
            return str(candidate), "agent_venv"
    executable = shutil.which("hermes")
    return (executable, "path") if executable else ("hermes", "unavailable")


def _kill(proc: Any) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _bounded_command(argv: list[str], env: dict[str, str]) -> tuple[str, bytes]:
    """Bound memory and wall time even if a child keeps the stdout pipe open."""
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, shell=False, env=probe_environment(env),
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError:
        return "CLI_MISSING", b""
    except OSError:
        return "PROBE_FAILED", b""
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    code = "PROBE_FAILED"
    output = bytearray()
    eof = False
    try:
        if os.name != "nt":
            os.set_blocking(proc.stdout.fileno(), False)
        while time.monotonic() < deadline:
            if not eof:
                chunk = _read_available(proc.stdout, min(4096, MAX_PROBE_BYTES + 1 - len(output)))
                if chunk is not None:
                    output.extend(chunk)
                    eof = not chunk
                if len(output) > MAX_PROBE_BYTES:
                    code = "OUTPUT_LIMIT"
                    break
                # Drain buffered output promptly, still checking the deadline.
                if chunk:
                    continue
            if eof and proc.poll() is not None:
                code = "OK" if proc.returncode == 0 else "PROBE_FAILED"
                break
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        else:
            code = "PROBE_TIMEOUT"
    except (OSError, ValueError):
        code = "PROBE_FAILED"
    finally:
        if code != "OK":
            _kill(proc)
        # No reader owns this descriptor, even when an escaped child still
        # holds its inherited write end. Closing is bounded on every platform.
        proc.stdout.close()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _kill(proc)
    return code, bytes(output) if code == "OK" else b""


def _read_available(pipe: Any, limit: int) -> bytes | None:
    """Read available bytes without an uncancellable pipe-reader thread.

    Windows Python 3.10/3.11 cannot make anonymous pipes nonblocking; query
    the pipe's byte count first instead. A broken pipe means EOF.
    """
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        available = wintypes.DWORD()
        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(pipe.fileno())
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in {109, 232}:  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
                return b""
            raise OSError(error, "pipe observation failed")
        if available.value == 0:
            return None
        return os.read(pipe.fileno(), min(limit, available.value))
    try:
        return os.read(pipe.fileno(), limit)
    except BlockingIOError:
        return None


def probe_stream_json(executable: str, env: dict[str, str] | None = None) -> dict[str, str]:
    code, output = _bounded_command([executable, "chat", "--help"], probe_environment(env))
    if code != "OK":
        return {"state": "unavailable", "code": code, "checked_at": _now()}
    supported = b"--format" in output and b"stream-json" in output
    return {
        "state": "supported" if supported else "unsupported",
        "code": "SUPPORTED" if supported else "FORMAT_NOT_ADVERTISED",
        "checked_at": _now(),
    }


def observe_session_preflight(executable: str, env: dict[str, str]) -> dict[str, Any]:
    """Called only from the existing authorized session-submission preflight."""
    key = _stamp(Path(executable))
    result = probe_stream_json(executable, env)
    observation = {**result, "provenance": "authorized_session_preflight"}
    # The executable may have changed while help was running. Do not retain an
    # observation under an identity it no longer describes.
    if _stamp(Path(executable)) == key:
        with _preflight_lock:
            if len(_preflights) >= 16:
                _preflights.clear()
            _preflights[key] = observation
    return observation


def _last_preflight(executable: str) -> dict[str, Any]:
    with _preflight_lock:
        observation = _preflights.get(_stamp(Path(executable)))
        return dict(observation) if observation is not None else {
            "state": "not_observed", "code": "NOT_OBSERVED", "checked_at": None,
            "provenance": "not_observed",
        }


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path.resolve())).hexdigest()


def _stamp(path: Path) -> tuple[Any, ...]:
    try:
        value = path.stat()
        return str(path.resolve()), value.st_dev, value.st_ino, value.st_ctime_ns, value.st_mtime_ns, value.st_size
    except OSError:
        return str(path), None, None


def _revision(root: Path) -> str | None:
    # No Git executable/config hooks. Worktree indirection is deliberately
    # unknown rather than following an arbitrary gitdir location.
    git = root / ".git"
    if not git.is_dir() or git.is_symlink():
        return None
    try:
        value = _read_confined(git, git / "HEAD", 1024).strip()
        if value.startswith(b"ref: "):
            ref = value[5:].decode("ascii")
            if not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref) or ".." in ref.split("/"):
                return None
            value = _read_confined(git, git / ref, 1024).strip()
        return value.decode("ascii") if re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", value) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _read_confined(root: Path, path: Path, limit: int) -> bytes:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("unavailable source metadata")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(resolved, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("unavailable source metadata")
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError("source metadata exceeds bound")
    return value


def source_protocol(root: Path | None) -> dict[str, Any]:
    """Recognize a bounded literal parser declaration, without running it."""
    result: dict[str, Any] = {"state": "unavailable", "code": "SOURCE_UNAVAILABLE", "parser_sha256": None, "checked_at": _now()}
    if root is None:
        return result
    try:
        content = _read_confined(root, root / "hermes_cli" / "_parser.py", MAX_PROBE_BYTES)
    except OSError:
        return result
    except ValueError:
        result["code"] = "SOURCE_UNREADABLE"
        return result
    result["parser_sha256"] = hashlib.sha256(content).hexdigest()
    try:
        tree = ast.parse(content)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_build_chat_parser"]
        if len(functions) != 1:
            raise ValueError("unknown parser shape")
        function = functions[0]
        for name in ("chat_parser", "add"):
            bindings = [node for node in ast.walk(function) if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))]
            if len(bindings) != 1 or isinstance(bindings[0].ctx, ast.Del):
                raise ValueError("ambiguous parser binding")
        has_chat_parser = False
        has_add_alias = False
        binding_positions = []
        chat_position = -1
        add_position = -1
        for position, node in enumerate(function.body):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and target.id == "chat_parser" and isinstance(value, ast.Call):
                has_chat_parser = (
                    isinstance(value.func, ast.Attribute) and value.func.attr == "add_parser"
                    and isinstance(value.func.value, ast.Name) and value.func.value.id == "subparsers"
                    and bool(value.args) and isinstance(value.args[0], ast.Constant) and value.args[0].value == "chat"
                )
                binding_positions.append(position)
                chat_position = position
            pairs = zip(target.elts, value.elts) if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) else [(target, value)]
            for name, attr in pairs:
                if isinstance(name, ast.Name) and name.id == "add" and isinstance(attr, ast.Attribute):
                    has_add_alias = attr.attr == "add_argument" and isinstance(attr.value, ast.Name) and attr.value.id == "chat_parser"
                    binding_positions.append(position)
                    add_position = position
        if not has_chat_parser or not has_add_alias or chat_position >= add_position:
            raise ValueError("unknown parser shape")
        declarations = []
        for position, statement in enumerate(function.body):
            node = statement.value if isinstance(statement, ast.Expr) else None
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "add":
                continue
            if not any(isinstance(arg, ast.Constant) and arg.value == "--format" for arg in node.args):
                continue
            if position <= max(binding_positions):
                raise ValueError("declaration before parser binding")
            choices = next((keyword.value for keyword in node.keywords if keyword.arg == "choices"), None)
            if not isinstance(choices, (ast.List, ast.Tuple)) or not all(isinstance(choice, ast.Constant) and isinstance(choice.value, str) for choice in choices.elts):
                raise ValueError("dynamic format choices")
            declarations.append("stream-json" in [choice.value for choice in choices.elts])
        if len(declarations) > 1:
            raise ValueError("ambiguous declarations")
        declared = declarations == [True]
        result.update(state="declared" if declared else "not_declared", code="DECLARED" if declared else "NOT_DECLARED")
    except (SyntaxError, ValueError, RecursionError, UnicodeError):
        result["code"] = "SOURCE_FORMAT_UNKNOWN"
    return result


def readiness(agent_root: Path | None = None) -> dict[str, Any]:
    """Read local metadata/source plus prior preflight; execute nothing."""
    root = Path(agent_root) if agent_root is not None else None
    source_available = root is not None and root.is_dir()
    executable, resolution = resolve_cli(root)
    cli_path = Path(executable)
    cli_available = resolution != "unavailable"
    # A symlink resolving outside the selected source is a mismatch even when
    # its lexical name is inside that source's venv.
    matches = None
    if source_available and cli_available:
        matches = cli_path.resolve().is_relative_to(root.resolve())
    observation = {
        "agent_source": {
            "state": "available" if source_available else "unavailable",
            "identity_sha256": _fingerprint(root) if source_available else None,
            "revision": _revision(root) if source_available else None,
        },
        "cli": {
            "state": "available" if cli_available else "missing",
            "resolution": resolution,
            "identity_sha256": _fingerprint(cli_path) if cli_available else None,
            "matches_agent_source": matches,
        },
        "source_protocol": source_protocol(root),
    }
    return {
        "schema_version": 1, "read_only": True,
        "checked_at": observation["source_protocol"]["checked_at"],
        "hermes_gpt_version": VERSION,
        **observation,
        "finite_protocol": _last_preflight(executable) if cli_available else {
            "state": "not_observed", "code": "NOT_OBSERVED", "checked_at": None, "provenance": "not_observed",
        },
        "cache_seconds": 0,
        "session_control_enabled": op.env_truthy("HERMES_GPT_ENABLE_SESSION_CONTROL"),
        "live_acceptance": "not_verified",
    }
