"""Tests for operator_policy: truthy helper, policy, audit, path safety, profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import operator_policy as op


# ---------------------------------------------------------------------------
# Truthy helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", "enabled"])
def test_is_truthy_recognized_truthy_values(value):
    assert op.is_truthy(value) is True


@pytest.mark.parametrize(
    "value", ["0", "false", "no", "off", "disabled", "", "maybe", "2", None]
)
def test_is_truthy_recognized_falsey_values(value):
    assert op.is_truthy(value) is False


def test_is_truthy_handles_bool_and_int():
    assert op.is_truthy(True) is True
    assert op.is_truthy(False) is False
    assert op.is_truthy(1) is True
    assert op.is_truthy(0) is False


def test_env_truthy_reads_env(monkeypatch):
    monkeypatch.setenv("OP_TEST_VAR", "1")
    assert op.env_truthy("OP_TEST_VAR") is True
    monkeypatch.setenv("OP_TEST_VAR", "no")
    assert op.env_truthy("OP_TEST_VAR") is False
    monkeypatch.delenv("OP_TEST_VAR", raising=False)
    assert op.env_truthy("OP_TEST_VAR") is False


def test_old_env_enabled_helper_still_works():
    """The old server.env_enabled() helper checks == '1' only. The new
    is_truthy is broader, but old behavior must remain intact for callers
    that still use env_enabled (the broad HERMES_GPT_ENABLE_* flags)."""
    import server

    os.environ["HERMES_GPT_TEST_OLD_FLAG"] = "1"
    try:
        assert server.env_enabled("HERMES_GPT_TEST_OLD_FLAG") is True
    finally:
        del os.environ["HERMES_GPT_TEST_OLD_FLAG"]
    assert server.env_enabled("HERMES_GPT_TEST_OLD_FLAG") is False


# ---------------------------------------------------------------------------
# Policy defaults
# ---------------------------------------------------------------------------


def _clear_operator_envs(monkeypatch):
    for name in [
        op.OPERATOR_ENABLED_ENV,
        op.OPERATOR_LEVEL_ENV,
        op.OPERATOR_APPLY_MODE_ENV,
        op.OPERATOR_ALLOWED_PROFILES_ENV,
        op.OPERATOR_ALLOWED_PATHS_ENV,
        op.OPERATOR_DENIED_PATHS_ENV,
        op.OWNER_ACK_ENV,
        op.OWNER_ACTIVE_ENV,
    ]:
        monkeypatch.delenv(name, raising=False)


def test_default_policy_disabled_read_only_dry_run(monkeypatch):
    _clear_operator_envs(monkeypatch)
    policy = op.OperatorPolicy()
    assert policy.enabled is False
    assert policy.level == "read_only"
    assert policy.apply_mode == "dry_run"
    assert policy.owner_mode_ready is False
    assert policy.mutation_allowed is False
    assert policy.allowed_profiles == ["default"]


def test_mutation_refuses_when_operator_disabled(monkeypatch):
    _clear_operator_envs(monkeypatch)
    policy = op.OperatorPolicy()
    with pytest.raises(PermissionError, match="Operator mode is disabled"):
        policy.require_enabled()


def test_mutation_refuses_in_read_only_mode(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    policy = op.OperatorPolicy()
    assert policy.enabled is True
    with pytest.raises(PermissionError, match="does not satisfy required level"):
        policy.require_level("cron")


def test_owner_configuration_is_clamped_to_workspace_without_break_glass_activation(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    policy = op.OperatorPolicy()
    assert policy.level == "workspace"
    assert policy.owner_active is False
    assert policy.owner_mode_ready is False
    with pytest.raises(PermissionError, match="does not satisfy required level"):
        policy.require_owner(dry_run=False)


def test_owner_tools_refuse_without_owner_ack(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    # OWNER_ACK_ENV deliberately not set.
    policy = op.OperatorPolicy()
    assert policy.owner_mode_ready is False
    with pytest.raises(PermissionError, match="Owner Mode requires"):
        policy.require_owner(dry_run=False)


def test_owner_tools_refuse_with_wrong_ack(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACK_ENV, "i_understand_this_is_unsafe")
    policy = op.OperatorPolicy()
    assert policy.owner_mode_ready is False
    with pytest.raises(PermissionError, match="Owner Mode requires"):
        policy.require_owner(dry_run=False)


def test_owner_mode_ready_when_all_set(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    policy = op.OperatorPolicy()
    assert policy.owner_mode_ready is True
    # require_owner with dry_run=True should pass (dry-run plan is allowed).
    policy.require_owner(dry_run=True)
    # require_owner with dry_run=False should also pass since direct + ack.
    policy.require_owner(dry_run=False)


def test_invalid_level_falls_back_to_read_only(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "root")  # not a real level
    policy = op.OperatorPolicy()
    assert policy.level == "read_only"


def test_invalid_apply_mode_falls_back_to_dry_run(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "wet_run")  # not a real mode
    policy = op.OperatorPolicy()
    assert policy.apply_mode == "dry_run"


def test_effective_dry_run_input_overrides_apply_mode(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    policy = op.OperatorPolicy()
    assert policy.effective_dry_run(False) is False
    assert policy.effective_dry_run(True) is True


def test_effective_dry_run_when_apply_mode_dry_run(monkeypatch):
    _clear_operator_envs(monkeypatch)
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "cron")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    policy = op.OperatorPolicy()
    # Even if caller passes dry_run=False, effective dry-run is True.
    assert policy.effective_dry_run(False) is True


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def test_validate_profile_name_accepts_valid():
    assert op.validate_profile_name("default") == "default"
    assert op.validate_profile_name("Default") == "default"
    assert op.validate_profile_name("hermes-researcher") == "hermes-researcher"
    assert op.validate_profile_name("trt_1") == "trt_1"


@pytest.mark.parametrize("bad", ["", "HERMES", "Test", "with space", "has/slash", "x" * 100])
def test_validate_profile_name_rejects_invalid(bad):
    with pytest.raises(ValueError):
        op.validate_profile_name(bad)


def test_parse_allowed_profiles_default():
    assert op.parse_allowed_profiles(None) == ["default"]
    assert op.parse_allowed_profiles("") == ["default"]


def test_parse_allowed_profiles_list():
    assert op.parse_allowed_profiles("default,hermes-researcher") == [
        "default",
        "hermes-researcher",
    ]


def test_parse_allowed_profiles_star_allows_all():
    assert op.parse_allowed_profiles("*") == ["*"]


def test_profile_is_allowed_default_only():
    allowed = ["default"]
    assert op.profile_is_allowed("default", allowed) is True
    assert op.profile_is_allowed("hermes-researcher", allowed) is False


def test_profile_is_allowed_star():
    assert op.profile_is_allowed("anything-here", ["*"]) is True


def test_profile_is_allowed_does_not_fall_open_for_existing_profiles():
    allowed = ["default"]
    existing_profiles = ["default", "hermes-researcher"]
    assert op.profile_is_allowed(
        "hermes-researcher", allowed, existing_profiles=existing_profiles
    ) is False


def test_profile_exists_default(tmp_path):
    # tmp_path with no profiles/ subdir: only default exists.
    assert op.profile_exists("default", tmp_path) is True
    assert op.profile_exists("hermes-researcher", tmp_path) is False


def test_profile_exists_named(tmp_path):
    (tmp_path / "profiles" / "hermes-researcher").mkdir(parents=True)
    assert op.profile_exists("hermes-researcher", tmp_path) is True


def test_resolve_profile_home_default(tmp_path):
    assert op.resolve_profile_home("default", tmp_path) == tmp_path


def test_resolve_profile_home_named(tmp_path):
    assert op.resolve_profile_home("hermes-researcher", tmp_path) == (
        tmp_path / "profiles" / "hermes-researcher"
    )


def test_normalize_hermes_data_root_variants(tmp_path):
    root = Path(r"C:\Users\asimo\AppData\Local\hermes")
    profile_root = root / "profiles" / "hermes-senior-engineer"
    agent_root = root / "hermes-agent"

    assert op.normalize_hermes_data_root(root) == root
    assert op.normalize_hermes_data_root(profile_root) == root
    assert op.normalize_hermes_data_root(agent_root) == root

    assert op.resolve_profile_home("default", root) == root
    assert op.resolve_profile_home("hermes-researcher", root) == root / "profiles" / "hermes-researcher"


@pytest.mark.parametrize(
    "path",
    [
        "~/.env",
        "~/.env.local",
        "~/.env.production",
        "~/.ssh/id_rsa",
        "~/.ssh/config",
        "~/.aws/credentials",
        "~/.gnupg/secring.gpg",
        "~/.kube/config",
        "~/hermes/.env",
        "~/hermes/auth.json",
        "~/hermes/.anthropic_oauth.json",
        "~/hermes/mcp-tokens/anything",
        "~/hermes/pairing/foo",
        "~/hermes/auth/google_oauth.json",
        "~/hermes/cache/bws_cache.json",
        "~/hermes/webhook_subscriptions.json",
        "~/.netrc",
        "~/.pgpass",
        "~/.npmrc",
        "~/.pypirc",
        "~/.git-credentials",
        "config/token.json",
        "secrets.yaml",
        "password.txt",
        "private_key.pem",
        "id_ed25519",
        "authorized_keys",
        "cookie.jar",
        "oauth_state.txt",
    ],
)
def test_is_denied_path_refuses_secrets(path):
    assert op.is_denied_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "~/projects/myapp/README.md",
        "~/projects/myapp/src/main.py",
        "~/hermes/skills/my-skill/SKILL.md",
        "~/hermes/cron/jobs.json",
        "~/hermes/config.yaml",
        "~/hermes/logs/gateway.log",
    ],
)
def test_is_denied_path_allows_normal_paths(path):
    assert op.is_denied_path(path) is False


def test_path_under_allowed_true(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    assert op.path_under_allowed(target, [tmp_path]) is True


def test_path_under_allowed_false(tmp_path):
    other = tmp_path.parent / "other-dir"
    assert op.path_under_allowed(other / "file.txt", [tmp_path]) is False


def test_path_under_allowed_empty_list(tmp_path):
    assert op.path_under_allowed(tmp_path / "x.txt", []) is False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_records_mutation_summary(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log_path)
    try:
        record = op.audit_record(
            tool="hermes_skill_create",
            level="skills",
            apply_mode="direct",
            dry_run=False,
            success=True,
            changed=True,
            summary="created skill at /foo/SKILL.md",
            profile="default",
            skill_name="my-skill",
            content="---\nname: my-skill\ndescription: x\n---\nbody",
        )
        # The record is returned and contains the summary.
        assert record["success"] is True
        assert record["changed"] is True
        assert record["skill_name"] == "my-skill"
        # The audit file contains one line that matches.
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        loaded = json.loads(lines[0])
        assert loaded["summary"] == "created skill at /foo/SKILL.md"
        assert loaded["skill_name"] == "my-skill"
    finally:
        op.set_audit_log_override(None)


def test_audit_log_does_not_include_full_prompt_or_content(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log_path)
    try:
        secret_prompt = "Please do not tell the user about SECRETPROMPT_MARKER"
        secret_content = "---\nname: x\n---\nSECRETCONTENT_MARKER body"
        op.audit_record(
            tool="hermes_skill_create",
            level="skills",
            apply_mode="direct",
            dry_run=False,
            success=True,
            summary="test",
            prompt=secret_prompt,
            content=secret_content,
        )
        text = log_path.read_text(encoding="utf-8")
        assert "SECRETPROMPT_MARKER" not in text
        assert "SECRETCONTENT_MARKER" not in text
        loaded = json.loads(text.strip().splitlines()[0])
        assert loaded["prompt_len"] == len(secret_prompt.encode("utf-8"))
        assert loaded["content_len"] == len(secret_content.encode("utf-8"))
        assert loaded["prompt_sha256"]
        assert loaded["content_sha256"]
    finally:
        op.set_audit_log_override(None)


def test_audit_tail_returns_records(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log_path)
    try:
        for i in range(5):
            op.audit_record(
                tool="test", level="read_only", apply_mode="dry_run",
                dry_run=True, success=True, summary=f"event {i}",
            )
        records = op.audit_tail(limit=3)
        assert len(records) == 3
        assert records[0]["summary"] == "event 2"
        assert records[-1]["summary"] == "event 4"
    finally:
        op.set_audit_log_override(None)


def test_audit_tail_empty_when_no_log(tmp_path):
    log_path = tmp_path / "missing.jsonl"
    op.set_audit_log_override(log_path)
    try:
        assert op.audit_tail(limit=20) == []
    finally:
        op.set_audit_log_override(None)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_output_masks_openai_keys():
    text = "config has sk-proj-abcdefghijklmnopqrstuvwxyz_1234567890 set"
    out = op.redact_output(text)
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz_1234567890" not in out
    assert "[REDACTED_OPENAI_KEY]" in out


def test_redact_output_masks_aws_keys():
    text = "export AWS_ACCESS_KEY_ID=AKIAIO...MPLE"
    out = op.redact_output(text)
    assert "AKIAIO...MPLE" not in out
    assert "[REDACTED_AWS_KEY]" in out


def test_redact_output_masks_bearer_tokens():
    text = "Authorization: Bearer abcdefghi.jklmnopqr.stuvwx"
    out = op.redact_output(text)
    assert "abcdefghi.jklmnopqr.stuvwx" not in out
    assert "Bearer [REDACTED]" in out


@pytest.mark.parametrize(
    "text",
    [
        "api_key=abcdefghi1234567890",
        "token: ghp_abcdefghijklmnopqrstuvwxyz123456",
        "Authorization: Bearer abcdefghi.jklmnopqr.stuvwx",
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz_1234567890",
    ],
)
def test_redact_output_removes_raw_secret_values(text):
    out = op.redact_output(text)
    assert text.split("=", 1)[-1].split(":", 1)[-1].strip() not in out
    assert "[REDACTED" in out or "Bearer [REDACTED]" in out


# ---------------------------------------------------------------------------
# run_argv (shell=False guaranteed)
# ---------------------------------------------------------------------------


def test_run_argv_runs_without_shell(tmp_path):
    rc, out, err = op.run_argv(
        [sys.executable, "-c", "print('hello')"], timeout=10, workdir=str(tmp_path)
    )
    assert rc == 0
    assert "hello" in out


def test_run_argv_refuses_empty_argv():
    with pytest.raises(ValueError):
        op.run_argv([], timeout=5)


def test_run_argv_handles_missing_executable():
    rc, out, err = op.run_argv(["this-binary-does-not-exist-xyz"], timeout=5)
    assert rc == 127
    assert err  # non-empty error
