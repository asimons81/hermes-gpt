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
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import operator_policy as op


ENABLE_SESSION_CONTROL_ENV = "HERMES_GPT_ENABLE_SESSION_CONTROL"
MAX_PROMPT_CHARS = 65_536
MAX_RESULT_CHARS = 24_000
MIN_JOB_RUNTIME_SECONDS = 10
MAX_JOB_RUNTIME_SECONDS = 7_200
MAX_JOB_WAIT_SECONDS = 120
SESSION_CREATE_SOURCE = "hermes-gpt"
MAX_SESSION_TITLE_CHARS = 200
_SESSION_TERMINAL_STATES = frozenset({"completed", "failed", "timed_out", "orphaned"})

# Injected by the server (require_imports) so this module stays unit-testable.
# Overridable in tests via monkeypatch.setattr(session, "SessionDB", fake).
SessionDB: Any = None

# -- Enveloppe MCP : budget en octets de la reponse complete (correction troncature) --
# La serilisation JSON Python (défaut ensure_ascii=True) echappe les accents (\uXXXX,
# x6) ; ce budget borne la réponse MCP COMPLETE sous cette representation reelle.
MAX_MCP_RESULT_BYTES = 16_384  # 16 KiB — conservateur, sous toute limite observee (OpenAI inconnue)
MCP_RESULT_BUDGET_ENV = "HERMES_GPT_MCP_RESULT_BUDGET_BYTES"
_MCP_OVERHEAD = 1024  # marge pour les metadonnees annexes (offset, original_*, budget_*)


def _mcp_result_budget() -> int:
    raw = os.environ.get(MCP_RESULT_BUDGET_ENV, "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return MAX_MCP_RESULT_BYTES


def _env_bytes_size(value: dict[str, Any]) -> int:
    """Taille JSON transportee par defaut (json.dumps ensure_ascii=True)."""
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _utf8_slice(text: str, start: int, max_bytes: int) -> tuple[str, int, int]:
    """Tranche de `text` par octets UTF-8, sans couper un caractere :
    retourne (sous-chaine, offset_debut_ajuste, offset_fin)."""
    data = text.encode("utf-8")
    total = len(data)
    start = max(0, min(int(start), total))
    while start < total and (data[start] & 0xC0) == 0x80:
        start += 1  # avancer jusqu'au debut d'un caractere
    end = min(start + max(0, int(max_bytes)), total)
    while end > start:
        try:
            data[start:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    return data[start:end].decode("utf-8"), start, end


def _bound_result_for_mcp(meta: dict[str, Any], response: str, budget: int) -> tuple[str, bool]:
    """Reduit `response` (slicing UTF-8 safe) pour que l'enveloppe MCP complete
    tienne sous `budget`. Retourne (response_bornee, limite_par_budget)."""
    def size_for(n: int) -> int:
        d = dict(meta)
        d["response"] = response[:n]
        return _env_bytes_size(d)

    if size_for(len(response)) <= budget:
        return response, False
    lo, hi = 0, len(response)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if size_for(mid) <= budget:
            lo = mid
        else:
            hi = mid - 1
    d = dict(meta)
    d["response"] = response[:lo]
    while lo > 0 and _env_bytes_size(d) > budget:
        lo -= 1
        d["response"] = response[:lo]
    return response[:lo], True

_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
_active_sessions: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hermes_data_root(hermes_root: Path | None = None) -> Path:
    """Return the normalized Hermes data root (not the agent source root)."""
    base = op.normalize_hermes_data_root(
        hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    return Path(base) if base else Path.home() / ".hermes"


def _root(hermes_root: Path | None = None) -> Path:
    return _hermes_data_root(hermes_root) / "session-jobs"


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


def _validate_start(
    session_id: str, prompt: str, max_job_runtime_seconds: int, profile: str = "default"
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
    if isinstance(max_job_runtime_seconds, bool) or not isinstance(max_job_runtime_seconds, int):
        return _error(
            "INVALID_MAX_JOB_RUNTIME_SECONDS",
            "max_job_runtime_seconds must be an integer number of seconds.",
            f"Choose {MIN_JOB_RUNTIME_SECONDS} to {MAX_JOB_RUNTIME_SECONDS} seconds.",
        )
    try:
        safe_profile = op.validate_profile_name(profile)
    except Exception:
        return _error("INVALID_PROFILE", "profile is not a valid Hermes profile name.", "Use an authorized profile name.")
    return session_id.strip(), prompt, max(MIN_JOB_RUNTIME_SECONDS, min(max_job_runtime_seconds, MAX_JOB_RUNTIME_SECONDS)), safe_profile


def hermes_session_continue(
    session_id: str,
    prompt: str,
    max_job_runtime_seconds: int = MAX_JOB_RUNTIME_SECONDS,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
) -> dict[str, Any]:
    """Start one bounded non-interactive turn in an existing Hermes session."""
    checked = _validate_start(session_id, prompt, max_job_runtime_seconds, profile)
    if isinstance(checked, dict):
        return checked
    safe_id, safe_prompt, safe_timeout, safe_profile = checked
    return _start_job(safe_id, safe_prompt, safe_timeout, safe_profile, hermes_root, agent_root)


def _start_job(
    safe_id: str,
    safe_prompt: str,
    safe_timeout: int,
    safe_profile: str,
    hermes_root: Path | None,
    agent_root: Path | None,
) -> dict[str, Any]:
    """Register and launch one bounded non-interactive Hermes turn as a job.

    Shared by :func:`hermes_session_continue` (existing session) and
    :func:`hermes_session_create` (newly created session). The caller has
    already validated inputs and, for create, persisted the session.
    """
    argv = [_hermes_executable(agent_root), "--resume", safe_id, "--oneshot", safe_prompt]
    active_key = f"{safe_profile}:{safe_id}"
    job_id = uuid4().hex
    meta = {
        "job_id": job_id,
        "session_id": safe_id,
        "profile": safe_profile,
        "status": "starting",
        "created_at": _now(),
        "started_at": None,
        "ended_at": None,
        "pid": None,
        "return_code": None,
        "timeout": safe_timeout,
        "prompt_len": len(safe_prompt),
        "prompt_sha256": hashlib.sha256(safe_prompt.encode("utf-8")).hexdigest(),
        "max_job_runtime_seconds": safe_timeout,
    }
    _, output_path = _paths(job_id, hermes_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = open(output_path, "w", encoding="utf-8")
    with _lock:
        active_job = _active_sessions.get(active_key)
        if active_job:
            output.close()
            output_path.unlink(missing_ok=True)
            return _error(
                "SESSION_BUSY",
                "This Hermes session already has a running session-control job.",
                f"Wait for job {active_job} to finish before sending another turn.",
            )
        _active_sessions[active_key] = job_id
    child_env = os.environ.copy()
    base_home = (
        Path(hermes_root)
        if hermes_root is not None
        else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    profile_home = op.resolve_profile_home(safe_profile, base_home)
    child_env["HERMES_HOME"] = str(profile_home)
    child_env["HERMES_PROFILE"] = safe_profile
    try:
        proc = subprocess.Popen(
            argv,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
            env=child_env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as exc:
        output.close()
        output_path.unlink(missing_ok=True)
        with _lock:
            if _active_sessions.get(active_key) == job_id:
                _active_sessions.pop(active_key, None)
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
    return _redact({
        "success": True,
        "job_id": job_id,
        "session_id": safe_id,
        "profile": safe_profile,
        "status": "running",
    })


def _validate_create(
    prompt: str, max_job_runtime_seconds: int, profile: str, title: str | None = None
) -> tuple[str, int, str, str | None] | dict[str, Any]:
    """Validate inputs for :func:`hermes_session_create` (no existing session id).

    Mirrors the session-continue validation for the shared fields (prompt,
    runtime, profile) and adds an optional bounded title. ``profile`` is
    restricted through :func:`operator_policy.validate_profile_name` exactly
    as the existing session tools — no arbitrary profile name is accepted.
    """
    if not op.env_truthy(ENABLE_SESSION_CONTROL_ENV):
        return _error(
            "SESSION_CONTROL_DISABLED",
            "Hermes session control is disabled.",
            f"Set {ENABLE_SESSION_CONTROL_ENV}=1 on the trusted local MCP server.",
        )
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("INVALID_PROMPT", "prompt must not be empty.", "Provide the first instruction for the new Hermes session.")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _error("PROMPT_TOO_LARGE", f"prompt exceeds the {MAX_PROMPT_CHARS}-character limit.", "Send a shorter prompt.")
    if isinstance(max_job_runtime_seconds, bool) or not isinstance(max_job_runtime_seconds, int):
        return _error(
            "INVALID_MAX_JOB_RUNTIME_SECONDS",
            "max_job_runtime_seconds must be an integer number of seconds.",
            f"Choose {MIN_JOB_RUNTIME_SECONDS} to {MAX_JOB_RUNTIME_SECONDS} seconds.",
        )
    try:
        safe_profile = op.validate_profile_name(profile)
    except Exception:
        return _error("INVALID_PROFILE", "profile is not a valid Hermes profile name.", "Use an authorized profile name.")
    safe_title = None
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            return _error("INVALID_TITLE", "title must be a non-empty string when provided.", "Omit title or provide a non-empty value.")
        if len(title) > MAX_SESSION_TITLE_CHARS:
            return _error("INVALID_TITLE", f"title exceeds the {MAX_SESSION_TITLE_CHARS}-character limit.", "Send a shorter title.")
        safe_title = title.strip()
    return (
        prompt.strip(),
        max(MIN_JOB_RUNTIME_SECONDS, min(max_job_runtime_seconds, MAX_JOB_RUNTIME_SECONDS)),
        safe_profile,
        safe_title,
    )


def _new_session_id() -> str:
    """Return a fresh session id in the CLI's ``{YYYYmmdd_HHMMSS}_{uuid6}`` shape."""
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _create_session_in_db(
    session_id: str, profile: str, title: str | None, hermes_root: Path | None = None
) -> bool:
    """Create a fresh Hermes session row (and optional title) in the profile store.

    Uses the real ``hermes_state.SessionDB`` write surface (the same one the
    CLI uses for ``chat -c <title> --create-if-missing``). ``SessionDB`` is
    injected at module level by the server; tests override it.
    """
    if SessionDB is None:
        raise RuntimeError("Hermes session database is unavailable: SessionDB is not injected.")
    profile_home = op.resolve_profile_home(profile, _hermes_data_root(hermes_root))
    db = SessionDB(db_path=str(profile_home / "state.db"), read_only=False)
    try:
        db.create_session(session_id, source=SESSION_CREATE_SOURCE)
        if title:
            db.set_session_title(session_id, title)
    finally:
        db.close()
    return True


def hermes_session_create(
    prompt: str,
    max_job_runtime_seconds: int = MAX_JOB_RUNTIME_SECONDS,
    *,
    hermes_root: Path | None = None,
    agent_root: Path | None = None,
    profile: str = "default",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a new Hermes session and start its first work asynchronously.

    Creates a genuinely new, distinct Hermes session in the target profile,
    then reuses the exact session-control job machinery (:func:`_start_job`)
    to run the first prompt. Returns immediately with ``success``,
    ``job_id``, ``session_id``, ``profile`` and ``status``; the first work is
    followed with :func:`hermes_session_job_wait` then
    :func:`hermes_session_job_result`.
    """
    checked = _validate_create(prompt, max_job_runtime_seconds, profile, title)
    if isinstance(checked, dict):
        return checked
    safe_prompt, safe_timeout, safe_profile, safe_title = checked
    new_session_id = _new_session_id()
    try:
        if not _create_session_in_db(new_session_id, safe_profile, safe_title, hermes_root):
            return _error(
                "SESSION_CREATE_FAILED",
                "The new Hermes session could not be persisted.",
                "Check the Hermes session database and profile.",
            )
    except Exception as exc:
        return _error(
            "SESSION_CREATE_FAILED",
            op.redact_output(str(exc)),
            "Check the Hermes session database and profile.",
        )
    return _start_job(new_session_id, safe_prompt, safe_timeout, safe_profile, hermes_root, agent_root)


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
    profile = str(meta.get("profile", "default") or "default")
    active_key = f"{profile}:{session_id}"
    with _lock:
        if _active_sessions.get(active_key) == job_id:
            _active_sessions.pop(active_key, None)
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


def _load_result_text(job_id: str, hermes_root: Path | None) -> str:
    _, output_path = _paths(job_id, hermes_root)
    try:
        return output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
    text = op.redact_output(_load_result_text(job_id, hermes_root))
    original_chars = len(text)
    original_bytes = len(text.encode("utf-8"))
    budget = _mcp_result_budget()
    include = {
        "success": True,
        "job_id": job_id,
        "session_id": meta.get("session_id"),
        "profile": meta.get("profile", "default"),
        "status": meta.get("status"),
        "return_code": meta.get("return_code"),
    }
    char_limited = len(text) > cap
    preview, budget_limited = _bound_result_for_mcp(include, (text[:cap] if char_limited else text), budget - _MCP_OVERHEAD)
    preview_bytes = len(preview.encode("utf-8"))
    if char_limited and not budget_limited:
        why = "max_chars"
    elif budget_limited:
        why = "budget"
    else:
        why = None
    return _redact({
        **include,
        "response": preview,
        "truncated": char_limited or budget_limited,
        "truncated_by": why,
        "original_chars": original_chars,
        "original_bytes": original_bytes,
        "offset": 0,
        "next_offset": preview_bytes,
        "bytes_returned": preview_bytes,
        "budget_bytes": budget,
    })


def hermes_session_job_result_page(
    job_id: str, offset: int = 0, max_bytes: int = 4096, hermes_root: Path | None = None
) -> dict[str, Any]:
    """Page suivante du resultat (par octets UTF-8, sans couper un caractere).

    Déterministe/idempotente : meme offset -> meme page. Continuez depuis
    job_result.next_offset / page.end_offset. L'enveloppe tient sous budget.
    """
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    if not meta:
        return _error("JOB_NOT_FOUND", "Hermes session job was not found.", "Check the job ID returned by hermes_session_continue.")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return _error("INVALID_MAX_BYTES", "max_bytes must be a positive integer.", f"Choose 64 to {MAX_MCP_RESULT_BYTES} bytes.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("INVALID_OFFSET", "offset must be a non-negative integer.", "Use job_result.next_offset / a previous page end_offset.")
    text = op.redact_output(_load_result_text(job_id, hermes_root))
    total = len(text.encode("utf-8"))
    budget = _mcp_result_budget()
    include = {
        "success": True,
        "job_id": job_id,
        "session_id": meta.get("session_id"),
        "profile": meta.get("profile", "default"),
        "status": meta.get("status"),
        "return_code": meta.get("return_code"),
    }
    want = min(max_bytes, max(budget - _MCP_OVERHEAD - 4096, 64))
    chunk, start, end = _utf8_slice(text, offset, want)
    while _env_bytes_size({**include, "offset": start, "end_offset": end, "response": chunk}) > (budget - _MCP_OVERHEAD) and want > 64:
        want = max(want // 2, 64)
        chunk, start, end = _utf8_slice(text, offset, want)
    eof = end >= total
    return _redact({
        **include,
        "response": chunk,
        "offset": start,
        "end_offset": end,
        "bytes_read": end - start,
        "chars_read": len(chunk),
        "original_bytes": total,
        "eof": eof,
        "truncated": not eof,
        "budget_bytes": budget,
    })


def hermes_session_job_wait(
    job_id: str,
    wait_seconds: int = MAX_JOB_WAIT_SECONDS,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Long-poll a Hermes session-control job to terminal state (max 120s).

    Returns early when the job reaches a terminal state (``completed``,
    ``failed``, ``timed_out``, or ``orphaned``) or when the bounded wait
    window elapses. The response mirrors :func:`hermes_session_job_status`
    plus an ``await`` block describing the wait outcome. Read-only: no
    operator level required (same gate as status/result).
    """
    try:
        seconds = max(0, min(int(wait_seconds or 0), MAX_JOB_WAIT_SECONDS))
    except (TypeError, ValueError):
        seconds = MAX_JOB_WAIT_SECONDS
    _reconcile(hermes_root)
    started = time.monotonic()
    deadline = started + seconds
    while True:
        meta = _load(job_id, hermes_root)
        if not meta:
            return _error(
                "JOB_NOT_FOUND",
                "Hermes session job was not found.",
                "Check the job ID returned by hermes_session_continue.",
            )
        status = str(meta.get("status") or "").lower()
        if status in _SESSION_TERMINAL_STATES:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    terminal = str((meta or {}).get("status") or "").lower() in _SESSION_TERMINAL_STATES
    return _redact({
        "success": True,
        "job_id": job_id,
        "session_id": (meta or {}).get("session_id"),
        "profile": (meta or {}).get("profile", "default"),
        "status": (meta or {}).get("status"),
        "return_code": (meta or {}).get("return_code"),
        "await": {
            "timed_out": not terminal,
            "wait_seconds": seconds,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        },
    })
