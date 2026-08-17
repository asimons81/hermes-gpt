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
    for ep in selected:
        try:
            candidate = ep.load()
            backend = candidate() if callable(candidate) and not hasattr(candidate, "name") else candidate
            register_backend(backend, replace=True)
            loaded.append(str(getattr(backend, "name", ep.name)))
        except Exception as exc:
            logger.debug("runner entry point %s failed to load", getattr(ep, "name", "unknown"), exc_info=exc)
            continue
    return loaded


def list_backends(*, hermes_root: Path | None = None) -> list[dict[str, Any]]:
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
        return str(execution["backend"])
    return "fleet"


def dispatch_contract(
    contract: dict[str, Any],
    *,
    confirm: bool,
    dry_run: bool,
    timeout: int,
    hermes_root: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    backend_name = selected_backend(contract)
    try:
        backend = get_backend(backend_name)
    except LookupError as exc:
        return {
            "success": False,
            "ok": False,
            "code": "RUNNER_BACKEND_UNKNOWN",
            "safe_message": str(exc),
            "suggested_action": "Select a registered execution.backend.",
            "backend": backend_name,
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
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", task_id, "--root", str(_root(hermes_root))],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        meta["pid"] = proc.pid
        meta["state"] = "running"
        meta["started_at"] = _now()
        _atomic_json(meta_path, meta)
        return {"success": True, "changed": True, "dry_run": False, "backend": self.name, "task_id": task_id, "state": "running", "pid": proc.pid}

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
        options = ((contract.get("execution") or {}).get("options") or {})
        auth_class = str(contract.get("authorization", {}).get("class") or "none")
        tools = options.get("tools")
        if not tools:
            tools = "read" if auth_class in {"none", "read_only"} else "read,bash,edit,write"
        return {"protocol": "jsonl-rpc", "mode": "rpc", "tools": tools, "model": options.get("model"), "provider": options.get("provider")}


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
        auth_class = str(contract.get("authorization", {}).get("class") or "none")
        sandbox = options.get("sandbox") or ("read-only" if auth_class in {"none", "read_only"} else "workspace-write")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("omx execution.options.sandbox must be read-only or workspace-write")
        return {"mode": "exec", "json": True, "sandbox": sandbox, "model": options.get("model"), "profile": options.get("profile")}


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
        auth_class = str(contract.get("authorization", {}).get("class") or "none")
        sandbox = options.get("sandbox") or ("read-only" if auth_class in {"none", "read_only"} else "workspace-write")
        result = op_codex.hermes_codex_start(
            prompt=contract["objective"],
            workdir=workspace,
            sandbox=sandbox,
            model=options.get("model"),
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
        root = (hermes_root or Path.home() / ".hermes") / "codex-jobs"
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


def _worker_pi(exe: str, contract: dict[str, Any], timeout: int, log_path: Path) -> tuple[int, str]:
    options = ((contract.get("execution") or {}).get("options") or {})
    auth_class = str(contract.get("authorization", {}).get("class") or "none")
    tools = str(options.get("tools") or ("read" if auth_class in {"none", "read_only"} else "read,bash,edit,write"))
    argv = [exe, "--mode", "rpc", "--no-session", "--tools", tools]
    if options.get("provider"):
        argv += ["--provider", str(options["provider"])]
    if options.get("model"):
        argv += ["--model", str(options["model"])]
    if options.get("thinking"):
        argv += ["--thinking", str(options["thinking"])]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps({"id": "dispatch", "type": "prompt", "message": contract["objective"]}, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    final_text = ""
    settled = False
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
        if etype == "message_end":
            text = _extract_pi_text(event.get("message"))
            if text:
                final_text = text
        if etype == "agent_settled":
            settled = True
            break
    selector.close()
    if not settled and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 124, final_text
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        rc = proc.wait(timeout=5)
    return rc, final_text


def _worker_omx(exe: str, contract: dict[str, Any], timeout: int, log_path: Path) -> tuple[int, str]:
    options = ((contract.get("execution") or {}).get("options") or {})
    auth_class = str(contract.get("authorization", {}).get("class") or "none")
    sandbox = str(options.get("sandbox") or ("read-only" if auth_class in {"none", "read_only"} else "workspace-write"))
    workspace = contract["allowed_scope"]["workspaces"][0]
    argv = [exe, "exec", "--json", "-C", workspace, "--sandbox", sandbox]
    if options.get("model"):
        argv += ["--model", str(options["model"])]
    if options.get("profile"):
        argv += ["--profile", str(options["profile"])]
    argv.append("-")
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(input=contract["objective"], timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
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
    try:
        backend = get_backend(backend_name)
        exe = backend.executable() if isinstance(backend, _LocalProcessBackend) else None
        if not exe:
            raise RuntimeError(f"{backend_name} executable not found")
        meta.update({"state": "running", "started_at": meta.get("started_at") or _now(), "pid": os.getpid()})
        _atomic_json(meta_path, meta)
        if backend_name == "pi_rpc":
            rc, final_text = _worker_pi(exe, contract, timeout, log_path)
        elif backend_name == "omx":
            rc, final_text = _worker_omx(exe, contract, timeout, log_path)
        else:
            raise RuntimeError(f"local worker does not support backend {backend_name}")
        meta["returncode"] = rc
        meta["ended_at"] = _now()
        if rc == 0:
            meta["state"] = "completed"
            meta["outcome"] = "completed"
            meta["error"] = ""
        else:
            meta["state"] = "failed"
            meta["outcome"] = "failed"
            meta["error"] = "runner timed out" if rc == 124 else f"runner exited with code {rc}"
        if final_text:
            meta["result_summary"] = _bounded_text(final_text)
        _atomic_json(meta_path, meta)
        return rc
    except Exception as exc:  # noqa: BLE001
        meta.update({"state": "failed", "outcome": "failed", "ended_at": _now(), "error": _bounded_text(exc, 500)})
        _atomic_json(meta_path, meta)
        return 1


def hermes_runner_list(hermes_root: Path | None = None) -> str:
    """List registered runner backends and bounded availability metadata."""
    try:
        op.OperatorPolicy().require_level("read_only")
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "backends": list_backends(hermes_root=hermes_root),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except PermissionError as exc:
        return json.dumps({"success": False, "code": "RUNNER_POLICY_DENIED", "safe_message": _bounded_text(exc, 300)}, indent=2)


def hermes_runner_status(task_id: str, hermes_root: Path | None = None) -> str:
    """Return bounded observed state for a contract task across runner backends."""
    try:
        op.OperatorPolicy().require_level("read_only")
        if not _TASK_ID_RE.fullmatch(task_id or ""):
            raise ValueError("task_id has an invalid format")
        runs = observed_runs(task_id, hermes_root=hermes_root)
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
        if effective:
            return json.dumps({"success": True, "dry_run": True, "changed": False, "backend": selected, "task_id": task_id, "plan": "cancel"}, indent=2)
        if not confirm:
            return json.dumps({"success": False, "code": "CONFIRMATION_REQUIRED", "backend": selected, "safe_message": "runner cancellation requires confirm=true"}, indent=2)
        result = target.cancel(task_id, hermes_root=hermes_root)
        result.setdefault("backend", selected)
        result.setdefault("task_id", task_id)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (PermissionError, ValueError, LookupError) as exc:
        return json.dumps({"success": False, "code": "RUNNER_CANCEL_ERROR", "safe_message": _bounded_text(exc, 300)}, indent=2)


def _register_builtins() -> None:
    for backend in (FleetBackend(), PiRpcBackend(), OmxBackend(), CodexBackend()):
        register_backend(backend, replace=True)


_register_builtins()
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
