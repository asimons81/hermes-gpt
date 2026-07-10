"""Transport-agnostic, Codex-focused Hermes GPT tool layer.

The existing ``server`` module owns the mature Hermes Agent integrations.  This
module deliberately owns only the Codex-specific contract: capability gates,
safe local/URL inputs, compact repository context, and response sanitization.
It accepts callbacks for Hermes operations so it can be tested without either
an MCP transport or a local Hermes installation.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import operator_policy as op_policy


ENABLE_CODEX_ENV = "HERMES_GPT_ENABLE_CODEX"
ENABLE_MCP_ENV = "HERMES_GPT_ENABLE_MCP"
ENABLE_VISION_ENV = "HERMES_GPT_ENABLE_VISION"
ENABLE_WEB_ENV = "HERMES_GPT_ENABLE_WEB"
ENABLE_CRON_ENV = "HERMES_GPT_ENABLE_CRON"
ENABLE_DIAGNOSTICS_ENV = "HERMES_GPT_ENABLE_DIAGNOSTICS"
ALLOW_WRITE_ENV = "HERMES_GPT_ALLOW_WRITE"
ALLOW_CRON_WRITE_ENV = "HERMES_GPT_ALLOW_CRON_WRITE"
ALLOW_SKILL_WRITE_ENV = "HERMES_GPT_ALLOW_SKILL_WRITE"
ALLOWED_ROOTS_ENV = "HERMES_GPT_CODEX_ALLOWED_ROOTS"
ALLOW_PRIVATE_NETWORK_ENV = "HERMES_GPT_ALLOW_PRIVATE_NETWORK"

ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_EXTRACT_CHARS = 50_000
MAX_PLAN_FILES = 200
IGNORED_TREE_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".next",
}


def _env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def _error(code: str, message: str, suggested_action: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": {"code": code, "message": _redact_text(message)}}
    if suggested_action:
        result["error"]["suggested_action"] = _redact_text(suggested_action)
    return result


def _redact_text(value: str) -> str:
    text = op_policy.redact_output(str(value))
    patterns = [
        (r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?i)\b(?:sk-ant|xai-|AIza)[A-Za-z0-9_-]{12,}\b", "[REDACTED_PROVIDER_KEY]"),
        (r"(?i)(\b(?:cookie|session(?:_id)?|set-cookie)\s*[:=]\s*[\"']?)([^\s;\"']{8,})", r"\1[REDACTED]"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.DOTALL)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact all textual values before an MCP response leaves us."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(?:token|secret|password|passwd|api[_-]?key|cookie|session|authorization|private[_-]?key)", key_text):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_value(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item) for item in value]
    return value


def _decoded_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_roots(raw: str | None) -> list[Path]:
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        if not part.strip():
            continue
        try:
            candidate = Path(part).expanduser().resolve(strict=True)
        except OSError:
            continue
        if candidate.is_dir() and not op_policy.is_denied_path(candidate):
            roots.append(candidate)
    return roots


def _is_within(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _image_mime(path: Path) -> str | None:
    """Identify the allowed image formats from file signatures, not its name."""
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_project_root(project_root: str) -> Path:
    if not project_root or not project_root.strip():
        raise ValueError("project_root is required for local Codex operations.")
    try:
        root = Path(project_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("project_root must be an existing directory.") from exc
    if not root.is_dir():
        raise ValueError("project_root must be a directory.")
    if op_policy.is_denied_path(root):
        raise PermissionError("project_root is denied by the Hermes secret-path policy.")
    return root


def resolve_project_file(image_path: str, project_root: str | None) -> Path:
    if not image_path or not image_path.strip():
        raise ValueError("image_path is required.")
    try:
        image = Path(image_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("image_path must be an existing file.") from exc
    if not image.is_file():
        raise ValueError("image_path must be a file.")
    if op_policy.is_denied_path(image):
        raise PermissionError("image_path is denied by the Hermes secret-path policy.")

    roots = [resolve_project_root(project_root)] if project_root else _parse_roots(os.environ.get(ALLOWED_ROOTS_ENV))
    if not roots:
        raise PermissionError(
            "A project_root is required unless HERMES_GPT_CODEX_ALLOWED_ROOTS contains an approved root."
        )
    if not _is_within(image, roots):
        raise PermissionError("image_path resolves outside the approved project root.")
    if image.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise ValueError("image_path must be a supported raster image (jpg, jpeg, png, gif, webp, or bmp).")
    mime, _ = mimetypes.guess_type(str(image))
    detected_mime = _image_mime(image)
    if not mime or not detected_mime or mime != detected_mime:
        raise ValueError("image_path extension and detected MIME type must be a supported matching image format.")
    if image.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image_path exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB safety limit.")
    return image


def validate_public_url(url: str) -> str:
    """Reject local, private, metadata, credentialed, and non-HTTP URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Credentialed URLs are not allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("A hostname is required.")
    host = hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise PermissionError("Local network URLs are blocked.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ValueError("The URL hostname could not be resolved safely.") from exc

    for address in addresses:
        if not address.is_global:
            if not _env_enabled(ALLOW_PRIVATE_NETWORK_ENV):
                raise PermissionError("Private, loopback, link-local, and metadata network URLs are blocked.")
    return url


def _cron_expression(request: str) -> str | None:
    normalized = " ".join(request.lower().split())
    every_minutes = re.search(r"every\s+(\d{1,3})\s+minutes?", normalized)
    if every_minutes:
        minutes = int(every_minutes.group(1))
        if 1 <= minutes <= 59:
            return f"*/{minutes} * * * *"
    match = re.search(r"(?:daily|every day)(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", normalized)
    if match:
        hour, minute, suffix = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        if suffix:
            if not 1 <= hour <= 12:
                return None
            hour = (hour % 12) + (12 if suffix == "pm" else 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{minute} {hour} * * *"
    weekdays = {"monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6, "sunday": 0}
    for name, dow in weekdays.items():
        match = re.search(rf"(?:every\s+)?{name}(?:\s+at)?\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?", normalized)
        if match:
            hour, minute, suffix = int(match.group(1)), int(match.group(2) or 0), match.group(3)
            if suffix:
                if not 1 <= hour <= 12:
                    return None
                hour = (hour % 12) + (12 if suffix == "pm" else 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{minute} {hour} * * {dow}"
    return None


@dataclass
class CodexToolCore:
    """A small, transport-independent facade over existing Hermes operations."""

    version: str
    imports_ready: Callable[[], bool]
    gateway_snapshot: Callable[[], Any]
    gateway_diagnostics_callback: Callable[[], Any]
    vision_analyze: Callable[[str, str], Any]
    web_search: Callable[[str, int], Any]
    web_extract: Callable[[list[str], int], Any]
    cron_create_callback: Callable[[str, str, bool], Any]
    skill_create_callback: Callable[[str, str, bool], Any]

    def _base_gate(self) -> dict[str, Any] | None:
        missing = [name for name in (ENABLE_CODEX_ENV, ENABLE_MCP_ENV) if not _env_enabled(name)]
        if missing:
            return _error(
                "CODEX_DISABLED",
                "Codex MCP integration is disabled.",
                "Set " + " and ".join(f"{name}=1" for name in missing) + " and restart the MCP server.",
            )
        return None

    def _capability_gate(self, name: str, env_name: str) -> dict[str, Any] | None:
        blocked = self._base_gate()
        if blocked:
            return blocked
        if not _env_enabled(env_name):
            return _error(
                "CAPABILITY_DISABLED",
                f"{name} is disabled.",
                f"Set {env_name}=1 and restart the MCP server.",
            )
        return None

    def status(self) -> dict[str, Any]:
        blocked = self._base_gate()
        if blocked:
            return redact_value({**blocked, "version": self.version, "capabilities": self.capabilities()["capabilities"]})
        try:
            snapshot = _decoded_json(self.gateway_snapshot())
            gateway = snapshot.get("gateway", {}) if isinstance(snapshot, dict) else {}
            if isinstance(snapshot, dict) and not isinstance(gateway, dict):
                gateway = {}
            if isinstance(snapshot, dict) and not gateway:
                gateway = {
                    "running": snapshot.get("gateway_running", False),
                    "pid": snapshot.get("gateway_pid"),
                    "pid_source": snapshot.get("gateway_pid_source"),
                    "state": snapshot.get("gateway_state"),
                }
            running = bool(gateway.get("running"))
            return redact_value({
                "ok": bool(self.imports_ready()),
                "gateway": "running" if running else "not_running",
                "agent": "reachable" if self.imports_ready() else "unavailable",
                "version": self.version,
                "capabilities": [name for name, item in self.capabilities()["capabilities"].items() if item["enabled"]],
                "gateway_pid": gateway.get("pid"),
                "gateway_pid_source": gateway.get("pid_source"),
            })
        except Exception as exc:
            return _error("STATUS_UNAVAILABLE", "Hermes status could not be read.", _redact_text(str(exc)))

    def capabilities(self) -> dict[str, Any]:
        base_enabled = not bool(self._base_gate())
        def state(env_name: str, reason: str) -> dict[str, Any]:
            enabled = base_enabled and _env_enabled(env_name)
            return {"enabled": enabled, **({} if enabled else {"reason": reason if base_enabled else "Codex MCP integration is disabled."})}
        return redact_value({
            "ok": True,
            "capabilities": {
                "status": {"enabled": base_enabled, **({} if base_enabled else {"reason": "Codex MCP integration is disabled."})},
                "planning": {"enabled": base_enabled, **({} if base_enabled else {"reason": "Codex MCP integration is disabled."})},
                "vision": state(ENABLE_VISION_ENV, f"Set {ENABLE_VISION_ENV}=1."),
                "web_search": state(ENABLE_WEB_ENV, f"Set {ENABLE_WEB_ENV}=1."),
                "web_extract": state(ENABLE_WEB_ENV, f"Set {ENABLE_WEB_ENV}=1."),
                "cron": state(ENABLE_CRON_ENV, f"Set {ENABLE_CRON_ENV}=1."),
                "diagnostics": state(ENABLE_DIAGNOSTICS_ENV, f"Set {ENABLE_DIAGNOSTICS_ENV}=1."),
                "skill_authoring": {
                    "enabled": base_enabled,
                    "write_enabled": base_enabled and _env_enabled(ALLOW_WRITE_ENV) and _env_enabled(ALLOW_SKILL_WRITE_ENV),
                    "reason": "Drafts are available in dry-run mode; direct writes need explicit write gates.",
                },
            },
        })

    def vision(self, image_path: str, prompt: str, project_root: str | None = None, detail: str = "medium") -> dict[str, Any]:
        blocked = self._capability_gate("Vision analysis", ENABLE_VISION_ENV)
        if blocked:
            return blocked
        if detail not in {"low", "medium", "high"}:
            return _error("INVALID_DETAIL", "detail must be low, medium, or high.")
        try:
            image = resolve_project_file(image_path, project_root)
            result = self.vision_analyze(str(image), f"Return {detail}-detail analysis. {prompt}".strip())
            return redact_value({"ok": True, "image_path": str(image), "detail": detail, "analysis": _decoded_json(result)})
        except (OSError, ValueError, PermissionError) as exc:
            return _error("VISION_INPUT_BLOCKED", str(exc))
        except Exception:
            return _error("VISION_UNAVAILABLE", "Hermes vision analysis failed without exposing internal details.")

    def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        blocked = self._capability_gate("Web search", ENABLE_WEB_ENV)
        if blocked:
            return blocked
        if not query or not query.strip():
            return _error("INVALID_QUERY", "query is required.")
        try:
            limit = max(1, min(int(max_results), 10))
            return redact_value({"ok": True, "query": query.strip(), "results": _decoded_json(self.web_search(query.strip(), limit))})
        except Exception:
            return _error("WEB_SEARCH_UNAVAILABLE", "Hermes web search failed without exposing internal details.")

    def extract_page(self, url: str, max_chars: int = 12000) -> dict[str, Any]:
        blocked = self._capability_gate("Web extraction", ENABLE_WEB_ENV)
        if blocked:
            return blocked
        try:
            safe_url = validate_public_url(url)
            limit = max(500, min(int(max_chars), MAX_EXTRACT_CHARS))
            return redact_value({"ok": True, "url": safe_url, "content": _decoded_json(self.web_extract([safe_url], limit))})
        except (ValueError, PermissionError) as exc:
            return _error("URL_BLOCKED", str(exc))
        except Exception:
            return _error("WEB_EXTRACT_UNAVAILABLE", "Hermes page extraction failed without exposing internal details.")

    def plan(self, goal: str, project_root: str, include_git_diff: bool = False, include_tree: bool = True, max_files: int = 80) -> dict[str, Any]:
        blocked = self._base_gate()
        if blocked:
            return blocked
        if not goal or not goal.strip():
            return _error("INVALID_GOAL", "goal is required.")
        try:
            root = resolve_project_root(project_root)
            cap = max(1, min(int(max_files), MAX_PLAN_FILES))
            files: list[str] = []
            for item in root.rglob("*"):
                if any(part in IGNORED_TREE_PARTS for part in item.parts):
                    continue
                if item.is_file() and not op_policy.is_denied_path(item):
                    files.append(item.relative_to(root).as_posix())
                    if len(files) >= cap:
                        break
            suggested = [name for name in files if Path(name).name.lower() in {"readme.md", "pyproject.toml", "package.json", "server.py", "main.py"}]
            if not suggested:
                suggested = files[: min(8, len(files))]
            git: dict[str, Any] = {}
            if (root / ".git").exists():
                status = subprocess.run(["git", "status", "--short"], cwd=root, shell=False, text=True, capture_output=True, timeout=10)
                git["status"] = _redact_text(status.stdout.strip())
                if include_git_diff:
                    diff = subprocess.run(["git", "diff", "--stat"], cwd=root, shell=False, text=True, capture_output=True, timeout=10)
                    git["diff_stat"] = _redact_text(diff.stdout.strip())
            return redact_value({
                "ok": True,
                "dry_run": True,
                "goal": goal.strip(),
                "project_root": str(root),
                "suggested_files": suggested,
                "repository_context": {"file_count_sampled": len(files), **({"tree": files} if include_tree else {}), **({"git": git} if git else {})},
                "steps": [
                    "Review the suggested files and existing tests before changing behavior.",
                    "Implement the smallest change that satisfies the stated goal.",
                    "Run focused tests first, then the full project test suite.",
                    "Inspect the diff for secrets, unintended writes, and scope creep before committing.",
                ],
                "risks": ["This context pack is read-only and deterministic; it does not invoke an external planner.", "Ignored and secret-like paths are excluded from the repository sample."],
            })
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
            return _error("PLAN_INPUT_BLOCKED", str(exc))

    def cron_plan(self, request: str, timezone: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        blocked = self._capability_gate("Cron planning", ENABLE_CRON_ENV)
        if blocked:
            return blocked
        if not request or not request.strip():
            return _error("INVALID_REQUEST", "request is required.")
        schedule = _cron_expression(request)
        result = {
            "ok": True,
            "dry_run": True,
            "request": request.strip(),
            "timezone": timezone or "local system timezone",
            "proposed_schedule": schedule,
            "command": "hermes-gpt codex mcp",
            "required_env_vars": [ENABLE_CODEX_ENV, ENABLE_MCP_ENV, ENABLE_CRON_ENV],
            "risks": ["Cron creation remains blocked until confirm=true and both cron write gates are enabled.", "Review timezone and schedule before creating a job."],
        }
        if not schedule:
            result["needs_clarification"] = "Use an explicit schedule such as 'daily at 9am', 'Monday at 14:30', or 'every 15 minutes'."
        return redact_value(result)

    def cron_create(self, request: str, timezone: str | None = None, confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
        plan = self.cron_plan(request=request, timezone=timezone, dry_run=True)
        if not plan.get("ok"):
            return plan
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "Cron creation requires confirm=true. Review hermes_cron_plan first.")
        if not _env_enabled(ALLOW_WRITE_ENV) or not _env_enabled(ALLOW_CRON_WRITE_ENV):
            return _error("CRON_WRITE_DISABLED", "Cron creation is write-gated.", f"Set {ALLOW_WRITE_ENV}=1 and {ALLOW_CRON_WRITE_ENV}=1; existing operator direct-mode gates also apply.")
        schedule = plan.get("proposed_schedule")
        if not schedule:
            return _error("SCHEDULE_UNRESOLVED", "The request could not be converted to a safe cron schedule.")
        try:
            result = self.cron_create_callback(str(schedule), request.strip(), bool(dry_run))
            return redact_value({"ok": True, "result": _decoded_json(result)})
        except Exception:
            return _error("CRON_CREATE_UNAVAILABLE", "Cron creation failed without exposing internal details.")

    def author_skill(self, skill_name: str, goal: str, project_root: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        blocked = self._base_gate()
        if blocked:
            return blocked
        name = (skill_name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            return _error("INVALID_SKILL_NAME", "skill_name must be lowercase letters, numbers, and hyphens.")
        if not goal or not goal.strip():
            return _error("INVALID_GOAL", "goal is required.")
        if project_root:
            try:
                resolve_project_root(project_root)
            except (ValueError, PermissionError) as exc:
                return _error("SKILL_INPUT_BLOCKED", str(exc))
        draft = f"---\nname: {name}\ndescription: {goal.strip()[:180]}\n---\n\n# {name}\n\n{goal.strip()}\n"
        if dry_run:
            return redact_value({"ok": True, "dry_run": True, "skill_name": name, "diff_preview": draft, "write_blocked_by_default": True})
        if not _env_enabled(ALLOW_WRITE_ENV) or not _env_enabled(ALLOW_SKILL_WRITE_ENV):
            return _error("SKILL_WRITE_DISABLED", "Skill writing is write-gated.", f"Set {ALLOW_WRITE_ENV}=1 and {ALLOW_SKILL_WRITE_ENV}=1; existing operator direct-mode gates also apply.")
        try:
            result = self.skill_create_callback(name, draft, False)
            return redact_value({"ok": True, "dry_run": False, "result": _decoded_json(result)})
        except Exception:
            return _error("SKILL_WRITE_UNAVAILABLE", "Skill creation failed without exposing internal details.")

    def gateway_diagnostics(self, verbose: bool = False) -> dict[str, Any]:
        blocked = self._capability_gate("Gateway diagnostics", ENABLE_DIAGNOSTICS_ENV)
        if blocked:
            return blocked
        try:
            data = _decoded_json(self.gateway_diagnostics_callback())
            if isinstance(data, dict) and not verbose:
                data = {key: data.get(key) for key in ("success", "profile", "checks", "failed_checks", "warnings", "recommended_action", "trace_id") if key in data}
            return redact_value({"ok": True, "diagnostics": data})
        except Exception:
            return _error("DIAGNOSTICS_UNAVAILABLE", "Gateway diagnostics failed without exposing internal details.")
