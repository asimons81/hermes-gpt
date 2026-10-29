"""Bounded asynchronous jobs for continuing existing Hermes sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import operator_policy as op

ENABLE_SESSION_CONTROL_ENV = "HERMES_GPT_ENABLE_SESSION_CONTROL"
MAX_PROMPT_CHARS = 65_536
MAX_RESULT_CHARS = 24_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 3_600
MAX_STREAM_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024

_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
_active_sessions: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    base = op.normalize_hermes_data_root(
        hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    return Path(base or Path.home() / ".hermes") / "session-jobs"


def _paths(job_id: str, hermes_root: Path | None = None) -> tuple[Path, Path]:
    root = _root(hermes_root)
    return root / f"{job_id}.json", root / f"{job_id}.txt"


def _error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(
        layer="session_control", code=code, safe_message=message, suggested_action=action
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return op.redact_output(value)
    return value


def _save(meta: dict[str, Any], hermes_root: Path | None = None) -> None:
    path, _ = _paths(meta["job_id"], hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _load(job_id: str, hermes_root: Path | None = None) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return None
    path, _ = _paths(job_id, hermes_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _hermes_executable(agent_root: Path | None = None) -> str:
    if agent_root:
        candidate = Path(agent_root) / "venv" / ("Scripts" if os.name == "nt" else "bin") / (
            "hermes.exe" if os.name == "nt" else "hermes"
        )
        if candidate.is_file():
            return str(candidate)
    return shutil.which("hermes") or "hermes"


def _validate_start(
    session_id: str, prompt: str, timeout: int, profile: str = "default"
) -> tuple[str, str, int, str] | dict[str, Any]:
    if not op.env_truthy(ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id.strip()) > 256:
        return _error("INVALID_SESSION_ID", "session_id must contain 1 to 256 characters.", "Use an ID returned by hermes_session_list.")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("INVALID_PROMPT", "prompt must not be empty.", "Provide the next instruction for the existing Hermes session.")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _error("PROMPT_TOO_LARGE", f"prompt exceeds the {MAX_PROMPT_CHARS}-character limit.", "Send a shorter prompt.")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return _error("INVALID_TIMEOUT", "timeout must be an integer number of seconds.", f"Choose {MIN_TIMEOUT} to {MAX_TIMEOUT} seconds.")
    try:
        safe_profile = op.validate_profile_name(profile)
    except (ValueError, TypeError):
        return _error("INVALID_PROFILE", "profile is not a valid Hermes profile name.", "Use an authorized profile name.")
    return session_id.strip(), prompt, max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT)), safe_profile


def _reservation(session_id: str, profile: str, root: Path | None) -> Path:
    key = hashlib.sha256(f"{profile}:{session_id}".encode()).hexdigest()
    return _root(root) / f"session-{key}.lock"


def _supports_stream_json(executable: str, env: dict[str, str]) -> bool:
    """Inspect CLI help only; never submit a capability probe."""
    try:
        result = subprocess.run(
            [executable, "chat", "--help"], capture_output=True, text=True, check=False,
            timeout=10, shell=False, env=env, stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0 and "--format" in result.stdout and "stream-json" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _consume(proc, session_id: str, result: dict[str, Any]) -> None:
    """Bounded, in-memory protocol reader. Tool events/diagnostics are never persisted."""
    total = 0
    terminal = None
    try:
        while True:
            line = proc.stdout.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_LINE_BYTES or total > MAX_STREAM_BYTES:
                raise ValueError("OUTPUT_LIMIT")
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                raise ValueError("INVALID_STREAM")  # noqa: TRY004 - protocol failure
            if terminal is not None:
                raise ValueError("AFTER_TERMINAL_RESULT")
            if record["type"] == "result":
                if record.get("session_id") != session_id:
                    raise ValueError("SESSION_MISMATCH")
                if type(record.get("exit_code")) is not int or not isinstance(record.get("text"), str):
                    raise ValueError("INVALID_RESULT")
                terminal = record
        if terminal is None:
            raise ValueError("MISSING_TERMINAL_RESULT")
        result.update(terminal=terminal)
    except (ValueError, OSError, UnicodeError) as exc:
        # Never expose raw parser text (which may contain the original prompt).
        result["error"] = str(exc) if str(exc) in {
            "OUTPUT_LIMIT", "INVALID_STREAM", "AFTER_TERMINAL_RESULT",
            "SESSION_MISMATCH", "INVALID_RESULT", "MISSING_TERMINAL_RESULT",
        } else "INVALID_STREAM"
        _terminate(proc)
    finally:
        proc.stdout.close()


def hermes_session_continue(
    session_id: str,
    prompt: str,
    timeout: int = 900,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
) -> dict[str, Any]:
    """Start one bounded non-interactive turn in an existing Hermes session."""
    checked = _validate_start(session_id, prompt, timeout, profile)
    if isinstance(checked, dict):
        return checked
    safe_id, safe_prompt, safe_timeout, safe_profile = checked
    active_key = f"{safe_profile}:{safe_id}"
    with _lock:
        if active_key in _active_sessions:
            return _error("SESSION_BUSY", "This session has unresolved work.", "Inspect the original job before continuing.")
    executable = _hermes_executable(agent_root)
    child_env = os.environ.copy()
    base_home = Path(hermes_root) if hermes_root is not None else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    child_env["HERMES_HOME"] = str(op.resolve_profile_home(safe_profile, base_home))
    child_env["HERMES_PROFILE"] = safe_profile
    # Never inherit approval bypasses into a finite, attended session turn.
    child_env.pop("HERMES_YOLO_MODE", None)
    if not _supports_stream_json(executable, child_env):
        return _error("SESSION_CLI_UNSUPPORTED", "The Hermes CLI does not advertise finite stream-json output.", "Use a compatible Hermes Agent; no turn was submitted.")
    job_id = uuid4().hex
    guard = _reservation(safe_id, safe_profile, hermes_root)
    meta = {
        "job_id": job_id, "session_id": safe_id, "profile": safe_profile,
        "status": "starting", "created_at": _now(), "started_at": None,
        "ended_at": None, "pid": None, "return_code": None,
        "timeout": safe_timeout, "prompt_len": len(safe_prompt),
        "prompt_sha256": hashlib.sha256(safe_prompt.encode()).hexdigest(),
        "protocol": "stream-json", "outcome_unknown": True, "retry_safe": False,
    }
    try:
        guard.parent.mkdir(parents=True, exist_ok=True)
        with guard.open("x", encoding="utf-8") as handle:
            handle.write(job_id)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return _error("SESSION_BUSY", "This session has a durable unresolved submission.", "Inspect its original session job; do not resubmit after an unknown outcome.")
    except OSError:
        return _error("SESSION_STATE_UNWRITABLE", "Cannot reserve the session.", "Repair local job storage; no turn was submitted.")
    try:
        _save(meta, hermes_root)  # durable intent BEFORE any possible submission
    except OSError:
        guard.unlink(missing_ok=True)
        return _error("SESSION_STATE_UNWRITABLE", "Cannot persist the session job.", "Repair local job storage; no turn was submitted.")
    with _lock:
        _active_sessions[active_key] = job_id
    argv = [executable, "chat", "--resume", safe_id, "-q", safe_prompt, "--format", "stream-json"]
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, shell=False, env=child_env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError):
        meta.update(status="failed", outcome_unknown=False, retry_safe=True, ended_at=_now())
        try:
            _save(meta, hermes_root)
        finally:
            guard.unlink(missing_ok=True)
            with _lock:
                _active_sessions.pop(active_key, None)
        return _error("HERMES_START_FAILED", "Hermes could not start.", "Check the local CLI; no turn was submitted.")
    with _lock:
        _processes[job_id] = proc
    meta.update(status="running", started_at=_now(), pid=proc.pid)
    try:
        _save(meta, hermes_root)
    except OSError:
        _terminate(proc)
        with _lock:
            _processes.pop(job_id, None)
        return {"success": False, "job_id": job_id, "code": "SESSION_STATE_UNWRITABLE", "outcome_unknown": True, "retry_safe": False}
    threading.Thread(target=_watch, args=(job_id, proc, None, safe_timeout, hermes_root), daemon=True).start()
    return {"success": True, "job_id": job_id, "session_id": safe_id, "profile": safe_profile, "status": "running"}


def _watch(job_id: str, proc, output: Any, timeout: int, hermes_root: Path | None) -> None:
    meta = _load(job_id, hermes_root) or {"job_id": job_id}
    session_id = str(meta.get("session_id", ""))
    profile = str(meta.get("profile", "default"))
    parsed: dict[str, Any] = {}
    reader = threading.Thread(target=_consume, args=(proc, session_id, parsed), daemon=True)
    reader.start()
    status = "failed"
    verified = False
    try:
        proc.wait(timeout=timeout)
        reader.join(timeout=3)
        terminal = parsed.get("terminal", {})
        verified = (not reader.is_alive() and not parsed.get("error") and
                    proc.returncode == 0 and terminal.get("exit_code") == 0 and not terminal.get("error"))
        if verified:
            status = "completed"
            _, path = _paths(job_id, hermes_root)
            # Store only the bounded, redacted terminal answer, never JSONL tool bodies.
            text = op.redact_output(terminal["text"])
            path.write_text(text[:MAX_RESULT_CHARS], encoding="utf-8")
            meta["result_truncated"] = len(text) > MAX_RESULT_CHARS
    except subprocess.TimeoutExpired:
        _terminate(proc)
        status = "timed_out"
    except (OSError, ValueError):
        verified = False
        status = "failed"
        parsed["error"] = "RESULT_WRITE_FAILED"
    finally:
        if reader.is_alive():
            _terminate(proc)
            reader.join(timeout=3)
        meta.update(status=status, return_code=proc.poll(), ended_at=_now(),
                    outcome_unknown=not verified, retry_safe=False,
                    result_code="VERIFIED_RESULT" if verified else parsed.get("error", "OUTCOME_UNKNOWN"))
        try:
            _save(meta, hermes_root)
        except OSError:
            verified = False  # leave durable reservation and starting/running intent
        with _lock:
            _processes.pop(job_id, None)
            if verified:
                _active_sessions.pop(f"{profile}:{session_id}", None)
        if verified:
            _reservation(session_id, profile, hermes_root).unlink(missing_ok=True)


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _reconcile(hermes_root: Path | None = None) -> None:
    root = _root(hermes_root)
    if not root.exists():
        return
    with _lock:
        owned = set(_processes)
    for path in root.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job_id = str(meta.get("job_id", ""))
        if meta.get("status") in {"starting", "running"} and job_id not in owned:
            meta.update({
                "status": "orphaned",
                "outcome_unknown": True, "retry_safe": False,
                "ended_at": _now(),
                "reconciliation": "server restarted; process ownership could not be proven",
            })
            _save(meta, hermes_root)


def hermes_session_job_status(job_id: str, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error("JOB_NOT_FOUND", "Hermes session job was not found.", "Check the job ID returned by hermes_session_continue.")
    return _redact({"success": True, "job": meta})


def hermes_session_job_result(job_id: str, max_chars: int = MAX_RESULT_CHARS, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error("JOB_NOT_FOUND", "Hermes session job was not found.", "Check the job ID returned by hermes_session_continue.")
    if isinstance(max_chars, bool):
        return _error("INVALID_MAX_CHARS", "max_chars must be an integer.", f"Choose 500 to {MAX_RESULT_CHARS} characters.")
    try:
        cap = max(500, min(int(max_chars), MAX_RESULT_CHARS))
    except (TypeError, ValueError):
        return _error("INVALID_MAX_CHARS", "max_chars must be an integer.", f"Choose 500 to {MAX_RESULT_CHARS} characters.")
    _, output_path = _paths(job_id, hermes_root)
    try:
        with output_path.open(encoding="utf-8", errors="replace") as handle:
            response = handle.read(MAX_RESULT_CHARS + 1)
    except OSError:
        response = ""
    response = op.redact_output(response)
    truncated = len(response) > cap or bool(meta.get("result_truncated"))
    if meta.get("protocol") == "stream-json" and meta.get("status") != "completed":
        response = ""
    return _redact({
        "success": True,
        "job_id": job_id,
        "session_id": meta.get("session_id"),
        "profile": meta.get("profile", "default"),
        "status": meta.get("status"),
        "return_code": meta.get("return_code"),
        "response": response[:cap],
        "truncated": truncated,
        "outcome_unknown": meta.get("outcome_unknown", False),
        "retry_safe": meta.get("retry_safe", False),
        "result_code": meta.get("result_code"),
    })
