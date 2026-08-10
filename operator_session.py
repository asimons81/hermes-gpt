"""Bounded asynchronous jobs for continuing existing Hermes sessions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import operator_policy as op


ENABLE_SESSION_CONTROL_ENV = "HERMES_GPT_ENABLE_SESSION_CONTROL"
MAX_PROMPT_CHARS = 65_536
MAX_RESULT_CHARS = 24_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 3_600

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
    temp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
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


def _validate_start(session_id: str, prompt: str, timeout: int) -> tuple[str, str, int] | dict[str, Any]:
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
    return session_id.strip(), prompt, max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))


def hermes_session_continue(
    session_id: str,
    prompt: str,
    timeout: int = 900,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Start one bounded non-interactive turn in an existing Hermes session."""
    checked = _validate_start(session_id, prompt, timeout)
    if isinstance(checked, dict):
        return checked
    safe_id, safe_prompt, safe_timeout = checked
    argv = [_hermes_executable(agent_root), "--resume", safe_id, "--oneshot", safe_prompt]
    job_id = uuid4().hex
    meta = {
        "job_id": job_id,
        "session_id": safe_id,
        "status": "starting",
        "created_at": _now(),
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "return_code": None,
        "timeout": safe_timeout,
        "prompt_len": len(safe_prompt),
        "prompt_sha256": hashlib.sha256(safe_prompt.encode("utf-8")).hexdigest(),
    }
    _, output_path = _paths(job_id, hermes_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = open(output_path, "w", encoding="utf-8")
    with _lock:
        active_job = _active_sessions.get(safe_id)
        if active_job:
            output.close()
            output_path.unlink(missing_ok=True)
            return _error(
                "SESSION_BUSY",
                "This Hermes session already has a running session-control job.",
                f"Wait for job {active_job} to finish before sending another turn.",
            )
        _active_sessions[safe_id] = job_id
    try:
        proc = subprocess.Popen(
            argv,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as exc:
        output.close()
        output_path.unlink(missing_ok=True)
        with _lock:
            if _active_sessions.get(safe_id) == job_id:
                _active_sessions.pop(safe_id, None)
        return _error(
            "HERMES_START_FAILED",
            op.redact_output(str(exc)),
            "Check the Hermes CLI installation, provider authentication, and session ID.",
        )
    meta.update({"status": "running", "started_at": _now(), "pid": proc.pid})
    _save(meta, hermes_root)
    with _lock:
        _processes[job_id] = proc
    threading.Thread(
        target=_watch,
        args=(job_id, proc, output, safe_timeout, hermes_root),
        daemon=True,
    ).start()
    return _redact({"success": True, "job_id": job_id, "session_id": safe_id, "status": "running"})


def _watch(job_id: str, proc: subprocess.Popen[str], output: Any, timeout: int, hermes_root: Path | None) -> None:
    try:
        proc.wait(timeout=timeout)
        status = "completed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        _terminate(proc)
        status = "timed_out"
    finally:
        output.close()
    with _lock:
        _processes.pop(job_id, None)
    meta = _load(job_id, hermes_root) or {"job_id": job_id}
    session_id = str(meta.get("session_id", ""))
    with _lock:
        if _active_sessions.get(session_id) == job_id:
            _active_sessions.pop(session_id, None)
    meta.update({"status": status, "return_code": proc.poll(), "ended_at": _now()})
    _save(meta, hermes_root)


def _terminate(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=3)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=3)
    except Exception:
        proc.kill()


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
        if meta.get("status") == "running" and job_id not in owned:
            meta.update({
                "status": "orphaned",
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
        response = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        response = ""
    response = op.redact_output(response)
    truncated = len(response) > cap
    return _redact({
        "success": True,
        "job_id": job_id,
        "session_id": meta.get("session_id"),
        "status": meta.get("status"),
        "return_code": meta.get("return_code"),
        "response": response[:cap],
        "truncated": truncated,
    })
