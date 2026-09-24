"""Adapter to the Hermes Agent's effective skill loader.

This module deliberately owns no skill registry and does not scan ``SKILL.md``
files itself. Hermes Agent already owns loading semantics (profile scope,
external/project directories, exclusions, disabled skills and platform gates),
so Operator surfaces consume that loader and only add bounded validation
provenance around its result.

Authority model (review follow-up):

- The dispatch hard gate probes the *explicit-load* path
  (``skill_view(..., preprocess=False)``, the same loader ``--skills`` /
  ``build_preloaded_skills_prompt`` uses), fresh on every call with no
  discovery-cache TTL. Preprocessing is disabled so a validation probe
  cannot execute ``skills.inline_shell`` snippets.
- The cross-profile catalog (``_find_all_skills`` + plugin skill metadata)
  is provenance/diagnostics only: it classifies a rejection as
  ``skill_not_found`` vs ``skill_not_resolvable_for_profile`` and reports
  ``available_profiles``. It never overrules an explicit-load probe.
- ``environments:``/``requires_apps:`` are offer-time filters. An explicit
  load bypasses them, so the gate does too.
- Loader/import failures are fail-closed as ``skill_resolution_unavailable``,
  never as ``skill_not_found``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import operator_policy as op


@dataclass(frozen=True)
class SkillEntry:
    """One skill exposed by the Agent loader for a logical profile."""

    name: str
    profile: str
    category: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SkillResolution:
    """Cross-profile provenance for one requested skill name."""

    name: str
    defined_in: tuple[str, ...]
    available_to: tuple[str, ...]
    scope: str
    entries: tuple[SkillEntry, ...]

    @property
    def exists(self) -> bool:
        return bool(self.defined_in)


@dataclass(frozen=True)
class SkillCatalog:
    entries: tuple[SkillEntry, ...]
    known_profiles: tuple[str, ...]


class SkillRequirementsError(ValueError):
    """A structured profile/required-skills preflight rejection."""

    def __init__(self, rejection: dict[str, Any]):
        self.rejection = rejection
        super().__init__(str(rejection.get("message", "skill requirements are invalid")))


SCOPE_GLOBAL = "global"
SCOPE_PROFILE_LOCAL = "profile_local"
ERROR_NOT_FOUND = "skill_not_found"
ERROR_NOT_RESOLVABLE = "skill_not_resolvable_for_profile"
ERROR_UNAVAILABLE = "skill_resolution_unavailable"

# Tests can inject an Agent-loader-shaped provider without making the unit
# suite depend on a separately installed Hermes Agent checkout. Production
# code leaves this unset and uses the real loader below.
_skill_loader_override: Callable[[str, Path], Iterable[dict[str, Any]]] | None = None


class _LoaderUnavailable(RuntimeError):
    """The Agent loader could not be reached; fail closed, never not_found."""


def _default_root() -> Path:
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for candidate in (
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / ".hermes"


def _root(hermes_root: Path | None) -> Path:
    return Path(hermes_root) if hermes_root is not None else _default_root()


def _agent_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    for variable in ("HERMES_AGENT_ROOT", "HERMES_ROOT"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser())
    executable = shutil.which("hermes")
    if executable:
        bin_dir = Path(executable).resolve().parent
        candidates.extend((bin_dir.parent / "hermes-agent", bin_dir.parent))
    for package in ("hermes-agent", "hermes_agent"):
        try:
            base = Path(importlib.metadata.distribution(package).locate_file(""))
        except (importlib.metadata.PackageNotFoundError, OSError):
            continue
        candidates.extend((base, base / "hermes-agent"))
    candidates.extend(
        (
            Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent",
            Path.home() / ".hermes" / "hermes-agent",
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _require_agent_modules() -> tuple[Any, Any]:
    """Return ``(skills_tool, hermes_constants)`` or raise _LoaderUnavailable."""
    last_error: Exception | None = None
    for candidate in [None, *_agent_root_candidates()]:
        if candidate is not None and candidate.is_dir():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            # The hermes-gpt checkout has a namespace ``tools/`` directory for
            # package-hygiene scripts. Remove that empty namespace only when
            # the real Agent package is about to be loaded; never replace a
            # concrete, already-loaded package.
            loaded_tools = sys.modules.get("tools")
            if (
                loaded_tools is not None
                and getattr(loaded_tools, "__file__", None) is None
                and (candidate / "tools" / "__init__.py").is_file()
            ):
                sys.modules.pop("tools", None)
        try:
            skills_tool = importlib.import_module("tools.skills_tool")
            constants = importlib.import_module("hermes_constants")
            if callable(getattr(skills_tool, "_find_all_skills", None)):
                return skills_tool, constants
        except Exception as exc:  # noqa: BLE001 - optional Agent runtime
            last_error = exc
            continue
    raise _LoaderUnavailable(
        "Hermes Agent loader is unavailable; refusing to validate skills"
        + (f": {last_error}" if last_error is not None else "")
    )


def _agent_modules() -> tuple[Any, Any] | None:
    """Return ``(skills_tool, hermes_constants)`` when Agent is available."""
    try:
        return _require_agent_modules()
    except _LoaderUnavailable:
        return None


@contextmanager
def _profile_scope(profile_home: Path, constants: Any):
    setter = getattr(constants, "set_hermes_home_override", None)
    resetter = getattr(constants, "reset_hermes_home_override", None)
    if not callable(setter) or not callable(resetter):
        yield
        return
    token = setter(profile_home)
    try:
        yield
    finally:
        resetter(token)


def _plugin_entries(skills_tool: Any) -> list[dict[str, Any]]:
    """Plugin-provided skill metadata, mirroring ``skills_list`` filtering.

    Best-effort provenance for the catalog: a plugin-registry failure here
    must not mask flat-tree skills, because the explicit-load probe resolves
    ``plugin:skill`` directly through ``skill_view``.
    """
    try:
        from hermes_cli.plugins import discover_plugins, get_plugin_manager

        discover_plugins()
        raw = get_plugin_manager().list_plugin_skill_metadata()
    except Exception:  # noqa: BLE001 - best-effort plugin provenance
        return []
    entries: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        meta = dict(item)
        frontmatter = meta.pop("frontmatter", {})
        if not isinstance(frontmatter, dict):
            frontmatter = {}
        name = str(meta.get("name") or "").strip()
        if not name:
            continue
        try:
            if not skills_tool.skill_matches_platform(frontmatter):
                continue
            if skills_tool._is_skill_disabled(name):
                continue
        except Exception:  # noqa: BLE001, S112 - per-skill best effort
            continue
        entries.append(
            {
                "name": name,
                "category": meta.get("category"),
                "description": meta.get("description"),
            }
        )
    return entries


def _discovery_entries(profile: str, hermes_root: Path) -> list[dict[str, Any]]:
    """Catalog entries for one profile; raises _LoaderUnavailable on failure."""
    skills_tool, constants = _require_agent_modules()
    profile_home = op.resolve_profile_home(profile, hermes_root)
    try:
        with _profile_scope(profile_home, constants):
            raw = skills_tool._find_all_skills()
            plugin_raw = _plugin_entries(skills_tool)
    except Exception as exc:
        raise _LoaderUnavailable(
            f"Hermes Agent loader failed for profile '{profile}': {exc}"
        ) from exc
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(
            {
                "name": name,
                "category": item.get("category"),
                "description": item.get("description"),
            }
        )
    for item in plugin_raw:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(item)
    return entries


def _agent_entries(profile: str, hermes_root: Path) -> list[dict[str, Any]]:
    """Backward-compatible discovery wrapper; loader failure yields []."""
    try:
        return _discovery_entries(profile, hermes_root)
    except _LoaderUnavailable:
        return []


def _explicit_load_ok(
    profile: str, name: str, hermes_root: Path
) -> tuple[bool, str]:
    """Probe the explicit-load path (``skill_view``) for one skill.

    Returns ``(True, detail)`` when the requested profile can load the skill
    right now, ``(False, detail)`` when it cannot. Raises _LoaderUnavailable
    when the loader itself cannot be reached. No discovery cache is consulted,
    so a skill removed between planning and dispatch fails immediately.

    The probe calls ``skill_view(..., preprocess=False)``. Hermes preload
    also disables preprocessing at load time and renders later; the default
    ``preprocess=True`` would execute ``!`cmd``` snippets when
    ``skills.inline_shell`` is enabled.
    """
    requested = str(name).strip()
    if not requested:
        return False, "empty skill name"
    if _skill_loader_override is not None:
        try:
            entries = list(_skill_loader_override(profile, hermes_root))
        except Exception as exc:
            raise _LoaderUnavailable(
                f"skill loader override failed for profile '{profile}': {exc}"
            ) from exc
        for item in entries:
            if (
                isinstance(item, dict)
                and str(item.get("name") or "").strip() == requested
            ):
                return True, "explicit-loadable via test override"
        return False, "not present in test override entries"
    skills_tool, constants = _require_agent_modules()
    try:
        profile_home = op.resolve_profile_home(profile, hermes_root)
    except Exception as exc:  # noqa: BLE001 - invalid profile is a state
        return False, f"invalid profile: {exc}"
    try:
        with _profile_scope(profile_home, constants):
            raw = skills_tool.skill_view(requested, preprocess=False)
    except Exception as exc:
        raise _LoaderUnavailable(
            f"Hermes Agent loader failed for profile '{profile}': {exc}"
        ) from exc
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:  # noqa: BLE001 - malformed loader response is a state
        return False, "unparseable skill_view response"
    if isinstance(payload, dict) and payload.get("success"):
        return True, "explicit-loadable"
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("error") or payload.get("message") or "")[:200]
    return False, detail or "not loadable via skill_view"


def profile_skill_entries(
    profile: str, hermes_root: Path | None = None
) -> list[SkillEntry]:
    """Return the Agent loader's effective skills for one profile."""
    root = _root(hermes_root)
    canon = op.validate_profile_name(profile)
    if _skill_loader_override is not None:
        raw = list(_skill_loader_override(canon, root))
    else:
        raw = _discovery_entries(canon, root)
    return [
        SkillEntry(
            name=str(item["name"]),
            profile=canon,
            category=(str(item["category"]) if item.get("category") else None),
            description=(
                str(item["description"]) if item.get("description") else None
            ),
        )
        for item in raw
        if isinstance(item, dict) and item.get("name")
    ]


def build_catalog(hermes_root: Path | None = None) -> SkillCatalog:
    """Build provenance from the effective Agent loader, without persistence."""
    root = _root(hermes_root)
    profiles = tuple(op.list_existing_profiles(root))
    entries: list[SkillEntry] = []
    for profile in profiles:
        entries.extend(profile_skill_entries(profile, root))
    return SkillCatalog(entries=tuple(entries), known_profiles=profiles)


def resolve_name(name: str, catalog: SkillCatalog | None = None) -> SkillResolution:
    """Resolve one skill name across the effective profile loaders."""
    requested = str(name).strip()
    catalog = catalog or build_catalog()
    matches = tuple(entry for entry in catalog.entries if entry.name == requested)
    defined = tuple(sorted({entry.profile for entry in matches}))
    return SkillResolution(
        name=requested,
        defined_in=defined,
        available_to=defined,
        scope=SCOPE_GLOBAL if "default" in defined else SCOPE_PROFILE_LOCAL,
        entries=matches,
    )


def resolution_for_profile(
    resolution: SkillResolution,
    profile: str,
    known_profiles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a bounded, actionable profile-specific resolution view."""
    known = list(known_profiles or ())
    if profile not in known:
        return {
            "profile": profile,
            "unknown_profile": True,
            "resolvable": False,
            "available_profiles": list(resolution.available_to),
            "known_profiles": known,
            "reason": f"profile '{profile}' is not a known Hermes profile",
        }
    resolvable = profile in resolution.available_to
    return {
        "profile": profile,
        "unknown_profile": False,
        "resolvable": resolvable,
        "available_profiles": list(resolution.available_to),
        "reason": (
            f"skill is defined in profile '{profile}' and can be loaded at execution"
            if resolvable
            else f"skill exists, but is not defined in profile '{profile}'"
        ),
    }


def _unavailable(profile: str, detail: str) -> dict[str, Any]:
    return {
        "error": ERROR_UNAVAILABLE,
        "profile": profile,
        "message": (
            "The Hermes Agent skill loader is unavailable; skill requirements "
            "cannot be verified and dispatch is refused."
        ),
        "detail": detail[:300],
    }


def validate_required_skills(
    profile: str,
    skills: Iterable[str] | None,
    hermes_root: Path | None = None,
    *,
    catalog: SkillCatalog | None = None,
) -> dict[str, Any] | None:
    """Validate required skills against the Agent's explicit-load path.

    The hard gate is the explicit load (``skill_view``): a skill the worker
    could load via ``--skills`` passes, including environment-filtered and
    ``plugin:skill`` names. The catalog only classifies failures and reports
    ``available_profiles``. Loader failures are fail-closed as
    ``skill_resolution_unavailable``.
    """
    requested: list[str] = []
    for skill in skills or ():
        value = str(skill).strip()
        if value and value not in requested:
            requested.append(value)
    if not requested:
        return None

    root = _root(hermes_root)
    if _skill_loader_override is None:
        try:
            _require_agent_modules()
        except _LoaderUnavailable as exc:
            return _unavailable(profile, str(exc))
    try:
        cat = catalog or build_catalog(root)
    except _LoaderUnavailable as exc:
        return _unavailable(profile, str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-closed catalog
        return _unavailable(profile, f"skill catalog failed: {exc}")

    not_found: list[str] = []
    not_resolvable: list[dict[str, Any]] = []
    for name in requested:
        try:
            ok, detail = _explicit_load_ok(profile, name, root)
        except _LoaderUnavailable as exc:
            return _unavailable(profile, str(exc))
        if ok:
            continue
        resolution = resolve_name(name, cat)
        if not resolution.exists:
            not_found.append(name)
            continue
        view = resolution_for_profile(resolution, profile, cat.known_profiles)
        if not view["resolvable"]:
            not_resolvable.append(
                {
                    "skill": name,
                    "profile": profile,
                    "available_profiles": view["available_profiles"],
                    "reason": view["reason"],
                    "detail": detail[:200],
                }
            )
        else:
            # Catalog claims this profile, but the live explicit load just
            # failed (removed/disabled/platform-gated after the catalog
            # snapshot, or stale discovery cache): fail closed.
            not_resolvable.append(
                {
                    "skill": name,
                    "profile": profile,
                    "available_profiles": view["available_profiles"],
                    "reason": (
                        f"skill is catalogued for profile '{profile}' but "
                        f"failed the live explicit-load check: {detail[:160]}"
                    ),
                }
            )

    if not_found and not_resolvable:
        return {
            "error": "skill_requirements_invalid",
            "profile": profile,
            "skills_not_found": sorted(not_found),
            "skills_not_resolvable": not_resolvable,
            "message": "One or more required skills are invalid for the requested profile.",
        }
    if not_found:
        return {
            "error": ERROR_NOT_FOUND,
            "profile": profile,
            "skills_not_found": sorted(not_found),
            "message": (
                "The following required skill names do not exist in the Hermes "
                f"skill catalog: {', '.join(sorted(not_found))}."
            ),
        }
    if not_resolvable:
        return {
            "error": ERROR_NOT_RESOLVABLE,
            "profile": profile,
            "skills": not_resolvable,
            "incompatible_skills": [item["skill"] for item in not_resolvable],
            "message": (
                "One or more required skills exist but are not resolvable by "
                f"profile '{profile}'."
            ),
        }
    return None


def require_required_skills(
    profile: str,
    skills: Iterable[str] | None,
    hermes_root: Path | None = None,
) -> None:
    rejection = validate_required_skills(profile, skills, hermes_root)
    if rejection is not None:
        raise SkillRequirementsError(rejection)


def skill_names_for_home(
    profile_home: Path, profile: str | None = None
) -> list[str]:
    """Return effective loader names for a manifest profile entity.

    Diagnostics only (Capability Manifest): best-effort, never raises, so a
    loader failure yields an empty skill list rather than a manifest outage.
    The dispatch hard gate uses ``validate_required_skills`` instead.
    """
    home = Path(profile_home)
    profile = profile or "default"
    root = home if profile == "default" else home.parent.parent
    try:
        if _skill_loader_override is not None:
            raw = list(_skill_loader_override(profile, root))
        else:
            raw = _discovery_entries(profile, root)
    except Exception:  # noqa: BLE001 - diagnostics never raise
        return []
    return sorted(
        {
            str(item["name"])
            for item in raw
            if isinstance(item, dict) and item.get("name")
        }
    )
