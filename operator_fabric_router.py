"""Capability-aware placement for Hermes GPT v0.8 Fabric G4-B.

The router is deliberately split into two phases:

1. hard eligibility filtering, where policy, authority, health, freshness and
   required capabilities fail closed;
2. deterministic soft ranking of eligible candidates only.

Soft hints can choose between safe candidates. They can never create authority,
turn an unknown capability into a positive fact, or revive an unhealthy peer.
Generic Fleet/A2A delegation is intentionally excluded from automatic verified
Fabric placement.

G4-B intentionally keeps automatic write placement fail-closed. Durable write
conflict ownership, retry/cancel reconciliation, and writer lifecycle containment
arrive in G4-C. Until those guards exist, ``auto`` may choose read-only work only;
explicit concrete backend selection keeps its existing policy-gated behavior.
"""

from __future__ import annotations

import copy
import json
import math
import os
import platform
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_fabric as fabric
import operator_policy as op
import operator_runners as runners

ROUTER_NAME = "fabric-router-v1"
ROUTING_POLICY_SCHEMA = "hermes.fabric-routing-policy/v1"
ROUTING_DECISION_SCHEMA = "hermes.fabric-routing-decision/v1"
ROUTING_POLICY_ENV = "HERMES_GPT_FABRIC_ROUTING_POLICY"

_AUTH_RANK = {"none": 0, "read_only": 1, "reversible_write": 2, "high_impact": 3}
_WRITE_AUTH_CLASSES = frozenset({"reversible_write", "high_impact"})
_TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
_ABS_WINDOWS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_MAX_TARGETS = 129
_MAX_LIST = 64
_MAX_AGE_SECONDS = 86_400
_ROUTER_LOG_LOCK = threading.Lock()


class RoutingError(RuntimeError):
    def __init__(self, code: str, message: str, *, decision: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.decision = decision


@dataclass(frozen=True)
class TargetFacts:
    observed_at: datetime
    max_age_seconds: int
    os_names: frozenset[str]
    runtimes: frozenset[str]
    runners: frozenset[str]
    providers: frozenset[str]
    models: frozenset[str]
    tools: frozenset[str]
    browser: bool | None
    vision: bool | None
    gpu_available: bool | None
    gpu_vendor: str
    gpu_memory_mb: int | None
    capacity: int | None
    active: int | None
    cost_bucket: int | None
    locality_bucket: int | None

    def fresh(self, now: datetime) -> bool:
        age = (now - self.observed_at).total_seconds()
        return -60 <= age <= self.max_age_seconds


@dataclass(frozen=True)
class RoutingPolicy:
    targets: dict[str, TargetFacts]


def _safe_text(value: Any, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} contains control characters")
    return value


def _tokens(value: Any, *, field: str, lower: bool = False) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > _MAX_LIST:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be a bounded list")
    out: set[str] = set()
    for raw in value:
        item = _safe_text(raw, field=field)
        if not _TOKEN_RE.fullmatch(item):
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} contains an invalid value")
        out.add(item.lower() if lower else item)
    return frozenset(out)


def _iso_datetime(value: Any, *, field: str) -> datetime:
    text = _safe_text(value, field=field, maximum=128)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _bucket(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be an integer from 0 through 9")
    return value


def _count(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be a bounded non-negative integer")
    return value


def _bool_or_none(value: Any, *, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"{field} must be boolean")
    return value


def _routing_policy_path(hermes_root: Path | None = None) -> Path:
    configured = os.environ.get(ROUTING_POLICY_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() or op.is_denied_path(path) or path.is_symlink():
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy path is not allowed")
        return path
    return fabric._root(hermes_root) / "config" / "fabric-routing.json"


def load_routing_policy(
    path: Path | None = None,
    *,
    hermes_root: Path | None = None,
) -> RoutingPolicy:
    target_path = path or _routing_policy_path(hermes_root)
    if not target_path.is_file():
        if os.environ.get(ROUTING_POLICY_ENV, "").strip():
            raise RoutingError("FABRIC_ROUTING_CONFIG_MISSING", "configured Fabric routing policy is missing")
        return RoutingPolicy(targets={})
    try:
        raw = fabric.strict_json_loads(target_path.read_bytes())
    except OSError as exc:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy could not be read") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "version", "targets"}:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy has unknown or missing fields")
    if raw["schema"] != ROUTING_POLICY_SCHEMA or raw["version"] != 1:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy schema/version is unsupported")
    targets_raw = raw["targets"]
    if not isinstance(targets_raw, dict) or len(targets_raw) > _MAX_TARGETS:
        raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing policy targets are invalid")

    targets: dict[str, TargetFacts] = {}
    allowed = {
        "observed_at",
        "max_age_seconds",
        "os",
        "runtimes",
        "runners",
        "providers",
        "models",
        "tools",
        "browser",
        "vision",
        "gpu",
        "capacity",
        "active",
        "cost_bucket",
        "locality_bucket",
    }
    for target_name, value in targets_raw.items():
        name = _safe_text(target_name, field="routing target", maximum=64)
        if not _TARGET_RE.fullmatch(name):
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "routing target name is invalid")
        if not isinstance(value, dict) or "observed_at" not in value or set(value) - allowed:
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", f"routing target {name!r} has invalid fields")
        max_age = value.get("max_age_seconds", 300)
        if isinstance(max_age, bool) or not isinstance(max_age, int) or not 1 <= max_age <= _MAX_AGE_SECONDS:
            raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "max_age_seconds is invalid")
        gpu_raw = value.get("gpu")
        gpu_available: bool | None = None
        gpu_vendor = ""
        gpu_memory_mb: int | None = None
        if gpu_raw is not None:
            if not isinstance(gpu_raw, dict) or set(gpu_raw) - {"available", "vendor", "memory_mb"}:
                raise RoutingError("FABRIC_ROUTING_CONFIG_INVALID", "gpu capability is invalid")
            gpu_available = _bool_or_none(gpu_raw.get("available"), field="gpu.available")
            if "vendor" in gpu_raw:
                gpu_vendor = _safe_text(gpu_raw["vendor"], field="gpu.vendor", maximum=64)
            gpu_memory_mb = _count(gpu_raw.get("memory_mb"), field="gpu.memory_mb")
        targets[name] = TargetFacts(
            observed_at=_iso_datetime(value["observed_at"], field=f"targets.{name}.observed_at"),
            max_age_seconds=max_age,
            os_names=_tokens(value.get("os"), field=f"targets.{name}.os", lower=True),
            runtimes=_tokens(value.get("runtimes"), field=f"targets.{name}.runtimes", lower=True),
            runners=_tokens(value.get("runners"), field=f"targets.{name}.runners", lower=True),
            providers=_tokens(value.get("providers"), field=f"targets.{name}.providers", lower=True),
            models=_tokens(value.get("models"), field=f"targets.{name}.models"),
            tools=_tokens(value.get("tools"), field=f"targets.{name}.tools", lower=True),
            browser=_bool_or_none(value.get("browser"), field="browser"),
            vision=_bool_or_none(value.get("vision"), field="vision"),
            gpu_available=gpu_available,
            gpu_vendor=gpu_vendor,
            gpu_memory_mb=gpu_memory_mb,
            capacity=_count(value.get("capacity"), field="capacity"),
            active=_count(value.get("active"), field="active"),
            cost_bucket=_bucket(value.get("cost_bucket"), field="cost_bucket"),
            locality_bucket=_bucket(value.get("locality_bucket"), field="locality_bucket"),
        )
    return RoutingPolicy(targets=targets)


def _reject_pathish(value: Any, *, field: str = "runner_options") -> None:
    if isinstance(value, str):
        if value.startswith("/") or _ABS_WINDOWS_RE.match(value):
            raise RoutingError(
                "FABRIC_ROUTING_CALLER_PATH_REJECTED",
                f"{field} may not contain caller-supplied absolute paths",
            )
        return
    if isinstance(value, list):
        for item in value:
            _reject_pathish(item, field=field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_pathish(item, field=f"{field}.{key}")


def _req_list(value: Any, *, field: str, lower: bool = False) -> list[str]:
    return sorted(_tokens(value, field=field, lower=lower))


def _auto_options(contract: dict[str, Any]) -> dict[str, Any]:
    execution = contract.get("execution")
    if not isinstance(execution, dict) or execution.get("backend") != "auto":
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto router requires execution.backend=auto")
    if contract.get("assigned_agent") != "auto":
        raise RoutingError(
            "FABRIC_ROUTING_INVALID",
            "execution.backend=auto requires assigned_agent=auto so placement is not silently overridden",
        )
    options = execution.get("options") or {}
    if not isinstance(options, dict):
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto execution options must be an object")
    allowed = {"requirements", "preferences", "logical_workspace", "runner_options", "evidence_provenance"}
    if set(options) - allowed:
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto execution options contain unknown fields")

    requirements = options.get("requirements") or {}
    if not isinstance(requirements, dict):
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto requirements must be an object")
    req_allowed = {
        "location",
        "os",
        "runtimes",
        "runners",
        "providers",
        "models",
        "tools",
        "browser",
        "vision",
        "gpu",
        "min_gpu_memory_mb",
    }
    if set(requirements) - req_allowed:
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto requirements contain unknown fields")
    location = str(requirements.get("location") or "any").strip().lower()
    if location not in {"any", "local", "remote"}:
        raise RoutingError("FABRIC_ROUTING_INVALID", "requirements.location must be any, local, or remote")
    for field in ("browser", "vision", "gpu"):
        if field in requirements and not isinstance(requirements[field], bool):
            raise RoutingError("FABRIC_ROUTING_INVALID", f"requirements.{field} must be boolean")
    min_gpu = requirements.get("min_gpu_memory_mb", 0)
    if isinstance(min_gpu, bool) or not isinstance(min_gpu, int) or not 0 <= min_gpu <= 1_000_000:
        raise RoutingError("FABRIC_ROUTING_INVALID", "requirements.min_gpu_memory_mb is invalid")

    preferences = options.get("preferences") or {}
    if not isinstance(preferences, dict) or set(preferences) - {"affinity_nodes", "prefer_local", "prefer_gpu"}:
        raise RoutingError("FABRIC_ROUTING_INVALID", "auto preferences are invalid")
    for field in ("prefer_local", "prefer_gpu"):
        if field in preferences and not isinstance(preferences[field], bool):
            raise RoutingError("FABRIC_ROUTING_INVALID", f"preferences.{field} must be boolean")
    affinity = _req_list(preferences.get("affinity_nodes"), field="preferences.affinity_nodes", lower=True)
    if any(not _TARGET_RE.fullmatch(item) for item in affinity):
        raise RoutingError("FABRIC_ROUTING_INVALID", "preferences.affinity_nodes contains an invalid target")

    logical_workspace = str(options.get("logical_workspace") or "").strip()
    if logical_workspace and (len(logical_workspace.encode("utf-8")) > 128 or any(ord(ch) < 32 for ch in logical_workspace)):
        raise RoutingError("FABRIC_ROUTING_INVALID", "logical_workspace is invalid")
    runner_options = fabric._bounded_json(options.get("runner_options") or {}, field="execution.options.runner_options")
    if not isinstance(runner_options, dict):
        raise RoutingError("FABRIC_ROUTING_INVALID", "runner_options must be an object")
    _reject_pathish(runner_options)

    evidence: dict[str, list[str]] | None = None
    if "evidence_provenance" in options:
        normalized = fabric._evidence_policy({"evidence_provenance": options["evidence_provenance"]})
        evidence = {key: list(value) for key, value in normalized.items()}

    return {
        "requirements": {
            "location": location,
            "os": _req_list(requirements.get("os"), field="requirements.os", lower=True),
            "runtimes": _req_list(requirements.get("runtimes"), field="requirements.runtimes", lower=True),
            "runners": _req_list(requirements.get("runners"), field="requirements.runners", lower=True),
            "providers": _req_list(requirements.get("providers"), field="requirements.providers", lower=True),
            "models": _req_list(requirements.get("models"), field="requirements.models"),
            "tools": _req_list(requirements.get("tools"), field="requirements.tools", lower=True),
            "browser": bool(requirements.get("browser", False)),
            "vision": bool(requirements.get("vision", False)),
            "gpu": bool(requirements.get("gpu", False)),
            "min_gpu_memory_mb": min_gpu,
        },
        "preferences": {
            "affinity_nodes": affinity,
            "prefer_local": bool(preferences.get("prefer_local", True)),
            "prefer_gpu": bool(preferences.get("prefer_gpu", False)),
        },
        "logical_workspace": logical_workspace,
        "runner_options": runner_options,
        "evidence_provenance": evidence,
    }


def _capability_missing(exclusions: list[dict[str, str]], code: str, detail: str) -> None:
    exclusions.append({"code": code, "detail": detail[:240]})


def _apply_write_guard(auth_class: str, exclusions: list[dict[str, str]]) -> None:
    if auth_class in _WRITE_AUTH_CLASSES:
        _capability_missing(
            exclusions,
            "WRITE_CONFLICT_GUARD_UNAVAILABLE",
            "automatic write placement is disabled until G4-C installs durable write ownership and lifecycle containment",
        )


def _has_any(required: list[str], actual: frozenset[str]) -> bool:
    return not required or bool(set(required).intersection(actual))


def _load_bucket(facts: TargetFacts | None, *, fresh: bool) -> int:
    if not facts or not fresh or facts.capacity in {None, 0} or facts.active is None:
        return 9
    ratio = facts.active / max(facts.capacity, 1)
    if ratio <= 0.25:
        return 0
    if ratio <= 0.50:
        return 1
    if ratio <= 0.75:
        return 2
    if ratio < 1.0:
        return 3
    return 4


def _latency_bucket(latency_ms: float | None) -> int:
    if latency_ms is None or not math.isfinite(latency_ms) or latency_ms < 0:
        return 9
    if latency_ms <= 50:
        return 0
    if latency_ms <= 150:
        return 1
    if latency_ms <= 500:
        return 2
    if latency_ms <= 1500:
        return 3
    return 4


def _target_facts_required(requirements: dict[str, Any], *, remote: bool) -> bool:
    rich = any(
        requirements[key]
        for key in ("os", "runtimes", "providers", "models", "tools")
    ) or any(requirements[key] for key in ("browser", "vision", "gpu")) or requirements["min_gpu_memory_mb"] > 0
    return remote or rich


def _apply_fact_requirements(
    *,
    requirements: dict[str, Any],
    facts: TargetFacts | None,
    fresh: bool,
    exclusions: list[dict[str, str]],
    dynamic_os: frozenset[str] | None = None,
) -> None:
    fact_needed = _target_facts_required(requirements, remote=False)
    if fact_needed and facts is not None and not fresh:
        _capability_missing(exclusions, "CAPABILITY_STALE", "required capability facts are stale")
        return

    os_actual = dynamic_os if dynamic_os is not None else (facts.os_names if facts else frozenset())
    if requirements["os"] and not _has_any(requirements["os"], os_actual):
        _capability_missing(exclusions, "CAPABILITY_OS_MISMATCH", "required operating system is unavailable or unknown")
    if requirements["runtimes"] and (not facts or not fresh or not _has_any(requirements["runtimes"], facts.runtimes)):
        _capability_missing(exclusions, "CAPABILITY_RUNTIME_MISMATCH", "required runtime is unavailable or unknown")
    if requirements["providers"] and (not facts or not fresh or not _has_any(requirements["providers"], facts.providers)):
        _capability_missing(exclusions, "CAPABILITY_PROVIDER_MISMATCH", "required provider is unavailable or unknown")
    if requirements["models"] and (not facts or not fresh or not _has_any(requirements["models"], facts.models)):
        _capability_missing(exclusions, "CAPABILITY_MODEL_MISMATCH", "required model is unavailable or unknown")
    if requirements["tools"] and (not facts or not fresh or not set(requirements["tools"]) <= set(facts.tools)):
        _capability_missing(exclusions, "CAPABILITY_TOOL_MISMATCH", "required tool classes are unavailable or unknown")
    if requirements["browser"] and (not facts or not fresh or facts.browser is not True):
        _capability_missing(exclusions, "CAPABILITY_BROWSER_MISMATCH", "browser capability is unavailable or unknown")
    if requirements["vision"] and (not facts or not fresh or facts.vision is not True):
        _capability_missing(exclusions, "CAPABILITY_VISION_MISMATCH", "vision capability is unavailable or unknown")
    if requirements["gpu"] and (not facts or not fresh or facts.gpu_available is not True):
        _capability_missing(exclusions, "CAPABILITY_GPU_MISMATCH", "GPU capability is unavailable or unknown")
    if requirements["min_gpu_memory_mb"] > 0 and (
        not facts
        or not fresh
        or facts.gpu_available is not True
        or facts.gpu_memory_mb is None
        or facts.gpu_memory_mb < requirements["min_gpu_memory_mb"]
    ):
        _capability_missing(exclusions, "CAPABILITY_GPU_MEMORY_MISMATCH", "required GPU memory is unavailable or unknown")


def _default_local_posture(*, dry_run: bool) -> dict[str, Any]:
    policy = op.OperatorPolicy()
    workspace = bool(policy.enabled and op.has_level("workspace", policy.level))
    if not dry_run:
        workspace = bool(workspace and policy.apply_mode == "direct")
    # ``high_impact`` is the Work Contract authorization ceiling, not Owner
    # Mode. The existing contract parser separately requires explicit approval
    # metadata for that class. Owner-only control-plane operations remain Owner-gated.
    return {"ready": workspace, "max_authorization": "high_impact" if workspace else "none"}


def _default_local_backends(*, hermes_root: Path | None = None) -> list[str]:
    out: list[str] = []
    for info in runners.list_backends(hermes_root=hermes_root):
        name = str(info.get("name") or "")
        if name in {"auto", "fabric", "fleet"}:
            continue
        if info.get("available") is True:
            out.append(name)
    return sorted(set(out))


def _journal_path(hermes_root: Path | None = None) -> Path:
    return fabric._root(hermes_root) / "fabric" / "routing-decisions.jsonl"


def _append_decision(decision: dict[str, Any], *, placed_sha256: str, hermes_root: Path | None = None) -> None:
    path = _journal_path(hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {
        "schema": ROUTING_DECISION_SCHEMA,
        "router": ROUTER_NAME,
        "task_id": decision["task_id"],
        "original_contract_sha256": decision["original_contract_sha256"],
        "placed_contract_sha256": placed_sha256,
        "mode": "auto",
        "requirements": decision.get("requirements") or {},
        "preferences": decision.get("preferences") or {},
        "selected": decision["selected"],
        "candidates": decision.get("candidates") or [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _ROUTER_LOG_LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _audit_route(decision: dict[str, Any], *, success: bool, dry_run: bool) -> None:
    selected = decision.get("selected") or {}
    try:
        policy = op.OperatorPolicy()
        op.audit_record(
            tool="hermes_fabric_route",
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=False,
            summary="Fabric auto placement selected" if success else "Fabric auto placement found no eligible candidate",
            extra={
                "task_id": decision.get("task_id", ""),
                "router": ROUTER_NAME,
                "selected_node": selected.get("node", ""),
                "selected_backend": selected.get("backend", ""),
                "eligible_count": sum(1 for item in decision.get("candidates", []) if item.get("eligible")),
                "candidate_count": len(decision.get("candidates", [])),
            },
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return


class AutoRouter:
    def __init__(
        self,
        *,
        registry_loader: Callable[[], dict[str, fabric.FabricNode]] | None = None,
        routing_policy_loader: Callable[[], RoutingPolicy] | None = None,
        remote_probe: Callable[[fabric.FabricNode, int], dict[str, Any]] | None = None,
        local_backends: Callable[[], list[str]] | None = None,
        local_posture: Callable[[bool], dict[str, Any]] | None = None,
        hermes_root: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.hermes_root = hermes_root
        self.registry_loader = registry_loader or (lambda: fabric.load_node_registry(hermes_root=hermes_root))
        self.routing_policy_loader = routing_policy_loader or (lambda: load_routing_policy(hermes_root=hermes_root))
        self.local_backends = local_backends or (lambda: _default_local_backends(hermes_root=hermes_root))
        self.local_posture = local_posture or (lambda dry_run: _default_local_posture(dry_run=dry_run))
        self.now = now or (lambda: datetime.now(timezone.utc))
        if remote_probe is not None:
            self.remote_probe = remote_probe
        else:
            coordinator = fabric.FabricCoordinator(hermes_root=hermes_root)

            def live_probe(node: fabric.FabricNode, timeout: int) -> dict[str, Any]:
                started = time.perf_counter()
                snapshot = coordinator._capabilities(node, timeout)
                latency_ms = (time.perf_counter() - started) * 1000.0
                return {
                    "healthy": True,
                    "latency_ms": latency_ms,
                    "snapshot_sha256": snapshot.get("snapshot_sha256", ""),
                }

            self.remote_probe = live_probe

    def _candidate_rank(
        self,
        *,
        node_name: str,
        backend: str,
        remote: bool,
        facts: TargetFacts | None,
        fresh: bool,
        preferences: dict[str, Any],
        latency_ms: float | None,
    ) -> tuple[Any, ...]:
        affinity = set(preferences["affinity_nodes"])
        if affinity:
            locality = 0 if node_name in affinity else 1
        elif preferences["prefer_local"]:
            locality = 1 if remote else 0
        elif facts and fresh and facts.locality_bucket is not None:
            locality = facts.locality_bucket
        else:
            locality = 0
        hardware = 0
        if preferences["prefer_gpu"]:
            hardware = 0 if facts and fresh and facts.gpu_available is True else 1
        load = _load_bucket(facts, fresh=fresh)
        latency = _latency_bucket(latency_ms)
        cost = facts.cost_bucket if facts and fresh and facts.cost_bucket is not None else 9
        return (locality, hardware, load, latency, cost, node_name, backend)

    def route(
        self,
        contract: dict[str, Any],
        *,
        timeout: int = 15,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        options = _auto_options(contract)
        requirements = options["requirements"]
        preferences = options["preferences"]
        routing_policy = self.routing_policy_loader()
        now = self.now().astimezone(timezone.utc)
        auth_class = str((contract.get("authorization") or {}).get("class") or "none")
        if auth_class not in _AUTH_RANK:
            raise RoutingError("FABRIC_ROUTING_AUTHORITY_INVALID", "contract authorization class is invalid")
        profile = str(contract.get("assigned_profile") or "")

        candidates: list[dict[str, Any]] = []
        local_facts = routing_policy.targets.get("local")
        local_fresh = bool(local_facts and local_facts.fresh(now))
        dynamic_os = frozenset({platform.system().lower()})
        posture = self.local_posture(dry_run)
        for backend in self.local_backends():
            exclusions: list[dict[str, str]] = []
            _apply_write_guard(auth_class, exclusions)
            if requirements["location"] == "remote":
                _capability_missing(exclusions, "LOCATION_MISMATCH", "contract requires remote execution")
            if requirements["runners"] and backend not in requirements["runners"]:
                _capability_missing(exclusions, "CAPABILITY_RUNNER_MISMATCH", "runner does not match the hard requirement")
            if not runners._runner_allowed(backend):
                _capability_missing(exclusions, "RUNNER_BACKEND_NOT_ALLOWED", "runner backend is excluded by coordinator policy")
            if not posture.get("ready"):
                _capability_missing(exclusions, "LOCAL_OPERATOR_NOT_READY", "local runner operator posture is not execution-ready")
            if _AUTH_RANK[auth_class] > _AUTH_RANK.get(str(posture.get("max_authorization") or "none"), 0):
                _capability_missing(exclusions, "AUTHORITY_INSUFFICIENT", "local execution posture cannot satisfy contract authorization")
            _apply_fact_requirements(
                requirements=requirements,
                facts=local_facts,
                fresh=local_fresh,
                exclusions=exclusions,
                dynamic_os=dynamic_os,
            )
            rank = self._candidate_rank(
                node_name="local",
                backend=backend,
                remote=False,
                facts=local_facts,
                fresh=local_fresh,
                preferences=preferences,
                latency_ms=0.0,
            )
            candidates.append(
                {
                    "node": "local",
                    "backend": backend,
                    "transport_backend": backend,
                    "remote": False,
                    "healthy": True,
                    "capability_fresh": local_fresh,
                    "authority_ceiling": str(posture.get("max_authorization") or "none"),
                    "eligible": not exclusions,
                    "exclusions": exclusions,
                    "rank": list(rank),
                }
            )

        try:
            nodes = self.registry_loader()
        except fabric.FabricError as exc:
            nodes = {}
            registry_error = {"code": exc.code, "detail": "Fabric node registry is unavailable"}
        except (OSError, RuntimeError, ValueError, TypeError):
            nodes = {}
            registry_error = {"code": "FABRIC_NODE_REGISTRY_INVALID", "detail": "Fabric node registry is unavailable"}
        else:
            registry_error = None

        for node_name in sorted(nodes):
            node = nodes[node_name]
            if not node.enabled:
                continue
            facts = routing_policy.targets.get(node.name)
            fresh = bool(facts and facts.fresh(now))
            latency_ms: float | None = None
            healthy = False
            probe_error: dict[str, str] | None = None
            try:
                probe = self.remote_probe(node, timeout)
                healthy = probe.get("healthy") is True
                raw_latency = probe.get("latency_ms")
                if isinstance(raw_latency, (int, float)) and not isinstance(raw_latency, bool):
                    latency_ms = float(raw_latency)
            except fabric.FabricError as exc:
                probe_error = {"code": exc.code, "detail": "live Fabric capability/identity probe failed"}
            except (OSError, RuntimeError, ValueError, TypeError):
                probe_error = {"code": "FABRIC_PEER_UNHEALTHY", "detail": "live Fabric capability/identity probe failed"}

            for backend in sorted(node.allowed_remote_backends):
                exclusions: list[dict[str, str]] = []
                _apply_write_guard(auth_class, exclusions)
                if contract.get("expected_artifacts"):
                    _capability_missing(
                        exclusions,
                        "REMOTE_ARTIFACT_ADMISSION_UNAVAILABLE",
                        "remote auto placement with required artifacts is disabled until G4-C installs bounded artifact admission",
                    )
                if registry_error:
                    exclusions.append(registry_error)
                if requirements["location"] == "local":
                    _capability_missing(exclusions, "LOCATION_MISMATCH", "contract requires local execution")
                if requirements["runners"] and backend not in requirements["runners"]:
                    _capability_missing(exclusions, "CAPABILITY_RUNNER_MISMATCH", "runner does not match the hard requirement")
                if not runners._runner_allowed("fabric"):
                    _capability_missing(exclusions, "RUNNER_BACKEND_NOT_ALLOWED", "Fabric transport backend is excluded by coordinator policy")
                if not healthy:
                    exclusions.append(probe_error or {"code": "FABRIC_PEER_UNHEALTHY", "detail": "managed peer is unavailable"})
                if profile not in node.allowed_profiles:
                    _capability_missing(exclusions, "PROFILE_NOT_ALLOWED", "managed node does not allow the contract profile")
                if _AUTH_RANK[auth_class] > _AUTH_RANK.get(node.max_authorization, 0):
                    _capability_missing(exclusions, "AUTHORITY_INSUFFICIENT", "managed node authority ceiling is below the contract")
                logical_workspace = options["logical_workspace"]
                if not logical_workspace:
                    _capability_missing(exclusions, "REMOTE_WORKSPACE_REQUIRED", "remote auto placement requires a logical_workspace")
                elif logical_workspace not in node.logical_workspaces:
                    _capability_missing(exclusions, "WORKSPACE_NOT_COMPATIBLE", "managed node does not map the requested logical workspace")
                if facts is None:
                    _capability_missing(exclusions, "CAPABILITY_MANIFEST_MISSING", "remote auto placement requires coordinator-owned capability facts")
                elif not fresh:
                    _capability_missing(exclusions, "CAPABILITY_STALE", "remote capability facts are stale")
                elif backend not in facts.runners:
                    _capability_missing(exclusions, "CAPABILITY_RUNNER_MISMATCH", "fresh capability facts do not affirm this runner")
                _apply_fact_requirements(
                    requirements=requirements,
                    facts=facts,
                    fresh=fresh,
                    exclusions=exclusions,
                )
                rank = self._candidate_rank(
                    node_name=node.name,
                    backend=backend,
                    remote=True,
                    facts=facts,
                    fresh=fresh,
                    preferences=preferences,
                    latency_ms=latency_ms,
                )
                candidates.append(
                    {
                        "node": node.name,
                        "backend": backend,
                        "transport_backend": "fabric",
                        "remote": True,
                        "healthy": healthy,
                        "capability_fresh": fresh,
                        "authority_ceiling": node.max_authorization,
                        "eligible": not exclusions,
                        "exclusions": exclusions,
                        "rank": list(rank),
                    }
                )

        eligible = sorted((item for item in candidates if item["eligible"]), key=lambda item: tuple(item["rank"]))
        selected = None
        if eligible:
            winner = eligible[0]
            selected = {
                "node": winner["node"],
                "backend": winner["backend"],
                "transport_backend": winner["transport_backend"],
                "remote": winner["remote"],
                "rank": winner["rank"],
            }
        decision = {
            "schema": ROUTING_DECISION_SCHEMA,
            "router": ROUTER_NAME,
            "mode": "auto",
            "task_id": str(contract.get("task_id") or ""),
            "original_contract_sha256": fabric.sha256_json(contract),
            "requirements": requirements,
            "preferences": preferences,
            "selected": selected,
            "candidates": candidates,
        }
        self._audit_decision(decision, dry_run=dry_run)
        return decision

    def _audit_decision(self, decision: dict[str, Any], *, dry_run: bool) -> None:
        """Audit the authoritative routing decision.

        G4-C overrides this hook so it can defer auditing until its live feature
        gates have transformed the preliminary G4-B candidate set. Ordinary
        G4-B callers retain the existing one-record behavior.
        """
        _audit_route(decision, success=decision.get("selected") is not None, dry_run=dry_run)

    def placed_contract(self, contract: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        selected = decision.get("selected")
        if not isinstance(selected, dict):
            raise RoutingError("FABRIC_NO_ELIGIBLE_CANDIDATE", "no eligible placement exists", decision=decision)
        options = _auto_options(contract)
        placed = copy.deepcopy(contract)
        if selected["remote"]:
            placed["assigned_agent"] = selected["node"]
            fabric_options: dict[str, Any] = {
                "node": selected["node"],
                "remote_backend": selected["backend"],
                "logical_workspace": options["logical_workspace"],
                "remote_options": options["runner_options"],
            }
            if options["evidence_provenance"] is not None:
                fabric_options["evidence_provenance"] = options["evidence_provenance"]
            placed["execution"] = {"backend": "fabric", "options": fabric_options}
        else:
            placed["execution"] = {
                "backend": selected["backend"],
                "options": options["runner_options"],
            }
        return placed


@dataclass
class AutoBackend:
    name: str = "auto"
    router_factory: Callable[..., AutoRouter] = AutoRouter
    dispatch_fn: Callable[..., dict[str, Any]] = runners.dispatch_contract

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        return {"available": True, "router": ROUTER_NAME, "reason": None}

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        confirm: bool,
        dry_run: bool,
        timeout: int,
        hermes_root: Path | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        router = kwargs.get("fabric_router") or self.router_factory(hermes_root=hermes_root)
        try:
            decision = router.route(contract, timeout=timeout, dry_run=dry_run)
            if decision["selected"] is None:
                return {
                    "success": False,
                    "ok": False,
                    "changed": False,
                    "backend": self.name,
                    "code": "FABRIC_NO_ELIGIBLE_CANDIDATE",
                    "safe_message": "No candidate satisfied all hard Fabric routing requirements.",
                    "placement": decision,
                }
            placed = router.placed_contract(contract, decision)
        except RoutingError as exc:
            return {
                "success": False,
                "ok": False,
                "changed": False,
                "backend": self.name,
                "code": exc.code,
                "safe_message": op.redact_output(str(exc))[:300],
                "placement": exc.decision,
            }

        placed_sha = fabric.sha256_json(placed)
        if not dry_run:
            _append_decision(decision, placed_sha256=placed_sha, hermes_root=hermes_root)
        downstream = self.dispatch_fn(
            placed,
            confirm=confirm,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=hermes_root,
        )
        result = dict(downstream)
        result["requested_backend"] = "auto"
        result["selected_backend"] = decision["selected"]["backend"]
        result["selected_node"] = decision["selected"]["node"]
        result["placed_contract_sha256"] = placed_sha
        result["placement"] = decision
        result["backend"] = self.name
        return result

    def observed_runs(self, task_id: str, *, hermes_root: Path | None = None) -> list[dict[str, Any]]:
        # The selected concrete backend is already observed by the global runner
        # registry. Returning it again here would duplicate evidence.
        return []

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        return {
            "success": False,
            "changed": False,
            "backend": self.name,
            "code": "RUNNER_CANCEL_UNSUPPORTED",
            "safe_message": "Auto-placement cancellation is resolved by the selected attempt in G4-C.",
        }


def register_runner_backend() -> None:
    try:
        if isinstance(runners.get_backend("auto"), AutoBackend):
            return
    except LookupError:
        pass
    runners.register_backend(AutoBackend(), replace=True)


__all__ = [
    "ROUTER_NAME",
    "ROUTING_DECISION_SCHEMA",
    "ROUTING_POLICY_ENV",
    "ROUTING_POLICY_SCHEMA",
    "AutoBackend",
    "AutoRouter",
    "RoutingError",
    "RoutingPolicy",
    "TargetFacts",
    "load_routing_policy",
    "register_runner_backend",
]
