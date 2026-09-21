"""Tests for operator_config tools using temp profile homes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_policy as op
import operator_config as ocfg


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "profiles" / "hermes-researcher").mkdir(parents=True)
    return root


@pytest.fixture
def clean_env(monkeypatch):
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
def audit_override(tmp_path):
    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    yield log
    op.set_audit_log_override(None)


_SAMPLE_CONFIG = """\
model:
  default: claude-sonnet-4
  provider: anthropic
gateway:
  port: 8642
telegram:
  bot_token: SUPER_SECRET_TELEGRAM_TOKEN
  home_channel: -1001234567890
memory:
  enabled: true
"""


def _write_config(profile_home: Path, content: str = _SAMPLE_CONFIG) -> None:
    (profile_home / "config.yaml").write_text(content, encoding="utf-8")


def _write_env(profile_home: Path, content: str) -> None:
    (profile_home / ".env").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# config_get
# ---------------------------------------------------------------------------


def test_config_get_redacts_secret_like_keys(hermes_root, clean_env, audit_override):
    _write_config(hermes_root)
    out = ocfg.hermes_config_get(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    text = json.dumps(parsed)
    assert "SUPER_SECRET_TELEGRAM_TOKEN" not in text
    # The telegram.bot_token key should be redacted.
    telegram = parsed["value"].get("telegram", {})
    assert telegram.get("bot_token") == "<redacted>"
    # Non-secret values are visible.
    assert parsed["value"]["model"]["default"] == "claude-sonnet-4"
    assert parsed["value"]["telegram"]["home_channel"] == -1001234567890


def test_config_get_specific_secret_key_returns_none(hermes_root, clean_env, audit_override):
    _write_config(hermes_root)
    out = ocfg.hermes_config_get(
        profile="default", key_path="telegram.bot_token", hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["value"] is None
    assert parsed["redacted"] is True


def test_config_get_specific_safe_key(hermes_root, clean_env, audit_override):
    _write_config(hermes_root)
    out = ocfg.hermes_config_get(
        profile="default", key_path="model.default", hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["value"] == "claude-sonnet-4"


def test_config_get_missing_file_returns_none(hermes_root, clean_env, audit_override):
    out = ocfg.hermes_config_get(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["exists"] is False


# ---------------------------------------------------------------------------
# config_set
# ---------------------------------------------------------------------------


def test_config_set_rejects_secret_like_keys(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_config(hermes_root)

    out = ocfg.hermes_config_set(
        profile="default", key_path="telegram.bot_token",
        value="new-secret-value", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "secret" in parsed["error"].lower()


def test_config_set_dry_run_returns_diff(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    _write_config(hermes_root)

    out = ocfg.hermes_config_set(
        profile="default", key_path="model.default",
        value="claude-opus-4", dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert parsed["plan"]["before"] == "claude-sonnet-4"
    assert parsed["plan"]["after"] == "claude-opus-4"


def test_config_set_direct_writes(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_config(hermes_root)

    out = ocfg.hermes_config_set(
        profile="default", key_path="gateway.port",
        value=9999, dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    import yaml
    new_cfg = yaml.safe_load((hermes_root / "config.yaml").read_text(encoding="utf-8"))
    assert new_cfg["gateway"]["port"] == 9999
    # Backup file was created.
    backups = list(hermes_root.glob("config.yaml.bak.*"))
    assert len(backups) == 1


def test_config_set_refuses_when_disabled(hermes_root, clean_env, audit_override):
    out = ocfg.hermes_config_set(
        profile="default", key_path="model.default",
        value="x", dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "disabled" in parsed["error"] or "level" in parsed["error"].lower()


# ---------------------------------------------------------------------------
# config_patch
# ---------------------------------------------------------------------------


def test_config_patch_dry_run_returns_diff(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    _write_config(hermes_root)

    out = ocfg.hermes_config_patch(
        profile="default", old_string="claude-sonnet-4", new_string="claude-opus-4",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert "claude-opus-4" in parsed["plan"]["diff"]


@pytest.mark.parametrize(
    "old_string,new_string",
    [
        ("api_key=old", "claude-opus-4"),
        ("token=old", "claude-opus-4"),
        ("secret=old", "claude-opus-4"),
        ("password=old", "claude-opus-4"),
        ("auth=old", "claude-opus-4"),
    ],
)
def test_config_patch_refuses_secret_old_string(hermes_root, clean_env, audit_override, monkeypatch, old_string, new_string):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_config(hermes_root, content=f"{old_string}\nmodel:\n  default: claude-sonnet-4\n")

    out = ocfg.hermes_config_patch(
        profile="default", old_string=old_string,
        new_string=new_string, dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "secret" in parsed["error"].lower()
    assert old_string not in json.dumps(parsed)


@pytest.mark.parametrize(
    "new_string",
    [
        "api_key: abc123",
        "token: abc123",
        "secret: abc123",
        "password: abc123",
        "auth: abc123",
    ],
)
def test_config_patch_refuses_secret_new_string(hermes_root, clean_env, audit_override, monkeypatch, new_string):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_config(hermes_root)

    out = ocfg.hermes_config_patch(
        profile="default", old_string="claude-sonnet-4",
        new_string=new_string, dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "secret" in parsed["error"].lower()
    assert new_string not in json.dumps(parsed)


def test_config_patch_refuses_ambiguous_match(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_config(hermes_root, content="duplicate\nduplicate\n")

    out = ocfg.hermes_config_patch(
        profile="default", old_string="duplicate", new_string="unique",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "multiple" in parsed["error"].lower()


# ---------------------------------------------------------------------------
# env_status
# ---------------------------------------------------------------------------


def test_env_status_returns_set_unset_only_no_values(hermes_root, clean_env, audit_override):
    _write_env(
        hermes_root,
        "# comment\nTELEGRAM_HOME_CHANNEL=-1001234567890\nANTHROPIC_API_KEY=sk-abc\n",
    )

    out = ocfg.hermes_env_status(
        profile="default",
        keys=["TELEGRAM_HOME_CHANNEL", "ANTHROPIC_API_KEY", "MISSING_KEY"],
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    text = json.dumps(parsed)
    # No actual values are returned.
    assert "-1001234567890" not in text
    assert "sk-abc" not in text
    # Each key reports set/unset + secret_like.
    by_key = {entry["key"]: entry for entry in parsed["keys"]}
    assert by_key["TELEGRAM_HOME_CHANNEL"]["set"] is True
    assert by_key["TELEGRAM_HOME_CHANNEL"]["secret_like"] is False
    assert by_key["ANTHROPIC_API_KEY"]["set"] is True
    assert by_key["ANTHROPIC_API_KEY"]["secret_like"] is True
    assert by_key["MISSING_KEY"]["set"] is False


def test_env_status_full_summary_lists_keys_only(hermes_root, clean_env, audit_override):
    _write_env(hermes_root, "FOO=bar\nBAZ=qux\n")
    out = ocfg.hermes_env_status(profile="default", hermes_root=hermes_root)
    parsed = json.loads(out)
    assert parsed["success"] is True
    keys = {entry["key"] for entry in parsed["keys"]}
    assert "FOO" in keys
    assert "BAZ" in keys
    text = json.dumps(parsed)
    assert "bar" not in text
    assert "qux" not in text


# ---------------------------------------------------------------------------
# env_set_nonsecret
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "TELEGRAM_TOKEN",
        "ANTHROPIC_API_KEY",
        "MY_SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "AUTH_TOKEN",
        "COOKIE_VALUE",
        "PRIVATE_KEY",
        "OAUTH_TOKEN",
        "LD_PRELOAD",
        "PYTHONPATH",
        "PATH",
        "EDITOR",
        "HERMES_HOME",
    ],
)
def test_env_set_nonsecret_rejects_secret_or_dangerous_keys(
    hermes_root, clean_env, audit_override, monkeypatch, bad_key
):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_env(hermes_root, "")

    out = ocfg.hermes_env_set_nonsecret(
        profile="default", key=bad_key, value="x",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "secret" in parsed["error"].lower() or "denylist" in parsed["error"].lower() or "dangerous" in parsed["error"].lower()


@pytest.mark.parametrize(
    "good_key",
    [
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_THREAD_ID",
        "TELEGRAM_CRON_THREAD_ID",
        "HERMES_PROFILE_LABEL",
        "HERMES_TOOL_PROGRESS",
    ],
)
def test_env_set_nonsecret_accepts_non_secret_keys(
    hermes_root, clean_env, audit_override, monkeypatch, good_key
):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_env(hermes_root, "")

    out = ocfg.hermes_env_set_nonsecret(
        profile="default", key=good_key, value="some-value",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True, f"{good_key} should be accepted: {parsed}"

    # Verify the value was written.
    text = (hermes_root / ".env").read_text(encoding="utf-8")
    assert f"{good_key}=some-value" in text


def test_env_set_nonsecret_dry_run_does_not_write(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    _write_env(hermes_root, "# keep me\nEXISTING=old\n")

    out = ocfg.hermes_env_set_nonsecret(
        profile="default", key="TELEGRAM_HOME_CHANNEL", value="-100",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    text = (hermes_root / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_HOME_CHANNEL" not in text
    assert "EXISTING=old" in text  # untouched


def test_env_set_nonsecret_preserves_comments_and_replaces_existing(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _write_env(hermes_root, "# header comment\nTELEGRAM_HOME_CHANNEL=-100\nOTHER=value\n")

    out = ocfg.hermes_env_set_nonsecret(
        profile="default", key="TELEGRAM_HOME_CHANNEL", value="-200",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    text = (hermes_root / ".env").read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "TELEGRAM_HOME_CHANNEL=-200" in text
    assert "OTHER=value" in text
    # Old value replaced, not duplicated.
    assert text.count("TELEGRAM_HOME_CHANNEL=") == 1


# ---------------------------------------------------------------------------
# env_copy_nonsecret
# ---------------------------------------------------------------------------


def test_env_copy_nonsecret_copies_without_printing_value(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_env(hermes_root, "TELEGRAM_HOME_CHANNEL=-1001234567890\n")
    _write_env(hermes_root / "profiles" / "hermes-researcher", "")

    out = ocfg.hermes_env_copy_nonsecret(
        source_profile="default", target_profile="hermes-researcher",
        key="TELEGRAM_HOME_CHANNEL", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    text = json.dumps(parsed)
    assert "-1001234567890" not in text  # value never printed
    # Value was actually written to the target.
    target_text = (hermes_root / "profiles" / "hermes-researcher" / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_HOME_CHANNEL=-1001234567890" in target_text


def test_env_copy_nonsecret_refuses_secret_key(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_env(hermes_root, "ANTHROPIC_API_KEY=sk-abc\n")

    out = ocfg.hermes_env_copy_nonsecret(
        source_profile="default", target_profile="hermes-researcher",
        key="ANTHROPIC_API_KEY", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "secret" in parsed["error"].lower()


def test_env_copy_nonsecret_refuses_missing_key_in_source(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills_config")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _write_env(hermes_root, "")

    out = ocfg.hermes_env_copy_nonsecret(
        source_profile="default", target_profile="hermes-researcher",
        key="TELEGRAM_HOME_CHANNEL", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not set" in parsed["error"].lower()
