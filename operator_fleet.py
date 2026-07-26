"""Bounded fleet routing tools backed by Hermes A2A's local registry.

This module deliberately delegates only to named peers already present in the
local authenticated A2A registry. It never accepts a peer URL or bearer token
from an MCP caller, never shells out, and never returns task prompts or remote
task histories.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import operator_policy as op

Runner = Callable[..., tuple[int, str, str]]

_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _hermes_bin(hermes_root: Path | None = None) -> str:
    """Locate the local Hermes executable without accepting caller input."""
    root = hermes_root or op.normalize_hermes_data_root(os.environ.get("HERMES_HOME"))
    if root:
        candidates = [
            root / "hermes-agent" / "venv" / "bin" / "hermes",
            root / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    raise RuntimeError("Hermes CLI was not found. Install Hermes Agent before using fleet tools.")


def _run(argv: list[str], *, timeout: int, runner: Runner | None) -> tuple[int, str, str]:
    if runner is not None:
        return runner(argv, timeout=timeout)
    return op.run_argv(argv, timeout=timeout)


def _error(code: str, message: str, action: str) -> str:
    return json.dumps(
        op.make_error_envelope(
            layer="operator",
            code=code,
            safe_message=message,
            suggested_action=action,
        ),
        indent=2,
    )


def _parse_json(stdout: str, *, operation: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{operation} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{operation} returned an invalid JSON shape")
    return parsed


def _registry(*, runner: Runner | None, hermes_bin: str | None) -> tuple[list[dict[str, Any]], str]:
    binary = hermes_bin or _hermes_bin()
    code, stdout, stderr = _run([binary, "a2a", "registry", "list", "--json"], timeout=15, runner=runner)
    if code != 0:
        raise RuntimeError(op.redact_output(stderr or "A2A registry lookup failed"))
    parsed = _parse_json(stdout, operation="A2A registry lookup")
    agents = parsed.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError("A2A registry returned an invalid agents list")
    clean: list[dict[str, Any]] = []
    for item in agents:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        url = item.get("url")
        if isinstance(name, str) and _AGENT_RE.fullmatch(name) and isinstance(url, str):
            clean.append({"name": name, "url": url, "has_token": bool(item.get("hasToken"))})
    return clean, binary


def _registered_agent(agent: str, *, runner: Runner | None, hermes_bin: str | None) -> tuple[str, str]:
    if not isinstance(agent, str) or not _AGENT_RE.fullmatch(agent):
        raise ValueError("agent must be a registered peer name")
    agents, binary = _registry(runner=runner, hermes_bin=hermes_bin)
    if agent not in {item["name"] for item in agents}:
        raise LookupError(f"unknown registered peer: {agent}")
    return agent, binary


def hermes_fleet_list(*, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    """List locally registered A2A fleet peers. Never exposes bearer tokens."""
    try:
        agents, _ = _registry(runner=runner, hermes_bin=hermes_bin)
        return json.dumps({"success": True, "count": len(agents), "agents": agents}, indent=2)
    except Exception as exc:
        return _error("FLEET_REGISTRY_ERROR", op.redact_output(str(exc)), "Verify the local Hermes A2A registry.")


def hermes_fleet_status(agent: str, timeout: int = 10, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    """Fetch metadata-only compatibility status for one registered fleet peer."""
    try:
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        capped_timeout = max(1, min(int(timeout), 30))
        code, stdout, stderr = _run(
            [binary, "a2a", "doctor", peer, "--timeout", str(capped_timeout), "--json"],
            timeout=capped_timeout + 5,
            runner=runner,
        )
        if code != 0:
            return _error("FLEET_STATUS_ERROR", op.redact_output(stderr or "A2A peer health check failed"), "Check the peer's A2A service and registry entry.")
        payload = _parse_json(stdout, operation="A2A peer health check")
        return json.dumps(
            {
                "success": bool(payload.get("ok")),
                "agent": peer,
                "status": payload.get("status", "unknown"),
                "name": payload.get("name"),
                "capabilities": payload.get("capabilities", {}),
                "warnings": payload.get("warnings", []),
                "errors": payload.get("errors", []),
            },
            indent=2,
        )
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except Exception as exc:
        return _error("FLEET_STATUS_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_dispatch(
    agent: str,
    message: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    *,
    runner: Runner | None = None,
    hermes_bin: str | None = None,
) -> str:
    """Submit a bounded task to one registered peer after explicit confirmation.

    A task can execute work remotely, so direct dispatch requires workspace-level
    Operator Mode, direct apply mode, ``dry_run=false``, and ``confirm=true``.
    The full message is hashed in audit records and never returned to the MCP
    caller.
    """
    policy = op.OperatorPolicy()
    try:
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        if not isinstance(message, str) or not message.strip():
            return _error("INVALID_MESSAGE", "message must be a non-empty string", "Provide a bounded task message.")
        if len(message.encode("utf-8")) > 16_000:
            return _error("MESSAGE_TOO_LARGE", "message exceeds the 16 KB fleet dispatch limit", "Send a shorter bounded task.")
        policy.require_level("workspace")
        effective_dry_run = policy.effective_dry_run(dry_run)
        policy.require_mutation(dry_run)
        if effective_dry_run:
            audit = op.audit_record(
                tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode,
                dry_run=True, success=True, changed=False, summary=f"fleet dispatch plan for {peer}",
                prompt=message, extra={"agent": peer},
            )
            return json.dumps({"success": True, "dry_run": True, "plan": {"agent": peer, "message_len": len(message)}, "audit": audit}, indent=2)
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "remote dispatch requires confirm=true", "Review the task and call again with confirm=true.")
        capped_timeout = max(5, min(int(timeout), 120))
        code, stdout, stderr = _run([binary, "a2a", "send", peer, message, "--json"], timeout=capped_timeout, runner=runner)
        if code != 0:
            audit = op.audit_record(
                tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode,
                dry_run=False, success=False, changed=False, summary=f"fleet dispatch failed for {peer}",
                error=op.redact_output(stderr), prompt=message, extra={"agent": peer},
            )
            return json.dumps({"success": False, "agent": peer, "code": "FLEET_DISPATCH_ERROR", "audit": audit}, indent=2)
        payload = _parse_json(stdout, operation="A2A task submission")
        task = payload.get("task", payload)
        if not isinstance(task, dict):
            return _error("FLEET_DISPATCH_ERROR", "A2A task submission returned an invalid task shape", "Check the peer's A2A service logs.")
        task_id = task.get("id")
        state = task.get("status", {}).get("state") if isinstance(task.get("status"), dict) else None
        if not isinstance(task_id, str) or not task_id:
            return _error("FLEET_DISPATCH_ERROR", "A2A task submission returned no task id", "Check the peer's A2A service logs.")
        audit = op.audit_record(
            tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=False, success=True, changed=True, summary=f"fleet task submitted to {peer}",
            prompt=message, job_id=task_id, extra={"agent": peer, "state": state},
        )
        return json.dumps({"success": True, "changed": True, "agent": peer, "task_id": task_id, "state": state}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable workspace-level Operator Mode and use direct mode only for intentional dispatch.")
    except Exception as exc:
        return _error("FLEET_DISPATCH_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_task(agent: str, task_id: str, timeout: int = 15, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    """Return a task's safe summary from one registered peer."""
    try:
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            return _error("INVALID_TASK_ID", "task_id has an invalid format", "Use the task id returned by hermes_fleet_dispatch.")
        capped_timeout = max(1, min(int(timeout), 60))
        code, stdout, stderr = _run([binary, "a2a", "task", task_id, "--agent", peer, "--json"], timeout=capped_timeout, runner=runner)
        if code != 0:
            return _error("FLEET_TASK_ERROR", op.redact_output(stderr or "A2A task lookup failed"), "Check the peer and task id.")
        payload = _parse_json(stdout, operation="A2A task lookup")
        task = payload.get("task", payload)
        if not isinstance(task, dict):
            return _error("FLEET_TASK_ERROR", "A2A task lookup returned an invalid task shape", "Check the peer and task id.")
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return json.dumps(
            {
                "success": True,
                "agent": peer,
                "task_id": task.get("id", task_id),
                "state": status.get("state"),
                "timestamp": status.get("timestamp"),
                "artifact_count": len(task.get("artifacts", [])) if isinstance(task.get("artifacts"), list) else 0,
            },
            indent=2,
        )
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except Exception as exc:
        return _error("FLEET_TASK_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")
