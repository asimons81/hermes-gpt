"""Regression coverage for canonical profile-aware skill resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_skill_resolution as resolution


def _use_real_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt out of the hermetic conftest fixture; skip without an Agent checkout.

    The repo suite isolates tests from any Hermes Agent checkout via
    ``_skill_loader_override``. Tests below that prove equivalence against the
    real loader must null that fixture and need a real checkout; upstream CI
    without one skips instead of failing.
    """
    monkeypatch.setattr(resolution, "_skill_loader_override", None)
    if resolution._agent_modules() is None:
        pytest.skip("Hermes Agent checkout not available")


def _skill(
    root: Path,
    relative: str,
    *,
    name: str | None = None,
    frontmatter_extra: str = "",
) -> Path:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    frontmatter = ""
    if name is not None:
        frontmatter = (
            f"---\nname: {name}\ndescription: test skill\n{frontmatter_extra}---\n"
        )
    path = directory / "SKILL.md"
    path.write_text(frontmatter + "# test\n", encoding="utf-8")
    return path


def test_profile_resolution_distinguishes_global_existence_from_profile_loadability(
    tmp_path: Path,
):
    root = tmp_path / "hermes"
    _skill(root / "skills", "default-only", name="default-only")
    _skill(root / "profiles" / "dev" / "skills", "dev-only", name="dev-only")

    catalog = resolution.build_catalog(root)
    resolved = resolution.resolve_name("default-only", catalog)

    assert resolved.exists is True
    assert resolved.defined_in == ("default",)
    assert resolved.available_to == ("default",)

    rejection = resolution.validate_required_skills(
        "dev", ["default-only"], root, catalog=catalog
    )
    assert rejection is not None
    assert rejection["error"] == resolution.ERROR_NOT_RESOLVABLE
    assert rejection["skills"][0]["available_profiles"] == ["default"]


def test_resolution_reports_missing_skill_separately(tmp_path: Path):
    root = tmp_path / "hermes"
    _skill(root / "profiles" / "dev" / "skills", "dev-only", name="dev-only")

    rejection = resolution.validate_required_skills("dev", ["ghost-skill"], root)

    assert rejection is not None
    assert rejection["error"] == resolution.ERROR_NOT_FOUND
    assert rejection["skills_not_found"] == ["ghost-skill"]


def test_nested_frontmatter_skill_uses_same_profile_semantics(tmp_path: Path):
    root = tmp_path / "hermes"
    _skill(
        root / "profiles" / "dev" / "skills",
        "category/nested-directory",
        name="nested-skill",
    )

    catalog = resolution.build_catalog(root)
    resolved = resolution.resolve_name("nested-skill", catalog)

    assert resolved.defined_in == ("dev",)
    assert resolution.validate_required_skills("dev", ["nested-skill"], root) is None
    assert [entry.name for entry in resolution.profile_skill_entries("dev", root)] == [
        "nested-skill"
    ]


class _StubSkillsTool:
    """Agent-loader-shaped stub: empty catalog, explicit-load authority."""

    def __init__(self, loadable: dict[str, str]):
        self._loadable = loadable

    def _find_all_skills(self):
        return []

    def skill_view(self, name):
        requested = str(name).strip()
        if requested in self._loadable:
            return json.dumps(
                {
                    "success": True,
                    "name": requested,
                    "description": self._loadable[requested],
                    "content": "# stub\n",
                    "path": f"{requested}/SKILL.md",
                }
            )
        return json.dumps(
            {"success": False, "error": f"Skill '{requested}' not found."}
        )

    def skill_matches_platform(self, frontmatter):
        return True

    def _is_skill_disabled(self, name):
        return False


def test_plugin_qualified_skill_resolves_via_explicit_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Plugin skills must resolve via the explicit-load path, not the catalog.

    The stub catalog is empty (what the old ``_find_all_skills``-only gate
    saw); the explicit probe serves ``myplugin:myskill``. The gate accepts.
    """
    root = tmp_path / "hermes"
    (root / "profiles" / "dev").mkdir(parents=True)
    monkeypatch.setattr(resolution, "_skill_loader_override", None)
    stub = _StubSkillsTool({"myplugin:myskill": "plugin-provided skill"})
    monkeypatch.setattr(
        resolution, "_require_agent_modules", lambda: (stub, object())
    )

    assert (
        resolution.validate_required_skills("dev", ["myplugin:myskill"], root) is None
    )


def test_bare_plugin_short_name_is_not_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bare short names of plugin skills stay rejected (qualified form only)."""
    root = tmp_path / "hermes"
    (root / "profiles" / "dev").mkdir(parents=True)
    monkeypatch.setattr(resolution, "_skill_loader_override", None)
    stub = _StubSkillsTool({"myplugin:myskill": "plugin-provided skill"})
    monkeypatch.setattr(
        resolution, "_require_agent_modules", lambda: (stub, object())
    )

    rejection = resolution.validate_required_skills("dev", ["myskill"], root)
    assert rejection is not None
    assert rejection["error"] == resolution.ERROR_NOT_FOUND


def test_loader_unavailable_is_distinct_from_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Import/discovery failures fail closed, never as ``skill_not_found``."""

    def _down():
        raise resolution._LoaderUnavailable("no Agent checkout")

    monkeypatch.setattr(resolution, "_skill_loader_override", None)
    monkeypatch.setattr(resolution, "_require_agent_modules", _down)
    root = tmp_path / "hermes"

    rejection = resolution.validate_required_skills("dev", ["any-skill"], root)
    assert rejection is not None
    assert rejection["error"] == resolution.ERROR_UNAVAILABLE

    with pytest.raises(resolution.SkillRequirementsError) as excinfo:
        resolution.require_required_skills("dev", ["any-skill"], root)
    assert excinfo.value.rejection["error"] == resolution.ERROR_UNAVAILABLE


def test_environment_filtered_skill_matches_explicit_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``environments:`` hides a skill from offer surfaces but not the gate.

    ``s6`` is inactive on dev/test hosts, so ``_find_all_skills`` filters the
    skill out while ``skill_view`` (and therefore ``--skills``) still loads it.
    The gate must follow the explicit load and accept.
    """
    _use_real_loader(monkeypatch)
    root = tmp_path / "hermes"
    _skill(
        root / "profiles" / "dev" / "skills",
        "env-only",
        name="env-only",
        frontmatter_extra="environments: [s6]\n",
    )

    assert "env-only" not in [
        entry.name for entry in resolution.profile_skill_entries("dev", root)
    ]
    ok, _detail = resolution._explicit_load_ok("dev", "env-only", root)
    assert ok is True
    assert resolution.validate_required_skills("dev", ["env-only"], root) is None


def test_disabled_skill_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_real_loader(monkeypatch)
    root = tmp_path / "hermes"
    _skill(root / "profiles" / "dev" / "skills", "gated", name="gated")
    assert resolution.validate_required_skills("dev", ["gated"], root) is None

    config = root / "profiles" / "dev" / "config.yaml"
    config.write_text("skills:\n  disabled:\n    - gated\n", encoding="utf-8")

    rejection = resolution.validate_required_skills("dev", ["gated"], root)
    assert rejection is not None
    assert rejection["error"] in (
        resolution.ERROR_NOT_FOUND,
        resolution.ERROR_NOT_RESOLVABLE,
    )


def test_removed_skill_rejected_after_delete_and_with_stale_catalog(tmp_path: Path):
    """A skill removed between planning and dispatch fails the live check."""
    root = tmp_path / "hermes"
    skill_dir = root / "profiles" / "dev" / "skills" / "ephemeral"
    _skill(root / "profiles" / "dev" / "skills", "ephemeral", name="ephemeral")
    assert resolution.validate_required_skills("dev", ["ephemeral"], root) is None

    planning_catalog = resolution.build_catalog(root)
    assert resolution.resolve_name("ephemeral", planning_catalog).exists is True

    for child in sorted(skill_dir.rglob("*")):
        if child.is_file():
            child.unlink()

    fresh = resolution.validate_required_skills("dev", ["ephemeral"], root)
    assert fresh is not None
    assert fresh["error"] == resolution.ERROR_NOT_FOUND

    stale = resolution.validate_required_skills(
        "dev", ["ephemeral"], root, catalog=planning_catalog
    )
    assert stale is not None
    assert stale["error"] == resolution.ERROR_NOT_RESOLVABLE
    assert "explicit-load" in stale["skills"][0]["reason"]


def test_cross_profile_reassignment_with_existing_skill_rejected(tmp_path: Path):
    """Profile A -> profile B with an existing skill rejects with provenance."""
    root = tmp_path / "hermes"
    _skill(root / "profiles" / "dev" / "skills", "only-dev", name="only-dev")
    (root / "profiles" / "prod").mkdir(parents=True)

    rejection = resolution.validate_required_skills("prod", ["only-dev"], root)
    assert rejection is not None
    assert rejection["error"] == resolution.ERROR_NOT_RESOLVABLE
    assert rejection["skills"][0]["available_profiles"] == ["dev"]


def test_real_loader_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """At least one test exercises the real Hermes Agent loader end to end."""
    _use_real_loader(monkeypatch)
    root = tmp_path / "hermes"
    _skill(root / "profiles" / "dev" / "skills", "real", name="real")

    skills_tool, _constants = resolution._require_agent_modules()
    assert callable(getattr(skills_tool, "skill_view", None))

    ok, _detail = resolution._explicit_load_ok("dev", "real", root)
    assert ok is True
    assert resolution.validate_required_skills("dev", ["real"], root) is None


def test_rejected_validation_mutates_nothing(tmp_path: Path):
    """A rejection leaves plan-adjacent state byte-identical (no mutation)."""
    root = tmp_path / "hermes"
    _skill(root / "profiles" / "dev" / "skills", "kept", name="kept")
    sentinel = root / "profiles" / "dev" / "sentinel.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")
    before = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    rejection = resolution.validate_required_skills("dev", ["ghost-skill"], root)
    assert rejection is not None

    after = {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    assert after == before
