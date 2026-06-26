"""Tests for operator_diagnostics: doctor, snapshot, release_doctor, recover."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import operator_diagnostics as od
import operator_policy as op


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    """A fake Hermes root with a default profile."""
    root = tmp_path / "hermes"
    (root / "cron").mkdir(parents=True)
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: x\n---\nbody", encoding="utf-8"
    )
    return root


@pytest.fixture
def clean_env(monkeypatch):
    """Clear operator env vars."""
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


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------


def test_make_error_envelope_has_required_fields():
    env = op.make_error_envelope(
        layer="gateway",
        code="GATEWAY_UNREACHABLE",
        safe_message="Gateway unreachable.",
        suggested_action="Restart it.",
    )
    assert env["success"] is False
    assert env["ok"] is False
    assert env["error"] == "Gateway unreachable."
    assert env["layer"] == "gateway"
    assert env["code"] == "GATEWAY_UNREACHABLE"
    assert env["safe_message"] == "Gateway unreachable."
    assert env["suggested_action"] == "Restart it."
    assert env["trace_id"]
    assert len(env["trace_id"]) >= 16


def test_error_from_exception_sanitizes_sensitive_message():
    exc = ValueError("token=sk-abc123 path=/home/user/.env secret=foo")
    env = op.error_from_exception(
        exc,
        layer="connector",
        code="UPSTREAM_502",
        suggested_action="Retry.",
    )
    assert env["layer"] == "connector"
    assert env["code"] == "UPSTREAM_502"
    assert "sk-abc123" not in env["safe_message"]
    assert "/home/user" not in env["safe_message"]
    assert ".env" not in env["safe_message"]
    assert "trace_id" in env


def test_error_envelope_does_not_leak_secret_value():
    env = op.make_error_envelope(
        layer="config",
        code="CONFIG_UNREADABLE",
        safe_message="Config could not be read.",
        suggested_action="Check permissions.",
    )
    assert "SECRET" not in json.dumps(env)


# ---------------------------------------------------------------------------
# hermes_operator_doctor
# ---------------------------------------------------------------------------


def test_doctor_passes_for_valid_hermes_root(hermes_root, clean_env, audit_override):
    # Provide enough state that non-connector checks pass.
    (hermes_root / "config.yaml").write_text("profile: default\n", encoding="utf-8")
    (hermes_root / ".env").write_text("PLAIN=value\n", encoding="utf-8")
    (hermes_root / "gateway.pid").write_text(str(os.getpid()), encoding="utf-8")
    (hermes_root / "cron" / "ticker_heartbeat").write_text("ok", encoding="utf-8")
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    # Connector re-registration is UNSUPPORTED, so overall is WARN.
    assert parsed["overall_status"] == "WARN"
    checks = parsed["checks"]
    assert checks["operator_runtime"]["status"] == "PASS"
    assert checks["config_readable"]["status"] == "PASS"
    assert checks["env_readable"]["status"] == "PASS"
    assert checks["cron_registry"]["status"] == "PASS"
    assert checks["skills_registry"]["status"] == "PASS"
    assert checks["operator_policy"]["status"] == "PASS"
    assert checks["last_audit_record"]["status"] in ("PASS", "WARN")
    assert checks["connector_api_bridge"]["status"] == "UNSUPPORTED"
    assert parsed["trace_id"]


def test_doctor_fails_for_dead_pid(hermes_root, clean_env, audit_override):
    pid_path = hermes_root / "gateway.pid"
    pid_path.write_text("99999999", encoding="utf-8")
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["overall_status"] == "FAIL"
    assert "gateway_status" in parsed["failed_checks"]
    assert parsed["checks"]["gateway_status"]["code"] == "GATEWAY_DEAD_PID"
    assert parsed["checks"]["gateway_status"]["layer"] == "gateway"


def test_doctor_fails_for_corrupt_cron_jobs(hermes_root, clean_env, audit_override):
    (hermes_root / "cron" / "jobs.json").write_text("not json", encoding="utf-8")
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["overall_status"] == "FAIL"
    assert "cron_registry" in parsed["failed_checks"]


def test_doctor_handles_missing_skills_dir(hermes_root, clean_env, audit_override):
    import shutil

    shutil.rmtree(hermes_root / "skills")
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["checks"]["skills_registry"]["status"] == "WARN"


def test_doctor_structures_upstream_502_like_failure(hermes_root, clean_env, audit_override, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("upstream returned 502 Bad Gateway for /connector/health")

    monkeypatch.setattr(od, "_check_gateway_status", lambda profile_home: od._check_result(
        status=od.STATUS_FAIL,
        layer="connector",
        code="UPSTREAM_502",
        message="Upstream connector health check returned 502 Bad Gateway.",
        suggested_action="Check the connector or restart the gateway.",
        extra={"trace_id": op.new_trace_id()},
    ))
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["overall_status"] == "FAIL"
    check = parsed["checks"]["gateway_status"]
    assert check["layer"] == "connector"
    assert check["code"] == "UPSTREAM_502"
    assert check["message"]
    assert check["suggested_action"]


def test_doctor_does_not_leak_env_values(hermes_root, clean_env, audit_override):
    (hermes_root / ".env").write_text("SECRET_TOKEN=supersecret\nPLAIN=value\n", encoding="utf-8")
    out = od.hermes_operator_doctor(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    text = json.dumps(parsed)
    assert "supersecret" not in text
    assert parsed["checks"]["env_readable"]["secret_like_count"] == 1


def test_doctor_rejects_invalid_profile(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_doctor(profile="BAD NAME", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["ok"] is False
    assert parsed["layer"] == "policy"
    assert parsed["code"] == "INVALID_PROFILE"
    assert parsed["trace_id"]


# ---------------------------------------------------------------------------
# hermes_operator_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_has_expected_top_level_keys(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_snapshot(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    for key in [
        "version",
        "profile",
        "gateway",
        "cron",
        "env",
        "skills",
        "last_audit_timestamp",
        "repo_status",
        "known_issues",
        "recommended_next_action",
        "trace_id",
    ]:
        assert key in parsed
    assert parsed["version"] == od.VERSION


def test_snapshot_partial_failure_does_not_crash(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setattr(od, "_count_skills_safe", lambda _profile_home: (_ for _ in ()).throw(OSError("nope")))
    out = od.hermes_operator_snapshot(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert any("skills_probe_failed" in issue for issue in parsed["known_issues"])
    assert parsed["recommended_next_action"]


def test_snapshot_never_includes_env_values(hermes_root, clean_env, audit_override):
    (hermes_root / ".env").write_text("API_KEY=sk-abcdef\n", encoding="utf-8")
    out = od.hermes_operator_snapshot(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    text = json.dumps(parsed)
    assert "sk-abcdef" not in text


# ---------------------------------------------------------------------------
# hermes_release_doctor
# ---------------------------------------------------------------------------


def test_release_doctor_clean_repo_passes(tmp_path, monkeypatch):
    # Build a minimal clean repo.
    repo = tmp_path / "repo"
    repo.mkdir()
    op.run_argv(["git", "init"], timeout=30, workdir=str(repo))
    op.run_argv(["git", "config", "user.email", "test@test.com"], timeout=30, workdir=str(repo))
    op.run_argv(["git", "config", "user.name", "Test"], timeout=30, workdir=str(repo))
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(f"## {od.VERSION}\n", encoding="utf-8")
    (repo / "README.md").write_text("hermes_operator_doctor\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "operator-mode.md").write_text("hermes_operator_recover\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")
    op.run_argv(["git", "add", "."], timeout=30, workdir=str(repo))
    op.run_argv(["git", "commit", "-m", "init"], timeout=30, workdir=str(repo))

    out = od.hermes_release_doctor(workdir=str(repo), full_tests=False)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "PASS"
    assert parsed["blocking_issues"] == []
    assert parsed["recommended_release_type"] == "minor"


def test_release_doctor_blocks_on_secret_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    out = od.hermes_release_doctor(workdir=str(repo), full_tests=False)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "BLOCKED"
    assert any(".env" in issue for issue in parsed["blocking_issues"])


def test_release_doctor_warns_on_dirty_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    op.run_argv(["git", "init"], timeout=30, workdir=str(repo))
    op.run_argv(["git", "config", "user.email", "test@test.com"], timeout=30, workdir=str(repo))
    op.run_argv(["git", "config", "user.name", "Test"], timeout=30, workdir=str(repo))
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(f"## {od.VERSION}\n", encoding="utf-8")
    (repo / "README.md").write_text("hermes_operator_doctor\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "operator-mode.md").write_text("hermes_operator_recover\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")
    op.run_argv(["git", "add", "."], timeout=30, workdir=str(repo))
    op.run_argv(["git", "commit", "-m", "init"], timeout=30, workdir=str(repo))
    (repo / "dirty.txt").write_text("x", encoding="utf-8")

    out = od.hermes_release_doctor(workdir=str(repo), full_tests=False)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "WARN"
    assert any("uncommitted" in issue for issue in parsed["non_blocking_issues"])


def test_release_doctor_warns_on_missing_changelog_version(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("## 0.1.0\n", encoding="utf-8")
    (repo / "README.md").write_text("hermes_operator_doctor\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "operator-mode.md").write_text("hermes_operator_recover\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")
    out = od.hermes_release_doctor(workdir=str(repo), full_tests=False)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "WARN"
    assert any(od.VERSION in issue for issue in parsed["non_blocking_issues"])


def test_release_doctor_full_tests_fail_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(f"## {od.VERSION}\n", encoding="utf-8")
    (repo / "README.md").write_text("hermes_operator_doctor\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "operator-mode.md").write_text("hermes_operator_recover\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")

    def fake_runner(argv, timeout=None, workdir=None):
        return (1, "", "pytest failed")

    out = od.hermes_release_doctor(workdir=str(repo), full_tests=True, runner=fake_runner)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "BLOCKED"
    assert parsed["test_result"]["success"] is False


def test_release_doctor_full_tests_timeout_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f'version = "{od.VERSION}"\n', encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(f"## {od.VERSION}\n", encoding="utf-8")
    (repo / "README.md").write_text("hermes_operator_doctor\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "operator-mode.md").write_text("hermes_operator_recover\n", encoding="utf-8")
    (repo / "server.py").write_text("print('ok')\n", encoding="utf-8")

    def fake_runner(argv, timeout=None, workdir=None):
        raise subprocess.TimeoutExpired(argv, timeout or 1)

    out = od.hermes_release_doctor(workdir=str(repo), full_tests=True, runner=fake_runner)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["status"] == "BLOCKED"
    assert any("timed out" in issue for issue in parsed["blocking_issues"])


# ---------------------------------------------------------------------------
# hermes_operator_recover
# ---------------------------------------------------------------------------


def test_recover_dry_run_does_not_mutate(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_recover(profile="default", apply=False, hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["apply"] is False
    # Gateway missing means a restart is planned, but no mutation is performed.
    assert parsed["mutations_attempted"] == 1
    assert parsed["mutations_performed"] == 0
    steps = {s["step"]: s for s in parsed["steps"]}
    assert steps["read_config"]["status"] == "WARN"  # config missing is WARN
    assert steps["connector_routes"]["status"] == "UNSUPPORTED"


def test_recover_apply_requires_direct_workspace_policy(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_recover(profile="default", apply=True, hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["ok"] is False
    assert parsed["code"] == "PERMISSION_DENIED"


def test_recover_plans_gateway_restart_when_unhealthy(hermes_root, clean_env, audit_override):
    (hermes_root / "gateway.pid").write_text("99999999", encoding="utf-8")
    out = od.hermes_operator_recover(profile="default", apply=False, hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["mutations_attempted"] == 1
    step = next(s for s in parsed["steps"] if s["step"] == "restart_gateway_if_needed")
    assert step["status"] == "WARN"
    assert step["would_restart"] is True


def test_recover_performs_restart_when_authorized(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    (hermes_root / "gateway.pid").write_text("99999999", encoding="utf-8")

    def fake_runner(argv, timeout=None, workdir=None):
        return (0, "restarted", "")

    out = od.hermes_operator_recover(profile="default", apply=True, hermes_root=hermes_root, runner=fake_runner)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["apply"] is True
    assert parsed["mutations_performed"] == 1
    step = next(s for s in parsed["steps"] if s["step"] == "restart_gateway_if_needed")
    assert step["status"] == "PASS"


def test_recover_connector_routes_is_unsupported(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_recover(profile="default", apply=False, hermes_root=hermes_root)
    parsed = json.loads(out)
    step = next(s for s in parsed["steps"] if s["step"] == "connector_routes")
    assert step["status"] == "UNSUPPORTED"
    assert step["supported"] is False
    assert step["action"] == "manual"


def test_recover_writes_audit_record(hermes_root, clean_env, audit_override):
    out = od.hermes_operator_recover(profile="default", apply=False, hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    lines = audit_override.read_text(encoding="utf-8").strip().splitlines()
    assert any(json.loads(line)["tool"] == "hermes_operator_recover" for line in lines)


# ---------------------------------------------------------------------------
# Integration with server.py tool registration
# ---------------------------------------------------------------------------


def test_new_tools_registered():
    import asyncio
    import server

    built = server.build_server()
    tools = asyncio.run(built.list_tools())
    names = {tool.name for tool in tools}
    for name in [
        "hermes_operator_doctor",
        "hermes_operator_snapshot",
        "hermes_release_doctor",
        "hermes_operator_recover",
    ]:
        assert name in names
