"""Asynchronous, policy-gated Codex CLI jobs for trusted Operator clients."""

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
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

import operator_policy as op

ENABLE_CODEX_RUNNER_ENV = "HERMES_GPT_ENABLE_CODEX_RUNNER"
ALLOW_CODEX_WRITE_ENV = "HERMES_GPT_ALLOW_CODEX_WRITE"
MAX_RESULT_CHARS = 24_000
MIN_TIMEOUT = 10
MAX_TIMEOUT = 3600
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_lock = threading.RLock()
_processes: dict[str, subprocess.Popen[str]] = {}
RETENTION_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    base = op.normalize_hermes_data_root(hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
    return Path(base or Path.home() / ".hermes") / "codex-jobs"


def _paths(job_id: str, hermes_root: Path | None = None) -> tuple[Path, Path]:
    root = _root(hermes_root)
    return root / f"{job_id}.json", root / f"{job_id}.jsonl"


def _safe_error(code: str, message: str, action: str) -> dict[str, Any]:
    return op.make_error_envelope(layer="operator", code=code, safe_message=message, suggested_action=action)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v) for v in value)
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
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _policy(workdir: str, sandbox: str, *, confirm: bool, dry_run: bool) -> tuple[op.OperatorPolicy, Path] | dict[str, Any]:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_workspace_path(workdir)
    except (PermissionError, ValueError) as exc:
        return _safe_error("POLICY_REFUSED", str(exc), "Enable Operator workspace level and approve the work directory.")
    if sandbox not in {"read-only", "workspace-write"}:
        return _safe_error("INVALID_SANDBOX", "sandbox must be read-only or workspace-write.", "Choose a supported Codex sandbox.")
    if not op.env_truthy(ENABLE_CODEX_RUNNER_ENV):
        return _safe_error("RUNNER_DISABLED", "Codex runner execution is disabled.", f"Set {ENABLE_CODEX_RUNNER_ENV}=1.")
    if sandbox == "workspace-write" and not op.env_truthy(ALLOW_CODEX_WRITE_ENV):
        return _safe_error("WRITE_DISABLED", "Codex workspace-write execution is disabled.", f"Set {ALLOW_CODEX_WRITE_ENV}=1 or use read-only.")
    if not dry_run and (not confirm or policy.apply_mode != "direct"):
        return _safe_error("DIRECT_CONFIRMATION_REQUIRED", "Execution requires direct apply mode and confirm=true.", "Review the plan, set direct mode, and retry with confirm=true and dry_run=false.")
    if policy.level == "owner" and not policy.owner_mode_ready:
        return _safe_error("OWNER_ACK_REQUIRED", "Configured Owner Mode is not acknowledged.", f"Set {op.OWNER_ACK_ENV} to the documented acknowledgement.")
    return policy, Path(workdir).expanduser().resolve()


def _argv(*, workdir: Path, sandbox: str, prompt: str, model: str | None, ignore_user_config: bool,
          review: bool = False, review_target: str = "uncommitted") -> list[str] | dict[str, Any]:
    codex = shutil.which("codex") or "codex"
    argv = [codex, "exec"]
    if review:
        argv += ["review", "--json", "--ephemeral"]
        if review_target == "uncommitted":
            argv.append("--uncommitted")
        elif review_target.startswith("base:") and re.fullmatch(r"base:[A-Za-z0-9._/-]{1,128}", review_target):
            argv += ["--base", review_target[5:]]
        elif review_target.startswith("commit:") and _SHA_RE.fullmatch(review_target[7:]):
            argv += ["--commit", review_target[7:]]
        else:
            return _safe_error("INVALID_REVIEW_TARGET", "Review target must be uncommitted, base:<branch>, or commit:<sha>.", "Choose exactly one supported review target.")
    else:
        argv += ["--json", "--ephemeral", "-C", str(workdir), "-s", sandbox]
    if model:
        if not _MODEL_RE.fullmatch(model):
            return _safe_error("INVALID_MODEL", "model contains unsupported characters.", "Use a configured model identifier without flags or whitespace.")
        argv += ["--model", model]
    if ignore_user_config:
        argv += ["-c", "mcp_servers={}"]
    if prompt:
        argv.append(prompt)
    return argv


def hermes_codex_status(hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    policy = op.OperatorPolicy()
    return {"success": True, "enabled": op.env_truthy(ENABLE_CODEX_RUNNER_ENV), "write_enabled": op.env_truthy(ALLOW_CODEX_WRITE_ENV),
            "operator_enabled": policy.enabled, "operator_level": policy.level, "apply_mode": policy.apply_mode,
            "codex_available": bool(shutil.which("codex")), "jobs_root": str(_root(hermes_root))}


def hermes_codex_plan(prompt: str, workdir: str, sandbox: str = "read-only", model: str | None = None,
                      ignore_user_config: bool = False, timeout: int = 900, review: bool = False,
                      review_target: str = "uncommitted") -> dict[str, Any]:
    return _start(prompt, workdir, sandbox, model, ignore_user_config, timeout, False, True, review, review_target, None)


def _start(prompt: str, workdir: str, sandbox: str, model: str | None, ignore_user_config: bool, timeout: int,
           confirm: bool, dry_run: bool, review: bool, review_target: str, hermes_root: Path | None) -> dict[str, Any]:
    checked = _policy(workdir, sandbox, confirm=confirm, dry_run=dry_run)
    if isinstance(checked, dict):
        return checked
    _, resolved = checked
    timeout = max(MIN_TIMEOUT, min(int(timeout), MAX_TIMEOUT))
    built = _argv(workdir=resolved, sandbox=sandbox, prompt=prompt, model=model, ignore_user_config=ignore_user_config, review=review, review_target=review_target)
    if isinstance(built, dict):
        return built
    sanitized_argv = ["<prompt>" if item == prompt and prompt else item for item in built]
    plan = {"success": True, "dry_run": dry_run, "mode": "review" if review else "task", "workdir": str(resolved),
            "sandbox": sandbox, "model": model, "timeout": timeout, "argv": sanitized_argv,
            "prompt_len": len(prompt), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    if dry_run:
        return _redact(plan)
    job_id = uuid4().hex
    meta = {**plan, "job_id": job_id, "status": "starting", "created_at": _now(), "started_at": None,
            "ended_at": None, "pid": None, "return_code": None, "thread_id": None, "cancel_requested": False}
    meta.pop("argv", None)
    path, output = _paths(job_id, hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    out_handle = open(output, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(built, cwd=resolved, stdout=out_handle, stderr=subprocess.STDOUT, text=True, shell=False,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")
    except (OSError, ValueError) as exc:
        out_handle.close()
        return _safe_error("CODEX_START_FAILED", op.redact_output(str(exc)), "Check Codex CLI installation, authentication, and model configuration.")
    meta.update({"status": "running", "started_at": _now(), "pid": proc.pid})
    _save(meta, hermes_root)
    with _lock:
        _processes[job_id] = proc
    threading.Thread(target=_watch, args=(job_id, proc, out_handle, timeout, hermes_root), daemon=True).start()
    op.audit_record(tool="hermes_codex_review_start" if review else "hermes_codex_start", level=checked[0].level,
                    apply_mode=checked[0].apply_mode, dry_run=False, success=True, changed=True, job_id=job_id,
                    path=str(resolved), prompt=prompt, extra={"mode": meta["mode"], "sandbox": sandbox, "model": model or ""})
    return _redact({"success": True, "dry_run": False, "job_id": job_id, "status": "running"})


def _watch(job_id: str, proc: subprocess.Popen[str], handle: Any, timeout: int, hermes_root: Path | None) -> None:
    try:
        proc.wait(timeout=timeout)
        status = "completed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        _terminate(proc)
        status = "timed_out"
    finally:
        handle.close()
    with _lock:
        _processes.pop(job_id, None)
    meta = _load(job_id, hermes_root) or {"job_id": job_id}
    meta.update({"status": "cancelled" if meta.get("cancel_requested") else status, "return_code": proc.poll(), "ended_at": _now()})
    _save(meta, hermes_root)


def hermes_codex_start(prompt: str, workdir: str, sandbox: str = "read-only", model: str | None = None,
                       ignore_user_config: bool = False, timeout: int = 900, confirm: bool = False,
                       dry_run: bool = True, hermes_root: Path | None = None) -> dict[str, Any]:
    return _start(prompt, workdir, sandbox, model, ignore_user_config, timeout, confirm, dry_run, False, "uncommitted", hermes_root)


def hermes_codex_review_start(workdir: str, target: str = "uncommitted", instructions: str = "", model: str | None = None,
                              ignore_user_config: bool = False, timeout: int = 900, confirm: bool = False,
                              dry_run: bool = True, hermes_root: Path | None = None) -> dict[str, Any]:
    return _start(instructions, workdir, "read-only", model, ignore_user_config, timeout, confirm, dry_run, True, target, hermes_root)


def hermes_codex_jobs(limit: int = 50, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    jobs = []
    for path in sorted(_root(hermes_root).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 200))]:
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return _redact({"success": True, "count": len(jobs), "jobs": jobs})


def hermes_codex_job_status(job_id: str, hermes_root: Path | None = None) -> dict[str, Any]:
    _reconcile(hermes_root)
    meta = _load(job_id, hermes_root)
    return _redact({"success": True, "job": meta}) if meta else _safe_error("JOB_NOT_FOUND", "Codex job was not found.", "Check the job ID with hermes_codex_jobs.")


def hermes_codex_job_result(job_id: str, max_chars: int = MAX_RESULT_CHARS, hermes_root: Path | None = None) -> dict[str, Any]:
    meta = _load(job_id, hermes_root)
    if not meta:
        return _safe_error("JOB_NOT_FOUND", "Codex job was not found.", "Check the job ID with hermes_codex_jobs.")
    _, output = _paths(job_id, hermes_root)
    latest, usage, thread_id = "", None, meta.get("thread_id")
    try:
        for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            thread_id = event.get("thread_id") or event.get("threadId") or thread_id
            if event.get("usage") is not None:
                usage = event["usage"]
            message = event.get("message") or event.get("text")
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in {"agent_message", "message"}:
                message = item.get("text") or item.get("content") or message
            if isinstance(message, str):
                latest = message
    except OSError:
        pass
    cap = max(500, min(int(max_chars), MAX_RESULT_CHARS))
    latest = op.redact_output(latest)
    truncated = len(latest) > cap
    latest = latest[:cap]
    return _redact({"success": True, "status": meta.get("status"), "return_code": meta.get("return_code"),
                                "thread_id": thread_id, "usage": usage, "response": latest, "truncated": truncated})


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
    """Conservatively reconcile persisted jobs without trusting reused PIDs."""
    root = _root(hermes_root)
    if not root.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    with _lock:
        owned = set(_processes)
    for path in root.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(str(meta.get("created_at", "")).replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError):
            continue
        job_id = str(meta.get("job_id", ""))
        if created < cutoff and meta.get("status") != "running":
            try:
                path.unlink()
                _, output = _paths(job_id, hermes_root)
                output.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if meta.get("status") == "running" and job_id not in owned:
            # A numeric PID alone is insufficient evidence after restart; it
            # may have been recycled. Never terminate or signal it.
            meta.update({"status": "orphaned", "ended_at": _now(), "reconciliation": "server restarted; PID ownership could not be proven"})
            _save(meta, hermes_root)


def hermes_codex_cancel(job_id: str, confirm: bool = False, dry_run: bool = True, hermes_root: Path | None = None) -> dict[str, Any]:
    meta = _load(job_id, hermes_root)
    if not meta:
        return _safe_error("JOB_NOT_FOUND", "Codex job was not found.", "Check the job ID with hermes_codex_jobs.")
    checked = _policy(str(meta.get("workdir", "")), str(meta.get("sandbox", "read-only")), confirm=confirm, dry_run=dry_run)
    if isinstance(checked, dict):
        return checked
    if dry_run:
        return {"success": True, "dry_run": True, "job_id": job_id, "would_cancel": meta.get("status") == "running"}
    with _lock:
        proc = _processes.get(job_id)
    if not proc or proc.poll() is not None:
        return _safe_error("JOB_NOT_RUNNING", "Codex job is not running in this server process.", "Refresh job status; orphaned jobs are not terminated by PID alone.")
    meta["cancel_requested"] = True
    _save(meta, hermes_root)
    _terminate(proc)
    return {"success": True, "dry_run": False, "job_id": job_id, "status": "cancelling"}
