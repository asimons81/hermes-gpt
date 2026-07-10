"""Tests for operator_skills tools using temp profile homes."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import operator_policy as op
import operator_skills as osk


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "skills").mkdir(parents=True)
    (root / "profiles" / "hermes-researcher" / "skills").mkdir(parents=True)
    (root / "profiles" / "target-profile" / "skills").mkdir(parents=True)
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


_VALID_FRONTMATTER = """---
name: my-skill
description: A test skill.
---

# My Skill

Do the thing.
"""


def _make_skill(profile_home: Path, name: str, content: str = _VALID_FRONTMATTER) -> Path:
    skill_dir = profile_home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# diff (read-only)
# ---------------------------------------------------------------------------


def test_skill_diff_returns_diff_and_does_not_mutate(hermes_root, clean_env, audit_override):
    skill_dir = _make_skill(hermes_root, "my-skill")
    out = osk.hermes_skill_diff(
        profile="default", name="my-skill",
        proposed_content=_VALID_FRONTMATTER + "extra line\n",
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert "diff" in parsed
    assert "+extra line" in parsed["diff"]
    # File unchanged.
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == _VALID_FRONTMATTER


def test_skill_diff_refuses_invalid_skill_name(hermes_root, clean_env, audit_override):
    out = osk.hermes_skill_diff(
        profile="default", name="BAD NAME", proposed_content="x",
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "Invalid skill name" in parsed["error"]


def test_skill_diff_refuses_nonexistent_skill(hermes_root, clean_env, audit_override):
    out = osk.hermes_skill_diff(
        profile="default", name="missing-skill", proposed_content="x",
        hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not found" in parsed["error"]


# ---------------------------------------------------------------------------
# Mutation gating
# ---------------------------------------------------------------------------


def test_skill_create_refuses_when_operator_disabled(hermes_root, clean_env, audit_override):
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "disabled" in parsed["error"]


def test_skill_create_refuses_in_read_only_mode(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "does not satisfy required level" in parsed["error"]


def test_skill_create_direct_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(kwargs["content"], encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "create"
    assert calls[0]["name"] == "new-skill"
    assert (hermes_root / "skills" / "new-skill" / "SKILL.md").exists()


def test_skill_create_refuses_direct_mutation_without_skill_manager(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: None, raising=False)
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "skill manager" in parsed["error"].lower()


def test_skill_create_lazy_import_can_recover_after_initial_failure(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setattr(osk, "_skill_manager_module", None, raising=False)
    attempts = {"count": 0}

    class FakeSkillManager:
        @staticmethod
        def skill_manage(**kwargs):
            return json.dumps({"success": True, "path": "ok"}, indent=2)

    def fake_import(name):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ImportError("not ready yet")
        return FakeSkillManager

    monkeypatch.setattr(osk.importlib, "import_module", fake_import)
    assert osk._get_skill_manager(hermes_root) is None
    assert osk._get_skill_manager(hermes_root) is FakeSkillManager


def test_skill_create_dry_run_returns_plan_and_does_not_mutate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert parsed["plan"]["would_create"] is True
    assert not (hermes_root / "skills" / "new-skill").exists()


def test_skill_create_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(kwargs["content"], encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_create(
        profile="default", name="new-skill", content=_VALID_FRONTMATTER,
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "create"
    assert (hermes_root / "skills" / "new-skill" / "SKILL.md").exists()


def test_skill_create_refuses_invalid_frontmatter(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    out = osk.hermes_skill_create(
        profile="default", name="bad-skill", content="no frontmatter here",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "frontmatter" in parsed["error"].lower()


def test_skill_create_refuses_duplicate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    out = osk.hermes_skill_create(
        profile="default", name="my-skill", content=_VALID_FRONTMATTER,
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "already exists" in parsed["error"]


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


def test_skill_edit_direct_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / "SKILL.md"
            target.write_text(kwargs["content"], encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    edited = _VALID_FRONTMATTER.replace("Do the thing.", "Do the edited thing.")
    out = osk.hermes_skill_edit(
        profile="default", name="my-skill", content=edited,
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "edit"
    assert "Do the edited thing." in (hermes_root / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8")


def test_skill_patch_dry_run_returns_diff_and_does_not_mutate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    skill_dir = _make_skill(hermes_root, "my-skill")
    original = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    out = osk.hermes_skill_patch(
        profile="default", name="my-skill",
        old_string="Do the thing.", new_string="Do the new thing.",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert "+Do the new thing." in parsed["plan"]["diff"]
    # File unchanged.
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == original


def test_skill_patch_direct_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    skill_dir = _make_skill(hermes_root, "my-skill")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / (kwargs.get("file_path") or "SKILL.md")
            target.write_text((skill_dir / "SKILL.md").read_text(encoding="utf-8").replace("Do the thing.", kwargs["new_string"]), encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_patch(
        profile="default", name="my-skill",
        old_string="Do the thing.", new_string="Do the new thing.",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "patch"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Do the new thing." in text


def test_skill_patch_refuses_direct_mutation_without_skill_manager(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: None, raising=False)
    out = osk.hermes_skill_patch(
        profile="default", name="my-skill",
        old_string="Do the thing.", new_string="Do the new thing.",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "skill manager" in parsed["error"].lower()


def test_skill_patch_refuses_ambiguous_match(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    content = _VALID_FRONTMATTER + "duplicate\nduplicate\n"
    skill_dir = _make_skill(hermes_root, "my-skill", content=content)

    out = osk.hermes_skill_patch(
        profile="default", name="my-skill",
        old_string="duplicate", new_string="unique",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "multiple locations" in parsed["error"]


def test_skill_patch_replace_all(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    content = _VALID_FRONTMATTER + "duplicate\nduplicate\n"
    skill_dir = _make_skill(hermes_root, "my-skill", content=content)
    calls = []

    class FakeSkillManager:
        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / "SKILL.md"
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace(kwargs["old_string"], kwargs["new_string"]), encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_patch(
        profile="default", name="my-skill",
        old_string="duplicate", new_string="unique",
        replace_all=True, dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["match_count"] == 2
    assert calls and calls[0]["action"] == "patch"


# ---------------------------------------------------------------------------
# write_file path safety
# ---------------------------------------------------------------------------


def test_skill_write_file_rejects_path_traversal(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")

    out = osk.hermes_skill_write_file(
        profile="default", name="my-skill",
        file_path="../../etc/passwd", file_content="bad",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "traversal" in parsed["error"].lower() or "escape" in parsed["error"].lower()


def test_skill_write_file_rejects_secret_looking_filenames(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")

    for bad_name in ["token.txt", "secret.env", "id_rsa", "credentials.json", ".env"]:
        out = osk.hermes_skill_write_file(
            profile="default", name="my-skill",
            file_path=f"references/{bad_name}", file_content="x",
            dry_run=False, hermes_root=hermes_root,
        )
        parsed = json.loads(out)
        assert parsed["success"] is False, f"{bad_name} should be refused"
        assert "secret" in parsed["error"].lower() or "denied" in parsed["error"].lower() or "refus" in parsed["error"].lower()


def test_skill_write_file_rejects_paths_outside_allowed_subdirs(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")

    out = osk.hermes_skill_write_file(
        profile="default", name="my-skill",
        file_path="random/random.txt", file_content="x",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "must be under" in parsed["error"]


def test_skill_write_file_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            target = hermes_root / "skills" / kwargs["name"] / kwargs["file_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(kwargs["file_content"], encoding="utf-8")
            return json.dumps({"success": True, "path": str(target)}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_write_file(
        profile="default", name="my-skill",
        file_path="references/guide.md", file_content="# Guide\n",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "write_file"
    assert (hermes_root / "skills" / "my-skill" / "references" / "guide.md").exists()


def test_skill_write_file_refuses_direct_mutation_without_skill_manager(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: None, raising=False)
    out = osk.hermes_skill_write_file(
        profile="default", name="my-skill",
        file_path="references/guide.md", file_content="# Guide\n",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "skill manager" in parsed["error"].lower()


# ---------------------------------------------------------------------------
# copy / sync / delete
# ---------------------------------------------------------------------------


def test_skill_copy_dry_run_lists_files(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    skill_dir = _make_skill(hermes_root, "my-skill")
    (skill_dir / "references").mkdir(exist_ok=True)
    (skill_dir / "references" / "guide.md").write_text("guide", encoding="utf-8")

    out = osk.hermes_skill_copy(
        source_profile="default", target_profile="hermes-researcher",
        name="my-skill", dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert parsed["plan"]["file_count"] >= 2  # SKILL.md + guide.md
    # Target unchanged.
    assert not (hermes_root / "profiles" / "hermes-researcher" / "skills" / "my-skill").exists()


def test_skill_copy_direct_refuses_mutation(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    skill_dir = _make_skill(hermes_root, "my-skill")
    (skill_dir / "references").mkdir(exist_ok=True)
    (skill_dir / "references" / "guide.md").write_text("guide", encoding="utf-8")

    out = osk.hermes_skill_copy(
        source_profile="default", target_profile="hermes-researcher",
        name="my-skill", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not supported safely" in parsed["error"].lower()
    assert not (hermes_root / "profiles" / "hermes-researcher" / "skills" / "my-skill").exists()


def test_skill_copy_refuses_when_target_already_has_skill(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _make_skill(hermes_root, "my-skill")
    _make_skill(hermes_root / "profiles" / "hermes-researcher", "my-skill")

    out = osk.hermes_skill_copy(
        source_profile="default", target_profile="hermes-researcher",
        name="my-skill", dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not supported safely" in parsed["error"].lower()


def test_skill_sync_to_default_refuses_direct_mutation(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PROFILES_ENV, "default,hermes-researcher")
    _make_skill(hermes_root / "profiles" / "hermes-researcher", "research-skill")

    out = osk.hermes_skill_sync_to_default(
        source_profile="hermes-researcher", name="research-skill",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not supported safely" in parsed["error"].lower()
    assert not (hermes_root / "skills" / "research-skill").exists()


def test_skill_delete_dry_run_does_not_mutate(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    skill_dir = _make_skill(hermes_root, "my-skill")

    out = osk.hermes_skill_delete(
        profile="default", name="my-skill",
        dry_run=True, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert parsed["dry_run"] is True
    assert skill_dir.exists()


def test_skill_delete_direct_uses_skill_manager_when_available(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    skill_dir = _make_skill(hermes_root, "my-skill")
    calls = []

    class FakeSkillManager:
        SKILLS_DIR = hermes_root / "skills"

        @staticmethod
        def skill_manage(**kwargs):
            calls.append(kwargs)
            shutil.rmtree(skill_dir)
            return json.dumps({"success": True, "message": "deleted"}, indent=2)

    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: FakeSkillManager, raising=False)
    out = osk.hermes_skill_delete(
        profile="default", name="my-skill",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is True
    assert calls and calls[0]["action"] == "delete"
    assert not skill_dir.exists()


def test_skill_delete_refuses_direct_mutation_without_skill_manager(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    _make_skill(hermes_root, "my-skill")
    monkeypatch.setattr(osk, "_get_skill_manager", lambda hermes_root=None: None, raising=False)
    out = osk.hermes_skill_delete(
        profile="default", name="my-skill",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    err_lower = parsed["error"].lower()
    assert "skill manager" in err_lower or "skill usage metadata" in err_lower


def test_skill_delete_refuses_nonexistent(hermes_root, clean_env, audit_override, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "skills")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    out = osk.hermes_skill_delete(
        profile="default", name="missing-skill",
        dry_run=False, hermes_root=hermes_root,
    )
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "not found" in parsed["error"]
