"""Tests for operator_cron tools using temp profile homes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import operator_policy as op
import operator_cron as oc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    """A fake Hermes root with a default profile and one named profile."""
    root = tmp_path / "hermes"
    (root / "cron").mkdir(parents=True)
    (root / "profiles" / "hermes-researcher" / "cron").mkdir(parents=True)
    (root / "profiles" / "target-profile" / "cron").mkdir(parents=True)
    return root


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all operator env vars and isolate the audit log."""
    for name in [
        op.OPERATOR_ENABLED_ENV,
        op.OPERATOR_LEVEL_ENV,
        op.OPERATOR_APPLY_MODE_ENV,
        op.OPERATOR_ALLOWED_PROFILES_ENV,
        op.OPERATOR_ALLOWED_PATHS_ENV,
        op.OPERATOR_DENIED_PATHS_ENV,
        op.OWNER_ACK_ENV,
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def audit_override(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    yield log
    op.set_audit_log_override(None)


def _make_job(
    job_id: str = "abc123",
    name: str = "test-job",
    prompt: str = "do the thing",
    schedule: str = "every 30m",
    enabled: bool = True,
    state: str = "scheduled",
    last_run_at: str = "2026-01-01T00:00:00",
    last_status: str = "ok",
    last_error: str = "",
    last_delivery_error: str = "",
    paused_at: str = "",
    paused_reason: str = "",
    fire_claim: str = "",
    repeat_completed: int = 3,
    repeat_times: int = 10,
    next_run_at: str = "2026-01-01T00:30:00",
    skills: list[str] | None = None,
    deliver: str = "telegram",
    workdir: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict:
    return {
        "id": job_id,
        "name": name,
        "prompt": prompt,
        "schedule": schedule,
        "schedule_display": schedule,
        "enabled": enabled,
        "state": state,
        "last_run_at": last_run_at,
        "last_status": last_status,
        "last_error": last_error,
        "last_delivery_error": last_delivery_error,
        "paused_at": paused_at,
        "paused_reason": paused_reason,
        "fire_claim": fire_claim,
        "next_run_at": next_run_at,
        "skills": skills or [],
        "deliver": deliver,
        "workdir": workdir,
        "model": model,
        "provider": provider,
        "repeat": {"times": repeat_times, "completed": repeat_completed},
    }


def _write_jobs(profile_home: Path, jobs: list[dict], *, shape: str = "list") -> None:
    cron_dir = profile_home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    path = cron_dir / "jobs.json"
    if shape == "dict":
        payload = {"jobs": jobs}
    else:
        payload = jobs
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_cron_jobs_shape_defaults_to_dict_when_missing(hermes_root, clean_env):
    profile_home = hermes_root / "profiles" / "shape-test"
    profile_home.mkdir(parents=True)
    jobs = [_make_job()]
    oc._write_jobs(profile_home, jobs)
    payload = json.loads((profile_home / "cron" / "jobs.json").read_text())
    assert isinstance(payload, dict)
    assert payload["jobs"][0]["id"] == "abc123"


def test_cron_jobs_shape_round_trips_list_and_dict(hermes_root, clean_env):
    profile_home = hermes_root / "profiles" / "shape-roundtrip"
    cron_dir = profile_home / "cron"
    cron_dir.mkdir(parents=True)

    list_jobs = [_make_job(job_id="list-job")]
    (cron_dir / "jobs.json").write_text(json.dumps(list_jobs, indent=2), encoding="utf-8")
    assert len(oc._read_jobs(profile_home)) == 1
    oc._write_jobs(profile_home, list_jobs)
    list_payload = json.loads((cron_dir / "jobs.json").read_text())
    assert isinstance(list_payload, list)
    assert list_payload[0]["id"] == "list-job"

    dict_jobs = [_make_job(job_id="dict-job")]
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": dict_jobs}, indent=2), encoding="utf-8")
    assert len(oc._read_jobs(profile_home)) == 1
    oc._write_jobs(profile_home, dict_jobs)
    dict_payload = json.loads((cron_dir / "jobs.json").read_text())
    assert isinstance(dict_payload, dict)
    assert dict_payload["jobs"][0]["id"] == "dict-job"


# ---------------------------------------------------------------------------
# read_only: list / status
# ---------------------------------------------------------------------------


def test_cron_list_default_profile_empty(hermes_root, clean_env, audit_override):
    out = oc.hermes_cron_list(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["count"] == 0


def test_cron_list_returns_safe_view_no_raw_prompt(hermes_root, clean_env, audit_override):
    job = _make_job(prompt="SECRET_PROMPT_MARKER do the thing")
    _write_jobs(hermes_root, [job])
    out = oc.hermes_cron_list(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["count"] == 1
    job_view = parsed["jobs"][0]
    assert "SECRET_PROMPT_MARKER" not in json.dumps(job_view)
    assert job_view["prompt_len"] > 0
    assert job_view["prompt_sha256"]
    assert job_view["job_id"] == "abc123"
    assert job_view["name"] == "test-job"


def test_cron_status_aggregates(hermes_root, clean_env, audit_override):
    jobs = [
        _make_job(job_id="1", name="a", enabled=True, last_error="boom"),
        _make_job(job_id="2", name="b", enabled=False, last_delivery_error="deliv fail"),
        _make_job(job_id="3", name="c", enabled=True),
    ]
    _write_jobs(hermes_root, jobs)
    out = oc.hermes_cron_status(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["jobs_count"] == 3
    assert parsed["enabled_count"] == 2
    assert parsed["disabled_count"] == 1
    assert parsed["jobs_with_errors"] == 1
    assert parsed["jobs_with_delivery_errors"] == 1


# ---------------------------------------------------------------------------
# Profile enforcement
# ---------------------------------------------------------------------------


def test_cron_list_refuses_invalid_profile(hermes_root, clean_env, audit_override):
    out = oc.hermes_cron_list(profile="BAD NAME", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "Invalid profile name" in parsed["error"]


def test_cron_list_refuses_nonexistent_profile(hermes_root, clean_env, audit_override):
    out = oc.hermes_cron_list(profile="does-not-exist", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "does not exist" in parsed["error"]


def test_cron_list_refuses_profile_not_in_allowed_list(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default")
    out = oc.hermes_cron_list(profile="hermes-researcher", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not in the allowed profiles list" in parsed["error"]


def test_cron_list_allows_star_profiles(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "*")
    out = oc.hermes_cron_list(profile="hermes-researcher", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True


# ---------------------------------------------------------------------------
# Mutation gating
# ---------------------------------------------------------------------------


def test_cron_run_refuses_when_operator_disabled(hermes_root, clean_env, audit_override):
    _write_jobs(hermes_root, [_make_job()])
    out = oc.hermes_cron_run(
        profile="default", job_id="abc123", dry_run=True, hermes_root=hermes_root
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "disabled" in parsed["error"]


def test_cron_run_refuses_in_read_only_mode(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    _write_jobs(hermes_root, [_make_job()])
    out = oc.hermes_cron_run(
        profile="default", job_id="abc123", dry_run=True, hermes_root=hermes_root
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "does not satisfy required level" in parsed["error"]


def test_cron_run_dry_run_returns_plan(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    _write_jobs(hermes_root, [_make_job()])
    out = oc.hermes_cron_run(
        profile="default", job_id="abc123", dry_run=True, hermes_root=hermes_root
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert parsed["plan"]["argv"] == ["hermes", "cron", "run", "abc123"]
    assert parsed["plan"]["shell"] is False


def test_cron_run_direct_mode_invokes_runner(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_jobs(hermes_root, [_make_job()])
    captured = {}

    def fake_runner(argv, timeout=120, workdir=None):
        captured["argv"] = argv
        return (0, "ok", "")

    out = oc.hermes_cron_run(
        profile="default", job_id="abc123", dry_run=False,
        hermes_root=hermes_root, runner=fake_runner,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is False
    assert parsed["returncode"] == 0
    assert captured["argv"] == ["hermes", "cron", "run", "abc123"]


def test_cron_run_direct_with_named_profile_uses_p_flag(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_jobs(hermes_root / "profiles" / "hermes-researcher", [_make_job()])
    captured = {}

    def fake_runner(argv, timeout=120, workdir=None):
        captured["argv"] = argv
        return (0, "ok", "")

    out = oc.hermes_cron_run(
        profile="hermes-researcher", job_id="abc123", dry_run=False,
        hermes_root=hermes_root, runner=fake_runner,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert captured["argv"] == ["hermes", "-p", "hermes-researcher", "cron", "run", "abc123"]


# ---------------------------------------------------------------------------
# Copy / move
# ---------------------------------------------------------------------------


def test_cron_copy_dry_run_does_not_mutate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_jobs(hermes_root, [_make_job()])
    target_home = hermes_root / "profiles" / "hermes-researcher"
    # Target cron dir exists from the fixture but jobs.json is not pre-created.

    out = oc.hermes_cron_copy(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    # Source and target jobs.json unchanged.
    assert len(json.loads((hermes_root / "cron" / "jobs.json").read_text())) == 1
    assert not (target_home / "cron" / "jobs.json").exists()


def test_cron_copy_resets_runtime_fields(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")

    source_job = _make_job(
        last_run_at="2026-01-01T00:00:00",
        last_status="ok",
        last_error="previous boom",
        last_delivery_error="previous deliv fail",
        fire_claim="claim-xyz",
        paused_at="2026-01-02T00:00:00",
        paused_reason="testing",
        repeat_completed=7,
        repeat_times=10,
        next_run_at="2026-01-03T00:00:00",
        skills=["my-skill"],
        deliver="telegram:-100:5",
        model="claude-sonnet-4",
        provider="anthropic",
    )
    _write_jobs(hermes_root, [source_job])
    target_home = hermes_root / "profiles" / "hermes-researcher"
    _write_jobs(target_home, [])

    out = oc.hermes_cron_copy(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    new_id = parsed["new_target_job_id"]
    target_jobs = json.loads((target_home / "cron" / "jobs.json").read_text())
    assert len(target_jobs) == 1
    new_job = target_jobs[0]
    # Preserved.
    assert new_job["name"] == "test-job"
    assert new_job["prompt"] == "do the thing"
    assert new_job["schedule"] == "every 30m"
    assert new_job["skills"] == ["my-skill"]
    assert new_job["deliver"] == "telegram:-100:5"
    assert new_job["model"] == "claude-sonnet-4"
    assert new_job["provider"] == "anthropic"
    assert new_job["repeat"]["times"] == 10
    # Reset / cleared.
    assert new_job["id"] == new_id
    assert new_job["id"] != "abc123"
    assert "last_run_at" not in new_job or new_job.get("last_run_at") is None
    assert "last_status" not in new_job
    assert "last_error" not in new_job
    assert "last_delivery_error" not in new_job
    assert "fire_claim" not in new_job
    assert "paused_at" not in new_job
    assert "paused_reason" not in new_job
    assert "next_run_at" not in new_job
    # repeat.completed is reset (not preserved).
    assert "completed" not in new_job.get("repeat", {}) or new_job["repeat"].get("completed") in (None, 0)
    # state / enabled reset to defaults.
    assert new_job["state"] == "scheduled"
    assert new_job["enabled"] is True


def test_cron_copy_refuses_duplicate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")

    source_job = _make_job(name="daily-briefing", schedule="every 30m")
    target_dup = _make_job(
        job_id="existing", name="daily-briefing", schedule="every 30m",
        enabled=True,
    )
    _write_jobs(hermes_root, [source_job])
    _write_jobs(hermes_root / "profiles" / "hermes-researcher", [target_dup])

    out = oc.hermes_cron_copy(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "duplicate" in parsed["error"].lower() or "same name" in parsed["error"].lower()
    # Target unchanged.
    target_jobs = json.loads(
        ((hermes_root / "profiles" / "hermes-researcher") / "cron" / "jobs.json").read_text()
    )
    assert len(target_jobs) == 1


def test_cron_move_dry_run_does_not_mutate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_jobs(hermes_root, [_make_job()])
    # Target cron dir exists from the fixture but jobs.json is not pre-created.

    out = oc.hermes_cron_move(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    # Source unchanged.
    assert len(json.loads((hermes_root / "cron" / "jobs.json").read_text())) == 1
    # Target still empty.
    target_jobs = (hermes_root / "profiles" / "hermes-researcher" / "cron" / "jobs.json")
    assert not target_jobs.exists()


def test_cron_move_direct_pauses_source_after_copy(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_jobs(hermes_root, [_make_job()])
    _write_jobs(hermes_root / "profiles" / "hermes-researcher", [])

    pause_calls: list[list[str]] = []

    def fake_runner(argv, timeout=120, workdir=None):
        if "pause" in argv:
            pause_calls.append(argv)
            # Simulate Hermes pause: rewrite source jobs.json to set enabled=False.
            if "cron" in argv and "pause" in argv:
                # Find the source profile from -p flag (or default).
                profile = "default"
                if "-p" in argv:
                    profile = argv[argv.index("-p") + 1]
                src_home = (
                    hermes_root if profile == "default"
                    else hermes_root / "profiles" / profile
                )
                jobs = json.loads((src_home / "cron" / "jobs.json").read_text())
                for j in jobs:
                    if str(j.get("id")) == argv[-1]:
                        j["enabled"] = False
                        j["state"] = "paused"
                _write_jobs(src_home, jobs)
            return (0, "ok", "")
        return (0, "ok", "")

    out = oc.hermes_cron_move(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", pause_source=True, test_run_target=False,
        dry_run=False, hermes_root=hermes_root, runner=fake_runner,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["new_target_job_id"]
    assert pause_calls, "pause was not called"
    # Source is paused after the move.
    source_after = json.loads((hermes_root / "cron" / "jobs.json").read_text())
    assert source_after[0]["enabled"] is False


def test_cron_move_copy_success_pause_failure_returns_partial_failure(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_jobs(hermes_root, [_make_job()])
    _write_jobs(hermes_root / "profiles" / "hermes-researcher", [])

    def fake_runner(argv, timeout=120, workdir=None):
        if "pause" in argv:
            return (1, "", "pause failed")
        return (0, "ok", "")

    out = oc.hermes_cron_move(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", pause_source=True, test_run_target=False,
        dry_run=False, hermes_root=hermes_root, runner=fake_runner,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["partial"] is True
    assert parsed["new_target_job_id"]
    assert parsed["pause_result"]["success"] is False
    assert parsed["pause_result"]["returncode"] == 1
    target_jobs = json.loads((hermes_root / "profiles" / "hermes-researcher" / "cron" / "jobs.json").read_text())
    assert len(target_jobs["jobs"] if isinstance(target_jobs, dict) else target_jobs) == 1


def test_cron_move_does_not_pause_source_when_copy_fails(hermes_root, clean_env, audit_override, monkeypatch):
    """If the copy step raises, pause_source must NOT happen."""
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    # Create a duplicate in target so copy refuses — pause should not be called.
    src_job = _make_job(name="dup", schedule="every 30m")
    target_dup = _make_job(job_id="existing", name="dup", schedule="every 30m")
    _write_jobs(hermes_root, [src_job])
    _write_jobs(hermes_root / "profiles" / "hermes-researcher", [target_dup])

    pause_called = []

    def fake_runner(argv, timeout=120, workdir=None):
        if "pause" in argv:
            pause_called.append(argv)
        return (0, "ok", "")

    out = oc.hermes_cron_move(
        source_profile="default", target_profile="hermes-researcher",
        job_id="abc123", pause_source=True, dry_run=False,
        hermes_root=hermes_root, runner=fake_runner,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert pause_called == [], "pause was called even though copy refused"


# ---------------------------------------------------------------------------
# Cron create
# ---------------------------------------------------------------------------


def test_cron_create_refuses_without_schedule(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    out = oc.hermes_cron_create(
        profile="default", schedule="", prompt="do stuff",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "schedule is required" in parsed["error"].lower()


def test_cron_create_refuses_without_prompt_or_skills(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    out = oc.hermes_cron_create(
        profile="default", schedule="every 30m", prompt="",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "prompt, skills, or script" in parsed["error"].lower()


def test_cron_create_dry_run_returns_plan(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    out = oc.hermes_cron_create(
        profile="default", schedule="every 30m", prompt="run report",
        name="my-job", skills=["report-skill"], deliver="telegram",
        repeat=5, dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    plan = parsed["plan"]
    assert plan["would_create"] is True
    assert plan["name"] == "my-job"
    assert plan["schedule"] == "every 30m"
    assert plan["prompt_len"] == len("run report")
    assert plan["skills"] == ["report-skill"]
    assert plan["deliver"] == "telegram"


def test_cron_create_direct_writes_job(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    out = oc.hermes_cron_create(
        profile="default", schedule="0 9 * * *", prompt="daily briefing",
        name="daily-brief", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is False
    assert parsed["profile"] == "default"
    assert parsed["job_id"]
    job = parsed["job"]
    assert job["name"] == "daily-brief"
    assert job["schedule"] == "0 9 * * *"
    assert job["enabled"] is True
    assert job["state"] == "scheduled"

    # Verify the job was written to disk.
    jobs = oc._read_jobs(hermes_root)
    assert len(jobs) == 1
    assert jobs[0]["id"] == parsed["job_id"]
    assert jobs[0]["name"] == "daily-brief"


def test_cron_create_direct_with_named_profile(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    target_home = hermes_root / "profiles" / "hermes-researcher"

    out = oc.hermes_cron_create(
        profile="hermes-researcher", schedule="every 1h",
        prompt="check status", name="health-check",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["profile"] == "hermes-researcher"

    jobs = oc._read_jobs(target_home)
    assert len(jobs) == 1
    assert jobs[0]["name"] == "health-check"
