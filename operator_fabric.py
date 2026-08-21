"""Hermes GPT v0.8 Fabric managed remote dispatch and evidence core.

Fabric uses A2A v1.0 as a bounded transport, but verified Fabric requests are
accepted by a deterministic Hermes GPT endpoint before any generic agent/LLM
path. The coordinator remains the authority owner and Work Contract validator.

G4-A intentionally does not implement automatic placement, artifact byte
transfer, or Flight Deck UI. Those belong to later v0.8 slices.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import operator_fleet as op_fleet
import operator_policy as op
import operator_runners as op_runners

FABRIC_VERSION = 1
REQUEST_SCHEMA = "hermes.fabric-request/v1"
RESPONSE_SCHEMA = "hermes.fabric-response/v1"
DISPATCH_SCHEMA = "hermes.fabric-dispatch/v1"
EVIDENCE_SCHEMA = "hermes.fabric-evidence/v1"
CAPABILITY_SCHEMA = "hermes.fabric-capability/v1"
NODE_REGISTRY_SCHEMA = "hermes.fabric-node-registry/v1"
PEER_POLICY_SCHEMA = "hermes.fabric-peer-policy/v1"

NODE_REGISTRY_ENV = "HERMES_GPT_FABRIC_NODE_REGISTRY"
PEER_POLICY_ENV = "HERMES_GPT_FABRIC_PEER_POLICY"
PEER_TOKENS_ENV = "HERMES_GPT_FABRIC_PEER_TOKENS"
COORDINATOR_DB_ENV = "HERMES_GPT_FABRIC_COORDINATOR_DB"
PEER_DB_ENV = "HERMES_GPT_FABRIC_PEER_DB"

_MAX_BODY = 128 * 1024
_MAX_STRING = 8_000
_MAX_ITEMS = 64
_MAX_DEPTH = 8
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_NODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_BACKEND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRETISH_KEY_RE = re.compile(
    r"(?:secret|token|password|passwd|api[_-]?key|credential|private[_-]?key)",
    re.IGNORECASE,
)
_URLISH_KEY_RE = re.compile(r"(?:url|uri|endpoint|host|hostname|proxy)", re.IGNORECASE)
_AUTH_RANK = {"none": 0, "read_only": 1, "reversible_write": 2, "high_impact": 3}
_TERMINAL_PEER = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "LOST_AMBIGUOUS", "BLOCKED"})
_TERMINAL_COORD = frozenset({"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"})
_PEER_WRITE_CLAIM_STATES = frozenset(
    {"NONE", "ACTIVE", "RELEASED", "SUPERSEDED", "UNKNOWN"}
)
_PEER_EXECUTION_UNIT_STATES = frozenset(
    {
        "active", "activating", "deactivating", "reloading", "inactive",
        "failed", "dead", "not-found", "terminal", "unknown",
    }
)
_SAFE_EVIDENCE_PROVENANCE = frozenset(
    {"coordinator_observed", "managed_peer_structured", "artifact_verified", "coordinator_local"}
)
_DEFAULT_FEATURES = (
    "dispatch-v1",
    "evidence-v1",
    "idempotency-v1",
    "principal-auth-v1",
    "closed-schema-v1",
)
_EXPECTED_ERRORS = (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error)


def _bounded_coordinator_peer_values(data: dict[str, Any]) -> dict[str, Any]:
    epoch = data.get("write_epoch")
    claim = data.get("write_claim_state")
    unit = data.get("execution_unit_state")
    return {
        "write_epoch": epoch
        if isinstance(epoch, int) and not isinstance(epoch, bool)
        else None,
        "write_claim_state": claim if claim in _PEER_WRITE_CLAIM_STATES else "UNKNOWN",
        "execution_unit_state": unit if unit in _PEER_EXECUTION_UNIT_STATES else "unknown",
    }


class FabricError(RuntimeError):
    def __init__(self, code: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


class _DuplicateKey(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(f"duplicate JSON field {key!r}")
        out[key] = value
    return out


def strict_json_loads(raw: str | bytes, *, maximum: int = _MAX_BODY) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > maximum:
            raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "JSON payload exceeds the bounded limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FabricError("FABRIC_INVALID_JSON", "payload is not valid UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > maximum:
            raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "JSON payload exceeds the bounded limit")
        text = raw
    else:
        raise FabricError("FABRIC_INVALID_JSON", "payload must be UTF-8 JSON")
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs)
    except _DuplicateKey as exc:
        raise FabricError("FABRIC_AMBIGUOUS_JSON", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise FabricError("FABRIC_INVALID_JSON", "payload is not valid JSON") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _closed(
    obj: Any,
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    name: str,
) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} must be an object")
    keys = set(obj)
    if required - keys:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} is missing required fields")
    if keys - required - optional:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{name} contains unknown fields")
    return obj


def _bounded_string(
    value: Any,
    *,
    field: str,
    maximum: int = _MAX_STRING,
    pattern: re.Pattern[str] | None = None,
    required: bool = True,
) -> str:
    if not isinstance(value, str) or (required and not value) or len(value.encode("utf-8")) > maximum:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} is invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains control characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} has an invalid format")
    return value


def _bounded_strings(
    value: Any,
    *,
    field: str,
    maximum: int = _MAX_ITEMS,
    item_max: int = 2_000,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} must be a bounded list")
    return [_bounded_string(item, field=f"{field} item", maximum=item_max) for item in value]


def _bounded_json(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} exceeds nesting depth")
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_STRING:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains an oversized string")
        if "://" in value:
            raise FabricError(
                "FABRIC_CALLER_NETWORK_TARGET",
                f"{field} may not contain caller-supplied URLs",
            )
        return value
    if isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains too many items")
        return [_bounded_json(item, field=field, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS:
            raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains too many keys")
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} has an invalid key")
            if _SECRETISH_KEY_RE.search(key):
                raise FabricError(
                    "FABRIC_CALLER_CREDENTIAL",
                    f"{field} may not contain credential fields",
                )
            if _URLISH_KEY_RE.search(key):
                raise FabricError(
                    "FABRIC_CALLER_NETWORK_TARGET",
                    f"{field} may not contain network target fields",
                )
            out[key] = _bounded_json(item, field=f"{field}.{key}", depth=depth + 1)
        return out
    raise FabricError("FABRIC_SCHEMA_INVALID", f"{field} contains a non-JSON value")


def _root(hermes_root: Path | None = None) -> Path:
    configured = hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    normalized = op.normalize_hermes_data_root(configured)
    return Path(normalized or Path.home() / ".hermes")


def _config_path(env_name: str, default_name: str, hermes_root: Path | None = None) -> Path:
    configured = os.environ.get(env_name, "").strip()
    path = Path(configured).expanduser() if configured else _root(hermes_root) / "config" / default_name
    if not path.is_absolute():
        raise FabricError("FABRIC_CONFIG_INVALID", f"{env_name} must resolve to an absolute path")
    if op.is_denied_path(path) or path.is_symlink():
        raise FabricError("FABRIC_CONFIG_INVALID", "Fabric configuration path is not allowed")
    return path


def _read_closed_json(path: Path, *, maximum: int = _MAX_BODY) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise FabricError("FABRIC_CONFIG_MISSING", f"Fabric configuration is missing: {path.name}") from exc
    value = strict_json_loads(raw, maximum=maximum)
    if not isinstance(value, dict):
        raise FabricError("FABRIC_CONFIG_INVALID", "Fabric configuration must be an object")
    return value


@dataclass(frozen=True)
class FabricNode:
    name: str
    a2a_peer_name: str
    expected_identity: str
    coordinator_principal: str
    enabled: bool
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allowed_remote_backends: tuple[str, ...]
    logical_workspaces: tuple[str, ...]
    required_features: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceMapping:
    logical_id: str
    local_path: Path
    revision: str
    conflict_domain: str


@dataclass(frozen=True)
class FabricPeerPolicy:
    node_name: str
    identity: str
    allowed_coordinator_principals: tuple[str, ...]
    allowed_profiles: tuple[str, ...]
    max_authorization: str
    allowed_backends: tuple[str, ...]
    required_features: tuple[str, ...]
    workspace_mappings: dict[str, WorkspaceMapping]
    digest: str


def load_node_registry(
    path: Path | None = None,
    *,
    hermes_root: Path | None = None,
) -> dict[str, FabricNode]:
    path = path or _config_path(NODE_REGISTRY_ENV, "fabric-nodes.json", hermes_root)
    raw = _read_closed_json(path)
    _closed(raw, required={"schema", "version", "nodes"}, name="node registry")
    if raw["schema"] != NODE_REGISTRY_SCHEMA or raw["version"] != 1 or not isinstance(raw["nodes"], list):
        raise FabricError("FABRIC_CONFIG_INVALID", "node registry schema/version is invalid")
    if len(raw["nodes"]) > 128:
        raise FabricError("FABRIC_CONFIG_INVALID", "node registry contains too many nodes")

    result: dict[str, FabricNode] = {}
    for item in raw["nodes"]:
        item = _closed(
            item,
            required={
                "name",
                "a2a_peer_name",
                "expected_identity",
                "coordinator_principal",
                "enabled",
                "allowed_profiles",
                "max_authorization",
                "allowed_remote_backends",
                "logical_workspaces",
                "required_features",
            },
            name="node",
        )
        name = _bounded_string(item["name"], field="node.name", pattern=_NODE_RE)
        if name in result:
            raise FabricError("FABRIC_CONFIG_INVALID", "node names must be unique")
        peer_name = _bounded_string(
            item["a2a_peer_name"],
            field="node.a2a_peer_name",
            pattern=op_fleet._AGENT_RE,
        )
        identity = _bounded_string(item["expected_identity"], field="node.expected_identity", maximum=128)
        principal = _bounded_string(
            item["coordinator_principal"],
            field="node.coordinator_principal",
            pattern=_PRINCIPAL_RE,
        )
        if not isinstance(item["enabled"], bool):
            raise FabricError("FABRIC_CONFIG_INVALID", "node.enabled must be boolean")
        profiles = tuple(
            _bounded_strings(item["allowed_profiles"], field="node.allowed_profiles", maximum=32, item_max=64)
        )
        if any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
            raise FabricError("FABRIC_CONFIG_INVALID", "node allowed profile is invalid")
        max_auth = _bounded_string(item["max_authorization"], field="node.max_authorization", maximum=32)
        if max_auth not in _AUTH_RANK:
            raise FabricError("FABRIC_CONFIG_INVALID", "node max_authorization is invalid")
        backends = tuple(
            _bounded_strings(
                item["allowed_remote_backends"],
                field="node.allowed_remote_backends",
                maximum=32,
                item_max=64,
            )
        )
        if any(not _BACKEND_RE.fullmatch(backend) or backend in {"fabric", "fleet"} for backend in backends):
            raise FabricError("FABRIC_CONFIG_INVALID", "node remote backend allowlist is invalid")
        workspaces = tuple(
            _bounded_strings(
                item["logical_workspaces"],
                field="node.logical_workspaces",
                maximum=64,
                item_max=128,
            )
        )
        features = tuple(
            _bounded_strings(
                item["required_features"],
                field="node.required_features",
                maximum=32,
                item_max=128,
            )
        )
        result[name] = FabricNode(
            name,
            peer_name,
            identity,
            principal,
            item["enabled"],
            profiles,
            max_auth,
            backends,
            workspaces,
            features,
        )
    return result


def load_peer_policy(path: Path | None = None, *, hermes_root: Path | None = None) -> FabricPeerPolicy:
    path = path or _config_path(PEER_POLICY_ENV, "fabric-peer-policy.json", hermes_root)
    raw = _read_closed_json(path)
    _closed(
        raw,
        required={
            "schema",
            "version",
            "node_name",
            "identity",
            "allowed_coordinator_principals",
            "allowed_profiles",
            "max_authorization",
            "allowed_backends",
            "required_features",
            "workspace_mappings",
        },
        name="peer policy",
    )
    if raw["schema"] != PEER_POLICY_SCHEMA or raw["version"] != 1:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer policy schema/version is invalid")

    node_name = _bounded_string(raw["node_name"], field="peer.node_name", pattern=_NODE_RE)
    identity = _bounded_string(raw["identity"], field="peer.identity", maximum=128)
    principals = tuple(
        _bounded_strings(
            raw["allowed_coordinator_principals"],
            field="peer.allowed_coordinator_principals",
            maximum=32,
            item_max=128,
        )
    )
    if not principals or any(not _PRINCIPAL_RE.fullmatch(principal) for principal in principals):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer principals are invalid")
    profiles = tuple(
        _bounded_strings(raw["allowed_profiles"], field="peer.allowed_profiles", maximum=32, item_max=64)
    )
    if not profiles or any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer profiles are invalid")
    max_auth = _bounded_string(raw["max_authorization"], field="peer.max_authorization", maximum=32)
    if max_auth not in _AUTH_RANK:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer max_authorization is invalid")
    backends = tuple(
        _bounded_strings(raw["allowed_backends"], field="peer.allowed_backends", maximum=32, item_max=64)
    )
    if not backends or any(
        not _BACKEND_RE.fullmatch(backend) or backend in {"fabric", "fleet"}
        for backend in backends
    ):
        raise FabricError("FABRIC_CONFIG_INVALID", "peer backend allowlist is invalid")
    features = tuple(
        _bounded_strings(raw["required_features"], field="peer.required_features", maximum=32, item_max=128)
    )

    mappings_raw = raw["workspace_mappings"]
    if not isinstance(mappings_raw, dict) or not mappings_raw or len(mappings_raw) > 64:
        raise FabricError("FABRIC_CONFIG_INVALID", "peer workspace mappings are invalid")
    mappings: dict[str, WorkspaceMapping] = {}
    for logical_id, mapping in mappings_raw.items():
        logical_id = _bounded_string(logical_id, field="workspace logical id", maximum=128)
        mapping = _closed(
            mapping,
            required={"local_path", "revision", "conflict_domain"},
            name="workspace mapping",
        )
        local_path_text = _bounded_string(mapping["local_path"], field="workspace.local_path", maximum=1_000)
        local_path = Path(local_path_text).expanduser()
        if not local_path.is_absolute() or op.is_denied_path(local_path):
            raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping path is not allowed")
        try:
            if local_path.is_symlink():
                raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping root may not be a symlink")
        except OSError as exc:
            raise FabricError("FABRIC_CONFIG_INVALID", "workspace mapping cannot be inspected") from exc
        mappings[logical_id] = WorkspaceMapping(
            logical_id,
            local_path.resolve(),
            _bounded_string(mapping["revision"], field="workspace.revision", maximum=128),
            _bounded_string(mapping["conflict_domain"], field="workspace.conflict_domain", maximum=128),
        )

    return FabricPeerPolicy(
        node_name,
        identity,
        principals,
        profiles,
        max_auth,
        backends,
        features,
        mappings,
        sha256_json(raw),
    )


def load_peer_tokens(value: str | None = None) -> dict[str, str]:
    raw_value = value if value is not None else os.environ.get(PEER_TOKENS_ENV, "")
    if not raw_value:
        raise FabricError(
            "FABRIC_PRINCIPAL_CONFIG_MISSING",
            "verified Fabric requires configured coordinator-principal tokens",
        )
    raw = strict_json_loads(raw_value, maximum=32_000)
    if not isinstance(raw, dict) or not raw or len(raw) > 32:
        raise FabricError("FABRIC_PRINCIPAL_CONFIG_INVALID", "peer token map is invalid")

    out: dict[str, str] = {}
    seen_tokens: set[str] = set()
    for principal, token in raw.items():
        principal = _bounded_string(principal, field="coordinator principal", pattern=_PRINCIPAL_RE)
        if not isinstance(token, str) or len(token) < 16 or len(token.encode("utf-8")) > 1_024:
            raise FabricError("FABRIC_PRINCIPAL_CONFIG_INVALID", "peer bearer token is invalid")
        if token in seen_tokens:
            raise FabricError(
                "FABRIC_PRINCIPAL_CONFIG_INVALID",
                "each coordinator principal must have a unique bearer token",
            )
        out[principal] = token
        seen_tokens.add(token)
    return out


def _db_path(env_name: str, default_name: str, hermes_root: Path | None = None) -> Path:
    configured = os.environ.get(env_name, "").strip()
    path = Path(configured).expanduser() if configured else _root(hermes_root) / "fabric" / default_name
    if not path.is_absolute() or op.is_denied_path(path) or path.is_symlink():
        raise FabricError("FABRIC_JOURNAL_PATH_INVALID", "Fabric journal path is not allowed")
    return path


def _prepare_db_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        return


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _init_coordinator_db(path: Path) -> None:
    _prepare_db_parent(path)
    with _connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS dispatches (
              dispatch_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              node_name TEXT NOT NULL,
              evidence_policy_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS dispatches_task_idx ON dispatches(task_id);
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id),
              envelope_sha256 TEXT NOT NULL,
              node_name TEXT NOT NULL,
              peer_name TEXT NOT NULL,
              remote_backend TEXT NOT NULL,
              coordinator_principal TEXT NOT NULL,
              capability_sha256 TEXT NOT NULL,
              peer_policy_sha256 TEXT,
              state TEXT NOT NULL,
              remote_task_id TEXT,
              evidence_json TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_dispatch_idx ON attempts(dispatch_id);
            """
        )


def _init_peer_db(path: Path) -> None:
    _prepare_db_parent(path)
    with _connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL,
              envelope_sha256 TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              task_id TEXT NOT NULL,
              coordinator_principal TEXT NOT NULL,
              node_name TEXT NOT NULL,
              remote_backend TEXT NOT NULL,
              logical_workspace TEXT NOT NULL,
              conflict_domain TEXT NOT NULL,
              authorization_class TEXT NOT NULL,
              policy_sha256 TEXT NOT NULL,
              local_task_id TEXT NOT NULL,
              state TEXT NOT NULL,
              dispatch_result_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS write_claims (
              conflict_domain TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              state TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              released_at TEXT
            );
            """
        )


def _is_loopback_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_transport(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FabricError("FABRIC_TRANSPORT_INVALID", "configured Fabric peer URL is invalid")
    if parsed.username or parsed.password:
        raise FabricError("FABRIC_TRANSPORT_INVALID", "Fabric peer URL may not contain credentials")
    if parsed.scheme != "https" and not _is_loopback_url(url):
        raise FabricError("FABRIC_TRANSPORT_INSECURE", "non-loopback verified Fabric requires HTTPS")


def _http_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = canonical_json(body).encode("utf-8") if body is not None else None
    req_headers = {"Accept": "application/json", **headers}
    if data is not None:
        req_headers.update({"Content-Type": "application/json", "A2A-Version": "1.0"})
    req = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_BODY + 1)
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise FabricError(
                "FABRIC_TRANSPORT_TIMEOUT",
                "Fabric peer request timed out",
                ambiguous=body is not None,
            ) from exc
        raise FabricError(
            "FABRIC_PEER_UNAVAILABLE",
            "Fabric peer is unavailable",
            ambiguous=body is not None,
        ) from exc
    if len(raw) > _MAX_BODY:
        raise FabricError("FABRIC_PAYLOAD_TOO_LARGE", "Fabric peer response exceeds the bounded limit")
    parsed = strict_json_loads(raw)
    if not isinstance(parsed, dict):
        raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric peer returned a non-object response")
    return parsed


def _fabric_card(base_url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    return _http_json(
        base_url.rstrip("/") + "/.well-known/agent-card.json",
        headers=headers,
        timeout=timeout,
    )


def _peer_entry(node: FabricNode) -> dict[str, Any]:
    peers = op_fleet._a2a_peers_with_resolved_tokens()
    entry = peers.get(node.a2a_peer_name)
    if not isinstance(entry, dict):
        raise FabricError("FABRIC_NODE_NOT_ENROLLED", "managed node has no configured A2A peer")
    url = entry.get("url")
    if not isinstance(url, str):
        raise FabricError("FABRIC_NODE_NOT_ENROLLED", "managed node peer has no configured URL")
    _require_secure_transport(url)
    headers = op_fleet._auth_header(entry)
    if "Authorization" not in headers:
        raise FabricError(
            "FABRIC_PRINCIPAL_AUTH_REQUIRED",
            "verified Fabric requires a configured bearer credential",
        )
    return entry


def _extract_data_response(task: Any) -> tuple[str | None, dict[str, Any]]:
    task_id: str | None = None
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal task_id
        if depth > 10 or len(found) > 8:
            return
        if isinstance(node, dict):
            if task_id is None and isinstance(node.get("id"), str) and _ID_RE.fullmatch(node["id"]):
                task_id = node["id"]
            if isinstance(node.get("data"), dict) and node.get("mediaType") == "application/json":
                found.append(node["data"])
            for key in ("result", "task", "status", "message", "parts", "artifacts"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node[:_MAX_ITEMS]:
                walk(item, depth + 1)

    walk(task)
    if len(found) != 1:
        raise FabricError(
            "FABRIC_PROTOCOL_ERROR",
            "Fabric A2A response must contain exactly one structured DataPart",
        )
    return task_id, found[0]


def _validate_response(response: Any, *, operation: str) -> dict[str, Any]:
    response = _closed(
        response,
        required={"schema", "version", "operation", "ok", "code", "data"},
        name="Fabric response",
    )
    if (
        response["schema"] != RESPONSE_SCHEMA
        or response["version"] != FABRIC_VERSION
        or response["operation"] != operation
    ):
        raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric response binding does not match the request")
    if not isinstance(response["ok"], bool) or not isinstance(response["code"], str) or not isinstance(response["data"], dict):
        raise FabricError("FABRIC_PROTOCOL_ERROR", "Fabric response fields are invalid")
    return response


def _rpc_call(
    node: FabricNode,
    request: dict[str, Any],
    *,
    timeout: int,
) -> tuple[str | None, dict[str, Any]]:
    entry = _peer_entry(node)
    base_url = str(entry["url"])
    headers = op_fleet._auth_header(entry)
    card = _fabric_card(base_url, headers, min(max(timeout, 1), 15))
    if card.get("name") != node.expected_identity:
        raise FabricError(
            "FABRIC_PEER_IDENTITY_MISMATCH",
            "managed peer identity does not match the node registry",
        )

    rpc_url = base_url.rstrip("/")
    interfaces = card.get("supportedInterfaces")
    if isinstance(interfaces, list):
        for interface in interfaces:
            if (
                isinstance(interface, dict)
                and interface.get("protocolBinding") == "JSONRPC"
                and isinstance(interface.get("url"), str)
            ):
                candidate = interface["url"]
                _require_secure_transport(candidate)
                if urllib.parse.urlparse(candidate).hostname != urllib.parse.urlparse(base_url).hostname:
                    raise FabricError(
                        "FABRIC_PEER_IDENTITY_MISMATCH",
                        "Agent Card redirected Fabric to a different host",
                    )
                rpc_url = candidate
                break

    request_id = "frpc-" + hashlib.sha256(
        (canonical_json(request) + str(time.time_ns())).encode()
    ).hexdigest()[:20]
    dispatch_id = str(request.get("dispatch_id") or request.get("request_id") or request_id)
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"data": request, "mediaType": "application/json"}],
                "messageId": "msg-" + request_id[5:],
                "contextId": dispatch_id,
            }
        },
    }
    outer = _http_json(rpc_url, headers=headers, timeout=max(1, min(timeout, 120)), body=body)
    _closed(
        outer,
        required={"jsonrpc", "id"},
        optional={"result", "error"},
        name="A2A JSON-RPC response",
    )
    if outer.get("jsonrpc") != "2.0" or outer.get("id") != request_id:
        raise FabricError("FABRIC_PROTOCOL_ERROR", "A2A response identity mismatch")
    if "error" in outer:
        error = outer["error"] if isinstance(outer["error"], dict) else {}
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        raise FabricError(
            str(data.get("code") or "FABRIC_REMOTE_ERROR"),
            str(error.get("message") or "Fabric peer rejected the request"),
        )
    if "result" not in outer:
        raise FabricError("FABRIC_PROTOCOL_ERROR", "A2A response contains no result")
    remote_task_id, response = _extract_data_response(outer["result"])
    return remote_task_id, _validate_response(response, operation=str(request["operation"]))


def _evidence_policy(options: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    raw = options.get("evidence_provenance")
    if raw is None:
        return {"run_state": ("managed_peer_structured", "coordinator_observed")}
    raw = _closed(
        raw,
        required={"run_state"},
        name="execution.options.evidence_provenance",
    )
    values = tuple(
        _bounded_strings(raw["run_state"], field="evidence_provenance.run_state", maximum=8, item_max=64)
    )
    if not values or any(value not in _SAFE_EVIDENCE_PROVENANCE for value in values):
        raise FabricError(
            "FABRIC_EVIDENCE_POLICY_INVALID",
            "run_state evidence provenance is invalid",
        )
    return {"run_state": values}


def _fabric_options(
    contract: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], dict[str, tuple[str, ...]]]:
    execution = contract.get("execution")
    if not isinstance(execution, dict) or execution.get("backend") != "fabric":
        raise FabricError(
            "FABRIC_EXECUTION_INVALID",
            "Fabric backend requires execution.backend=fabric",
        )
    options = execution.get("options")
    if not isinstance(options, dict):
        raise FabricError("FABRIC_EXECUTION_INVALID", "Fabric execution options must be an object")
    allowed = {"node", "remote_backend", "logical_workspace", "remote_options", "evidence_provenance"}
    if set(options) - allowed:
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric execution options contain unknown fields")
    node_name = _bounded_string(options.get("node"), field="execution.options.node", pattern=_NODE_RE)
    remote_backend = _bounded_string(
        options.get("remote_backend"),
        field="execution.options.remote_backend",
        pattern=_BACKEND_RE,
    )
    if remote_backend in {"fabric", "fleet"}:
        raise FabricError("FABRIC_EXECUTION_INVALID", "nested Fabric/fleet delegation is not allowed")
    logical_workspace = _bounded_string(
        options.get("logical_workspace"),
        field="execution.options.logical_workspace",
        maximum=128,
    )
    remote_options = _bounded_json(
        options.get("remote_options") or {},
        field="execution.options.remote_options",
    )
    if not isinstance(remote_options, dict):
        raise FabricError("FABRIC_EXECUTION_INVALID", "remote_options must be an object")
    return (
        node_name,
        remote_backend,
        logical_workspace,
        remote_options,
        _evidence_policy(options),
    )


def _contract_sha(contract: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(contract).encode("utf-8")).hexdigest()


def _dispatch_id(contract_sha: str, task_id: str, node_name: str) -> str:
    return "fabd-" + hashlib.sha256(f"{contract_sha}:{task_id}:{node_name}".encode()).hexdigest()[:32]


def _attempt_id(dispatch_id: str, sequence: int = 1) -> str:
    return "faba-" + hashlib.sha256(f"{dispatch_id}:{sequence}".encode()).hexdigest()[:32]


def _contract_profile_scope(contract: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Fail closed if Fabric placement would widen Work Contract profile scope."""
    scope = contract.get("allowed_scope")
    if not isinstance(scope, dict):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "contract allowed_scope must be an object",
        )
    profiles = tuple(
        _bounded_strings(
            scope.get("profiles"),
            field="contract.allowed_scope.profiles",
            maximum=16,
            item_max=64,
        )
    )
    if not profiles or any(not _PROFILE_RE.fullmatch(profile) for profile in profiles):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "contract profile scope is invalid",
        )
    assigned_profile = _bounded_string(
        contract.get("assigned_profile"),
        field="contract.assigned_profile",
        pattern=_PROFILE_RE,
    )
    if assigned_profile not in profiles:
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "assigned profile is outside the Work Contract profile scope",
        )
    return assigned_profile, profiles


def _contract_forbidden_actions_present(contract: dict[str, Any]) -> bool:
    """Return whether the contract declares checks Fabric v1 cannot yet prove."""
    forbidden = contract.get("forbidden_actions", [])
    if not isinstance(forbidden, list) or len(forbidden) > 32:
        raise FabricError(
            "FABRIC_SCHEMA_INVALID",
            "contract forbidden_actions must be a bounded list",
        )
    return bool(forbidden)


def _auth_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization must be an object")
    allowed = {"class", "approved", "approved_by", "approval_reference"}
    if set(value) - allowed:
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization contains unknown fields")
    auth_class = value.get("class")
    if auth_class not in _AUTH_RANK or not isinstance(value.get("approved"), bool):
        raise FabricError("FABRIC_AUTHORITY_DENIED", "authorization is invalid")
    out: dict[str, Any] = {"class": auth_class, "approved": value["approved"]}
    for key in ("approved_by", "approval_reference"):
        if key in value:
            out[key] = _bounded_string(value[key], field=f"authorization.{key}", maximum=128)
    if auth_class == "high_impact" and (
        not out["approved"] or "approved_by" not in out or "approval_reference" not in out
    ):
        raise FabricError(
            "FABRIC_AUTHORITY_DENIED",
            "high-impact authorization requires explicit approval metadata",
        )
    return out


def _build_envelope(
    contract: dict[str, Any],
    node: FabricNode,
    *,
    remote_backend: str,
    logical_workspace: str,
    remote_options: dict[str, Any],
    evidence_policy: dict[str, tuple[str, ...]],
    capability_sha: str,
) -> dict[str, Any]:
    contract_sha = _contract_sha(contract)
    task_id = _bounded_string(contract.get("task_id"), field="contract.task_id", pattern=op_fleet._TASK_ID_RE)
    dispatch_id = _dispatch_id(contract_sha, task_id, node.name)
    envelope = {
        "schema": DISPATCH_SCHEMA,
        "version": FABRIC_VERSION,
        "dispatch_id": dispatch_id,
        "attempt_id": _attempt_id(dispatch_id),
        "contract_sha256": contract_sha,
        "task_id": task_id,
        "target_node": node.name,
        "coordinator_principal": node.coordinator_principal,
        "assigned_profile": _bounded_string(
            contract.get("assigned_profile"),
            field="contract.assigned_profile",
            pattern=_PROFILE_RE,
        ),
        "objective": _bounded_string(contract.get("objective"), field="contract.objective", maximum=8_000),
        "inputs": _bounded_strings(contract.get("inputs", []), field="contract.inputs"),
        "constraints": _bounded_strings(contract.get("constraints", []), field="contract.constraints"),
        "authorization": _auth_object(contract.get("authorization")),
        "logical_workspace": logical_workspace,
        "remote_backend": remote_backend,
        "remote_options": remote_options,
        "required_features": list(dict.fromkeys((*_DEFAULT_FEATURES, *node.required_features))),
        "capability_snapshot_sha256": capability_sha,
        "evidence_policy": {"run_state": list(evidence_policy["run_state"])},
        "created_at": _now(),
    }
    if len(canonical_json(envelope).encode("utf-8")) > 64_000:
        raise FabricError(
            "FABRIC_PAYLOAD_TOO_LARGE",
            "canonical Fabric dispatch envelope exceeds 64 KB",
        )
    return envelope


def _validate_envelope(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "version",
        "dispatch_id",
        "attempt_id",
        "contract_sha256",
        "task_id",
        "target_node",
        "coordinator_principal",
        "assigned_profile",
        "objective",
        "inputs",
        "constraints",
        "authorization",
        "logical_workspace",
        "remote_backend",
        "remote_options",
        "required_features",
        "capability_snapshot_sha256",
        "evidence_policy",
        "created_at",
    }
    envelope = _closed(value, required=required, name="Fabric dispatch envelope")
    if envelope["schema"] != DISPATCH_SCHEMA or envelope["version"] != FABRIC_VERSION:
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric dispatch schema/version is unsupported",
        )
    _bounded_string(envelope["dispatch_id"], field="dispatch_id", pattern=_ID_RE)
    _bounded_string(envelope["attempt_id"], field="attempt_id", pattern=_ID_RE)
    _bounded_string(envelope["contract_sha256"], field="contract_sha256", pattern=_SHA_RE)
    _bounded_string(envelope["task_id"], field="task_id", pattern=op_fleet._TASK_ID_RE)
    _bounded_string(envelope["target_node"], field="target_node", pattern=_NODE_RE)
    _bounded_string(
        envelope["coordinator_principal"],
        field="coordinator_principal",
        pattern=_PRINCIPAL_RE,
    )
    _bounded_string(envelope["assigned_profile"], field="assigned_profile", pattern=_PROFILE_RE)
    _bounded_string(envelope["objective"], field="objective", maximum=8_000)
    _bounded_strings(envelope["inputs"], field="inputs")
    _bounded_strings(envelope["constraints"], field="constraints")
    _auth_object(envelope["authorization"])
    _bounded_string(envelope["logical_workspace"], field="logical_workspace", maximum=128)
    backend = _bounded_string(envelope["remote_backend"], field="remote_backend", pattern=_BACKEND_RE)
    if backend in {"fabric", "fleet"}:
        raise FabricError("FABRIC_EXECUTION_INVALID", "nested Fabric/fleet delegation is not allowed")
    _bounded_json(envelope["remote_options"], field="remote_options")
    _bounded_strings(
        envelope["required_features"],
        field="required_features",
        maximum=32,
        item_max=128,
    )
    _bounded_string(
        envelope["capability_snapshot_sha256"],
        field="capability_snapshot_sha256",
        pattern=_SHA_RE,
    )
    evidence_policy = _closed(
        envelope["evidence_policy"],
        required={"run_state"},
        name="evidence_policy",
    )
    provenance = _bounded_strings(
        evidence_policy["run_state"],
        field="evidence_policy.run_state",
        maximum=8,
        item_max=64,
    )
    if not provenance or any(item not in _SAFE_EVIDENCE_PROVENANCE for item in provenance):
        raise FabricError(
            "FABRIC_EVIDENCE_POLICY_INVALID",
            "Fabric run-state evidence policy is invalid",
        )
    _bounded_string(envelope["created_at"], field="created_at", maximum=128)
    return envelope


def _request(
    operation: str,
    principal: str,
    *,
    data: dict[str, Any],
    dispatch_id: str = "",
    attempt_id: str = "",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "version": FABRIC_VERSION,
        "operation": operation,
        "coordinator_principal": principal,
        "request_id": "freq-"
        + hashlib.sha256(
            f"{operation}:{dispatch_id}:{attempt_id}:{time.time_ns()}".encode()
        ).hexdigest()[:24],
        "data": data,
    }
    if dispatch_id:
        request["dispatch_id"] = dispatch_id
    if attempt_id:
        request["attempt_id"] = attempt_id
    return request


def _validate_request(value: Any) -> dict[str, Any]:
    request = _closed(
        value,
        required={"schema", "version", "operation", "coordinator_principal", "request_id", "data"},
        optional={"dispatch_id", "attempt_id"},
        name="Fabric request",
    )
    if request["schema"] != REQUEST_SCHEMA or request["version"] != FABRIC_VERSION:
        raise FabricError(
            "FABRIC_PROTOCOL_INCOMPATIBLE",
            "Fabric request schema/version is unsupported",
        )
    operation = _bounded_string(request["operation"], field="operation", maximum=32)
    if operation not in {"capabilities", "accept", "status", "reconcile", "cancel", "evidence"}:
        raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "Fabric operation is unsupported")
    _bounded_string(
        request["coordinator_principal"],
        field="coordinator_principal",
        pattern=_PRINCIPAL_RE,
    )
    _bounded_string(request["request_id"], field="request_id", pattern=_ID_RE)
    if "dispatch_id" in request:
        _bounded_string(request["dispatch_id"], field="dispatch_id", pattern=_ID_RE)
    if "attempt_id" in request:
        _bounded_string(request["attempt_id"], field="attempt_id", pattern=_ID_RE)
    if not isinstance(request["data"], dict):
        raise FabricError("FABRIC_SCHEMA_INVALID", "Fabric request data must be an object")
    return request


def _response(operation: str, *, ok: bool, code: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "version": FABRIC_VERSION,
        "operation": operation,
        "ok": ok,
        "code": code,
        "data": data,
    }


def _audit(
    tool: str,
    *,
    success: bool,
    changed: bool,
    summary: str,
    extra: dict[str, Any],
) -> None:
    try:
        policy = op.OperatorPolicy()
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=not changed,
            success=success,
            changed=changed,
            summary=summary[:500],
            extra=extra,
        )
    except _EXPECTED_ERRORS:
        return


class FabricPeerService:
    """Deterministic managed peer acceptor. No request reaches an LLM path."""

    def __init__(
        self,
        *,
        policy_loader: Callable[[], FabricPeerPolicy] | None = None,
        tokens: dict[str, str] | None = None,
        db_path: Path | None = None,
        dispatch_fn: Callable[..., dict[str, Any]] | None = None,
        observed_fn: Callable[[str], list[dict[str, Any]]] | None = None,
        cancel_fn: Callable[[str, str], dict[str, Any]] | None = None,
        hermes_root: Path | None = None,
    ):
        self.hermes_root = hermes_root
        self.policy_loader = policy_loader or (lambda: load_peer_policy(hermes_root=hermes_root))
        self.tokens = tokens or load_peer_tokens()
        self.db_path = db_path or _db_path(PEER_DB_ENV, "peer.db", hermes_root)
        _init_peer_db(self.db_path)
        self.dispatch_fn = dispatch_fn or self._dispatch_local
        self.observed_fn = observed_fn or (
            lambda task_id: op_runners.observed_runs(task_id, hermes_root=hermes_root)
        )
        self.cancel_fn = cancel_fn or self._cancel_local
        self._lock = threading.RLock()

    def _dispatch_local(self, contract: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return op_runners.dispatch_contract(
            contract,
            confirm=True,
            dry_run=False,
            timeout=int(kwargs.get("timeout", 30)),
            hermes_root=self.hermes_root,
        )

    def _cancel_local(self, backend: str, task_id: str) -> dict[str, Any]:
        return op_runners.get_backend(backend).cancel(task_id, hermes_root=self.hermes_root)

    def authenticate(self, authorization: str) -> str:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_REQUIRED",
                "verified Fabric requires bearer authentication",
            )
        presented = authorization[7:]
        matches = [
            principal
            for principal, token in self.tokens.items()
            if hmac.compare_digest(token, presented)
        ]
        if len(matches) != 1:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal authentication failed",
            )
        return matches[0]

    def capabilities(self, policy: FabricPeerPolicy) -> dict[str, Any]:
        payload = {
            "schema": CAPABILITY_SCHEMA,
            "version": FABRIC_VERSION,
            "node_name": policy.node_name,
            "identity": policy.identity,
            "features": list(dict.fromkeys((*_DEFAULT_FEATURES, *policy.required_features))),
            "operations": ["capabilities", "accept", "status", "reconcile", "cancel", "evidence"],
            "policy_sha256": policy.digest,
        }
        payload["snapshot_sha256"] = sha256_json(payload)
        return payload

    def handle(self, request_value: dict[str, Any], authorization: str) -> dict[str, Any]:
        request = _validate_request(request_value)
        principal = self.authenticate(authorization)
        if principal != request["coordinator_principal"]:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "authenticated principal does not match the request",
            )
        policy = self.policy_loader()
        if principal not in policy.allowed_coordinator_principals:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "coordinator principal is not authorized by peer policy",
            )
        operation = request["operation"]
        if operation == "capabilities":
            _closed(request["data"], required=set(), name="capabilities data")
            return _response(operation, ok=True, code="FABRIC_OK", data=self.capabilities(policy))
        if operation == "accept":
            return self._accept(request, principal, policy)

        _closed(request["data"], required=set(), name=f"{operation} data")
        dispatch_id = _bounded_string(request.get("dispatch_id"), field="dispatch_id", pattern=_ID_RE)
        attempt_id = _bounded_string(request.get("attempt_id"), field="attempt_id", pattern=_ID_RE)
        if operation == "status":
            data = self._status(dispatch_id, attempt_id, reconcile=False)
        elif operation == "reconcile":
            data = self._status(dispatch_id, attempt_id, reconcile=True)
        elif operation == "cancel":
            data = self._cancel(dispatch_id, attempt_id, principal, policy)
        elif operation == "evidence":
            data = self._evidence(dispatch_id, attempt_id, principal, policy)
        else:
            raise FabricError("FABRIC_OPERATION_UNSUPPORTED", "unsupported Fabric operation")
        return _response(operation, ok=True, code="FABRIC_OK", data=data)

    def _authorize_envelope(
        self,
        envelope: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> WorkspaceMapping:
        if envelope["coordinator_principal"] != principal:
            raise FabricError("FABRIC_PRINCIPAL_AUTH_FAILED", "dispatch principal mismatch")
        if envelope["target_node"] != policy.node_name:
            raise FabricError(
                "FABRIC_NODE_IDENTITY_MISMATCH",
                "dispatch targets a different managed node",
            )
        if envelope["assigned_profile"] not in policy.allowed_profiles:
            raise FabricError("FABRIC_AUTHORITY_DENIED", "assigned profile is outside peer policy")
        auth = _auth_object(envelope["authorization"])
        if _AUTH_RANK[auth["class"]] > _AUTH_RANK[policy.max_authorization]:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "dispatch exceeds peer authorization ceiling",
            )
        if envelope["remote_backend"] not in policy.allowed_backends:
            raise FabricError("FABRIC_AUTHORITY_DENIED", "remote backend is outside peer policy")
        mapping = policy.workspace_mappings.get(envelope["logical_workspace"])
        if mapping is None:
            raise FabricError(
                "FABRIC_WORKSPACE_DENIED",
                "logical workspace is not mapped on the peer",
            )
        supported = set(_DEFAULT_FEATURES).union(policy.required_features)
        if not set(envelope["required_features"]) <= supported:
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "peer does not support all required Fabric features",
            )
        return mapping

    def _local_contract(
        self,
        envelope: dict[str, Any],
        mapping: WorkspaceMapping,
    ) -> dict[str, Any]:
        return {
            "schema": "hermes.work-contract/v1",
            "task_id": envelope["attempt_id"],
            "objective": envelope["objective"],
            "assigned_agent": envelope["target_node"],
            "assigned_profile": envelope["assigned_profile"],
            "inputs": list(envelope["inputs"]),
            "constraints": list(envelope["constraints"]),
            "allowed_scope": {
                "workspaces": [str(mapping.local_path)],
                "profiles": [envelope["assigned_profile"]],
            },
            "forbidden_actions": [],
            "expected_artifacts": [],
            "tests": [],
            "review_requirements": {
                "required": False,
                "reviewer": "",
                "evidence": "",
                "approval_required": False,
            },
            "completion_criteria": {
                "run_state": {"terminal": True, "outcome_ok": ["completed"]},
                "artifacts_present": False,
                "tests_pass": False,
                "review_satisfied": False,
                "no_forbidden_actions": True,
            },
            "authorization": dict(envelope["authorization"]),
            "execution": {
                "backend": envelope["remote_backend"],
                "options": _bounded_json(envelope["remote_options"], field="remote_options"),
            },
        }

    def _accept(
        self,
        request: dict[str, Any],
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        data = _closed(request["data"], required={"envelope"}, name="accept data")
        envelope = _validate_envelope(data["envelope"])
        if request.get("dispatch_id") and request["dispatch_id"] != envelope["dispatch_id"]:
            raise FabricError(
                "FABRIC_IDEMPOTENCY_CONFLICT",
                "request and envelope dispatch identity differ",
            )
        if request.get("attempt_id") and request["attempt_id"] != envelope["attempt_id"]:
            raise FabricError(
                "FABRIC_IDEMPOTENCY_CONFLICT",
                "request and envelope attempt identity differ",
            )
        mapping = self._authorize_envelope(envelope, principal, policy)
        envelope_sha = sha256_json(envelope)
        now = _now()

        with self._lock, _connect(self.db_path) as db:
            existing = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (envelope["attempt_id"],),
            ).fetchone()
            if existing is not None:
                if (
                    existing["dispatch_id"] != envelope["dispatch_id"]
                    or existing["envelope_sha256"] != envelope_sha
                ):
                    raise FabricError(
                        "FABRIC_IDEMPOTENCY_CONFLICT",
                        "attempt identity was reused with different canonical content",
                    )
                return _response(
                    "accept",
                    ok=True,
                    code="FABRIC_IDEMPOTENT_REPLAY",
                    data={
                        "dispatch_id": existing["dispatch_id"],
                        "attempt_id": existing["attempt_id"],
                        "state": existing["state"],
                        "local_task_id": existing["local_task_id"],
                        "policy_sha256": existing["policy_sha256"],
                    },
                )

            auth_class = envelope["authorization"]["class"]
            if _AUTH_RANK[auth_class] >= _AUTH_RANK["reversible_write"]:
                claim = db.execute(
                    "SELECT * FROM write_claims WHERE conflict_domain=?",
                    (mapping.conflict_domain,),
                ).fetchone()
                if claim is not None and claim["state"] == "ACTIVE":
                    raise FabricError(
                        "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                        "peer write conflict domain already has an active claim",
                    )
                db.execute(
                    "INSERT OR REPLACE INTO write_claims"
                    "(conflict_domain,attempt_id,state,acquired_at,released_at) VALUES(?,?,?,?,NULL)",
                    (mapping.conflict_domain, envelope["attempt_id"], "ACTIVE", now),
                )

            db.execute(
                "INSERT INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,contract_sha256,task_id,"
                "coordinator_principal,node_name,remote_backend,logical_workspace,"
                "conflict_domain,authorization_class,policy_sha256,local_task_id,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    envelope["attempt_id"],
                    envelope["dispatch_id"],
                    envelope_sha,
                    envelope["contract_sha256"],
                    envelope["task_id"],
                    principal,
                    policy.node_name,
                    envelope["remote_backend"],
                    envelope["logical_workspace"],
                    mapping.conflict_domain,
                    auth_class,
                    policy.digest,
                    envelope["attempt_id"],
                    "ACCEPTED",
                    now,
                    now,
                ),
            )

        # G3-S06: re-read local policy immediately before runner start.
        prestart = self.policy_loader()
        prestart_mapping = self._authorize_envelope(envelope, principal, prestart)
        if (
            prestart_mapping.local_path != mapping.local_path
            or prestart_mapping.revision != mapping.revision
            or prestart_mapping.conflict_domain != mapping.conflict_domain
        ):
            with _connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,policy_sha256=?,updated_at=? WHERE attempt_id=?",
                    ("BLOCKED", prestart.digest, _now(), envelope["attempt_id"]),
                )
            raise FabricError(
                "FABRIC_POLICY_DRIFT",
                "peer workspace policy changed between acceptance and runner start",
            )

        try:
            backend = op_runners.get_backend(envelope["remote_backend"])
        except LookupError as exc:
            raise FabricError("FABRIC_RUNNER_UNAVAILABLE", "remote runner is not registered") from exc
        if not bool(backend.availability(hermes_root=self.hermes_root).get("available")):
            raise FabricError(
                "FABRIC_RUNNER_UNAVAILABLE",
                "remote runner is unavailable at pre-start revalidation",
            )

        result = self.dispatch_fn(self._local_contract(envelope, prestart_mapping), timeout=30)
        if not isinstance(result, dict):
            result = {"success": False, "code": "FABRIC_RUNNER_INVALID_RESULT"}
        state = "RUNNING" if bool(result.get("success")) else "FAILED"
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,dispatch_result_json=?,policy_sha256=?,updated_at=?"
                " WHERE attempt_id=?",
                (
                    state,
                    canonical_json(_bounded_json(result, field="dispatch_result")),
                    prestart.digest,
                    _now(),
                    envelope["attempt_id"],
                ),
            )

        _audit(
            "hermes_fabric_peer_accept",
            success=bool(result.get("success")),
            changed=bool(result.get("success")),
            summary=f"Fabric attempt accepted on {policy.node_name}",
            extra={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "task_id": envelope["task_id"],
                "backend": envelope["remote_backend"],
                "principal": principal,
                "policy_sha256": prestart.digest,
            },
        )
        return _response(
            "accept",
            ok=bool(result.get("success")),
            code="FABRIC_ACCEPTED" if result.get("success") else str(result.get("code") or "FABRIC_RUNNER_REJECTED"),
            data={
                "dispatch_id": envelope["dispatch_id"],
                "attempt_id": envelope["attempt_id"],
                "state": state,
                "local_task_id": envelope["attempt_id"],
                "policy_sha256": prestart.digest,
            },
        )

    def _row(self, dispatch_id: str, attempt_id: str) -> sqlite3.Row:
        with _connect_readonly(self.db_path) as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=? AND dispatch_id=?",
                (attempt_id, dispatch_id),
            ).fetchone()
        if row is None:
            raise FabricError(
                "FABRIC_ATTEMPT_NOT_FOUND",
                "Fabric attempt is not present in the peer journal",
            )
        return row

    def _status(self, dispatch_id: str, attempt_id: str, *, reconcile: bool) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["state"] in _TERMINAL_PEER:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "local_task_id": row["local_task_id"],
                "policy_sha256": row["policy_sha256"],
            }

        primary = _latest_run(self.observed_fn(row["local_task_id"]))
        state = row["state"]
        if primary is not None:
            run_state = str(primary.get("state") or primary.get("status") or "").lower()
            if run_state in {"completed", "succeeded", "success"}:
                state = "SUCCEEDED"
            elif run_state in {"failed", "error"}:
                state = "FAILED"
            elif run_state in {"cancelled", "canceled"}:
                state = "CANCELLED"
            else:
                state = "RUNNING"
        elif reconcile and state in {"ACCEPTED", "RUNNING", "CANCEL_REQUESTED"}:
            state = "LOST_AMBIGUOUS"

        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, _now(), attempt_id),
            )
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "local_task_id": row["local_task_id"],
            "policy_sha256": row["policy_sha256"],
        }

    def _cancel(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "cancel identity does not match accepted attempt",
            )
        if row["state"] in _TERMINAL_PEER:
            return {
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "state": row["state"],
                "idempotent": True,
            }
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                ("CANCEL_REQUESTED", _now(), attempt_id),
            )
        result = self.cancel_fn(row["remote_backend"], row["local_task_id"])
        state = (
            "CANCELLED"
            if bool(result.get("success"))
            and str(result.get("state") or "").lower() in {"cancelled", "canceled"}
            else "CANCEL_REQUESTED"
        )
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,updated_at=? WHERE attempt_id=?",
                (state, _now(), attempt_id),
            )
        return {
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "state": state,
            "changed": bool(result.get("changed")),
        }

    def _evidence(
        self,
        dispatch_id: str,
        attempt_id: str,
        principal: str,
        policy: FabricPeerPolicy,
    ) -> dict[str, Any]:
        status = self._status(dispatch_id, attempt_id, reconcile=False)
        row = self._row(dispatch_id, attempt_id)
        if row["coordinator_principal"] != principal or row["node_name"] != policy.node_name:
            raise FabricError(
                "FABRIC_PRINCIPAL_AUTH_FAILED",
                "evidence identity does not match accepted attempt",
            )
        if status["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise FabricError(
                "FABRIC_EVIDENCE_NOT_READY",
                "terminal remote execution evidence is not available",
            )

        primary = _latest_run(self.observed_fn(row["local_task_id"]))
        observations: list[dict[str, Any]] = []
        if primary is not None:
            observations.append(
                {
                    "kind": "run_state",
                    "provenance": "managed_peer_structured",
                    "state": str(primary.get("status") or primary.get("state") or ""),
                    "outcome": str(primary.get("outcome") or primary.get("state") or primary.get("status") or ""),
                    "started_at": str(primary.get("started_at") or primary.get("dispatched_at") or ""),
                    "ended_at": str(primary.get("ended_at") or primary.get("completed_at") or ""),
                    "error": op.redact_output(str(primary.get("error") or ""))[:1_000],
                    "source": f"runner:{row['remote_backend']}",
                }
            )
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "version": FABRIC_VERSION,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "contract_sha256": row["contract_sha256"],
            "task_id": row["task_id"],
            "node_name": row["node_name"],
            "peer_identity": policy.identity,
            "coordinator_principal": principal,
            "remote_backend": row["remote_backend"],
            "terminal_state": status["state"],
            "observations": observations,
            "policy_sha256": row["policy_sha256"],
            "created_at": _now(),
        }
        return {"evidence": evidence}


def _latest_run(runs: Any) -> dict[str, Any] | None:
    if not isinstance(runs, list):
        return None
    candidates = [run for run in runs if isinstance(run, dict)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda run: (
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or run.get("state") or ""),
        ),
    )


def _validate_evidence(
    value: Any,
    *,
    attempt: Any,
    node: FabricNode,
    allowed_provenance: tuple[str, ...],
) -> dict[str, Any]:
    required = {
        "schema",
        "version",
        "dispatch_id",
        "attempt_id",
        "contract_sha256",
        "task_id",
        "node_name",
        "peer_identity",
        "coordinator_principal",
        "remote_backend",
        "terminal_state",
        "observations",
        "policy_sha256",
        "created_at",
    }
    evidence = _closed(value, required=required, name="Fabric evidence")
    if evidence["schema"] != EVIDENCE_SCHEMA or evidence["version"] != FABRIC_VERSION:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence schema/version is invalid")

    exact = {
        "dispatch_id": attempt["dispatch_id"],
        "attempt_id": attempt["attempt_id"],
        "node_name": node.name,
        "peer_identity": node.expected_identity,
        "coordinator_principal": node.coordinator_principal,
        "remote_backend": attempt["remote_backend"],
    }
    for key, expected in exact.items():
        if evidence.get(key) != expected:
            raise FabricError(
                "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
                f"evidence {key} does not match the admitted attempt",
            )

    coordinator_db = Path(attempt["_coordinator_db"])
    if not coordinator_db.is_file():
        raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
    with _connect_readonly(coordinator_db) as db:
        dispatch = db.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?",
            (attempt["dispatch_id"],),
        ).fetchone()
    if (
        dispatch is None
        or evidence.get("contract_sha256") != dispatch["contract_sha256"]
        or evidence.get("task_id") != dispatch["task_id"]
    ):
        raise FabricError(
            "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
            "evidence contract/task lineage does not match",
        )
    if attempt.get("peer_policy_sha256") and evidence.get("policy_sha256") != attempt.get(
        "peer_policy_sha256"
    ):
        raise FabricError(
            "FABRIC_EVIDENCE_LINEAGE_MISMATCH",
            "evidence peer policy digest does not match accepted attempt",
        )
    if evidence["terminal_state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence is not terminal")

    observations = evidence["observations"]
    if not isinstance(observations, list) or len(observations) > _MAX_ITEMS:
        raise FabricError("FABRIC_EVIDENCE_REJECTED", "evidence observations are invalid")
    clean: list[dict[str, Any]] = []
    for observation in observations:
        observation = _closed(
            observation,
            required={"kind", "provenance", "state", "outcome", "started_at", "ended_at", "error", "source"},
            name="evidence observation",
        )
        if observation["kind"] != "run_state":
            raise FabricError(
                "FABRIC_EVIDENCE_REJECTED",
                "G4-A accepts only run_state remote observations",
            )
        provenance = _bounded_string(
            observation["provenance"],
            field="observation.provenance",
            maximum=64,
        )
        if provenance not in allowed_provenance or provenance == "worker_statement":
            raise FabricError(
                "FABRIC_EVIDENCE_PROVENANCE_REJECTED",
                "evidence provenance is not allowed for the check",
            )
        clean.append(
            {
                "kind": "run_state",
                "provenance": provenance,
                "state": _bounded_string(observation["state"], field="observation.state", maximum=1_000, required=False),
                "outcome": _bounded_string(observation["outcome"], field="observation.outcome", maximum=1_000, required=False),
                "started_at": _bounded_string(observation["started_at"], field="observation.started_at", maximum=1_000, required=False),
                "ended_at": _bounded_string(observation["ended_at"], field="observation.ended_at", maximum=1_000, required=False),
                "error": _bounded_string(observation["error"], field="observation.error", maximum=1_000, required=False),
                "source": _bounded_string(observation["source"], field="observation.source", maximum=1_000, required=False),
            }
        )
    admitted = dict(evidence)
    admitted["observations"] = clean
    return admitted


class FabricCoordinator:
    def __init__(
        self,
        *,
        registry_loader: Callable[[], dict[str, FabricNode]] | None = None,
        db_path: Path | None = None,
        rpc: Callable[[FabricNode, dict[str, Any], int], tuple[str | None, dict[str, Any]]] | None = None,
        hermes_root: Path | None = None,
    ):
        self.hermes_root = hermes_root
        self.registry_loader = registry_loader or (
            lambda: load_node_registry(hermes_root=hermes_root)
        )
        self.db_path = db_path or _db_path(COORDINATOR_DB_ENV, "coordinator.db", hermes_root)
        # Deliberately lazy: constructing the Fabric backend during ordinary
        # Work Contract validation must not create or mutate state.
        self.rpc = rpc or (lambda node, request, timeout: _rpc_call(node, request, timeout=timeout))
        self._lock = threading.RLock()

    def _ensure_db(self) -> None:
        _init_coordinator_db(self.db_path)

    def _node(self, name: str) -> FabricNode:
        node = self.registry_loader().get(name)
        if node is None or not node.enabled:
            raise FabricError("FABRIC_NODE_NOT_ENROLLED", "Fabric node is not enrolled/enabled")
        return node

    def _capabilities(self, node: FabricNode, timeout: int) -> dict[str, Any]:
        _, response = self.rpc(
            node,
            _request("capabilities", node.coordinator_principal, data={}),
            timeout,
        )
        response = _validate_response(response, operation="capabilities")
        if not response["ok"]:
            raise FabricError(response["code"], "Fabric capability negotiation failed")
        data = _closed(
            response["data"],
            required={
                "schema",
                "version",
                "node_name",
                "identity",
                "features",
                "operations",
                "policy_sha256",
                "snapshot_sha256",
            },
            name="capability response",
        )
        if (
            data["schema"] != CAPABILITY_SCHEMA
            or data["version"] != FABRIC_VERSION
            or data["node_name"] != node.name
            or data["identity"] != node.expected_identity
        ):
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "managed peer capability identity/version is incompatible",
            )
        features = set(
            _bounded_strings(data["features"], field="capability.features", maximum=32, item_max=128)
        )
        if not set(_DEFAULT_FEATURES).union(node.required_features) <= features:
            raise FabricError(
                "FABRIC_PROTOCOL_INCOMPATIBLE",
                "managed peer lacks required Fabric features",
            )
        expected_sha = sha256_json({key: data[key] for key in data if key != "snapshot_sha256"})
        if data["snapshot_sha256"] != expected_sha:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "capability snapshot digest is invalid")
        return data

    def dispatch(
        self,
        contract: dict[str, Any],
        *,
        dry_run: bool,
        confirm: bool,
        timeout: int,
    ) -> dict[str, Any]:
        node_name, remote_backend, logical_workspace, remote_options, evidence_policy = _fabric_options(
            contract
        )
        node = self._node(node_name)
        assigned_profile, _profile_scope = _contract_profile_scope(contract)
        if _contract_forbidden_actions_present(contract):
            raise FabricError(
                "FABRIC_EVIDENCE_POLICY_INVALID",
                "verified Fabric cannot prove non-empty forbidden-action checks",
            )
        if contract.get("assigned_agent") != node.name:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "Fabric contract assigned_agent must match the managed node name",
            )
        if assigned_profile not in node.allowed_profiles:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "contract profile is outside managed-node policy",
            )
        auth = _auth_object(contract.get("authorization"))
        if _AUTH_RANK[auth["class"]] > _AUTH_RANK[node.max_authorization]:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "contract authorization exceeds managed-node ceiling",
            )
        if remote_backend not in node.allowed_remote_backends:
            raise FabricError(
                "FABRIC_AUTHORITY_DENIED",
                "remote backend is outside managed-node policy",
            )
        if logical_workspace not in node.logical_workspaces:
            raise FabricError(
                "FABRIC_WORKSPACE_DENIED",
                "logical workspace is outside managed-node policy",
            )

        contract_sha = _contract_sha(contract)
        stable_dispatch_id = _dispatch_id(contract_sha, str(contract["task_id"]), node.name)
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "changed": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": stable_dispatch_id,
                "remote_backend": remote_backend,
                "logical_workspace": logical_workspace,
                "live_peer_verification": "required_before_dispatch",
            }
        if not confirm:
            raise FabricError("CONFIRMATION_REQUIRED", "Fabric dispatch requires confirm=true")

        self._ensure_db()
        with _connect_readonly(self.db_path) as db:
            existing = db.execute(
                "SELECT a.* FROM attempts a WHERE a.dispatch_id=? ORDER BY a.created_at LIMIT 1",
                (stable_dispatch_id,),
            ).fetchone()
        if existing is not None:
            return {
                "success": existing["state"]
                in {"SUBMITTED", "RUNNING", "TERMINAL_REPORTED", "COMPLETED"},
                "changed": False,
                "backend": "fabric",
                "node": node.name,
                "dispatch_id": stable_dispatch_id,
                "attempt_id": existing["attempt_id"],
                "state": existing["state"],
                "remote_task_id": existing["remote_task_id"],
                "idempotent": True,
            }

        capabilities = self._capabilities(node, timeout)
        envelope = _build_envelope(
            contract,
            node,
            remote_backend=remote_backend,
            logical_workspace=logical_workspace,
            remote_options=remote_options,
            evidence_policy=evidence_policy,
            capability_sha=capabilities["snapshot_sha256"],
        )
        envelope_sha = sha256_json(envelope)
        dispatch_id = envelope["dispatch_id"]
        attempt_id = envelope["attempt_id"]
        now = _now()
        with self._lock, _connect(self.db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO dispatches"
                "(dispatch_id,task_id,contract_sha256,node_name,evidence_policy_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    dispatch_id,
                    envelope["task_id"],
                    envelope["contract_sha256"],
                    node.name,
                    canonical_json({key: list(value) for key, value in evidence_policy.items()}),
                    now,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO attempts"
                "(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,"
                "coordinator_principal,capability_sha256,peer_policy_sha256,state,remote_task_id,"
                "evidence_json,error_code,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL,NULL,NULL,?,?)",
                (
                    attempt_id,
                    dispatch_id,
                    envelope_sha,
                    node.name,
                    node.a2a_peer_name,
                    remote_backend,
                    node.coordinator_principal,
                    capabilities["snapshot_sha256"],
                    "SUBMITTING",
                    now,
                    now,
                ),
            )

        request = _request(
            "accept",
            node.coordinator_principal,
            data={"envelope": envelope},
            dispatch_id=dispatch_id,
            attempt_id=attempt_id,
        )
        try:
            remote_task_id, response = self.rpc(node, request, timeout)
        except FabricError as exc:
            state = "SUBMISSION_AMBIGUOUS" if exc.ambiguous else "BLOCKED"
            with _connect(self.db_path) as db:
                db.execute(
                    "UPDATE attempts SET state=?,error_code=?,updated_at=? WHERE attempt_id=?",
                    (state, exc.code, _now(), attempt_id),
                )
            _audit(
                "hermes_fabric_dispatch",
                success=False,
                changed=exc.ambiguous,
                summary="Fabric dispatch ambiguous" if exc.ambiguous else "Fabric dispatch failed",
                extra={
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "node": node.name,
                    "code": exc.code,
                    "principal": node.coordinator_principal,
                },
            )
            if exc.ambiguous:
                return {
                    "success": False,
                    "changed": True,
                    "backend": "fabric",
                    "code": exc.code,
                    "node": node.name,
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "state": state,
                    "submission_may_have_succeeded": True,
                    "suggested_action": (
                        "Reconcile this Fabric attempt; do not create a replacement writer."
                    ),
                }
            raise

        response = _validate_response(response, operation="accept")
        data = response["data"]
        if data.get("dispatch_id") != dispatch_id or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer accept response lineage mismatch")
        state = "SUBMITTED" if response["ok"] else "BLOCKED"
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,remote_task_id=?,peer_policy_sha256=?,error_code=?,"
                "updated_at=? WHERE attempt_id=?",
                (
                    *(peer_values[key] for key in optional),
                    state,
                    remote_task_id,
                    data.get("policy_sha256"),
                    None if response["ok"] else response["code"],
                    _now(),
                    attempt_id,
                ),
            )
        _audit(
            "hermes_fabric_dispatch",
            success=response["ok"],
            changed=response["ok"],
            summary=f"Fabric attempt submitted to {node.name}",
            extra={
                "dispatch_id": dispatch_id,
                "attempt_id": attempt_id,
                "task_id": envelope["task_id"],
                "node": node.name,
                "remote_backend": remote_backend,
                "principal": node.coordinator_principal,
                "remote_task_id": remote_task_id or "",
            },
        )
        return {
            "success": response["ok"],
            "changed": response["ok"],
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "remote_task_id": remote_task_id,
            "state": state,
            "code": response["code"],
        }

    def _attempt(self, attempt_id: str) -> tuple[sqlite3.Row, sqlite3.Row, FabricNode]:
        if not self.db_path.is_file():
            raise FabricError(
                "FABRIC_ATTEMPT_NOT_FOUND",
                "coordinator Fabric attempt does not exist",
            )
        with _connect_readonly(self.db_path) as db:
            attempt = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise FabricError(
                    "FABRIC_ATTEMPT_NOT_FOUND",
                    "coordinator Fabric attempt does not exist",
                )
            dispatch = db.execute(
                "SELECT * FROM dispatches WHERE dispatch_id=?",
                (attempt["dispatch_id"],),
            ).fetchone()
        if dispatch is None:
            raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
        return attempt, dispatch, self._node(attempt["node_name"])

    def poll(
        self,
        attempt_id: str,
        *,
        reconcile: bool = False,
        timeout: int = 15,
    ) -> dict[str, Any]:
        attempt, dispatch, node = self._attempt(attempt_id)
        operation = "reconcile" if reconcile else "status"
        _, response = self.rpc(
            node,
            _request(
                operation,
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation=operation)
        data = response["data"]
        if data.get("dispatch_id") != attempt["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer status lineage mismatch")
        peer_state = str(data.get("state") or "")
        state = {
            "ACCEPTED": "SUBMITTED",
            "RUNNING": "RUNNING",
            "CANCEL_REQUESTED": "CANCEL_REQUESTED",
            "SUCCEEDED": "TERMINAL_REPORTED",
            "FAILED": "TERMINAL_REPORTED",
            "CANCELLED": "CANCELLED",
            "LOST_AMBIGUOUS": "BLOCKED",
            "BLOCKED": "BLOCKED",
        }.get(peer_state, "BLOCKED")
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,updated_at=? WHERE attempt_id=?",
                (*(peer_values[key] for key in optional), state, _now(), attempt_id),
            )
        result = {
            "success": True,
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": attempt["dispatch_id"],
            "attempt_id": attempt_id,
            "state": state,
            "peer_state": peer_state,
            "task_id": dispatch["task_id"],
        }
        result.update({key: peer_values[key] for key in optional})
        return result

    def collect(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        attempt, _dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            _request(
                "evidence",
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation="evidence")
        if not response["ok"]:
            raise FabricError(response["code"], "peer did not return admissible evidence")
        data = _closed(response["data"], required={"evidence"}, name="evidence response")
        with _connect_readonly(self.db_path) as db:
            dispatch = db.execute(
                "SELECT * FROM dispatches WHERE dispatch_id=?",
                (attempt["dispatch_id"],),
            ).fetchone()
        if dispatch is None:
            raise FabricError("FABRIC_JOURNAL_CORRUPT", "coordinator dispatch lineage is missing")
        policy_raw = strict_json_loads(dispatch["evidence_policy_json"], maximum=16_000)
        allowed = (
            tuple(policy_raw.get("run_state") or [])
            if isinstance(policy_raw, dict)
            else ()
        )
        attempt_map = dict(attempt)
        attempt_map["_coordinator_db"] = str(self.db_path)
        admitted = _validate_evidence(
            data["evidence"],
            attempt=attempt_map,
            node=node,
            allowed_provenance=allowed,
        )
        terminal = admitted["terminal_state"]
        state = (
            "COMPLETED"
            if terminal == "SUCCEEDED"
            else "FAILED"
            if terminal == "FAILED"
            else "CANCELLED"
        )
        with _connect(self.db_path) as db:
            db.execute(
                "UPDATE attempts SET state=?,evidence_json=?,updated_at=? WHERE attempt_id=?",
                (state, canonical_json(admitted), _now(), attempt_id),
            )
        _audit(
            "hermes_fabric_evidence",
            success=True,
            changed=False,
            summary=f"Fabric evidence admitted from {node.name}",
            extra={
                "dispatch_id": attempt["dispatch_id"],
                "attempt_id": attempt_id,
                "task_id": dispatch["task_id"],
                "node": node.name,
                "principal": node.coordinator_principal,
                "terminal_state": terminal,
            },
        )
        return {
            "success": True,
            "backend": "fabric",
            "node": node.name,
            "dispatch_id": attempt["dispatch_id"],
            "attempt_id": attempt_id,
            "state": state,
            "evidence": admitted,
        }

    def cancel(self, attempt_id: str, *, timeout: int = 15) -> dict[str, Any]:
        attempt, dispatch, node = self._attempt(attempt_id)
        _, response = self.rpc(
            node,
            _request(
                "cancel",
                node.coordinator_principal,
                data={},
                dispatch_id=attempt["dispatch_id"],
                attempt_id=attempt_id,
            ),
            timeout,
        )
        response = _validate_response(response, operation="cancel")
        data = response["data"]
        if data.get("dispatch_id") != attempt["dispatch_id"] or data.get("attempt_id") != attempt_id:
            raise FabricError("FABRIC_PROTOCOL_ERROR", "peer cancel lineage mismatch")
        state = "CANCELLED" if data.get("state") == "CANCELLED" else "CANCEL_REQUESTED"
        with _connect(self.db_path) as db:
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(attempts)")}
            peer_values = _bounded_coordinator_peer_values(data)
            optional = [
                key
                for key in ("write_epoch", "write_claim_state", "execution_unit_state")
                if key in columns and key in data and peer_values[key] is not None
            ]
            assignments = ",".join(f"{key}=?" for key in optional)
            if assignments:
                assignments += ","
            db.execute(
                f"UPDATE attempts SET {assignments}state=?,updated_at=? WHERE attempt_id=?",
                (*(peer_values[key] for key in optional), state, _now(), attempt_id),
            )
        return {
            "success": True,
            "changed": bool(data.get("changed")),
            "backend": "fabric",
            "attempt_id": attempt_id,
            "dispatch_id": attempt["dispatch_id"],
            "task_id": dispatch["task_id"],
            "state": state,
        }

    def observed_runs(self, task_id: str, *, refresh: bool = True) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        if refresh:
            with _connect_readonly(self.db_path) as db:
                rows = db.execute(
                    "SELECT a.* FROM attempts a JOIN dispatches d ON d.dispatch_id=a.dispatch_id"
                    " WHERE d.task_id=? ORDER BY a.created_at",
                    (task_id,),
                ).fetchall()
            for row in rows:
                try:
                    if row["state"] not in _TERMINAL_COORD:
                        status = self.poll(
                            row["attempt_id"],
                            reconcile=row["state"] == "SUBMISSION_AMBIGUOUS",
                            timeout=10,
                        )
                        if status["state"] == "TERMINAL_REPORTED":
                            self.collect(row["attempt_id"], timeout=10)
                    elif row["state"] in {"COMPLETED", "FAILED", "CANCELLED"} and not row[
                        "evidence_json"
                    ]:
                        self.collect(row["attempt_id"], timeout=10)
                except FabricError:
                    continue

        with _connect_readonly(self.db_path) as db:
            rows = db.execute(
                "SELECT a.* FROM attempts a JOIN dispatches d ON d.dispatch_id=a.dispatch_id"
                " WHERE d.task_id=? ORDER BY a.created_at",
                (task_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row["evidence_json"]:
                continue
            try:
                evidence = strict_json_loads(row["evidence_json"], maximum=_MAX_BODY)
            except FabricError:
                continue
            if not isinstance(evidence, dict):
                continue
            for observation in evidence.get("observations", []):
                if (
                    not isinstance(observation, dict)
                    or observation.get("kind") != "run_state"
                    or observation.get("provenance") not in _SAFE_EVIDENCE_PROVENANCE
                ):
                    continue
                terminal = evidence.get("terminal_state")
                mapped = (
                    "completed"
                    if terminal == "SUCCEEDED"
                    else "failed"
                    if terminal == "FAILED"
                    else "cancelled"
                )
                out.append(
                    {
                        "task_id": task_id,
                        "status": mapped,
                        "outcome": mapped,
                        "error": observation.get("error") or "",
                        "started_at": observation.get("started_at") or "",
                        "ended_at": observation.get("ended_at") or "",
                        "scope": f"fabric:{row['node_name']}",
                        "backend": "fabric",
                        "remote_backend": row["remote_backend"],
                        "attempt_id": row["attempt_id"],
                        "dispatch_id": row["dispatch_id"],
                        "evidence_provenance": observation.get("provenance"),
                    }
                )
        return out


@dataclass
class FabricBackend:
    name: str = "fabric"
    coordinator_factory: Callable[..., FabricCoordinator] = FabricCoordinator

    def availability(self, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            enabled = sorted(
                name
                for name, node in load_node_registry(hermes_root=hermes_root).items()
                if node.enabled
            )
            return {
                "available": bool(enabled),
                "node_count": len(enabled),
                "reason": None if enabled else "no enabled Fabric nodes",
            }
        except _EXPECTED_ERRORS as exc:
            return {
                "available": False,
                "node_count": 0,
                "reason": op.redact_output(str(exc))[:300],
            }

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
        coordinator = kwargs.get("fabric_coordinator") or self.coordinator_factory(
            hermes_root=hermes_root
        )
        try:
            return coordinator.dispatch(
                contract,
                dry_run=dry_run,
                confirm=confirm,
                timeout=timeout,
            )
        except FabricError as exc:
            return {
                "success": False,
                "ok": False,
                "changed": bool(exc.ambiguous),
                "backend": self.name,
                "code": exc.code,
                "safe_message": op.redact_output(str(exc))[:300],
                "submission_may_have_succeeded": bool(exc.ambiguous),
            }

    def observed_runs(
        self,
        task_id: str,
        *,
        hermes_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        # Work Contract validation is observational. It must only read evidence
        # previously admitted by an explicit Fabric status/evidence operation.
        return self.coordinator_factory(hermes_root=hermes_root).observed_runs(
            task_id,
            refresh=False,
        )

    def observed_artifacts(
        self,
        task_id: str,
        *,
        contract_sha256: str,
        hermes_root: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Return coordinator-verified admitted artifact metadata, when supported.

        The base G4-A coordinator has no artifact admission store. G4-C installs
        an enhanced coordinator that exposes this read-only evidence surface.
        Keeping the adapter here lets Work Contract validation consume admitted
        artifacts without importing Fabric internals or materializing active
        remote content into an allowed workspace.
        """
        coordinator = self.coordinator_factory(hermes_root=hermes_root)
        observer = getattr(coordinator, "observed_artifacts", None)
        if not callable(observer):
            return []
        try:
            value = observer(task_id, contract_sha256=contract_sha256)
        except FabricError:
            return []
        return value if isinstance(value, list) else []

    def cancel(self, task_id: str, *, hermes_root: Path | None = None) -> dict[str, Any]:
        try:
            return self.coordinator_factory(hermes_root=hermes_root).cancel(task_id)
        except FabricError as exc:
            return {
                "success": False,
                "changed": False,
                "backend": self.name,
                "code": exc.code,
                "safe_message": op.redact_output(str(exc))[:300],
            }


def register_runner_backend() -> None:
    try:
        if isinstance(op_runners.get_backend("fabric"), FabricBackend):
            return
    except LookupError:
        pass
    op_runners.register_backend(FabricBackend(), replace=True)


class _PeerHandler(BaseHTTPRequestHandler):
    server_version = "HermesGPTFabric/0.8"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def service(self) -> FabricPeerService:
        return self.server.fabric_service

    @property
    def advertised_url(self) -> str:
        return self.server.fabric_advertised_url

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = canonical_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if urllib.parse.urlparse(self.path).path not in {
            "/.well-known/agent-card.json",
            "/.well-known/agent.json",
        }:
            self._send(404, {"error": "not found"})
            return
        try:
            policy = self.service.policy_loader()
            self._send(
                200,
                {
                    "protocolVersion": "1.0",
                    "name": policy.identity,
                    "description": "Hermes GPT managed Fabric peer",
                    "version": "0.8",
                    "url": self.advertised_url,
                    "capabilities": {},
                    "defaultInputModes": ["application/json"],
                    "defaultOutputModes": ["application/json"],
                    "skills": [
                        {
                            "id": "hermes-fabric-v1",
                            "name": "Hermes Fabric v1",
                            "description": "Deterministic managed remote execution",
                            "tags": ["hermes", "fabric"],
                        }
                    ],
                    "supportedInterfaces": [
                        {
                            "url": self.advertised_url,
                            "protocolBinding": "JSONRPC",
                            "protocolVersion": "1.0",
                        }
                    ],
                },
            )
        except FabricError as exc:
            self._send(503, {"error": exc.code})

    def do_POST(self) -> None:
        outer: Any = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > _MAX_BODY:
                raise FabricError(
                    "FABRIC_PAYLOAD_TOO_LARGE",
                    "A2A request has an invalid body size",
                )
            outer = _closed(
                strict_json_loads(self.rfile.read(length)),
                required={"jsonrpc", "id", "method", "params"},
                name="A2A JSON-RPC request",
            )
            if outer["jsonrpc"] != "2.0" or outer["method"] not in {
                "SendMessage",
                "message/send",
            }:
                raise FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "only A2A SendMessage is accepted by the Fabric peer",
                )
            params = _closed(
                outer["params"],
                required={"message"},
                name="A2A params",
            )
            message = _closed(
                params["message"],
                required={"role", "parts", "messageId", "contextId"},
                name="A2A message",
            )
            if (
                message["role"] != "ROLE_USER"
                or not isinstance(message["parts"], list)
                or len(message["parts"]) != 1
            ):
                raise FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric A2A message must contain exactly one structured DataPart",
                )
            raw_part = message["parts"][0]
            if (
                not isinstance(raw_part, dict)
                or "text" in raw_part
                or "data" not in raw_part
                or raw_part.get("mediaType") != "application/json"
            ):
                raise FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric does not accept text or generic agent messages",
                )
            part = _closed(
                raw_part,
                required={"data", "mediaType"},
                name="A2A DataPart",
            )
            if not isinstance(part["data"], dict):
                raise FabricError(
                    "FABRIC_PROTOCOL_ERROR",
                    "Fabric DataPart must contain a structured JSON object",
                )
            request = _validate_request(part["data"])
            response = self.service.handle(
                request,
                self.headers.get("Authorization", ""),
            )
            context_id = request.get("dispatch_id") or message["contextId"]
            task_id = "ftask-" + hashlib.sha256(
                f"{request['request_id']}:{request.get('attempt_id', '')}".encode()
            ).hexdigest()[:24]
            result = {
                "id": task_id,
                "contextId": context_id,
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {
                        "role": "ROLE_AGENT",
                        "parts": [{"data": response, "mediaType": "application/json"}],
                        "messageId": "resp-" + task_id[6:],
                        "contextId": context_id,
                    },
                },
            }
            self._send(200, {"jsonrpc": "2.0", "id": outer["id"], "result": result})
        except FabricError as exc:
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": outer.get("id") if isinstance(outer, dict) else None,
                    "error": {
                        "code": -32001,
                        "message": str(exc)[:300],
                        "data": {"code": exc.code},
                    },
                },
            )
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": outer.get("id") if isinstance(outer, dict) else None,
                    "error": {
                        "code": -32603,
                        "message": "internal Fabric peer error",
                        "data": {"code": "FABRIC_INTERNAL_ERROR"},
                    },
                },
            )


def peer_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hermes-gpt-fabric-peer",
        description="Run the deterministic Hermes GPT Fabric A2A peer endpoint.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4780)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--advertised-url", default="")
    args = parser.parse_args(argv)
    if bool(args.cert) != bool(args.key):
        raise SystemExit("Fabric TLS requires both --cert and --key.")
    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and not (args.cert and args.key):
        raise SystemExit("Non-loopback verified Fabric requires direct TLS (--cert and --key).")
    scheme = "https" if args.cert else "http"
    advertised = args.advertised_url or f"{scheme}://{args.host}:{args.port}"
    _require_secure_transport(advertised)
    service = FabricPeerService()
    server = ThreadingHTTPServer((args.host, args.port), _PeerHandler)
    server.fabric_service = service
    server.fabric_advertised_url = advertised
    if args.cert and args.key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "FabricBackend",
    "FabricCoordinator",
    "FabricError",
    "FabricNode",
    "FabricPeerPolicy",
    "FabricPeerService",
    "WorkspaceMapping",
    "canonical_json",
    "load_node_registry",
    "load_peer_policy",
    "load_peer_tokens",
    "peer_main",
    "register_runner_backend",
    "sha256_json",
    "strict_json_loads",
]

if __name__ == "__main__":
    peer_main()
