"""Skill operator tools for hermes-gpt.

All operations are tier-gated by ``OperatorPolicy``:

- ``hermes_skill_diff``           : read_only  — preview only
- ``hermes_skill_create``         : skills     — create a new SKILL.md
- ``hermes_skill_edit``           : skills     — full rewrite of SKILL.md
- ``hermes_skill_patch``          : skills     — find-and-replace within a file
- ``hermes_skill_write_file``     : skills     — supporting file (refs/templates/scripts/assets)
- ``hermes_skill_copy``           : skills     — copy a skill across profiles
- ``hermes_skill_sync_to_default``: skills     — convenience: copy <profile> -> default
- ``hermes_skill_delete``         : skills     — delete (respects Hermes pin guard)

Safety rules:
- No path traversal: skill name is validated; supporting file paths must
  resolve inside the skill directory.
- No secret-looking filenames in write_file.
- Skill directories are scoped to the profile's ``skills/`` root. Bundled
  skills under ``<hermes_root>/skills`` may be read (via diff) but the
  default write target is the profile's user skills dir.
- Dry-run is the default; direct mode requires operator enabled + level
  >= skills + apply_mode=direct + dry_run=false.
- Audit logs skill name + content hash/length; never the raw content.
"""

from __future__ import annotations

import hashlib
import json
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import operator_policy as op

_skill_manager_module: Any | None = None

# Skill name rules mirror Hermes' skill_manager_tool.
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_NAME_LENGTH = 64
_MAX_CONTENT_CHARS = 100_000
_MAX_FILE_BYTES = 1_048_576

# Supporting-file subdirs allowed under a skill directory.
_ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}

# Filenames that look secret-like and must be refused by write_file.
# Two checks: (1) basename contains a secret-like substring anywhere;
# (2) basename ends with a secret-like extension.
_SECRET_FILE_SUBSTRING_RE = re.compile(
    r"(?i)(token|secret|credential|oauth|cookie|private|password|passwd|"
    r"id_rsa|id_ed25519|authorized_keys)"
)
_SECRET_FILE_SUFFIX_RE = re.compile(
    r"(?i)(\.env|\.pem|\.key)$"
)


def _is_secret_filename(name: str) -> bool:
    if _SECRET_FILE_SUBSTRING_RE.search(name):
        return True
    if _SECRET_FILE_SUFFIX_RE.search(name):
        return True
    if name.lower() == ".env":
        return True
    return False


def _validate_skill_name(name: str) -> str:
    if not name:
        raise ValueError("Skill name is required.")
    if len(name) > _MAX_NAME_LENGTH:
        raise ValueError(f"Skill name exceeds {_MAX_NAME_LENGTH} characters.")
    if not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid skill name {name!r}. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return name


def _skills_dir(profile_home: Path) -> Path:
    return profile_home / "skills"


def _skill_dir(profile_home: Path, name: str) -> Path:
    return _skills_dir(profile_home) / _validate_skill_name(name)


def _find_skill_dir(profile_home: Path, name: str) -> Optional[Path]:
    """Find a skill by name under the profile's skills dir.

    Mirrors Hermes' walk: rglob SKILL.md, match parent dir name.
    """
    canon = _validate_skill_name(name)
    root = _skills_dir(profile_home)
    if not root.is_dir():
        return None
    try:
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == canon:
                return skill_md.parent
    except OSError:
        return None
    return None


def _resolve_supporting_file(skill_dir: Path, file_path: str) -> Path:
    """Resolve a supporting-file path inside ``skill_dir`` and refuse escapes.

    Accepts ``SKILL.md`` and ``<name>/SKILL.md`` as spellings for the main
    file. Anything else must be under references/ templates/ scripts/ assets/.
    Refuses path traversal and secret-looking filenames.
    """
    from_parts = Path(file_path).parts
    if not from_parts:
        raise ValueError("file_path is required.")

    # Path traversal check.
    if ".." in from_parts:
        raise ValueError("Path traversal ('..') is not allowed.")
    if Path(file_path).is_absolute():
        raise ValueError("Absolute paths are not allowed.")

    # SKILL.md main file.
    if from_parts[-1] == "SKILL.md" and (len(from_parts) == 1 or len(from_parts) == 2):
        target = skill_dir / "SKILL.md"
        # Containment check.
        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            raise ValueError("File path escapes the skill directory.")
        return target

    # Supporting file must be under an allowed subdir.
    if from_parts[0] not in _ALLOWED_SUBDIRS:
        raise ValueError(
            f"File must be under one of: {', '.join(sorted(_ALLOWED_SUBDIRS))} "
            f"(or be SKILL.md). Got: {file_path!r}"
        )
    if len(from_parts) < 2:
        raise ValueError(
            f"Provide a file path, not just a directory. "
            f"Example: '{from_parts[0]}/example.md'"
        )

    target = skill_dir / Path(*from_parts)
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        raise ValueError("File path escapes the skill directory.")

    # Secret-looking filename refusal.
    if _is_secret_filename(from_parts[-1]):
        raise ValueError(
            f"Refusing to write secret-looking filename {from_parts[-1]!r}."
        )

    return target


def _hash_content(content: str | None) -> tuple[int, str]:
    if not content:
        return (0, "")
    data = content.encode("utf-8", errors="replace")
    return (len(data), hashlib.sha256(data).hexdigest())


def _add_sys_path_once(path: Path) -> None:
    resolved = str(path.resolve()).lower()
    existing = {str(Path(p).resolve()).lower() for p in sys.path if p}
    if resolved not in existing:
        sys.path.insert(0, str(path))


def _get_skill_manager(hermes_root: Path | None = None) -> Any | None:
    global _skill_manager_module
    if _skill_manager_module is not None:
        return _skill_manager_module

    if hermes_root is not None:
        try:
            _add_sys_path_once(Path(hermes_root))
        except Exception:
            pass

    try:
        module = importlib.import_module("tools.skill_manager_tool")
    except Exception:
        return None

    manage = getattr(module, "skill_manage", None)
    if not callable(manage):
        return None

    _skill_manager_module = module
    return module


def _call_skill_manager(action: str, name: str, hermes_root: Path | None = None, **payload: Any) -> dict[str, Any]:
    manager = _get_skill_manager(hermes_root)
    if manager is None:
        return {"success": False, "error": "Hermes skill manager is unavailable for direct mutation."}
    kwargs = {k: v for k, v in payload.items() if v is not None}
    kwargs.update({"action": action, "name": name})
    result = manager.skill_manage(**kwargs)  # type: ignore[union-attr]
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return json.loads(result)
    try:
        return json.loads(str(result))
    except Exception:
        return {"success": False, "error": f"skill manager returned unsupported result: {result!r}"}


def _validate_frontmatter(content: str) -> None:
    """Validate SKILL.md frontmatter. Raises ValueError on bad shape."""
    if not content.strip():
        raise ValueError("Content cannot be empty.")
    if not content.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter (---).")
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        raise ValueError("SKILL.md frontmatter is not closed. Ensure you have a closing '---' line.")
    yaml_text = content[3 : end.start() + 3]
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for SKILL.md frontmatter validation.") from exc
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML frontmatter parse error: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Frontmatter must be a YAML mapping (key: value pairs).")
    if "name" not in parsed:
        raise ValueError("Frontmatter must include 'name' field.")
    if "description" not in parsed:
        raise ValueError("Frontmatter must include 'description' field.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def hermes_skill_diff(
    profile: str = "default",
    name: str = "",
    proposed_content: Optional[str] = None,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    file_path: str = "SKILL.md",
    hermes_root: Path | None = None,
) -> str:
    """Preview a diff for a skill. Read-only — never mutates."""
    try:
        policy = op.OperatorPolicy()
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )

        target = _resolve_supporting_file(skill_dir, file_path)
        if not target.exists():
            raise FileNotFoundError(f"File {file_path!r} not found in skill {canon!r}.")

        current = target.read_text(encoding="utf-8", errors="replace")

        if proposed_content is not None:
            new = proposed_content
        elif old_string is not None and new_string is not None:
            if old_string not in current:
                raise ValueError("old_string not found in current content.")
            new = current.replace(old_string, new_string, 1)
        else:
            raise ValueError(
                "Provide either proposed_content or (old_string + new_string)."
            )

        diff = op.unified_diff(current, new, label=file_path)
        cur_len, cur_sha = _hash_content(current)
        new_len, new_sha = _hash_content(new)
        result = {
            "success": True,
            "profile": profile,
            "skill": canon,
            "file_path": file_path,
            "current_len": cur_len,
            "current_sha256": cur_sha,
            "proposed_len": new_len,
            "proposed_sha256": new_sha,
            "diff": diff,
        }
        op.audit_record(
            tool="hermes_skill_diff",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=True,
            changed=False,
            summary="diff preview",
            profile=profile,
            skill_name=canon,
            content=new,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_DIFF_ERROR",
                suggested_action="Check skill name, file_path, and that the skill exists.",
            ),
            indent=2,
        )


def hermes_skill_create(
    profile: str = "default",
    name: str = "",
    content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not content:
            raise ValueError("content is required for create.")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Content is {len(content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )
        _validate_frontmatter(content)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        existing = _find_skill_dir(profile_home, canon)
        if existing:
            raise ValueError(
                f"A skill named {canon!r} already exists at {existing}."
            )
        skill_dir = _skill_dir(profile_home, canon)
        skill_md = skill_dir / "SKILL.md"

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_create": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_md),
                "content_len": len(content),
                "content_sha256": _hash_content(content)[1],
                "diff": op.unified_diff("", content, label="SKILL.md"),
            }
            op.audit_record(
                tool="hermes_skill_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        result = _call_skill_manager(
            "create",
            canon,
            hermes_root=hermes_root,
            content=content,
            category=None,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_create",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager create failed")),
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_create",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"created skill at {skill_md}",
            profile=profile,
            skill_name=canon,
            content=content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_create",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_CREATE_ERROR",
                suggested_action="Check skill name, frontmatter, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_edit(
    profile: str = "default",
    name: str = "",
    content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not content:
            raise ValueError("content is required for edit.")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Content is {len(content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )
        _validate_frontmatter(content)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )
        skill_md = skill_dir / "SKILL.md"
        current = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_edit": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_md),
                "current_len": len(current),
                "proposed_len": len(content),
                "diff": op.unified_diff(current, content, label="SKILL.md"),
            }
            op.audit_record(
                tool="hermes_skill_edit",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        result = _call_skill_manager(
            "edit",
            canon,
            hermes_root=hermes_root,
            content=content,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_edit",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager edit failed")),
                profile=profile,
                skill_name=canon,
                content=content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_edit",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"edited skill at {skill_md}",
            profile=profile,
            skill_name=canon,
            content=content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_edit",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_EDIT_ERROR",
                suggested_action="Check skill name, frontmatter, skill existence, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_patch(
    profile: str = "default",
    name: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "SKILL.md",
    replace_all: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not old_string:
            raise ValueError("old_string is required for patch.")
        if new_string is None:
            raise ValueError("new_string is required for patch.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )
        target = _resolve_supporting_file(skill_dir, file_path)
        if not target.exists():
            raise FileNotFoundError(f"File {file_path!r} not found in skill {canon!r}.")

        content = target.read_text(encoding="utf-8", errors="replace")

        if old_string not in content:
            raise ValueError(
                "old_string not found in current content. Use hermes_skill_diff "
                "to preview or check the file path."
            )
        if not replace_all and content.count(old_string) > 1:
            raise ValueError(
                "old_string matches multiple locations. Provide more surrounding "
                "context for a unique match, or set replace_all=True."
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            match_count = content.count(old_string)
        else:
            new_content = content.replace(old_string, new_string, 1)
            match_count = 1

        if len(new_content) > _MAX_CONTENT_CHARS:
            raise ValueError(
                f"Patched content is {len(new_content):,} chars (limit: {_MAX_CONTENT_CHARS:,})."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_patch": True,
                "profile": profile,
                "skill": canon,
                "file_path": file_path,
                "match_count": match_count,
                "diff": op.unified_diff(content, new_content, label=file_path),
            }
            op.audit_record(
                tool="hermes_skill_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=new_content,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        result = _call_skill_manager(
            "patch",
            canon,
            hermes_root=hermes_root,
            old_string=old_string,
            new_string=new_string,
            file_path=file_path,
            replace_all=replace_all,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_patch",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager patch failed")),
                profile=profile,
                skill_name=canon,
                content=new_content,
            )
            return json.dumps(result, indent=2)
        result.setdefault("match_count", match_count)
        op.audit_record(
            tool="hermes_skill_patch",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"patched {file_path} ({match_count} replacement(s))",
            profile=profile,
            skill_name=canon,
            content=new_content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_patch",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_PATCH_ERROR",
                suggested_action="Check skill name, file_path, old_string/new_string, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_write_file(
    profile: str = "default",
    name: str = "",
    file_path: str = "",
    file_content: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)
        if not file_path:
            raise ValueError("file_path is required.")
        if file_content is None:
            raise ValueError("file_content is required.")

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}. "
                "Create it first with hermes_skill_create."
            )

        target = _resolve_supporting_file(skill_dir, file_path)

        content_bytes = len(file_content.encode("utf-8"))
        if content_bytes > _MAX_FILE_BYTES:
            raise ValueError(
                f"File content is {content_bytes:,} bytes (limit: {_MAX_FILE_BYTES:,})."
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_write": True,
                "profile": profile,
                "skill": canon,
                "file_path": file_path,
                "target": str(target),
                "content_len": len(file_content),
                "content_sha256": _hash_content(file_content)[1],
            }
            op.audit_record(
                tool="hermes_skill_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
                content=file_content,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        policy.require_mutation(dry_run)
        result = _call_skill_manager(
            "write_file",
            canon,
            hermes_root=hermes_root,
            file_path=file_path,
            file_content=file_content,
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_write_file",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager write_file failed")),
                profile=profile,
                skill_name=canon,
                content=file_content,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_write_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"wrote {file_path} in skill {canon}",
            profile=profile,
            skill_name=canon,
            content=file_content,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_write_file",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_WRITE_FILE_ERROR",
                suggested_action="Check skill name, file_path, content size, and operator level/apply mode.",
            ),
            indent=2,
        )


def _copy_skill_tree(source_dir: Path, target_dir: Path) -> list[Path]:
    raise RuntimeError("Direct skill copy is disabled; use dry-run only.")


def hermes_skill_copy(
    source_profile: str,
    target_profile: str,
    name: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(source_profile, hermes_root)
        policy.require_profile(target_profile, hermes_root)
        canon = _validate_skill_name(name)
        if source_profile == target_profile:
            raise ValueError("source_profile and target_profile must differ.")

        source_home = op.resolve_profile_home(source_profile, hermes_root)
        target_home = op.resolve_profile_home(target_profile, hermes_root)
        source_dir = _find_skill_dir(source_home, canon)
        if not source_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in source profile {source_profile!r}."
            )
        target_dir = _skill_dir(target_home, canon)

        # Enumerate files for dry-run preview.
        files: list[dict[str, Any]] = []
        total_size = 0
        for src_path in source_dir.rglob("*"):
            if src_path.is_dir():
                continue
            if _is_secret_filename(src_path.name):
                continue
            try:
                size = src_path.stat().st_size
            except OSError:
                size = 0
            total_size += size
            files.append(
                {
                    "rel": str(src_path.relative_to(source_dir)),
                    "size": size,
                }
            )

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_copy": True,
                "source_profile": source_profile,
                "target_profile": target_profile,
                "skill": canon,
                "source_path": str(source_dir),
                "target_path": str(target_dir),
                "file_count": len(files),
                "total_size": total_size,
                "files": files[:50],
            }
            op.audit_record(
                tool="hermes_skill_copy",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                source_profile=source_profile,
                target_profile=target_profile,
                skill_name=canon,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        op.audit_record(
            tool="hermes_skill_copy",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=False,
            changed=False,
            error="Direct skill copy is not supported safely in v0.2.0; use dry-run or Owner Mode.",
            source_profile=source_profile,
            target_profile=target_profile,
            skill_name=canon,
        )
        return json.dumps(
            {
                "success": False,
                "error": "Direct skill copy is not supported safely in v0.2.0; use dry-run or Owner Mode.",
            },
            indent=2,
        )
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_copy",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            source_profile=source_profile,
            target_profile=target_profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_COPY_ERROR",
                suggested_action="Check source/target profiles, skill name, and operator level/apply mode.",
            ),
            indent=2,
        )


def hermes_skill_sync_to_default(
    source_profile: str,
    name: str,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Convenience: copy a skill from source_profile to default."""
    return hermes_skill_copy(
        source_profile=source_profile,
        target_profile="default",
        name=name,
        dry_run=dry_run,
        hermes_root=hermes_root,
    )


def hermes_skill_delete(
    profile: str = "default",
    name: str = "",
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    try:
        policy = op.OperatorPolicy()
        policy.require_level("skills")
        policy.require_profile(profile, hermes_root)
        canon = _validate_skill_name(name)

        profile_home = op.resolve_profile_home(profile, hermes_root)
        skill_dir = _find_skill_dir(profile_home, canon)
        if not skill_dir:
            raise FileNotFoundError(
                f"Skill {canon!r} not found in profile {profile!r}."
            )

        # List files for preview.
        files: list[str] = []
        for p in skill_dir.rglob("*"):
            if p.is_file():
                files.append(str(p.relative_to(skill_dir)))

        if policy.effective_dry_run(dry_run):
            plan = {
                "would_delete": True,
                "profile": profile,
                "skill": canon,
                "path": str(skill_dir),
                "file_count": len(files),
                "files_preview": files[:20],
            }
            op.audit_record(
                tool="hermes_skill_delete",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=True,
                success=True,
                changed=False,
                summary="dry-run plan",
                profile=profile,
                skill_name=canon,
            )
            return json.dumps({"success": True, "dry_run": True, "plan": plan}, indent=2)

        # Pin guard: best-effort check via Hermes skill_usage only for direct
        # mutations. Dry-run always succeeds without this check.
        pin_blocker = None
        try:
            import sys as _sys

            if str(hermes_root) not in _sys.path:
                _sys.path.insert(0, str(hermes_root))
            from tools import skill_usage  # type: ignore

            rec = skill_usage.get_record(canon)
            if rec.get("pinned"):
                pin_blocker = (
                    f"Skill {canon!r} is pinned and cannot be deleted. "
                    "Ask the user to run `hermes curator unpin " + canon + "`."
                )
        except Exception:
            pin_blocker = (
                f"Hermes skill usage metadata is unavailable; refusing to delete {canon!r}."
            )
        if pin_blocker:
            raise PermissionError(pin_blocker)

        result = _call_skill_manager(
            "delete",
            canon,
            hermes_root=hermes_root,
            absorbed_into="",
        )
        if not result.get("success", False):
            op.audit_record(
                tool="hermes_skill_delete",
                level=policy.level,
                apply_mode=policy.apply_mode,
                dry_run=False,
                success=False,
                changed=False,
                error=str(result.get("error", "skill manager delete failed")),
                profile=profile,
                skill_name=canon,
            )
            return json.dumps(result, indent=2)
        op.audit_record(
            tool="hermes_skill_delete",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=True,
            summary=f"deleted skill {canon}",
            profile=profile,
            skill_name=canon,
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        op.audit_record(
            tool="hermes_skill_delete",
            level="unknown",
            apply_mode="unknown",
            dry_run=dry_run,
            success=False,
            error=str(exc),
            profile=profile,
            skill_name=name,
        )
        return json.dumps(
            op.error_from_exception(
                exc,
                layer="skills",
                code="SKILL_DELETE_ERROR",
                suggested_action="Check skill name, pin status, skill manager availability, and operator level/apply mode.",
            ),
            indent=2,
        )
