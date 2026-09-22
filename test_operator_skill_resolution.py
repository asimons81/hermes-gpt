"""Regression coverage for canonical profile-aware skill resolution."""

from __future__ import annotations

from pathlib import Path

import operator_skill_resolution as resolution


def _skill(root: Path, relative: str, *, name: str | None = None) -> Path:
    directory = root / relative
    directory.mkdir(parents=True)
    frontmatter = ""
    if name is not None:
        frontmatter = f"---\nname: {name}\ndescription: test skill\n---\n"
    path = directory / "SKILL.md"
    path.write_text(frontmatter + "# test\n", encoding="utf-8")
    return path


def test_profile_resolution_distinguishes_global_existence_from_profile_loadability(tmp_path: Path):
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
