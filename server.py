from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import operator_policy as op_policy
import operator_cron as op_cron
import operator_skills as op_skills
import operator_config as op_config
import operator_workspace as op_workspace
import operator_diagnostics as op_diagnostics


LOCAL_DEV_PROFILE = "local-dev"
REMOTE_PROFILE = "remote"
UNSAFE_REMOTE_ACK = "--i-understand-this-is-unsafe"
UNSAFE_REMOTE_ENV = "HERMES_GPT_UNSAFE_REMOTE_NOAUTH"
ENABLE_WRITE_ENV = "HERMES_GPT_ENABLE_WRITE"
ENABLE_MEMORY_WRITE_ENV = "HERMES_GPT_ENABLE_MEMORY_WRITE"
ENABLE_SESSION_SEARCH_ENV = "HERMES_GPT_ENABLE_SESSION_SEARCH"
ENABLE_TERMINAL_ENV = "HERMES_GPT_ENABLE_TERMINAL"
NOAUTH_META = {"securitySchemes": [{"type": "noauth"}]}

HERMES_ROOT: Path | None = None
IMPORT_ERROR: str | None = None
file_tools: Any = None
terminal_tool: Any = None
memory_tool: Any = None
skill_manager_tool: Any = None
SessionDB: Any = None
get_hermes_home: Any = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def is_hermes_root(path: Path) -> bool:
    return path.exists() and ((path / "tools").is_dir() or (path / "hermes_state.py").exists())


def candidate_roots() -> list[Path]:
    candidates: list[Path] = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        env_path = Path(env_home).expanduser()
        candidates.extend([env_path, env_path / "hermes-agent"])

    home = Path.home()
    candidates.extend(
        [
            home / "AppData" / "Local" / "hermes" / "hermes-agent",
            home / ".hermes" / "hermes-agent",
        ]
    )

    for package in ("hermes-agent", "hermes_agent"):
        try:
            dist = importlib.metadata.distribution(package)
            base = Path(dist.locate_file("")).resolve()
        except Exception:
            continue
        for parent in [base, *base.parents]:
            if parent.name == "hermes-agent":
                candidates.append(parent)
                break

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def find_hermes_root() -> Path:
    for candidate in candidate_roots():
        if is_hermes_root(candidate):
            return candidate
    raise RuntimeError("Could not find a Hermes Agent source root with a tools directory.")


def add_path_once(path: Path, *, prepend: bool = True) -> None:
    value = str(path)
    existing = {str(Path(p).resolve()).lower() for p in sys.path if p}
    if str(path.resolve()).lower() not in existing:
        if prepend:
            sys.path.insert(0, value)
        else:
            sys.path.append(value)


def add_hermes_to_syspath(root: Path) -> None:
    add_path_once(root)
    if os.name == "nt":
        site_packages = root / "venv" / "Lib" / "site-packages"
    else:
        candidates = sorted((root / "venv" / "lib").glob("python*/site-packages")) if (root / "venv" / "lib").exists() else []
        site_packages = candidates[0] if candidates else root / "venv" / "lib" / "site-packages"
    if site_packages.exists():
        # Keep Hermes' bundled dependencies available for Hermes internals, but do
        # not let them shadow the MCP SDK used to run this sidecar.
        add_path_once(site_packages, prepend=False)


def import_hermes() -> None:
    global HERMES_ROOT, IMPORT_ERROR, file_tools, terminal_tool, memory_tool
    global skill_manager_tool, SessionDB, get_hermes_home
    try:
        HERMES_ROOT = find_hermes_root()
        add_hermes_to_syspath(HERMES_ROOT)
        from tools import file_tools as ft
        from tools import memory_tool as mt
        from tools import terminal_tool as tt

        file_tools = ft
        terminal_tool = tt
        memory_tool = mt

        try:
            from tools import skill_manager_tool as smt

            skill_manager_tool = smt
        except Exception as exc:
            eprint(f"hermes-gpt: skill manager unavailable: {exc}")

        try:
            from hermes_state import SessionDB as SDB
            from hermes_state import get_hermes_home as ghh

            SessionDB = SDB
            get_hermes_home = ghh
        except Exception as exc:
            eprint(f"hermes-gpt: session search unavailable: {exc}")
    except Exception as exc:
        IMPORT_ERROR = str(exc)
        eprint(f"hermes-gpt: Hermes imports failed: {exc}")


def call_with_supported_kwargs(func: Any, **kwargs: Any) -> Any:
    params = inspect.signature(func).parameters
    supported = {key: value for key, value in kwargs.items() if key in params}
    return func(**supported)


def expand_path(value: str | None) -> str | None:
    if value is None:
        return None
    return str(Path(value).expanduser())


def require_imports() -> None:
    if IMPORT_ERROR:
        raise RuntimeError(f"Hermes imports are unavailable: {IMPORT_ERROR}")
    missing = [
        name
        for name, module in {
            "file_tools": file_tools,
            "terminal_tool": terminal_tool,
            "memory_tool": memory_tool,
        }.items()
        if module is None
    ]
    if missing:
        raise RuntimeError(f"Hermes imports are unavailable: missing {', '.join(missing)}")


def skill_roots() -> list[Path]:
    roots: list[Path] = []
    hermes_home = None
    if callable(get_hermes_home):
        try:
            hermes_home = Path(get_hermes_home())
        except Exception:
            hermes_home = None
    if hermes_home is None:
        env_home = os.environ.get("HERMES_HOME")
        hermes_home = Path(env_home).expanduser() if env_home else Path.home() / ".hermes"

    roots.append(hermes_home / "skills")
    profiles = hermes_home / "profiles"
    if profiles.exists():
        roots.extend(path / "skills" for path in profiles.iterdir() if path.is_dir())
    if HERMES_ROOT:
        roots.append(HERMES_ROOT / "skills")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except Exception:
            continue
        key = str(resolved).lower()
        if resolved.exists() and key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def parse_skill_doc(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
            for line in parts[1].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip("'\"")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value
    if not description:
        for line in body.splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean:
                description = clean[:180]
                break
    return {"name": name, "description": description, "path": str(path)}


def discover_skills() -> list[dict[str, str]]:
    skills: list[dict[str, str]] = []
    for root in skill_roots():
        for skill_md in root.rglob("SKILL.md"):
            try:
                skills.append(parse_skill_doc(skill_md))
            except Exception as exc:
                eprint(f"hermes-gpt: could not read skill {skill_md}: {exc}")
    return sorted(skills, key=lambda item: (item["name"].lower(), item["path"].lower()))


def clean_error(tool_name: str, exc: Exception) -> RuntimeError:
    eprint(f"hermes-gpt: {tool_name} failed: {exc}")
    return RuntimeError(f"{tool_name} failed: {exc}")


from mcp.server.fastmcp import FastMCP

import_hermes()


def tool_meta(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = dict(NOAUTH_META)
    if extra:
        meta.update(extra)
    return meta


def hermes_read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    try:
        require_imports()
        return file_tools.read_file_tool(path=expand_path(path), offset=offset, limit=limit)
    except Exception as exc:
        raise clean_error("hermes_read_file", exc) from exc


def hermes_write_file(path: str, content: str) -> str:
    try:
        require_imports()
        return file_tools.write_file_tool(path=expand_path(path), content=content)
    except Exception as exc:
        raise clean_error("hermes_write_file", exc) from exc


def hermes_patch(
    path: str,
    old_string: str,
    new_string: str,
    mode: str = "replace",
    replace_all: bool = False,
) -> str:
    try:
        require_imports()
        return call_with_supported_kwargs(
            file_tools.patch_tool,
            mode=mode,
            path=expand_path(path),
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )
    except Exception as exc:
        raise clean_error("hermes_patch", exc) from exc


def hermes_search_files(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str | None = None,
    limit: int = 50,
) -> str:
    try:
        require_imports()
        return call_with_supported_kwargs(
            file_tools.search_tool,
            pattern=pattern,
            target=target,
            path=expand_path(path),
            file_glob=file_glob,
            limit=limit,
        )
    except Exception as exc:
        raise clean_error("hermes_search_files", exc) from exc


def hermes_run_command(command: str, timeout: int = 30, workdir: str | None = None) -> str:
    try:
        require_imports()
        if not env_enabled(ENABLE_TERMINAL_ENV):
            raise RuntimeError(f"Terminal execution is disabled. Set {ENABLE_TERMINAL_ENV}=1 to enable it.")
        capped_timeout = max(1, min(int(timeout), 120))
        return call_with_supported_kwargs(
            terminal_tool.terminal_tool,
            command=command,
            timeout=capped_timeout,
            workdir=expand_path(workdir),
        )
    except Exception as exc:
        raise clean_error("hermes_run_command", exc) from exc


def hermes_memory(
    action: str,
    target: str = "memory",
    content: str | None = None,
    old_text: str | None = None,
) -> str:
    try:
        require_imports()
        if action not in {"add", "replace", "remove", "search"}:
            raise RuntimeError("Unsupported memory action. Use add, replace, remove, or search.")
        if action in {"add", "replace", "remove"} and not env_enabled(ENABLE_MEMORY_WRITE_ENV):
            raise RuntimeError(f"Memory write actions are disabled. Set {ENABLE_MEMORY_WRITE_ENV}=1 to enable them.")
        return memory_tool.memory_tool(action=action, target=target, content=content, old_text=old_text)
    except Exception as exc:
        raise clean_error("hermes_memory", exc) from exc


def hermes_skill_list() -> str:
    try:
        require_imports()
        skills = discover_skills()
        if not skills:
            return "No Hermes skills found."
        # Deduplicate by name, keeping the first (user-level skills take priority)
        seen_names: set[str] = set()
        unique_skills: list[dict[str, str]] = []
        for skill in skills:
            if skill["name"].lower() not in seen_names:
                seen_names.add(skill["name"].lower())
                unique_skills.append(skill)
        lines = []
        for skill in unique_skills:
            desc = f" - {skill['description']}" if skill["description"] else ""
            lines.append(f"- {skill['name']}{desc}\n  {skill['path']}")
        return "\n".join(lines)
    except Exception as exc:
        raise clean_error("hermes_skill_list", exc) from exc


def hermes_skill_view(name: str) -> str:
    try:
        require_imports()
        query = name.strip().lower()
        matches = [
            skill for skill in discover_skills()
            if skill["name"].lower() == query or Path(skill["path"]).parent.name.lower() == query
        ]
        if not matches:
            return f"No skill matched {name!r}."
        if len(matches) > 1:
            return "Multiple skills matched:\n" + "\n".join(f"- {m['name']}: {m['path']}" for m in matches)
        skill_path = Path(matches[0]["path"])
        # Size guard: if file > 80KB, return bounded chunk with guidance
        MAX_VIEW_BYTES = 80_000
        file_size = skill_path.stat().st_size
        if file_size > MAX_VIEW_BYTES:
            text = skill_path.read_text(encoding="utf-8", errors="replace")
            return text[:MAX_VIEW_BYTES] + f"\n\n--- TRUNCATED (showing {MAX_VIEW_BYTES} of {file_size} bytes). Use hermes_read_file for specific sections. ---"
        return skill_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise clean_error("hermes_skill_view", exc) from exc


def hermes_session_search(query: str, limit: int = 20, offset: int = 0) -> str:
    try:
        require_imports()
        if SessionDB is None:
            return "Hermes session search is unavailable in this install: SessionDB import failed."
        db = SessionDB(read_only=True)
        if not hasattr(db, "search_messages"):
            return "Hermes session search is unavailable in this install: search_messages API is missing."
        rows = db.search_messages(query=query, limit=limit, offset=offset)
        if not rows:
            return "No matching Hermes session messages found."
        rendered = []
        for row in rows:
            session_id = row.get("session_id", "")
            role = row.get("role", "")
            content = (row.get("content") or "").replace("\r", " ").replace("\n", " ")
            rendered.append(f"- {session_id} [{role}] {content[:500]}")
        return "\n".join(rendered)
    except Exception as exc:
        message = f"Hermes session search is unavailable in this install: {exc}"
        eprint(f"hermes-gpt: {message}")
        return message


# ---------------------------------------------------------------------------
# Operator / Owner Mode tools
# ---------------------------------------------------------------------------
#
# These wrap the operator_* modules. They are registered unconditionally
# (so MCP clients can see them and understand why they refuse), but
# mutating tools refuse unless the operator policy is explicitly enabled.
#
# Read-only tools (policy/status/audit_tail, cron list/status, skill diff,
# config get, env status, gateway status, git status/diff) work at any
# enabled level. Mutating tools refuse without sufficient level + apply_mode.


def _hermes_root_for_operator() -> Path | None:
    """Return the Hermes data root for operator operations.

    This intentionally normalizes profile-scoped HERMES_HOME values back to the
    shared Hermes data root so operator/profile tools never treat a profile
    directory or the hermes-agent source checkout as the global root.
    """
    return _default_hermes_root()


def _default_hermes_root() -> Path | None:
    """Return the default Hermes root path (the data root, not the agent source)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op_policy.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    # The Hermes data root is ~/.hermes (Windows: ~/AppData/Local/hermes).
    # The agent source root lives next to it under hermes-agent/ and is not
    # the same path.
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    # Final fallback: ~/.hermes even if it doesn't exist (so tests that
    # monkeypatch this can still pass profile_root into the operator tools).
    return Path.home() / ".hermes"


def _active_profile_name() -> str:
    """Return the active Hermes profile name, or 'default'."""
    try:
        env_home = os.environ.get("HERMES_HOME")
        if env_home:
            p = Path(env_home).expanduser().resolve()
            parts = p.parts
            if "profiles" in parts:
                idx = parts.index("profiles")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        return "default"
    except Exception:
        return "default"


# --- Policy / status / audit (always registered, read-only) ---------------


def hermes_operator_policy() -> str:
    """Return the current operator policy summary. Read-only. Never secrets."""
    try:
        policy = op_policy.OperatorPolicy()
        summary = policy.to_summary()
        summary["success"] = True
        return json.dumps(summary, indent=2)
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="operator",
                code="POLICY_SUMMARY_ERROR",
                suggested_action="Check operator environment variables.",
            ),
            indent=2,
        )


def hermes_operator_status() -> str:
    """Return operator runtime status. Read-only. Never secrets."""
    try:
        policy = op_policy.OperatorPolicy()
        project_path = str(Path(__file__).resolve().parent)
        agent_root = str(HERMES_ROOT) if HERMES_ROOT else None
        default_root = str(_default_hermes_root()) if _default_hermes_root() else None
        active_profile = _active_profile_name()

        # Discover registered operator tools by checking this module's
        # attributes. We list the names we explicitly register below.
        registered = [
            "hermes_operator_policy",
            "hermes_operator_status",
            "hermes_operator_audit_tail",
            "hermes_operator_doctor",
            "hermes_operator_snapshot",
            "hermes_release_doctor",
            "hermes_operator_recover",
            "hermes_cron_list",
            "hermes_cron_status",
            "hermes_skill_diff",
            "hermes_config_get",
            "hermes_env_status",
            "hermes_gateway_status",
            "hermes_git_status",
            "hermes_git_diff",
            "hermes_cron_run",
            "hermes_cron_pause",
            "hermes_cron_copy",
            "hermes_cron_move",
            "hermes_skill_create",
            "hermes_skill_edit",
            "hermes_skill_patch",
            "hermes_skill_write_file",
            "hermes_skill_copy",
            "hermes_skill_sync_to_default",
            "hermes_skill_delete",
            "hermes_config_set",
            "hermes_config_patch",
            "hermes_env_set_nonsecret",
            "hermes_env_copy_nonsecret",
            "hermes_gateway_restart",
            "hermes_workspace_read",
            "hermes_workspace_patch",
            "hermes_workspace_write_file",
            "hermes_workspace_run_test",
            "hermes_owner_run_command",
            "hermes_owner_patch",
            "hermes_owner_write_file",
        ]
        result = {
            "success": True,
            "hermes_gpt_project_path": project_path,
            "hermes_agent_root": agent_root,
            "default_hermes_root": default_root,
            "active_profile": active_profile,
            "enabled": policy.enabled,
            "level": policy.level,
            "apply_mode": policy.apply_mode,
            "owner_mode_ready": policy.owner_mode_ready,
            "registered_operator_tools": registered,
            "audit_log_path": str(op_policy.audit_log_path()),
        }
        return json.dumps(result, indent=2)
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="operator",
                code="OPERATOR_STATUS_ERROR",
                suggested_action="Check HERMES_HOME and operator environment variables.",
            ),
            indent=2,
        )


def hermes_operator_audit_tail(limit: int = 20) -> str:
    """Return the last ``limit`` audit records. Read-only."""
    try:
        records = op_policy.audit_tail(limit=limit)
        return json.dumps(
            {"success": True, "count": len(records), "records": records}, indent=2
        )
    except Exception as exc:
        return json.dumps(
            op_policy.error_from_exception(
                exc,
                layer="audit",
                code="AUDIT_TAIL_ERROR",
                suggested_action="Check audit log path and permissions.",
            ),
            indent=2,
        )


def hermes_operator_doctor(profile: str = "default") -> str:
    """Run a read-only health check across operator surfaces."""
    return op_diagnostics.hermes_operator_doctor(
        profile=profile, hermes_root=_default_hermes_root()
    )


def hermes_operator_snapshot(profile: str = "default") -> str:
    """Return a single current-state summary of the operator."""
    return op_diagnostics.hermes_operator_snapshot(
        profile=profile, hermes_root=_default_hermes_root()
    )


def hermes_release_doctor(workdir: str | None = None, full_tests: bool = False, timeout: int = 180) -> str:
    """Check whether the repo/operator is safe to ship."""
    return op_diagnostics.hermes_release_doctor(
        workdir=workdir, full_tests=full_tests, timeout=timeout
    )


def hermes_operator_recover(profile: str = "default", apply: bool = False) -> str:
    """Conservative recovery sequence. Dry-run by default."""
    return op_diagnostics.hermes_operator_recover(
        profile=profile, apply=apply, hermes_root=_default_hermes_root()
    )


# --- Cron wrappers (pass hermes_root through) ----------------------------


def hermes_cron_list(profile: str = "default", include_disabled: bool = False) -> str:
    return op_cron.hermes_cron_list(
        profile=profile, include_disabled=include_disabled,
        hermes_root=_default_hermes_root(),
    )


def hermes_cron_status(profile: str = "default") -> str:
    return op_cron.hermes_cron_status(profile=profile, hermes_root=_default_hermes_root())


def hermes_cron_run(profile: str = "default", job_id: str = "", dry_run: bool = True) -> str:
    return op_cron.hermes_cron_run(
        profile=profile, job_id=job_id, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_cron_pause(profile: str = "default", job_id: str = "", reason: str = "", dry_run: bool = True) -> str:
    return op_cron.hermes_cron_pause(
        profile=profile, job_id=job_id, reason=reason, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_cron_copy(source_profile: str, target_profile: str, job_id: str, dry_run: bool = True) -> str:
    return op_cron.hermes_cron_copy(
        source_profile=source_profile, target_profile=target_profile,
        job_id=job_id, dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


def hermes_cron_move(
    source_profile: str,
    target_profile: str,
    job_id: str,
    pause_source: bool = True,
    test_run_target: bool = False,
    dry_run: bool = True,
) -> str:
    return op_cron.hermes_cron_move(
        source_profile=source_profile, target_profile=target_profile,
        job_id=job_id, pause_source=pause_source,
        test_run_target=test_run_target, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


# --- Skill wrappers ------------------------------------------------------


def hermes_skill_diff(
    profile: str = "default",
    name: str = "",
    proposed_content: str | None = None,
    old_string: str | None = None,
    new_string: str | None = None,
    file_path: str = "SKILL.md",
) -> str:
    return op_skills.hermes_skill_diff(
        profile=profile, name=name, proposed_content=proposed_content,
        old_string=old_string, new_string=new_string, file_path=file_path,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_create(profile: str = "default", name: str = "", content: str = "", dry_run: bool = True) -> str:
    return op_skills.hermes_skill_create(
        profile=profile, name=name, content=content, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_edit(profile: str = "default", name: str = "", content: str = "", dry_run: bool = True) -> str:
    return op_skills.hermes_skill_edit(
        profile=profile, name=name, content=content, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_patch(
    profile: str = "default",
    name: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "SKILL.md",
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    return op_skills.hermes_skill_patch(
        profile=profile, name=name, old_string=old_string, new_string=new_string,
        file_path=file_path, replace_all=replace_all, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_write_file(
    profile: str = "default",
    name: str = "",
    file_path: str = "",
    file_content: str = "",
    dry_run: bool = True,
) -> str:
    return op_skills.hermes_skill_write_file(
        profile=profile, name=name, file_path=file_path,
        file_content=file_content, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_copy(source_profile: str, target_profile: str, name: str, dry_run: bool = True) -> str:
    return op_skills.hermes_skill_copy(
        source_profile=source_profile, target_profile=target_profile,
        name=name, dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


def hermes_skill_sync_to_default(source_profile: str, name: str, dry_run: bool = True) -> str:
    return op_skills.hermes_skill_sync_to_default(
        source_profile=source_profile, name=name, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_skill_delete(profile: str = "default", name: str = "", dry_run: bool = True) -> str:
    return op_skills.hermes_skill_delete(
        profile=profile, name=name, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


# --- Config / env wrappers -----------------------------------------------


def hermes_config_get(profile: str = "default", key_path: str | None = None) -> str:
    return op_config.hermes_config_get(
        profile=profile, key_path=key_path, hermes_root=_default_hermes_root(),
    )


def hermes_config_set(profile: str = "default", key_path: str = "", value: Any = None, dry_run: bool = True) -> str:
    return op_config.hermes_config_set(
        profile=profile, key_path=key_path, value=value, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_config_patch(profile: str = "default", old_string: str = "", new_string: str = "", dry_run: bool = True) -> str:
    return op_config.hermes_config_patch(
        profile=profile, old_string=old_string, new_string=new_string,
        dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


def hermes_env_status(profile: str = "default", keys: list[str] | None = None) -> str:
    return op_config.hermes_env_status(
        profile=profile, keys=keys, hermes_root=_default_hermes_root(),
    )


def hermes_env_set_nonsecret(profile: str = "default", key: str = "", value: str = "", dry_run: bool = True) -> str:
    return op_config.hermes_env_set_nonsecret(
        profile=profile, key=key, value=value, dry_run=dry_run,
        hermes_root=_default_hermes_root(),
    )


def hermes_env_copy_nonsecret(source_profile: str, target_profile: str, key: str, dry_run: bool = True) -> str:
    return op_config.hermes_env_copy_nonsecret(
        source_profile=source_profile, target_profile=target_profile,
        key=key, dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


# --- Gateway / workspace / git / owner wrappers --------------------------


def hermes_gateway_status(profile: str = "default") -> str:
    return op_workspace.hermes_gateway_status(
        profile=profile, hermes_root=_default_hermes_root(),
    )


def hermes_gateway_restart(profile: str = "default", dry_run: bool = True) -> str:
    return op_workspace.hermes_gateway_restart(
        profile=profile, dry_run=dry_run, hermes_root=_default_hermes_root(),
    )


def hermes_workspace_read(path: str, offset: int = 1, limit: int = 500) -> str:
    return op_workspace.hermes_workspace_read(path=path, offset=offset, limit=limit)


def hermes_workspace_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    return op_workspace.hermes_workspace_patch(
        path=path, old_string=old_string, new_string=new_string,
        replace_all=replace_all, dry_run=dry_run,
    )


def hermes_workspace_write_file(path: str, content: str, dry_run: bool = True) -> str:
    return op_workspace.hermes_workspace_write_file(
        path=path, content=content, dry_run=dry_run,
    )


def hermes_workspace_run_test(command: str, workdir: str | None = None, timeout: int = 120, dry_run: bool = True) -> str:
    return op_workspace.hermes_workspace_run_test(
        command=command, workdir=workdir, timeout=timeout, dry_run=dry_run,
    )


def hermes_git_status(workdir: str) -> str:
    return op_workspace.hermes_git_status(workdir=workdir)


def hermes_git_diff(workdir: str, pathspec: str | None = None, stat: bool = False) -> str:
    return op_workspace.hermes_git_diff(workdir=workdir, pathspec=pathspec, stat=stat)


def hermes_owner_run_command(command: str, timeout: int = 120, workdir: str | None = None, dry_run: bool = True) -> str:
    return op_workspace.hermes_owner_run_command(
        command=command, timeout=timeout, workdir=workdir, dry_run=dry_run,
    )


def hermes_owner_patch(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    dry_run: bool = True,
) -> str:
    return op_workspace.hermes_owner_patch(
        path=path, old_string=old_string, new_string=new_string,
        replace_all=replace_all, dry_run=dry_run,
    )


def hermes_owner_write_file(path: str, content: str, dry_run: bool = True) -> str:
    return op_workspace.hermes_owner_write_file(path=path, content=content, dry_run=dry_run)


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 7677,
    http: bool = False,
    include_local_settings: bool = False,
) -> FastMCP:
    server = FastMCP(
        "hermes-gpt",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        sse_path="/sse",
        message_path="/messages/",
        stateless_http=http,
        json_response=http,
    )
    register_tools(server)
    return server


def register_tools(server: FastMCP) -> None:
    server.add_tool(hermes_read_file, meta=tool_meta())
    server.add_tool(hermes_search_files, meta=tool_meta())
    server.add_tool(hermes_memory, meta=tool_meta())
    server.add_tool(hermes_skill_list, meta=tool_meta())
    server.add_tool(hermes_skill_view, meta=tool_meta())

    if env_enabled(ENABLE_WRITE_ENV):
        server.add_tool(hermes_write_file, meta=tool_meta())
        server.add_tool(hermes_patch, meta=tool_meta())
    if env_enabled(ENABLE_TERMINAL_ENV):
        server.add_tool(hermes_run_command, meta=tool_meta())
    if env_enabled(ENABLE_SESSION_SEARCH_ENV):
        server.add_tool(hermes_session_search, meta=tool_meta())

    # --- Operator / Owner Mode tools -----------------------------------
    #
    # Read-only tools are always registered. Mutating tools are registered
    # unconditionally too (per spec: "register with refusal so the user can
    # see why unavailable") — the wrappers above return a JSON error string
    # when the operator policy is not enabled / level is insufficient /
    # apply_mode is dry_run / owner ack is missing.
    server.add_tool(hermes_operator_policy, meta=tool_meta())
    server.add_tool(hermes_operator_status, meta=tool_meta())
    server.add_tool(hermes_operator_audit_tail, meta=tool_meta())
    server.add_tool(hermes_operator_doctor, meta=tool_meta())
    server.add_tool(hermes_operator_snapshot, meta=tool_meta())
    server.add_tool(hermes_release_doctor, meta=tool_meta())
    server.add_tool(hermes_operator_recover, meta=tool_meta())

    # Cron
    server.add_tool(hermes_cron_list, meta=tool_meta())
    server.add_tool(hermes_cron_status, meta=tool_meta())
    server.add_tool(hermes_cron_run, meta=tool_meta())
    server.add_tool(hermes_cron_pause, meta=tool_meta())
    server.add_tool(hermes_cron_copy, meta=tool_meta())
    server.add_tool(hermes_cron_move, meta=tool_meta())

    # Skills
    server.add_tool(hermes_skill_diff, meta=tool_meta())
    server.add_tool(hermes_skill_create, meta=tool_meta())
    server.add_tool(hermes_skill_edit, meta=tool_meta())
    server.add_tool(hermes_skill_patch, meta=tool_meta())
    server.add_tool(hermes_skill_write_file, meta=tool_meta())
    server.add_tool(hermes_skill_copy, meta=tool_meta())
    server.add_tool(hermes_skill_sync_to_default, meta=tool_meta())
    server.add_tool(hermes_skill_delete, meta=tool_meta())

    # Config / env
    server.add_tool(hermes_config_get, meta=tool_meta())
    server.add_tool(hermes_config_set, meta=tool_meta())
    server.add_tool(hermes_config_patch, meta=tool_meta())
    server.add_tool(hermes_env_status, meta=tool_meta())
    server.add_tool(hermes_env_set_nonsecret, meta=tool_meta())
    server.add_tool(hermes_env_copy_nonsecret, meta=tool_meta())

    # Gateway / workspace / git / owner
    server.add_tool(hermes_gateway_status, meta=tool_meta())
    server.add_tool(hermes_gateway_restart, meta=tool_meta())
    server.add_tool(hermes_workspace_read, meta=tool_meta())
    server.add_tool(hermes_workspace_patch, meta=tool_meta())
    server.add_tool(hermes_workspace_write_file, meta=tool_meta())
    server.add_tool(hermes_workspace_run_test, meta=tool_meta())
    server.add_tool(hermes_git_status, meta=tool_meta())
    server.add_tool(hermes_git_diff, meta=tool_meta())
    server.add_tool(hermes_owner_run_command, meta=tool_meta())
    server.add_tool(hermes_owner_patch, meta=tool_meta())
    server.add_tool(hermes_owner_write_file, meta=tool_meta())


mcp = build_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent MCP sidecar.")
    parser.add_argument("--http", action="store_true", help="Run streamable HTTP transport instead of stdio.")
    parser.add_argument("--sse", action="store_true", help="Run legacy SSE transport instead of stdio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7677)
    parser.add_argument("--cert", help="Path to SSL certificate file (enables HTTPS)")
    parser.add_argument("--key", help="Path to SSL key file (enables HTTPS)")
    parser.add_argument(
        "--profile",
        choices=[LOCAL_DEV_PROFILE, REMOTE_PROFILE],
        default=LOCAL_DEV_PROFILE,
        help="Release safety profile. Remote no-auth is refused unless explicitly acknowledged.",
    )
    parser.add_argument(
        UNSAFE_REMOTE_ACK,
        action="store_true",
        dest="unsafe_remote_ack",
        help="Allow remote profile without auth. For experiments only; not release-safe.",
    )
    args = parser.parse_args()

    if args.http and args.sse:
        raise SystemExit("Choose only one of --http or --sse.")
    if args.profile == REMOTE_PROFILE and not (args.unsafe_remote_ack and env_enabled(UNSAFE_REMOTE_ENV)):
        raise SystemExit(
            "Remote profile requires real authentication, which is not implemented yet. "
            f"For temporary experiments only, pass {UNSAFE_REMOTE_ACK} and set {UNSAFE_REMOTE_ENV}=1."
        )
    if args.profile == LOCAL_DEV_PROFILE and not is_loopback_host(args.host):
        eprint(
            "WARNING: local-dev profile is bound to a non-loopback host. "
            "Do not expose hermes-gpt without real authentication."
        )
    if args.profile == REMOTE_PROFILE:
        eprint("WARNING: remote no-auth mode is explicitly unsafe and intended only for temporary experiments.")

    transport = "streamable-http" if args.http else "sse" if args.sse else "stdio"
    server = build_server(host=args.host, port=args.port, http=args.http)
    if transport == "stdio":
        eprint("hermes-gpt MCP server starting in stdio mode.")
        server.run(transport="stdio")
    else:
        path = "/mcp" if args.http else "/sse"
        eprint(f"hermes-gpt MCP server running at http://{args.host}:{args.port}{path}")

        # Run with uvicorn instead of FastMCP.run() so TLS can be enabled for
        # local-only testing when cert/key are provided.
        import uvicorn
        app = server.streamable_http_app() if args.http else server.sse_app()

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            ssl_certfile=args.cert if args.cert else None,
            ssl_keyfile=args.key if args.key else None,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )


if __name__ == "__main__":
    main()
