"""Structured, bounded fleet control backed by Hermes A2A's local registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import operator_policy as op

Runner = Callable[..., tuple[int, str, str]]

AUTHORITY_MANIFEST_ENV = "HERMES_GPT_FLEET_AUTHORITY_MANIFEST"
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_AUTH_CLASSES = frozenset({"none", "read_only", "reversible_write", "high_impact"})
_MAX_REMOTE_BYTES = 1_048_576
_MAX_MANIFEST_BYTES = 64_000
_MAX_TEXT = 4_000
_MAX_ITEMS = 64
_BUILTIN_PROFILES = {
    "nous-girl": frozenset({"default"}),
    "rza": frozenset({"default", "gza", "masta-killa", "inspectah-deck", "ghostface-killah", "method-man", "raekwon"}),
}
_PUBLIC_ACTION_RE = re.compile(
    r"\b(?:"
    r"publish\b"
    r"|post\b(?=.{0,80}\b(?:online|publicly|on\s+(?:x|twitter|facebook|instagram|linkedin))\b)"
    r"|deploy\b(?=.{0,80}\b(?:publicly|public|website|site)\b)"
    r"|send\b(?=.{0,80}\b(?:an?\s+|this\s+|the\s+)?emails?\b)"
    r"|release\b(?=.{0,80}\bpublicly\b)"
    r"|make\b(?=.{0,40}\bpublic\b)"
    r")",
    re.I,
)
_NEGATED_ACTION_RE = re.compile(r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|without)\s+(?:\w+\s+){0,3}$", re.I)
_COMMAND_PREFIX_RE = re.compile(
    r"(?:^|[.!?;:]\s*|(?:\b(?:and|then)\b)\s+)"
    r"(?:please\s+|(?:can|could|would|will)\s+you\s+|you\s+(?:must|should|need\s+to)\s+)?$",
    re.I,
)
_SECRET_RE = re.compile(r"\b(raw|reveal|return|print|show|read|extract|dump|expose)\b.{0,40}\b(secret|token|password|credential|api[_ -]?key|private key|environment values?)s?\b", re.I | re.S)
_VAULT_RE = re.compile(r"\b(vault|credential store)\b.{0,40}\b(policy|policies|edit|write|change|update)\b|\b(edit|write|change|update)\b.{0,40}\b(vault|credential store)\b", re.I | re.S)


@dataclass(frozen=True)
class AuthorityPeer:
    name: str
    expected_host_role: str
    expected_card_identity: str
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allow_public_actions: bool = False


class PeerVerificationError(RuntimeError):
    """A live Agent Card could not be safely matched to local authority."""


def _hermes_bin(hermes_root: Path | None = None) -> str:
    root = hermes_root or op.normalize_hermes_data_root(os.environ.get("HERMES_HOME"))
    if root:
        for candidate in (root / "hermes-agent" / "venv" / "bin" / "hermes", root / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"):
            if candidate.is_file():
                return str(candidate)
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    raise RuntimeError("Hermes CLI was not found. Install Hermes Agent before using fleet tools.")


def _run(argv: list[str], *, timeout: int, runner: Runner | None) -> tuple[int, str, str]:
    return runner(argv, timeout=timeout) if runner is not None else op.run_argv(argv, timeout=timeout)


def _error(code: str, message: str, action: str) -> str:
    return json.dumps(op.make_error_envelope(layer="operator", code=code, safe_message=message, suggested_action=action), indent=2)


def _clean_text(value: Any, *, field: str, maximum: int = _MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = _CONTROL_RE.sub("", value).strip()
    if required and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its {maximum} byte limit")
    return value


def _string_list(value: Any, *, field: str, maximum: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a list with at most {maximum} items")
    return [_clean_text(item, field=f"{field} item", maximum=1_000) for item in value]


def _parse_json(stdout: str, *, operation: str) -> dict[str, Any]:
    if not isinstance(stdout, str):
        raise ValueError(f"{operation} returned invalid UTF-8 text")
    if len(stdout.encode("utf-8")) > _MAX_REMOTE_BYTES:
        raise ValueError(f"{operation} response exceeded the bounded response limit")
    cleaned = _CONTROL_RE.sub("", stdout).lstrip("\ufeff")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\{\[]", cleaned):
        try:
            parsed, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if cleaned.rstrip().endswith(("{", "[", ",", ":")) or cleaned.count("{") > cleaned.count("}"):
        raise ValueError(f"{operation} returned truncated JSON")
    raise ValueError(f"{operation} returned invalid JSON")


def _registry(*, runner: Runner | None, hermes_bin: str | None) -> tuple[list[dict[str, Any]], str]:
    binary = hermes_bin or _hermes_bin()
    code, stdout, stderr = _run([binary, "a2a", "registry", "list", "--json"], timeout=15, runner=runner)
    if code != 0:
        raise RuntimeError(op.redact_output(stderr or "A2A registry lookup failed"))
    agents = _parse_json(stdout, operation="A2A registry lookup").get("agents", [])
    if not isinstance(agents, list) or len(agents) > 256:
        raise ValueError("A2A registry returned an invalid agents list")
    clean = []
    for item in agents:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and _AGENT_RE.fullmatch(item["name"]) and isinstance(item.get("url"), str):
            clean.append({"name": item["name"], "has_token": bool(item.get("hasToken"))})
    return clean, binary


def _registered_agent(agent: str, *, runner: Runner | None, hermes_bin: str | None) -> tuple[str, str]:
    if not isinstance(agent, str) or not _AGENT_RE.fullmatch(agent):
        raise ValueError("agent must be a registered peer name")
    agents, binary = _registry(runner=runner, hermes_bin=hermes_bin)
    if agent not in {item["name"] for item in agents}:
        raise LookupError("unknown registered peer")
    return agent, binary


def _manifest_path(path: Path | None = None) -> Path:
    if path is None:
        configured = os.environ.get(AUTHORITY_MANIFEST_ENV)
        if configured:
            path = Path(configured)
        else:
            root = op.normalize_hermes_data_root(os.environ.get("HERMES_HOME")) or (Path.home() / ".hermes")
            path = root / "config" / "fleet-authority.json"
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("authority manifest path must be absolute")
    if op.is_denied_path(path):
        raise PermissionError("authority manifest path is denied by the secret-path policy")
    if path.is_symlink():
        raise PermissionError("authority manifest must not be a symbolic link")
    return path


def _load_authority(path: Path | None = None) -> dict[str, AuthorityPeer]:
    manifest_path = _manifest_path(path)
    data = manifest_path.read_bytes()
    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("authority manifest exceeds 64 KB")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authority manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or set(raw) - {"version", "peers"} or raw.get("version") != 1 or not isinstance(raw.get("peers"), list):
        raise ValueError("authority manifest schema is invalid")
    peers: dict[str, AuthorityPeer] = {}
    for item in raw["peers"]:
        allowed_keys = {"name", "expected_host_role", "expected_card_identity", "allowed_profiles", "max_authorization", "allow_public_actions"}
        if not isinstance(item, dict) or set(item) - allowed_keys:
            raise ValueError("authority peer schema is invalid")
        name = item.get("name")
        role = item.get("expected_host_role")
        identity = item.get("expected_card_identity")
        profiles = item.get("allowed_profiles")
        auth = item.get("max_authorization", "read_only")
        if not isinstance(name, str) or not _AGENT_RE.fullmatch(name) or name in peers:
            raise ValueError("authority peer name is invalid or duplicated")
        if name not in _BUILTIN_PROFILES:
            raise ValueError("authority manifest contains an unsupported peer")
        if not isinstance(role, str) or not _ROLE_RE.fullmatch(role):
            raise ValueError("expected_host_role is invalid")
        if not isinstance(identity, str) or not _AGENT_RE.fullmatch(identity):
            raise ValueError("expected_card_identity is invalid")
        if not isinstance(profiles, list) or not profiles or len(profiles) > 16 or any(not isinstance(p, str) or not _PROFILE_RE.fullmatch(p) for p in profiles):
            raise ValueError("allowed_profiles is invalid")
        if not set(profiles) <= _BUILTIN_PROFILES[name]:
            raise PermissionError("authority manifest exceeds the built-in profile ceiling")
        if auth not in _AUTH_CLASSES:
            raise ValueError("max_authorization is invalid")
        public = item.get("allow_public_actions", False)
        if not isinstance(public, bool) or (name == "nous-girl" and public):
            raise PermissionError("Nous Girl may not receive public actions")
        peers[name] = AuthorityPeer(name, role, identity, tuple(sorted(set(profiles))), auth, public)
    return peers


def _unwrap_task(payload: dict[str, Any]) -> dict[str, Any]:
    current: Any = payload
    for _ in range(8):
        if not isinstance(current, dict):
            break
        if isinstance(current.get("task"), dict):
            current = current["task"]
        elif isinstance(current.get("result"), dict):
            current = current["result"]
        elif isinstance(current.get("data"), dict):
            current = current["data"]
        else:
            break
    if not isinstance(current, dict):
        raise ValueError("A2A task lookup returned an invalid task shape")
    return current


def _authorization(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"class": value}
    if not isinstance(value, dict) or set(value) - {"class", "approved", "approved_by", "approval_reference"}:
        raise ValueError("authorization metadata has invalid fields")
    auth_class = value.get("class")
    if auth_class not in _AUTH_CLASSES:
        raise ValueError("authorization class is invalid")
    approved = value.get("approved", False)
    if not isinstance(approved, bool):
        raise ValueError("authorization.approved must be boolean")
    result: dict[str, Any] = {"class": auth_class, "approved": approved}
    for key in ("approved_by", "approval_reference"):
        if key in value:
            result[key] = _clean_text(value[key], field=f"authorization.{key}", maximum=128)
    if auth_class == "high_impact" and (not approved or "approved_by" not in result or "approval_reference" not in result):
        raise PermissionError("high-impact work requires explicit approval metadata")
    return result


def _requests_public_action(objective: str) -> bool:
    """Detect affirmative public-action commands without scanning supporting fields."""
    for match in _PUBLIC_ACTION_RE.finditer(objective):
        prefix = objective[max(0, match.start() - 48):match.start()]
        if _COMMAND_PREFIX_RE.search(prefix) and not _NEGATED_ACTION_RE.search(prefix):
            return True
    return False


def _canonical_work_order(*, agent: Any, task_id: Any, target_profile: Any, objective: Any, workspace: Any,
                          inputs: Any, constraints: Any, acceptance_checks: Any, deliverables: Any, authorization: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(agent, str) or not _AGENT_RE.fullmatch(agent):
        raise ValueError("agent is invalid")
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    if not isinstance(target_profile, str) or not _PROFILE_RE.fullmatch(target_profile):
        raise ValueError("target_profile is invalid")
    objective_text = _clean_text(objective, field="objective", maximum=8_000)
    workspace_text = _clean_text(workspace, field="workspace", maximum=1_000)
    if op.is_denied_path(workspace_text):
        raise PermissionError("workspace is denied by the secret-path policy")
    auth = _authorization(authorization)
    envelope = {
        "schema": "hermes.fleet.work-order/v1",
        "agent": agent,
        "task_id": task_id,
        "target_profile": target_profile,
        "objective": objective_text,
        "workspace": workspace_text,
        "inputs": _string_list(inputs, field="inputs"),
        "constraints": _string_list(constraints, field="constraints"),
        "acceptance_checks": _string_list(acceptance_checks, field="acceptance_checks"),
        "deliverables": _string_list(deliverables, field="deliverables"),
        "authorization": auth,
    }
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > 32_000:
        raise ValueError("canonical work order exceeds 32 KB")
    return canonical, envelope


def _authorize_order(peer: AuthorityPeer, envelope: dict[str, Any], canonical: str) -> None:
    if envelope["target_profile"] not in peer.allowed_profiles:
        raise PermissionError("target profile is not authorized for this peer")
    ranks = {"none": 0, "read_only": 1, "reversible_write": 2, "high_impact": 3}
    if ranks[envelope["authorization"]["class"]] > ranks[peer.max_authorization]:
        raise PermissionError("request exceeds peer role authority")
    if _SECRET_RE.search(canonical):
        raise PermissionError("raw-secret requests are forbidden")
    if _VAULT_RE.search(canonical):
        raise PermissionError("Vault-policy edits by peers are forbidden")
    if _requests_public_action(envelope["objective"]) and (peer.name == "nous-girl" or not peer.allow_public_actions):
        raise PermissionError("public actions are not authorized for this peer")


def _verify_live_peer(peer: AuthorityPeer, binary: str, timeout: int, runner: Runner | None) -> None:
    capped = max(1, min(int(timeout), 30))
    code, stdout, _ = _run(
        [binary, "a2a", "doctor", peer.name, "--timeout", str(capped), "--json"],
        timeout=capped + 5,
        runner=runner,
    )
    if code != 0:
        raise PeerVerificationError("peer verification failed")
    try:
        card = _parse_json(stdout, operation="A2A peer verification")
    except (TypeError, ValueError) as exc:
        raise PeerVerificationError("peer verification failed") from exc
    identity = card.get("name") or card.get("identity")
    host_role = card.get("host_role") or card.get("role")
    if card.get("ok") is not True:
        raise PeerVerificationError("peer verification failed")
    if not isinstance(identity, str) or not _AGENT_RE.fullmatch(identity):
        raise PeerVerificationError("peer verification failed")
    if not isinstance(host_role, str) or not _ROLE_RE.fullmatch(host_role):
        raise PeerVerificationError("peer verification failed")
    if identity != peer.expected_card_identity or host_role != peer.expected_host_role:
        raise PeerVerificationError("peer verification failed")


def hermes_fleet_list(*, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        agents, _ = _registry(runner=runner, hermes_bin=hermes_bin)
        return json.dumps({"success": True, "count": len(agents), "agents": agents}, indent=2)
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before inspecting fleet peers.")
    except Exception as exc:
        return _error("FLEET_REGISTRY_ERROR", op.redact_output(str(exc)), "Verify the local Hermes A2A registry.")


def hermes_fleet_status(agent: str, timeout: int = 10, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        capped = max(1, min(int(timeout), 30))
        code, stdout, stderr = _run([binary, "a2a", "doctor", peer, "--timeout", str(capped), "--json"], timeout=capped + 5, runner=runner)
        if code != 0:
            return _error("FLEET_STATUS_ERROR", op.redact_output(stderr or "A2A peer health check failed"), "Check the peer's A2A service and registry entry.")
        payload = _parse_json(stdout, operation="A2A peer health check")
        return json.dumps({"success": bool(payload.get("ok")), "agent": peer, "status": payload.get("status", "unknown"),
                           "capability_count": len(payload.get("capabilities", {})) if isinstance(payload.get("capabilities"), dict) else 0,
                           "warnings_count": len(payload.get("warnings", [])) if isinstance(payload.get("warnings"), list) else 0,
                           "errors_count": len(payload.get("errors", [])) if isinstance(payload.get("errors"), list) else 0}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before checking fleet peers.")
    except Exception as exc:
        return _error("FLEET_STATUS_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_dispatch(agent: str, message: str, confirm: bool = False, dry_run: bool = True, timeout: int = 30,
                          *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    """Backward-compatible free-form dispatch with original gates."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        effective = policy.effective_dry_run(dry_run)
        policy.require_mutation(dry_run)
        message = _clean_text(message, field="message", maximum=16_000)
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        if effective:
            audit = op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=True,
                                    success=True, changed=False, summary=f"fleet dispatch plan for {peer}", prompt=message, extra={"agent": peer})
            return json.dumps({"success": True, "dry_run": True, "plan": {"agent": peer, "message_len": len(message)}, "audit": audit}, indent=2)
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "remote dispatch requires confirm=true", "Review the task and call again with confirm=true.")
        capped = max(5, min(int(timeout), 120))
        code, stdout, stderr = _run([binary, "a2a", "send", "--json", peer, "--", message], timeout=capped, runner=runner)
        if code != 0:
            audit = op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                                    success=False, changed=False, summary=f"fleet dispatch failed for {peer}", error=op.redact_output(stderr),
                                    prompt=message, extra={"agent": peer})
            return json.dumps({"success": False, "agent": peer, "code": "FLEET_DISPATCH_ERROR", "audit": audit}, indent=2)
        task = _unwrap_task(_parse_json(stdout, operation="A2A task submission"))
        task_id = task.get("id")
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            return _error("FLEET_DISPATCH_ERROR", "A2A task submission returned no valid task id", "Check the peer's A2A service logs.")
        op.audit_record(tool="hermes_fleet_dispatch", level=policy.level, apply_mode=policy.apply_mode, dry_run=False, success=True,
                        changed=True, summary=f"fleet task submitted to {peer}", prompt=message, job_id=task_id, extra={"agent": peer, "state": status.get("state")})
        return json.dumps({"success": True, "changed": True, "agent": peer, "task_id": task_id, "state": status.get("state")}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable workspace-level Operator Mode and use direct mode only for intentional dispatch.")
    except ValueError as exc:
        return _error("INVALID_MESSAGE", str(exc), "Provide a bounded task message.")
    except Exception as exc:
        return _error("FLEET_DISPATCH_ERROR", op.redact_output(str(exc)), "Check the local A2A registry and peer service.")


def hermes_fleet_dispatch_work_order(agent: str, task_id: str, target_profile: str, objective: str, workspace: str,
                                     inputs: list[str], constraints: list[str], acceptance_checks: list[str],
                                     deliverables: list[str], authorization: dict[str, Any] | str,
                                     confirm: bool = False, dry_run: bool = True, timeout: int = 30,
                                     *, runner: Runner | None = None, hermes_bin: str | None = None,
                                     authority_manifest: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        effective = policy.effective_dry_run(dry_run)
        policy.require_mutation(dry_run)
        canonical, envelope = _canonical_work_order(agent=agent, task_id=task_id, target_profile=target_profile, objective=objective,
                                                    workspace=workspace, inputs=inputs, constraints=constraints,
                                                    acceptance_checks=acceptance_checks, deliverables=deliverables, authorization=authorization)
        authorities = _load_authority(authority_manifest)
        peer_authority = authorities.get(agent)
        if peer_authority is None:
            raise PermissionError("peer is absent from the authority manifest")
        _authorize_order(peer_authority, envelope, canonical)
        peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if effective:
            audit = op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode,
                                    dry_run=True, success=True, changed=False, summary=f"fleet work-order plan for {peer}",
                                    prompt=canonical, job_id=task_id, extra={"agent": peer, "profile": target_profile})
            return json.dumps({"success": True, "dry_run": True, "plan": {"agent": peer, "task_id": task_id, "target_profile": target_profile,
                              "authorization": envelope["authorization"], "work_order_bytes": len(canonical.encode("utf-8")),
                              "live_peer_verification": "required_before_dispatch", "prompt_sha256": digest}, "audit": audit}, indent=2)
        if not confirm:
            return _error("CONFIRMATION_REQUIRED", "remote work-order dispatch requires confirm=true", "Review the work order and call again with confirm=true.")
        capped = max(5, min(int(timeout), 120))
        _verify_live_peer(peer_authority, binary, capped, runner)
        code, stdout, stderr = _run([binary, "a2a", "send", "--json", peer, "--", canonical], timeout=capped, runner=runner)
        if code != 0:
            op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                            success=False, changed=False, summary=f"fleet work-order failed for {peer}", error=op.redact_output(stderr),
                            prompt=canonical, job_id=task_id, extra={"agent": peer, "profile": target_profile})
            return _error("FLEET_DISPATCH_ERROR", "A2A work-order submission failed", "Check the peer's A2A service.")
        remote = _unwrap_task(_parse_json(stdout, operation="A2A work-order submission"))
        remote_id = remote.get("id")
        if not isinstance(remote_id, str) or not _TASK_ID_RE.fullmatch(remote_id):
            return _error("FLEET_DISPATCH_ERROR", "A2A submission returned no valid task id", "Check the peer's A2A service.")
        status = remote.get("status") if isinstance(remote.get("status"), dict) else {}
        op.audit_record(tool="hermes_fleet_dispatch_work_order", level=policy.level, apply_mode=policy.apply_mode, dry_run=False,
                        success=True, changed=True, summary=f"fleet work-order submitted to {peer}", prompt=canonical,
                        job_id=remote_id, extra={"agent": peer, "profile": target_profile})
        return json.dumps({"success": True, "changed": True, "agent": peer, "task_id": remote_id,
                           "requested_task_id": task_id, "state": status.get("state"), "live_peer_verified": True,
                           "prompt_sha256": digest}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except FileNotFoundError:
        return _error("AUTHORITY_MANIFEST_MISSING", "fleet authority manifest is not configured", "Install the authority manifest before dispatch.")
    except PermissionError as exc:
        code = "AUTHORIZATION_DENIED" if "Operator Mode" not in str(exc) and "operator" not in str(exc).lower() else "FLEET_POLICY_DENIED"
        return _error(code, str(exc), "Review fleet authority and explicit approval metadata.")
    except ValueError as exc:
        return _error("INVALID_WORK_ORDER", str(exc), "Correct the bounded work-order fields.")
    except PeerVerificationError:
        return _error("FLEET_PEER_VERIFICATION_FAILED", "registered peer verification failed",
                      "Verify the peer identity and host role before retrying.")
    except Exception as exc:
        return _error("FLEET_DISPATCH_ERROR", op.redact_output(str(exc)), "Check local authority, registry, and peer service.")


def _fetch_task(agent: str, task_id: str, timeout: int, runner: Runner | None, hermes_bin: str | None) -> tuple[str, dict[str, Any]]:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    peer, binary = _registered_agent(agent, runner=runner, hermes_bin=hermes_bin)
    capped = max(1, min(int(timeout), 60))
    code, stdout, stderr = _run([binary, "a2a", "task", "--agent", peer, "--json", "--", task_id], timeout=capped, runner=runner)
    if code != 0:
        raise RuntimeError(op.redact_output(stderr or "A2A task lookup failed"))
    return peer, _unwrap_task(_parse_json(stdout, operation="A2A task lookup"))


def hermes_fleet_task(agent: str, task_id: str, timeout: int = 15, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return _error("INVALID_TASK_ID", "task_id has an invalid format", "Use the task id returned by fleet dispatch.")
    try:
        op.OperatorPolicy().require_level("read_only")
        peer, task = _fetch_task(agent, task_id, timeout, runner, hermes_bin)
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return json.dumps({"success": True, "agent": peer, "task_id": task.get("id", task_id), "state": status.get("state"),
                           "timestamp": status.get("timestamp"), "artifact_count": len(task.get("artifacts", [])) if isinstance(task.get("artifacts"), list) else 0}, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode before inspecting fleet tasks.")
    except Exception as exc:
        return _error("FLEET_TASK_ERROR", op.redact_output(str(exc)), "Check the peer and task id.")


def _text_parts(value: Any) -> list[str]:
    found: list[str] = []
    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= _MAX_ITEMS:
            return
        if isinstance(node, dict):
            if isinstance(node.get("text"), str):
                found.append(_clean_text(node["text"], field="result text", maximum=_MAX_TEXT, required=False))
            for key in ("parts", "content", "data"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)
    walk(value)
    return [x for x in found if x]


def _completion_payload(task: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for source in (task.get("result"), task.get("artifacts"), task.get("status")):
        if isinstance(source, dict):
            candidates.append(source)
        elif isinstance(source, list):
            candidates.extend(item for item in source[:_MAX_ITEMS] if isinstance(item, dict))
    texts = _text_parts(candidates)
    for text in texts:
        try:
            candidates.insert(0, _parse_json(text, operation="completed task content"))
        except ValueError:
            pass
    allowed = {"status", "node", "profile", "summary", "changed_paths", "artifacts", "verification", "residual_risk", "recommended_next_action", "authorization"}
    raw: dict[str, Any] = {}
    for candidate in candidates:
        inner = candidate.get("completion_bundle", candidate)
        if isinstance(inner, dict) and any(key in inner for key in allowed):
            raw = inner
            break
    status_obj = task.get("status") if isinstance(task.get("status"), dict) else {}
    def bounded(value: Any, field: str) -> str:
        return op.redact_output(_clean_text(value if isinstance(value, str) else "", field=field, maximum=_MAX_TEXT, required=False))
    result = {
        "status": bounded(raw.get("status") or status_obj.get("state") or "unknown", "status"),
        "node": bounded(raw.get("node") or "", "node"),
        "profile": bounded(raw.get("profile") or "", "profile"),
        "summary": bounded(raw.get("summary") or (texts[0] if texts else ""), "summary"),
        "changed_paths": [],
        "artifacts": [],
        "verification": [],
        "residual_risk": bounded(raw.get("residual_risk") or "", "residual_risk"),
        "recommended_next_action": bounded(raw.get("recommended_next_action") or "", "recommended_next_action"),
        "authorization": {"class": "none", "approved": False},
    }
    for field in ("changed_paths", "artifacts", "verification"):
        value = raw.get(field, [])
        if isinstance(value, list):
            result[field] = [bounded(x, field) for x in value[:_MAX_ITEMS] if isinstance(x, str)]
    if isinstance(raw.get("authorization"), (dict, str)):
        try:
            result["authorization"] = _authorization(raw["authorization"])
        except (ValueError, PermissionError):
            pass
    return result


def hermes_fleet_result(agent: str, task_id: str, timeout: int = 15, *, runner: Runner | None = None, hermes_bin: str | None = None) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        return _error("INVALID_TASK_ID", "task_id has an invalid format", "Use the task id returned by fleet dispatch.")
    try:
        op.OperatorPolicy().require_level("read_only")
        _, task = _fetch_task(agent, task_id, timeout, runner, hermes_bin)
        return json.dumps({"success": True, "task_id": task_id, **_completion_payload(task)}, ensure_ascii=False, indent=2)
    except LookupError:
        return _error("UNKNOWN_AGENT", "agent is not a registered fleet peer", "Call hermes_fleet_list and use one returned name.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode.")
    except Exception as exc:
        return _error("FLEET_RESULT_ERROR", op.redact_output(str(exc)), "Check the peer, task id, and completion shape.")


def hermes_fleet_authority_drift(*, runner: Runner | None = None, hermes_bin: str | None = None,
                                 authority_manifest: Path | None = None) -> str:
    try:
        op.OperatorPolicy().require_level("read_only")
        authorities = _load_authority(authority_manifest)
        registered, binary = _registry(runner=runner, hermes_bin=hermes_bin)
        registered_names = {x["name"] for x in registered}
        findings: list[dict[str, str]] = []
        for name in sorted(registered_names | set(authorities)):
            if name not in registered_names:
                findings.append({"agent": name, "code": "MANIFEST_ONLY", "severity": "error"})
                continue
            if name not in authorities:
                findings.append({"agent": name, "code": "REGISTRY_ONLY", "severity": "error"})
                continue
            code, stdout, _ = _run([binary, "a2a", "doctor", name, "--timeout", "10", "--json"], timeout=15, runner=runner)
            if code != 0:
                findings.append({"agent": name, "code": "CARD_UNAVAILABLE", "severity": "warning"})
                continue
            card = _parse_json(stdout, operation="A2A peer health check")
            reported = card.get("name") or card.get("identity")
            role = card.get("host_role") or card.get("role")
            expected = authorities[name]
            if reported != expected.expected_card_identity:
                findings.append({"agent": name, "code": "IDENTITY_MISMATCH", "severity": "error"})
            if role != expected.expected_host_role:
                findings.append({"agent": name, "code": "HOST_ROLE_MISMATCH", "severity": "error"})
        return json.dumps({"success": True, "valid": not findings, "registered_count": len(registered_names),
                           "manifest_count": len(authorities), "finding_count": len(findings), "findings": findings}, indent=2)
    except FileNotFoundError:
        return _error("AUTHORITY_MANIFEST_MISSING", "fleet authority manifest is not configured", "Install the authority manifest.")
    except PermissionError as exc:
        return _error("FLEET_POLICY_DENIED", str(exc), "Enable read-only Operator Mode and check manifest safety.")
    except Exception as exc:
        return _error("FLEET_AUTHORITY_ERROR", op.redact_output(str(exc)), "Check the local manifest, registry, and Agent Cards.")
