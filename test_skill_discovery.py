from __future__ import annotations

from pathlib import Path

import pytest

import server


def _skill(root: Path, name: str, description: str, *, manual: bool = False) -> dict[str, str]:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    manual_line = "disable-model-invocation: true\n" if manual else ""
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: {description}\n{manual_line}---\n\n# {name}\n",
        encoding="utf-8",
    )
    return {"name": name, "description": description, "path": str(skill_md)}


def test_skill_list_filters_queries_limits_and_manual_only(tmp_path, monkeypatch):
    primary = _skill(tmp_path / "primary", "ce-plan", "Plan implementation work")
    duplicate = _skill(tmp_path / "duplicate", "ce-plan", "lower-priority duplicate")
    manual = _skill(tmp_path / "manual", "ce-polish", "Polish completed work", manual=True)
    other = _skill(tmp_path / "other", "maps", "Map locations")

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "discover_skills", lambda: [primary, duplicate, manual, other])

    default_listing = server.hermes_skill_list(query="ce-", limit=50)
    assert "ce-plan" in default_listing
    assert "ce-polish" not in default_listing
    assert "lower-priority duplicate" not in default_listing

    full_listing = server.hermes_skill_list(query="ce-", limit=50, include_manual=True)
    assert "ce-plan" in full_listing
    assert "ce-polish" in full_listing
    assert full_listing.count("- ce-plan") == 1

    limited = server.hermes_skill_list(include_manual=True, limit=1)
    assert limited.count("\n  ") == 1


def test_skill_view_dedupes_and_reads_skill_local_chunks(tmp_path, monkeypatch):
    primary = _skill(tmp_path / "primary", "ce-plan", "Plan implementation work")
    duplicate = _skill(tmp_path / "duplicate", "ce-plan", "lower-priority duplicate")
    ref = Path(primary["path"]).parent / "references" / "research.md"
    ref.parent.mkdir()
    ref.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "discover_skills", lambda: [primary, duplicate])

    rendered = server.hermes_skill_view(
        "ce-plan", file_path="references/research.md", offset=2, limit=2
    )
    assert rendered.startswith("two\nthree")
    assert "2 lines remain" in rendered
    assert "Multiple skills matched" not in rendered


def test_skill_view_rejects_traversal(tmp_path, monkeypatch):
    primary = _skill(tmp_path / "primary", "ce-plan", "Plan implementation work")
    monkeypatch.setattr(server, "require_imports", lambda: None)
    monkeypatch.setattr(server, "discover_skills", lambda: [primary])

    with pytest.raises(RuntimeError, match="file_path must be a relative path"):
        server.hermes_skill_view("ce-plan", file_path="../secret.txt")


def test_skill_tools_have_model_facing_progressive_loading_descriptions():
    assert "progressive model loading" in (server.hermes_skill_list.__doc__ or "")
    assert "skill-local text files" in (server.hermes_skill_view.__doc__ or "")
