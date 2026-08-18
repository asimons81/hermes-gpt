"""Pluggable execution backends for hermes-gpt work contracts.

A runner backend is responsible only for executing a canonical work contract and
reporting bounded observed state. Contract policy, authorization, completion
criteria, artifact checks, and review remain owned by ``operator_contract``.

Built-ins:
- ``fleet``: existing Hermes A2A fleet work-order transport (compatibility default)
- ``pi_rpc``: Pi coding agent JSONL RPC mode
- ``omx``: Oh My Codex non-interactive ``omx exec``
- ``codex``: existing hermes-gpt Codex operator job runner

Third-party backends can implement ``RunnerBackend`` and call
``register_backend()``. No contract/schema changes are required for additional
backend names; contracts select them with ``execution.backend``.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import operator_fleet as op_fleet
import operator_policy as op

SCHEMA_VERSION = "0.6-runner.1"
RUNNER_PLUGINS_ENV = "HERMES_GPT_ENABLE_RUNNER_PLUGINS"
RUNNER_PLUGIN_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_PLUGIN_ALLOWLIST"
RUNNER_BACKEND_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_BACKEND_ALLOWLIST"
RUNNER_PROVIDER_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_PROVIDER_ALLOWLIST"
RUNNER_MODEL_ALLOWLIST_ENV = "HERMES_GPT_RUNNER_MODEL_ALLOWLIST"
_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_ID_RE = op_fleet._TASK_ID_RE
_MAX_OPTIONS_BYTES = 8_000
_MAX_RESULT_CHARS = 8_000
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(hermes_root: Path | None = None) -> Path:
    base = op.normalize_hermes_data_root(hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
    return Path(base or Path.home() / ".hermes") / "runner-jobs"


def _job_paths(task_id: str, hermes_root: Path | None = None) -> tuple[Path, Path, Path]:
    root = _root(hermes_root)
    return root / f"{task_id}.json", root / f"{task_id}.request.json", root / f"{task_id}.jsonl"


def _cancel_path(task_id: str, hermes_root: Path | None = None) -> Path:
    return _root(hermes_root) / f"{task_id}.cancel.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _bounded_text(value: Any, maximum: int = _MAX_RESULT_CHARS) -> str:
    text = op.redact_output(str(value or ""))
    return text if len(text) <= maximum else text[: maximum - 3] + "..."


def _popen_process_group(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a child with a platform-specific process-tree boundary."""
    kwargs.setdefault("close_fds", True)
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(argv, **kwargs)


def _terminate_process_tree(proc: subprocess.Popen[Any], *, timeout: float = 5.0) -> None:
    """Terminate the child process tree created by _popen_process_group.

    POSIX uses the child session/process group. Windows does not expose a true
    process-tree kill through the Python standard library, so production Windows
    cleanup uses taskkill /T /F for descendants and falls back to direct-child
    terminate/kill if taskkill is unavailable.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _terminate_process_group(proc: subprocess.Popen[Any], *, timeout: float = 5.0) -> None:
    """Backward-compatible alias for the process-tree terminator."""
    _terminate_process_tree(proc, timeout=timeout)


def _audit_runner(
    *,
    tool: str,
    policy: op.OperatorPolicy,
    dry_run: bool,
    success: bool,
    changed: bool = False,
    summary: str = "",
    task_id: str = "",
    backend: str = "",
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=summary,
            extra={"task_id": task_id, "backend": backend},
        )
    except Exception as exc:
        logger.debug("runner audit failed", exc_info=exc)


def _split_allowlist(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _allowed_by_env(value: str, env_name: str) -> bool:
    allowed = _split_allowlist(os.environ.get(env_name))
    return not allowed or value in allowed


def _runner_allowed(name: str) -> bool:
    return _allowed_by_env(name, RUNNER_BACKEND_ALLOWLIST_ENV)


def _minimal_child_env() -> dict[str, str]:
    """Return a non-secret, explicit child env baseline for local runners."""
    allowed_keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "PYTHONIOENCODING",
        "PI_CODING_AGENT_DIR",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
    return {key: value for key, value in os.environ.items() if key in allowed_keys and value}


def _cleanup_stale_request_envelopes(*, hermes_root: Path | None = None, ttl_seconds: int = 3600) -> int:
    """Delete stale transient request envelopes after their TTL expires."""
    root = _root(hermes_root)
    if not root.is_dir():
        return 0
    now = datetime.now(timezone.utc).timestamp()
    removed = 0
    for request_path in root.glob("*.request.json"):
        try:
            age = now - request_path.stat().st_mtime
        except OSError:
            continue
        if age < ttl_seconds:
            continue
        try:
            request_path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def normalize_execution(value: Any) -> dict[str, Any] | None:
    """Validate an optional contract ``execution`` selector.

    The block is deliberately backend-agnostic. ``options`` must be a bounded
    JSON object; individual backends validate the options they understand.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("execution must be an object")
    backend = str(value.get("backend") or "").strip().lower()
    if not _BACKEND_RE.fullmatch(backend):
        raise ValueError("execution.backend is invalid")
    options = value.get("options") or {}
    if not isinstance(options, dict):
        raise TypeError("execution.options must be an object")
    secretish = re.compile(r"(?:secret|token|password|api[_-]?key|credential|private[_-]?key)", re.IGNORECASE)
    bad_keys = [str(key) for key in options if secretish.search(str(key))]
    if bad_keys:
        raise ValueError("execution.options must not carry secrets; use runner environment/config instead")
    try:
        encoded = json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("execution.options must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > _MAX_OPTIONS_BYTES:
        raise ValueError(f"execution.options exceeds {_MAX_OPTIONS_BYTES} bytes")
    return {"backend": backend, "options": options}


def _authorization_class(contract: dict[str, Any]) -> str:
    return str(contract.get("authorization", {}).get("class") or "none")


def _pi_tools(contract: dict[str, Any]) -> str:
    """Return Pi tools for a confined-read execution posture.

    Pi is intentionally limited to its read tool until Hermes GPT has a real
    filesystem confinement boundary for absolute paths, ``..`` traversal, and
    shell ``cd`` escape cases. CWD is not a sandbox.
    """
    options = ((contract.get("execution") or {}).get("options") or {})
    auth_class = _authorization_class(contract)
    if auth_class not in {"none", "read_only"}:
        raise PermissionError("pi_rpc is read-only until filesystem confinement is available")
    requested = options.get("tools")
    if requested is None or requested == "":
        return "read"
    if not isinstance(requested, str):
        raise TypeError("pi_rpc execution.options.tools must be a comma-delimited string")
    tools = [item.strip() for item in requested.split(",") if item.strip()]
    if not tools:
        raise ValueError("pi_rpc execution.options.tools must not be empty")
    if set(tools) != {"read"}:
        raise PermissionError("pi_rpc may only enable Pi's read tool until filesystem confinement is available")
    return "read"


def _pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"


def _pi_selection(contract: dict[str, Any]) -> tuple[str, str]:
    """Return the effective Pi provider/model without exposing credentials."""
    options = ((contract.get("execution") or {}).get("options") or {})
    provider = str(options.get("provider") or "").strip()
    model = str(options.get("model") or "").strip()
    settings = _load_json(_pi_agent_dir() / "settings.json") or {}
    if not provider:
        provider = str(settings.get("defaultProvider") or "").strip()
    if not model:
        model = str(settings.get("defaultModel") or "").strip()
    return provider, model


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _pi_child_env(contract: dict[str, Any], hermes_root: Path | None, provider: str) -> dict[str, str]:
    """Build a minimal child environment with only the selected provider credential.

    This intentionally does not inherit ``os.environ`` wholesale. The child gets
    a small non-secret baseline plus, when configured, exactly the single env-ref
    credential referenced by the selected Pi provider.
    """
    child_env = _minimal_child_env()
    if not provider:
        return child_env

    models = _load_json(_pi_agent_dir() / "models.json") or {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    config = providers.get(provider) if isinstance(providers, dict) else None
    raw_key = config.get("apiKey") if isinstance(config, dict) else None
    if not isinstance(raw_key, str):
        return child_env
    match = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", raw_key.strip())
    if not match:
        return child_env
    key_name = match.group(1)

    profile = str(contract.get("assigned_profile") or "default")
    allowed_profiles = contract.get("allowed_scope", {}).get("profiles") or []
    if allowed_profiles and profile not in allowed_profiles:
        raise PermissionError("Pi runner profile is outside the contract allowed_scope")

    value = os.environ.get(key_name)
    if not value:
        profile_home = op.resolve_profile_home(profile, hermes_root)
        import operator_config as op_config

        value = op_config._read_env_value(profile_home / ".env", key_name)
    if value:
        child_env[key_name] = _unquote_env_value(value)
    return child_env


def _sandbox_for(contract: dict[str, Any], *, backend: str) -> str:
    options = ((contract.get("execution") or {}).get("options") or {})
    auth_class = _authorization_class(contract)
    requested = options.get("sandbox")
    sandbox = str(requested or ("read-only" if auth_class in {"none", "read_only"} else "workspace-write"))
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError(f"{backend} execution.options.sandbox must be read-only or workspace-write")
    if auth_class in {"none", "read_only"} and sandbox != "read-only":
        raise PermissionError(f"read-only authorization may not use {backend} workspace-write sandbox")
    return sandbox


class RunnerBackend(Protocol):
    name: str

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]: ...

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]: ...

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]: ...


_BACKENDS: dict[str, RunnerBackend] = {}
_REGISTRY_LOCK = threading.RLock()


def register_backend(backend: RunnerBackend, *, replace: bool = False) -> None:
    name = str(getattr(backend, "name", "") or "").strip().lower()
    if not _BACKEND_RE.fullmatch(name):
        raise ValueError("runner backend name is invalid")
    with _REGISTRY_LOCK:
        if name in _BACKENDS and not replace:
            raise ValueError(f"runner backend {name!r} is already registered")
        _BACKENDS[name] = backend


def get_backend(name: str) -> RunnerBackend:
    key = str(name or "").strip().lower()
    with _REGISTRY_LOCK:
        backend = _BACKENDS.get(key)
    if backend is None:
        raise LookupError(f"runner backend {key!r} is not registered")
    return backend


def load_entrypoint_backends() -> list[str]:
    """Load external runner plugins from the ``hermes_gpt.runners`` group.

    Each entry point may expose either a backend instance or a zero-argument
    factory/class returning one. Broken plugins are isolated and skipped.
    """
    loaded: list[str] = []
    try:
        eps = importlib.metadata.entry_points()
        selected = eps.select(group="hermes_gpt.runners") if hasattr(eps, "select") else eps.get("hermes_gpt.runners", [])
    except Exception as exc:
        logger.debug("runner entry-point discovery failed", exc_info=exc)
        return loaded
    builtin_names = {"fleet", "pi_rpc", "omx", "codex"}
    for ep in selected:
        try:
            candidate = ep.load()
            if isinstance(candidate, type) or (callable(candidate) and not hasattr(candidate, "dispatch")):
                backend = candidate()
            else:
                backend = candidate
            name = str(getattr(backend, "name", "") or "").strip().lower()
            if name in builtin_names:
                raise ValueError(f"external runner may not shadow built-in backend {name!r}")
            allowed_plugins = _split_allowlist(os.environ.get(RUNNER_PLUGIN_ALLOWLIST_ENV))
            if not allowed_plugins or (name not in allowed_plugins and getattr(ep, "name", "") not in allowed_plugins):
                raise PermissionError(f"external runner {name!r} is not allowlisted by {RUNNER_PLUGIN_ALLOWLIST_ENV}")
            register_backend(backend, replace=False)
            loaded.append(str(getattr(backend, "name", ep.name)))
        except Exception as exc:
            logger.debug("runner entry point %s failed to load", getattr(ep, "name", "unknown"), exc_info=exc)
            continue
    return loaded


def list_backends(*, hermes_root: Path | None = None) -> list[dict[str, Any]]:
    _cleanup_stale_request_envelopes(hermes_root=hermes_root)
    with _REGISTRY_LOCK:
        items = list(_BACKENDS.values())
    out: list[dict[str, Any]] = []
    for backend in items:
        try:
            info = backend.availability(hermes_root=hermes_root)
        except Exception as exc:  # noqa: BLE001
            info = {"available": False, "reason": exc.__class__.__name__}
        out.append({"name": backend.name, **info})
    return sorted(out, key=lambda item: item["name"])


def selected_backend(contract: dict[str, Any]) -> str:
    execution = contract.get("execution")
    if isinstance(execution, dict) and execution.get("backend"):
        backend = str(execution["backend"])
    else:
        backend = "fleet"
    if not _runner_allowed(backend):
        raise PermissionError(f"runner backend {backend!r} is not allowed by {RUNNER_BACKEND_ALLOWLIST_ENV}")
    return backend


def dispatch_contract(
    contract: dict[str, Any],
    *,
    confirm: bool,
    dry_run: bool,
    timeout: int,
    hermes_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        backend_name = selected_backend(contract)
        backend = get_backend(backend_name)
    except LookupError as exc:
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_BACKEND_UNKNOWN",
            "safe_message": str(exc),
            "suggested_action": "Select a registered execution.backend.",
            "backend": str((contract.get("execution") or {}).get("backend") or "fleet"),
        }
    except PermissionError as exc:
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_BACKEND_NOT_ALLOWED",
            "safe_message": _bounded_text(exc, 300),
            "suggested_action": "Update runner backend allowlist or select an allowed backend.",
            "backend": str((contract.get("execution") or {}).get("backend") or "fleet"),
        }
    try:
        result = backend.dispatch(
            contract,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=hermes_root,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        if backend_name == "fleet" and not isinstance(contract.get("execution"), dict):
            # Legacy contract-dispatch compatibility applies only to contracts
            # that omit the execution selector and therefore use the historical
            # implicit fleet path. An explicit execution.backend="fleet" is a
            # runner selection and uses the runner error contract below.
            return {
                "success": False,
                "ok": False,
                "code": "CONTRACT_DISPATCH_ERROR",
                "safe_message": _bounded_text(exc, 300),
                "suggested_action": "Check fleet authority manifest, registry, and peer service.",
                "backend": backend_name,
            }
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_DISPATCH_ERROR",
            "safe_message": _bounded_text(exc, 300),
            "suggested_action": f"Check the {backend_name} runner backend and retry.",
            "backend": backend_name,
        }
    result.setdefault("backend", backend_name)
    return result


def observed_runs(task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with _REGISTRY_LOCK:
        items = list(_BACKENDS.values())
    for backend in items:
        try:
            out.extend(backend.observed_runs(task_id, hermes_root=hermes_root))
        except Exception as exc:
            logger.debug("runner backend %s observation failed", getattr(backend, "name", "unknown"), exc_info=exc)
            continue
    return out


@dataclass
class FleetBackend:
    name: str = "fleet"

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            payload = json.loads(op_fleet.hermes_fleet_list())
            return {"available": bool(payload.get("success")), "reason": payload.get("safe_message") if not payload.get("success") else None}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": _bounded_text(exc, 200)}

    def dispatch(self, contract: dict[str, Any], *, confirm: bool, dry_run: bool, timeout: int, hermes_root: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        workspaces = contract["allowed_scope"]["workspaces"]
        acceptance_checks = [
            f"run_state outcome_ok={contract['completion_criteria']['run_state']['outcome_ok']}",
            f"artifacts_present={contract['completion_criteria']['artifacts_present']}",
            f"tests_pass={contract['completion_criteria']['tests_pass']}",
            f"review_satisfied={contract['completion_criteria']['review_satisfied']}",
            f"no_forbidden_actions={contract['completion_criteria']['no_forbidden_actions']}",
        ]
        deliverables = [a["path"] for a in contract["expected_artifacts"]]
        text = op_fleet.hermes_fleet_dispatch_work_order(
            agent=contract["assigned_agent"],
            task_id=contract["task_id"],
            target_profile=contract["assigned_profile"],
            objective=contract["objective"],
            workspace=workspaces[0] if workspaces else "",
            inputs=contract["inputs"],
            constraints=contract["constraints"],
            acceptance_checks=acceptance_checks,
            deliverables=deliverables,
            authorization=contract["authorization"],
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            runner=kwargs.get("runner"),
            hermes_bin=kwargs.get("hermes_bin"),
            authority_manifest=kwargs.get("authority_manifest"),
        )
        payload = json.loads(text)
        payload["backend"] = self.name
        return payload

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        return []  # Fleet observations remain sourced from Mission Control.

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        return {"success": False, "code": "RUNNER_CANCEL_UNSUPPORTED", "backend": self.name}


class _LocalProcessBackend:
    name = "local"

    def executable(self) -> str | None:
        raise NotImplementedError

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        exe = self.executable()
        return {"available": bool(exe), "executable": exe}

    def _policy_workspace(self, contract: dict[str, Any]) -> Path:
        workspaces = contract.get("allowed_scope", {}).get("workspaces") or []
        if not workspaces:
            raise ValueError("local runner requires at least one allowed workspace")
        workspace = Path(workspaces[0]).expanduser().resolve()
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_workspace_path(str(workspace))
        return workspace

    def dispatch(self, contract: dict[str, Any], *, confirm: bool, dry_run: bool, timeout: int, hermes_root: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
        workspace = self._policy_workspace(contract)
        exe = self.executable()
        if not exe:
            return {"success": False, "code": "RUNNER_UNAVAILABLE", "backend": self.name, "safe_message": f"{self.name} executable not found"}
        plan = self.build_plan(contract)
        plan.update({"backend": self.name, "workspace": str(workspace), "task_id": contract["task_id"]})
        if effective:
            return {"success": True, "dry_run": True, "changed": False, "backend": self.name, "plan": plan}
        if not confirm:
            return {"success": False, "code": "CONFIRMATION_REQUIRED", "backend": self.name, "safe_message": "local runner dispatch requires confirm=true"}

        task_id = contract["task_id"]
        meta_path, request_path, _ = _job_paths(task_id, hermes_root)
        if meta_path.exists():
            return {"success": False, "code": "RUNNER_JOB_EXISTS", "backend": self.name, "safe_message": f"runner job {task_id!r} already exists"}
        request = {
            "backend": self.name,
            "contract": contract,
            "timeout": max(10, min(int(timeout), 3600)),
            "hermes_root": str((hermes_root or Path.home() / ".hermes").expanduser()),
        }
        _atomic_json(request_path, request)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "backend": self.name,
            "state": "queued",
            "outcome": "",
            "workspace": str(workspace),
            "created_at": _now(),
            "started_at": None,
            "ended_at": None,
            "pid": None,
            "returncode": None,
            "error": "",
        }
        _atomic_json(meta_path, meta)
        try:
            proc = _popen_process_group(
                [sys.executable, str(Path(__file__).resolve()), "--worker", task_id, "--root", str(_root(hermes_root))],
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            # Spawn failed after the request/meta envelopes were written.
            # Delete the request envelope so the raw objective/prompt cannot
            # remain on disk, and leave only bounded failed metadata. Catch
            # broadly because wrappers/test doubles can fail before a child
            # process exists with exceptions other than OSError.
            for path in (request_path, request_path.with_suffix(request_path.suffix + ".tmp")):
                try:
                    path.unlink()
                except OSError:
                    pass
            meta.update({
                "state": "failed",
                "outcome": "failed",
                "ended_at": _now(),
                "error": _bounded_text(f"runner spawn failed: {exc}", 300),
            })
            _atomic_json(meta_path, meta)
            return {
                "success": False,
                "code": "RUNNER_SPAWN_FAILED",
                "backend": self.name,
                "task_id": task_id,
                "safe_message": _bounded_text(exc, 300),
            }
        # The worker owns durable state transitions. Avoid a parent-side write
        # after spawn: a fast worker could otherwise complete and then be
        # overwritten back to "running" by the parent.
        return {"success": True, "changed": True, "dry_run": False, "backend": self.name, "task_id": task_id, "state": "queued", "pid": proc.pid}

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            return []
        meta_path, _, _ = _job_paths(task_id, hermes_root)
        meta = _load_json(meta_path)
        if not meta or meta.get("backend") != self.name:
            return []
        return [{
            "task_id": task_id,
            "status": meta.get("state"),
            "outcome": meta.get("outcome") or meta.get("state"),
            "error": meta.get("error") or None,
            "started_at": meta.get("started_at") or meta.get("created_at"),
            "ended_at": meta.get("ended_at"),
            "scope": f"runner:{self.name}",
        }]

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        meta_path, _, _ = _job_paths(task_id, hermes_root)
        meta = _load_json(meta_path)
        if not meta or meta.get("backend") != self.name:
            return {"success": False, "code": "RUNNER_JOB_NOT_FOUND", "backend": self.name}
        if meta.get("state") in _TERMINAL_STATES:
            return {"success": True, "changed": False, "backend": self.name, "state": meta.get("state")}
        _atomic_json(_cancel_path(task_id, hermes_root), {"task_id": task_id, "requested_at": _now()})
        pid = meta.get("pid")
        if isinstance(pid, int) and pid > 1:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        meta["state"] = "cancelled"
        meta["outcome"] = "cancelled"
        meta["ended_at"] = _now()
        _atomic_json(meta_path, meta)
        return {"success": True, "changed": True, "backend": self.name, "state": "cancelled"}


@dataclass
class PiRpcBackend(_LocalProcessBackend):
    name: str = "pi_rpc"

    def executable(self) -> str | None:
        configured = os.environ.get("HERMES_GPT_PI_EXE")
        candidates = [configured, shutil.which("pi"), str(Path.home() / ".local" / "bin" / "pi")]
        package_cli = Path.home() / ".local" / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
        candidates.append(str(package_cli))
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        tools = _pi_tools(contract)
        provider, model = _pi_selection(contract)
        if provider and not _allowed_by_env(provider, RUNNER_PROVIDER_ALLOWLIST_ENV):
            raise PermissionError(f"Pi provider {provider!r} is not allowed by {RUNNER_PROVIDER_ALLOWLIST_ENV}")
        if model and not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(f"Pi model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}")
        return {"protocol": "jsonl-rpc", "mode": "rpc", "tools": tools, "model": model or None, "provider": provider or None}


@dataclass
class OmxBackend(_LocalProcessBackend):
    name: str = "omx"

    def executable(self) -> str | None:
        configured = os.environ.get("HERMES_GPT_OMX_EXE")
        candidates = [configured, shutil.which("omx"), "/usr/bin/omx", str(Path.home() / ".local" / "bin" / "omx")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def build_plan(self, contract: dict[str, Any]) -> dict[str, Any]:
        options = ((contract.get("execution") or {}).get("options") or {})
        sandbox = _sandbox_for(contract, backend="omx")
        model = options.get("model")
        if model and not _allowed_by_env(str(model), RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(f"OMX model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}")
        return {"mode": "exec", "json": True, "sandbox": sandbox, "model": model, "profile": options.get("profile")}


@dataclass
class CodexBackend:
    name: str = "codex"

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            import operator_codex as op_codex
            status = op_codex.hermes_codex_status()
            if isinstance(status, str):
                status = json.loads(status)
            return {"available": bool(status.get("codex_available")), "enabled": bool(status.get("enabled")), "write_enabled": bool(status.get("write_enabled"))}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": _bounded_text(exc, 200)}

    def dispatch(self, contract: dict[str, Any], *, confirm: bool, dry_run: bool, timeout: int, hermes_root: Path | None = None, **kwargs: Any) -> dict[str, Any]:
        import operator_codex as op_codex
        options = ((contract.get("execution") or {}).get("options") or {})
        workspace = contract["allowed_scope"]["workspaces"][0]
        sandbox = _sandbox_for(contract, backend="codex")
        model = options.get("model")
        if model and not _allowed_by_env(str(model), RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(f"Codex model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}")
        result = op_codex.hermes_codex_start(
            prompt=contract["objective"],
            workdir=workspace,
            sandbox=sandbox,
            model=model,
            ignore_user_config=bool(options.get("ignore_user_config", False)),
            timeout=max(10, min(int(timeout), 3600)),
            confirm=confirm,
            dry_run=dry_run,
        )
        if isinstance(result, str):
            result = json.loads(result)
        job_id = result.get("job_id") if isinstance(result, dict) else None
        if isinstance(job_id, str) and result.get("success"):
            try:
                meta = op_codex._load(job_id, hermes_root)
                if isinstance(meta, dict):
                    meta["task_id"] = contract["task_id"]
                    op_codex._save(meta, hermes_root)
            except Exception as exc:
                logger.debug("failed to persist Codex task linkage for %s", job_id, exc_info=exc)
        result["backend"] = self.name
        result.setdefault("task_id", contract["task_id"])
        return result

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        # Codex jobs use their own opaque job ids, so contract linkage is only
        # available when the operator metadata recorded task_id (newer stores).
        import operator_codex as op_codex

        root = op_codex._root(hermes_root)
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in list(root.glob("*.json"))[:500]:
            meta = _load_json(path)
            if not meta or meta.get("task_id") != task_id:
                continue
            state = str(meta.get("state") or meta.get("status") or "unknown")
            out.append({"task_id": task_id, "status": state, "outcome": meta.get("outcome") or state, "error": meta.get("error") or None, "started_at": meta.get("started_at") or meta.get("created_at"), "ended_at": meta.get("ended_at") or meta.get("completed_at"), "scope": "runner:codex"})
        return out

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        return {"success": False, "code": "RUNNER_CANCEL_REQUIRES_JOB_ID", "backend": self.name}


def _extract_pi_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _worker_pi(
    exe: str,
    contract: dict[str, Any],
    timeout: int,
    log_path: Path,
    hermes_root: Path | None = None,
) -> tuple[int, str]:
    options = ((contract.get("execution") or {}).get("options") or {})
    tools = _pi_tools(contract)
    provider, model = _pi_selection(contract)
    if provider and not _allowed_by_env(provider, RUNNER_PROVIDER_ALLOWLIST_ENV):
        raise PermissionError(f"Pi provider {provider!r} is not allowed by {RUNNER_PROVIDER_ALLOWLIST_ENV}")
    if model and not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
        raise PermissionError(f"Pi model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}")
    argv = [exe, "--mode", "rpc", "--no-session", "--tools", tools]
    if _authorization_class(contract) in {"none", "read_only"}:
        # Pi extensions execute arbitrary startup code outside the built-in tool
        # allowlist. Disable extension discovery for read-only contracts so an
        # extension cannot mutate the workspace before the model even runs.
        argv.append("--no-extensions")
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    if options.get("thinking"):
        argv += ["--thinking", str(options["thinking"])]
    child_env = _pi_child_env(contract, hermes_root, provider)
    proc = _popen_process_group(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # Route stderr away from PIPE so noisy Pi startup/logging cannot fill an
        # unconsumed pipe and stall RPC progress before agent_settled.
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=child_env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({"id": "dispatch", "type": "prompt", "message": contract["objective"]}, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    final_text = ""
    settled = False
    rpc_error = ""
    deadline = datetime.now(timezone.utc).timestamp() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    while datetime.now(timezone.utc).timestamp() < deadline:
        remaining = max(0.0, deadline - datetime.now(timezone.utc).timestamp())
        ready = selector.select(timeout=min(0.5, remaining))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype in {"response", "agent_start", "agent_end", "agent_settled", "turn_end", "message_end", "extension_error", "auto_retry_start", "auto_retry_end"}:
            _append_event(log_path, {"type": etype, "at": _now(), "success": event.get("success"), "command": event.get("command")})
        if etype == "response" and event.get("command") == "prompt" and event.get("success") is False:
            rpc_error = _bounded_text(event.get("error") or "Pi RPC prompt rejected", 500)
            break
        if etype == "message_end":
            text = _extract_pi_text(event.get("message"))
            if text:
                final_text = text
        if etype == "agent_settled":
            settled = True
            break
    selector.close()
    if rpc_error:
        _terminate_process_group(proc)
        raise RuntimeError(f"Pi RPC prompt failed: {rpc_error}")
    if not settled:
        if proc.poll() is None:
            _terminate_process_group(proc)
            return 124, final_text
        rc = int(proc.returncode or 0)
        return (rc if rc else 1), final_text
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        rc = int(proc.returncode or 124)
    return rc, final_text


def _worker_omx(exe: str, contract: dict[str, Any], timeout: int, log_path: Path) -> tuple[int, str]:
    options = ((contract.get("execution") or {}).get("options") or {})
    sandbox = _sandbox_for(contract, backend="omx")
    workspace = contract["allowed_scope"]["workspaces"][0]
    argv = [exe, "exec", "--json", "-C", workspace, "--sandbox", sandbox]
    if options.get("model"):
        model = str(options["model"])
        if not _allowed_by_env(model, RUNNER_MODEL_ALLOWLIST_ENV):
            raise PermissionError(f"OMX model {model!r} is not allowed by {RUNNER_MODEL_ALLOWLIST_ENV}")
        argv += ["--model", model]
    if options.get("profile"):
        argv += ["--profile", str(options["profile"])]
    argv.append("-")
    proc = _popen_process_group(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(input=contract["objective"], timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        _append_event(log_path, {"type": "timeout", "at": _now()})
        return 124, ""
    final_text = ""
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        _append_event(log_path, {"type": etype or "event", "at": _now()})
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        text = item.get("text") or event.get("text")
        if isinstance(text, str) and text:
            final_text = text
    if proc.returncode and stderr:
        _append_event(log_path, {"type": "stderr", "at": _now(), "summary": _bounded_text(stderr, 500)})
    return int(proc.returncode or 0), final_text


def _worker(task_id: str, jobs_root: Path) -> int:
    meta_path = jobs_root / f"{task_id}.json"
    request_path = jobs_root / f"{task_id}.request.json"
    log_path = jobs_root / f"{task_id}.jsonl"
    meta = _load_json(meta_path) or {}
    request = _load_json(request_path)
    if not request or not isinstance(request.get("contract"), dict):
        meta.update({"state": "failed", "outcome": "failed", "ended_at": _now(), "error": "runner request missing"})
        _atomic_json(meta_path, meta)
        return 2
    contract = request["contract"]
    backend_name = str(request.get("backend") or "")
    timeout = max(10, min(int(request.get("timeout") or 900), 3600))
    request_root_raw = request.get("hermes_root")
    request_root = Path(str(request_root_raw)).expanduser() if request_root_raw else None
    normalized_root = op.normalize_hermes_data_root(request_root) if request_root is not None else None
    worker_hermes_root = Path(normalized_root) if normalized_root is not None else request_root
    # The objective is needed only to start the worker. Remove the durable
    # request envelope as soon as it has been loaded so prompt text is not
    # retained after dispatch.
    try:
        request_path.unlink()
    except OSError:
        pass
    def _terminalize(meta: dict[str, Any], *, state: str, rc: int | None = None, error: str = "") -> None:
        """Persist a terminal state, resolving to ``cancelled`` if a cancel
        marker exists (cancellation wins over later completed/failed). Clean
        the marker once the job is safely terminal."""
        cancelled = (jobs_root / f"{task_id}.cancel.json").exists()
        if cancelled:
            meta.update({"state": "cancelled", "outcome": "cancelled", "error": ""})
        else:
            meta.update({"state": state, "outcome": state})
            if error:
                meta["error"] = error
        if rc is not None:
            meta["returncode"] = rc
        meta["ended_at"] = _now()
        _atomic_json(meta_path, meta)
        # Close the check/write race with hermes_runner_cancel: cancellation
        # writes the marker before publishing cancelled metadata. If that marker
        # appeared after our first check but before/just after the terminal
        # write, cancellation still wins and no later worker write follows.
        cancel_path = jobs_root / f"{task_id}.cancel.json"
        if not cancelled and cancel_path.exists():
            cancelled = True
            meta.update({"state": "cancelled", "outcome": "cancelled", "error": "", "ended_at": _now()})
            _atomic_json(meta_path, meta)
        if cancelled:
            try:
                cancel_path.unlink()
            except OSError:
                pass

    try:
        backend = get_backend(backend_name)
        exe = backend.executable() if isinstance(backend, _LocalProcessBackend) else None
        if not exe:
            raise RuntimeError(f"{backend_name} executable not found")
        meta.update({"state": "running", "started_at": meta.get("started_at") or _now(), "pid": os.getpid()})
        _atomic_json(meta_path, meta)
        if (jobs_root / f"{task_id}.cancel.json").exists():
            _terminalize(meta, state="cancelled")
            return 0
        if backend_name == "pi_rpc":
            rc, _ = _worker_pi(exe, contract, timeout, log_path, worker_hermes_root)
        elif backend_name == "omx":
            rc, _ = _worker_omx(exe, contract, timeout, log_path)
        else:
            raise RuntimeError(f"local worker does not support backend {backend_name}")
        meta["returncode"] = rc
        if rc == 0:
            _terminalize(meta, state="completed", rc=rc)
        else:
            _terminalize(meta, state="failed", rc=rc, error="runner timed out" if rc == 124 else f"runner exited with code {rc}")
        # Completion evidence is state/exit metadata only. Do not persist the
        # model's final text in the runner store; contract validation must not
        # depend on worker self-report or retain prompt-derived output.
        return rc
    except Exception as exc:  # noqa: BLE001
        _terminalize(meta, state="failed", error=_bounded_text(exc, 500))
        return 1


def hermes_runner_list(hermes_root: Path | None = None) -> str:
    """List registered runner backends and bounded availability metadata."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "backends": list_backends(hermes_root=hermes_root),
        }
        _audit_runner(tool="hermes_runner_list", policy=policy, dry_run=True, success=True, summary="listed runner backends")
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except PermissionError as exc:
        return json.dumps({"success": False, "code": "RUNNER_POLICY_DENIED", "safe_message": _bounded_text(exc, 300)}, indent=2)


def hermes_runner_status(task_id: str, hermes_root: Path | None = None) -> str:
    """Return bounded observed state for a contract task across runner backends."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("read_only")
        _cleanup_stale_request_envelopes(hermes_root=hermes_root)
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            raise ValueError("task_id has an invalid format")
        runs = observed_runs(task_id, hermes_root=hermes_root)
        _audit_runner(tool="hermes_runner_status", policy=policy, dry_run=True, success=True, summary=f"observed {len(runs)} runner record(s)", task_id=task_id)
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "task_id": task_id, "runs": runs, "count": len(runs)}, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError) as exc:
        return json.dumps({"success": False, "code": "RUNNER_STATUS_ERROR", "safe_message": _bounded_text(exc, 300)}, indent=2)


def hermes_runner_cancel(task_id: str, backend: str = "", confirm: bool = False, dry_run: bool = True, hermes_root: Path | None = None) -> str:
    """Cancel a runner job when the selected backend supports cancellation."""
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            raise ValueError("task_id has an invalid format")
        selected = str(backend or "").strip().lower()
        if not selected:
            meta_path, _, _ = _job_paths(task_id, hermes_root)
            meta = _load_json(meta_path) or {}
            selected = str(meta.get("backend") or "")
        if not selected:
            return json.dumps({"success": False, "code": "RUNNER_BACKEND_REQUIRED", "safe_message": "backend could not be inferred for task"}, indent=2)
        target = get_backend(selected)
        if isinstance(target, _LocalProcessBackend):
            meta_path, _, _ = _job_paths(task_id, hermes_root)
            meta = _load_json(meta_path)
            if not meta or meta.get("backend") != selected:
                return json.dumps({"success": False, "code": "RUNNER_JOB_NOT_FOUND", "backend": selected, "task_id": task_id}, indent=2)
            workspace = meta.get("workspace")
            if not isinstance(workspace, str) or not workspace:
                raise PermissionError("runner job has no valid workspace scope")
            policy.require_workspace_path(workspace)
        if effective:
            _audit_runner(tool="hermes_runner_cancel", policy=policy, dry_run=True, success=True, changed=False, summary="cancel plan", task_id=task_id, backend=selected)
            return json.dumps({"success": True, "dry_run": True, "changed": False, "backend": selected, "task_id": task_id, "plan": "cancel"}, indent=2)
        if not confirm:
            _audit_runner(tool="hermes_runner_cancel", policy=policy, dry_run=True, success=False, changed=False, summary="confirmation required", task_id=task_id, backend=selected)
            return json.dumps({"success": False, "code": "CONFIRMATION_REQUIRED", "backend": selected, "safe_message": "runner cancellation requires confirm=true"}, indent=2)
        result = target.cancel(task_id, hermes_root=hermes_root)
        result.setdefault("backend", selected)
        result.setdefault("task_id", task_id)
        _audit_runner(tool="hermes_runner_cancel", policy=policy, dry_run=False, success=bool(result.get("success")), changed=bool(result.get("changed")), summary="cancelled runner job" if result.get("success") else "runner cancel failed", task_id=task_id, backend=selected)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, LookupError) as exc:
        return json.dumps({"success": False, "code": "RUNNER_CANCEL_ERROR", "safe_message": _bounded_text(exc, 300)}, indent=2)


def _register_builtins() -> None:
    for backend in (FleetBackend(), PiRpcBackend(), OmxBackend(), CodexBackend()):
        register_backend(backend, replace=True)


_register_builtins()
if op.env_truthy(RUNNER_PLUGINS_ENV):
    load_entrypoint_backends()


def _main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--worker":
        task_id = argv[2]
        if not _TASK_ID_RE.fullmatch(task_id):
            return 2
        jobs_root = None
        if len(argv) >= 5 and argv[3] == "--root":
            jobs_root = Path(argv[4]).expanduser().resolve()
        if jobs_root is None:
            jobs_root = _root()
        return _worker(task_id, jobs_root)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))


__all__ = [
    "CodexBackend",
    "FleetBackend",
    "OmxBackend",
    "PiRpcBackend",
    "RunnerBackend",
    "dispatch_contract",
    "get_backend",
    "hermes_runner_cancel",
    "hermes_runner_list",
    "hermes_runner_status",
    "list_backends",
    "load_entrypoint_backends",
    "normalize_execution",
    "observed_runs",
    "register_backend",
    "selected_backend",
]
