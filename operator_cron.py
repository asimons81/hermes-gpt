"""Cron operator tools for hermes-gpt.

All operations are tier-gated by ``OperatorPolicy``:

- ``hermes_cron_list``    : read_only  — list jobs for a profile
- ``hermes_cron_status``  : read_only  — aggregate status for a profile
- ``hermes_cron_run``     : cron       — run a job immediately (fixed argv)
- ``hermes_cron_pause``   : cron       — pause a job (fixed argv)
- ``hermes_cron_copy``    : cron       — copy a job across profiles (reset state)
- ``hermes_cron_create``  : cron       — create a new cron job from scratch
- ``hermes_cron_move``    : cron       — copy + pause source (atomic-ish)

Safety rules:
- No full prompt in any tool output (only ``prompt_len`` + ``prompt_sha256``).
- No shell=True. Ever.
- Direct mutation requires operator enabled + level >= cron + apply_mode=direct + dry_run=false.
- Run/pause use a fixed argv (``hermes cron run <job_id>`` / ``hermes cron pause <job_id>``)
  via ``run_argv`` (shell=False).
- Copy/move read/write the cron ``jobs.json`` file directly. This is safe
  because we control exactly which fields are reset (no provider/secret
  leakage), and we never touch .env / vault / auth files.
- Cross-profile copy requires both profiles to be in the allowed list.
- Duplicate detection on copy/move refuses if target has an active job with
  the same name and schedule.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

import operator_policy as op

# Preserve the original jobs.json container shape per profile_home so writes
# round-trip list-shaped files and Hermes' canonical {"jobs": [...]} form.
_jobs_shape_cache: dict[str, str] = {}

# Fields that are PRESERVED when copying a job across profiles.
_PRESERVED_COPY_FIELDS: tuple[str, ...] = (
    "name",
    "prompt",
    "schedule",
    "schedule_display",
    "deliver",
    "skills",
    "skill",
    "model",
    "provider",
    "base_url",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "repeat",  # only repeat.times is preserved; completed is reset below
)

# Fields that are RESET (cleared or zeroed) when copying.
_RESET_FIELDS: tuple[str, ...] = (
    "id",
    "last_run_at",
    "last_status",
    "last_error",
    "last_delivery_error",
    "paused_at",
    "paused_reason",
    "fire_claim",
    "next_run_at",  # will be recomputed by Hermes on next tick / save
    "output",  # any cached output path
)

# Fields that get a specific reset value rather than being cleared.
_RESET_VALUES: dict[str, Any] = {
    "state": "scheduled",
    "enabled": True,
}


def _cron_dir(profile_home: Path) -> Path:
    return profile_home / "cron"


def _jobs_file(profile_home: Path) -> Path:
    return _cron_dir(profile_home) / "jobs.json"


def _jobs_shape_key(profile_home: Path) -> str:
    try:
        return str(profile_home.resolve())
    except Exception:
        return str(profile_home)


def _read_jobs(profile_home: Path) -> list[dict[str, Any]]:
    """Read jobs.json. Returns [] if missing or unparseable."""
    path = _jobs_file(profile_home)
    if not path.exists():
        _jobs_shape_cache.setdefault(_jobs_shape_key(profile_home), "dict")
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    shape = "dict" if isinstance(data, dict) else "list" if isinstance(data, list) else "dict"
    _jobs_shape_cache[_jobs_shape_key(profile_home)] = shape
    if isinstance(data, dict):
        # Some Hermes versions store {"jobs": [...]}.
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


def _write_jobs(profile_home: Path, jobs: list[dict[str, Any]]) -> None:
    """Atomically write jobs.json. Creates cron dir if missing."""
    cron_dir = _cron_dir(profile_home)
    cron_dir.mkdir(parents=True, exist_ok=True)
    target = _jobs_file(profile_home)
    tmp = target.with_suffix(".json.tmp")
    shape = _jobs_shape_cache.get(_jobs_shape_key(profile_home), "dict")
    payload: Any
    if shape == "list":
        payload = jobs
    else:
        payload = {"jobs": jobs}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


def _hash_prompt(prompt: str | None) -> tuple[int, str]:
    if not prompt:
        return (0, "")
    data = prompt.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def _format_job_safe(job: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe job view with no raw prompt."""
    prompt = str(job.get("prompt") or "")
    prompt_len, prompt_sha = _hash_prompt(prompt)
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if isinstance(skills, str):
        skills = [skills]
    skills = [str(s) for s in skills if s]
    repeat = job.get("repeat") or {}
    return {
        "job_id": str(job.get("id") or "unknown"),
        "name": str(job.get("name") or prompt[:50] or (skills[0] if skills else "") or "cron job"),
        "schedule": str(job.get("schedule_display") or job.get("schedule") or "?"),
        "enabled": bool(job.get("enabled", True)),
        "state": str(job.get("state") or ("scheduled" if job.get("enabled", True) else "paused")),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_delivery_error": job.get("last_delivery_error"),
        "deliver": job.get("deliver", "local"),
        "skills": skills,
        "workdir": job.get("workdir"),
        "prompt_len": prompt_len,
        "prompt_sha256": prompt_sha,
    }


def _find_job(jobs: list[dict[str, Any]], job_id: str) -> Optional[dict[str, Any]]:
    """Find a job by id or name (case-insensitive name match)."""
    if not job_id:
        return None
    needle = str(job_id).strip().lower()
    for job in jobs:
        if str(job.get("id") or "") == job_id:
            return job
        if str(job.get("id") or "").lower() == needle:
            return job
        if str(job.get("name") or "").lower() == needle:
            return job
    return None


def _is_duplicate(
    target_jobs: list[dict[str, Any]], source_job: dict[str, Any]
) -> bool:
    """Return True if target has an active job with same name + schedule."""
    src_name = str(source_job.get("name") or "").lower()
    src_sched = str(source_job.get("schedule_display") or source_job.get("schedule") or "").lower()
    for job in target_jobs:
        if not job.get("enabled", True):
            continue
        tgt_name = str(job.get("name") or "").lower()
        tgt_sched = str(job.get("schedule_display") or job.get("schedule") or "").lower()
        if tgt_name == src_name and tgt_sched == src_sched:
            return True
    return False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def hermes_cron_list(
    profile: str = "default",
    include_disabled: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """List cron jobs for a profile. Read-only."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        if not include_disabled:
            jobs = [j for j in jobs if j.get("enabled", True)]
        formatted = [_format_job_safe(j) for j in jobs]
        result = {
            "success": True,
            "profile": profile,
            "count": len(formatted),
            "jobs": formatted,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_LIST_ERROR",
                suggested_action="Check cron/jobs.json and profile name.",
            ),
            indent=2,
        )


def hermes_cron_status(
    profile: str = "default",
    hermes_root: Path | None = None,
) -> str:
    """Aggregate cron status for a profile. Read-only."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        enabled = [j for j in jobs if j.get("enabled", True)]
        disabled = [j for j in jobs if not j.get("enabled", True)]
        with_errors = [j for j in jobs if j.get("last_error")]
        with_delivery_errors = [j for j in jobs if j.get("last_delivery_error")]

        # Gateway / ticker state is best-effort: check ticker_heartbeat file.
        cron_dir = _cron_dir(profile_home)
        ticker_heartbeat = None
        hb_path = cron_dir / "ticker_heartbeat"
        if hb_path.exists():
            try:
                ticker_heartbeat = hb_path.stat().st_mtime
            except OSError:
                ticker_heartbeat = None

        result = {
            "success": True,
            "profile": profile,
            "jobs_count": len(jobs),
            "enabled_count": len(enabled),
            "disabled_count": len(disabled),
            "jobs_with_errors": len(with_errors),
            "jobs_with_delivery_errors": len(with_delivery_errors),
            "ticker_heartbeat_mtime": ticker_heartbeat,
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_STATUS_ERROR",
                suggested_action="Check cron/jobs.json and profile name.",
            ),
            indent=2,
        )


def _hermes_argv(profile: str, sub: list[str]) -> list[str]:
    """Build a fixed-argv Hermes CLI invocation for the given profile."""
    if profile == "default":
        return ["hermes", *sub]
    return ["hermes", "-p", profile, *sub]


def hermes_cron_run(
    profile: str = "default",
    job_id: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Run a cron job immediately. Requires level >= cron."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        job = _find_job(jobs, job_id)
        if not job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in profile {profile!r}."
            )

        argv = _hermes_argv(profile, ["cron", "run", str(job.get("id"))])

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_run": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
                "job_id": str(job.get("id")),
                "job_name": str(job.get("name")),
            }
            op.audit_record(
                tool="hermes_cron_run",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=str(job.get("id")),
                prompt=str(job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=120, workdir=None)
        redacted_out = op.redact_output(out)
        redacted_err = op.redact_output(err)

        # Refresh job state.
        refreshed = _find_job(_read_jobs(profile_home), str(job.get("id")))
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": redacted_out,
            "stderr": redacted_err,
            "job": _format_job_safe(refreshed or job),
        }
        op.audit_record(
            tool="hermes_cron_run",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc}",
            profile=profile,
            job_id=str(job.get("id")),
            prompt=str(job.get("prompt") or ""),
            error=redacted_err if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_cron_run",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_RUN_ERROR",
                suggested_action="Check job_id, profile, Hermes CLI availability, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_pause(
    profile: str = "default",
    job_id: str = "",
    reason: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Pause a cron job. Requires level >= cron."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)
        job = _find_job(jobs, job_id)
        if not job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in profile {profile!r}."
            )

        argv = _hermes_argv(profile, ["cron", "pause", str(job.get("id"))])

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_pause": True,
                "argv": argv,
                "shell": False,
                "profile": profile,
                "job_id": str(job.get("id")),
                "job_name": str(job.get("name")),
                "reason": (reason or "")[:200],
            }
            op.audit_record(
                tool="hermes_cron_pause",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=str(job.get("id")),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        run_fn = runner or op.run_argv
        rc, out, err = run_fn(argv, timeout=60, workdir=None)
        redacted_out = op.redact_output(out)
        redacted_err = op.redact_output(err)
        refreshed = _find_job(_read_jobs(profile_home), str(job.get("id")))
        result = {
            "success": rc == 0,
            "dry_run": False,
            "returncode": rc,
            "stdout": redacted_out,
            "stderr": redacted_err,
            "job": _format_job_safe(refreshed or job),
        }
        op.audit_record(
            tool="hermes_cron_pause",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=rc == 0,
            changed=True,
            summary=f"rc={rc} reason={(reason or '')[:80]}",
            profile=profile,
            job_id=str(job.get("id")),
            error=redacted_err if rc != 0 else "",
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_cron_pause",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_PAUSE_ERROR",
                suggested_action="Check job_id, profile, Hermes CLI availability, and operator level/apply mode.",
            ),
            indent=2,
        )


def _build_copy_job(source_job: dict[str, Any], new_id: str) -> dict[str, Any]:
    """Build a new job dict from source, preserving config and resetting state."""
    new_job: dict[str, Any] = {}
    for field in _PRESERVED_COPY_FIELDS:
        if field in source_job:
            new_job[field] = source_job[field]
    # repeat: preserve times, reset completed.
    if "repeat" in new_job and isinstance(new_job["repeat"], dict):
        new_job["repeat"] = {"times": new_job["repeat"].get("times")}
    elif "repeat" in new_job:
        # preserve as-is if shape is unexpected
        pass
    else:
        new_job["repeat"] = {}

    for field in _RESET_FIELDS:
        new_job.pop(field, None)
    # Set specific reset values.
    for key, value in _RESET_VALUES.items():
        new_job[key] = value
    new_job["id"] = new_id
    return new_job


def _new_job_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


def hermes_cron_copy(
    source_profile: str,
    target_profile: str,
    job_id: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Copy a cron job from source_profile to target_profile."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_jobs = _read_jobs(source_home)
        target_jobs = _read_jobs(target_home)

        source_job = _find_job(source_jobs, job_id)
        if not source_job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in source profile {source_profile!r}."
            )

        if _is_duplicate(target_jobs, source_job):
            raise ValueError(
                f"Target profile {target_profile!r} already has an active job "
                f"with the same name {source_job.get('name')!r} and schedule "
                f"{source_job.get('schedule_display') or source_job.get('schedule')!r}. "
                "Refusing to create a duplicate."
            )

        new_id = _new_job_id()
        new_job = _build_copy_job(source_job, new_id)

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "source_job_id": str(source_job.get("id")),
                "new_target_job_id": new_id,
                "new_job_summary": _format_job_safe(new_job),
            }
            op.audit_record(
                tool="hermes_cron_copy",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
                prompt=str(source_job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        target_jobs.append(new_job)
        _write_jobs(target_home, target_jobs)
        result = {
            "success": True,
            "dry_run": False,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_job_id": str(source_job.get("id")),
            "new_target_job_id": new_id,
            "new_job": _format_job_safe(new_job),
        }
        op.audit_record(
            tool="hermes_cron_copy",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"copied to {target_profile} as {new_id}",
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=str(source_job.get("id")),
            prompt=str(source_job.get("prompt") or ""),
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_cron_copy",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_COPY_ERROR",
                suggested_action="Check source/target profiles, job_id, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_move(
    source_profile: str,
    target_profile: str,
    job_id: str,
    pause_source: bool = True,
    test_run_target: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
    runner=None,
) -> str:
    """Move a cron job: copy to target, optionally test-run, pause source.

    Direct mode ordering (no dry-run):
      1. copy source -> target
      2. if copy fails, do NOT pause source
      3. if test_run_target and test run fails, do NOT pause source
      4. pause source only after copy (and optional test run) succeeds
      5. re-list both profiles
    """
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        if not job_id:
            raise ValueError("job_id is required.")
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_jobs = _read_jobs(source_home)
        target_jobs = _read_jobs(target_home)

        source_job = _find_job(source_jobs, job_id)
        if not source_job:
            raise FileNotFoundError(
                f"Job {job_id!r} not found in source profile {source_profile!r}."
            )
        if _is_duplicate(target_jobs, source_job):
            raise ValueError(
                f"Target profile {target_profile!r} already has an active job "
                f"with the same name and schedule. Refusing to create a duplicate."
            )

        new_id = _new_job_id()
        new_job = _build_copy_job(source_job, new_id)

        plan = {
            "would_move": True,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "source_job_id": str(source_job.get("id")),
            "new_target_job_id": new_id,
            "pause_source": pause_source,
            "test_run_target": test_run_target,
            "new_job_summary": _format_job_safe(new_job),
        }

        if policy.effective_dry_run(dry_run):
            plan["dry_run"] = True
            op.audit_record(
                tool="hermes_cron_move",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
                prompt=str(source_job.get("prompt") or ""),
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)

        # Step 1: copy (write to target).
        try:
            target_jobs_after = list(target_jobs) + [new_job]
            _write_jobs(target_home, target_jobs_after)
        except Exception as exc:
            op.audit_record(
                tool="hermes_cron_move",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                error=f"copy failed: {exc}",
                source_profile=source_profile,
                target_profile=target_profile,
                job_id=str(source_job.get("id")),
            )
            raise

        # Step 2: optional test run.
        test_run_result = None
        if test_run_target:
            run_fn = runner or op.run_argv
            argv = _hermes_argv(target_profile, ["cron", "run", new_id])
            rc, out, err = run_fn(argv, timeout=120, workdir=None)
            test_run_result = {
                "returncode": rc,
                "success": rc == 0,
                "stdout": op.redact_output(out),
                "stderr": op.redact_output(err),
            }
            if rc != 0:
                # Do NOT pause source. Leave the target copy in place; caller
                # can decide whether to remove it.
                op.audit_record(
                    tool="hermes_cron_move",
                    level=policy.level,
                    apply_mode=policy.apply_mode,
                    dry_run=False,
                    success=False,
                    changed=True,
                    summary="test_run_target failed; source NOT paused",
                    source_profile=source_profile,
                    target_profile=target_profile,
                    job_id=str(source_job.get("id")),
                    error=test_run_result["stderr"],
                )
                return json.dumps(
                    {
                        "success": False,
                        "dry_run": False,
                        "error": "test_run_target failed; source was NOT paused.",
                        "copy_result": {
                            "new_target_job_id": new_id,
                            "new_job": _format_job_safe(new_job),
                        },
                        "test_run_result": test_run_result,
                    },
                    indent=2,
                )

        # Step 3: pause source.
        pause_result = None
        partial = False
        if pause_source:
            run_fn = runner or op.run_argv
            argv = _hermes_argv(source_profile, ["cron", "pause", str(source_job.get("id"))])
            rc, out, err = run_fn(argv, timeout=60, workdir=None)
            pause_result = {
                "returncode": rc,
                "success": rc == 0,
                "stdout": op.redact_output(out),
                "stderr": op.redact_output(err),
            }
            if rc != 0:
                partial = True
                source_after = _read_jobs(source_home)
                target_after = _read_jobs(target_home)
                op.audit_record(
                    tool="hermes_cron_move",
                    level=policy.level,
                    apply_mode=policy.apply_mode,
                    dry_run=False,
                    success=False,
                    changed=True,
                    summary=f"copy succeeded; pause failed for {new_id}",
                    source_profile=source_profile,
                    target_profile=target_profile,
                    job_id=str(source_job.get("id")),
                    error=pause_result["stderr"],
                )
                return json.dumps(
                    {
                        "success": False,
                        "partial": True,
                        "dry_run": False,
                        "source_profile": source_profile,
                        "target_profile": target_profile,
                        "new_target_job_id": new_id,
                        "new_job": _format_job_safe(new_job),
                        "pause_result": pause_result,
                        "test_run_result": test_run_result,
                        "source_after_count": len(source_after),
                        "target_after_count": len(target_after),
                    },
                    indent=2,
                )

        # Step 4: re-list both.
        source_after = _read_jobs(source_home)
        target_after = _read_jobs(target_home)

        op.audit_record(
            tool="hermes_cron_move",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"moved to {target_profile} as {new_id}; paused_source={pause_source}",
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=str(source_job.get("id")),
            prompt=str(source_job.get("prompt") or ""),
        )

        return json.dumps(
            {
                "success": True,
                "dry_run": False,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "new_target_job_id": new_id,
                "new_job": _format_job_safe(new_job),
                "pause_result": pause_result,
                "test_run_result": test_run_result,
                "source_after_count": len(source_after),
                "target_after_count": len(target_after),
            },
            indent=2,
        )
    except Exception as exc:
        op.audit_record(
            tool="hermes_cron_move",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="cron",
                code="CRON_MOVE_ERROR",
                suggested_action="Check source/target profiles, job_id, disk permissions, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_cron_create(
    profile: str = "default",
    schedule: str = "",
    prompt: str = "",
    name: str | None = None,
    skills: list[str] | None = None,
    deliver: str | None = None,
    repeat: int | None = None,
    script: str | None = None,
    workdir: str | None = None,
    no_agent: bool | None = None,
    context_from: list[str] | None = None,
    enabled_toolsets: list[str] | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Create a new cron job. Requires level >= cron."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("cron")
        policy.require_profile(profile, hermes_root)

        if not schedule:
            raise ValueError("schedule is required for creating a cron job.")
        if not prompt and not skills and not script:
            raise ValueError(
                "At least one of prompt, skills, or script is required."
            )

        profile_home = op.resolve_profile_home(profile, hermes_root)
        jobs = _read_jobs(profile_home)

        new_id = _new_job_id()
        job_name = (
            name
            or (prompt[:50] if prompt else "")
            or (skills[0] if skills else "")
            or "cron job"
        )

        new_job: dict[str, Any] = {
            "id": new_id,
            "name": job_name,
            "prompt": prompt,
            "schedule": schedule,
            "schedule_display": schedule,
            "skills": skills or [],
            "deliver": deliver or "local",
            "enabled": True,
            "state": "scheduled",
        }
        if repeat is not None:
            new_job["repeat"] = {"times": repeat}
        if script:
            new_job["script"] = script
        if workdir:
            new_job["workdir"] = workdir
        if no_agent is not None:
            new_job["no_agent"] = no_agent
        if context_from:
            new_job["context_from"] = list(context_from)
        if enabled_toolsets:
            new_job["enabled_toolsets"] = list(enabled_toolsets)
        if model_provider or model_name:
            model: dict[str, str] = {}
            if model_provider:
                model["provider"] = model_provider
            if model_name:
                model["model"] = model_name
            new_job["model"] = model

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_create": True,
                "profile": profile,
                "job_id": new_id,
                "name": job_name,
                "schedule": schedule,
                "prompt_len": len(prompt),
                "skills": skills or [],
                "deliver": deliver or "local",
            }
            op.audit_record(
                tool="hermes_cron_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                job_id=new_id,
                prompt=prompt,
            )
            return json.dumps(
                {"success": True, "dry_run": True, "plan": plan}, indent=2
            )

        policy.require_mutation(dry_run)
        jobs.append(new_job)
        _write_jobs(profile_home, jobs)

        result = {
            "success": True,
            "dry_run": False,
            "profile": profile,
            "job_id": new_id,
            "job": _format_job_safe(new_job),
        }
        op.audit_record(
            tool="hermes_cron_create",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"created {new_id} in {profile}",
            profile=profile,
            job_id=new_id,
            prompt=prompt,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_cron_create",
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
                layer="cron",
                code="CRON_CREATE_ERROR",
                suggested_action="Check profile, operator level/apply mode, and parameters.",
            ),
            indent=2,
        )
