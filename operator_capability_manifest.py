"""Derived capability-manifest read model for hermes-gpt vNext (slice-1, phase 1).

This module implements proposal §17 item 3 / §2.2 row 2 / §6.2
(``placement_cache``) as a **derived, read-only query view**: it answers
"what can X do" by *reading* the existing authoritative registries and
merging them into a single normalized capability registry. It is a read
model, never a source of truth — nothing here writes, opens a store in
write mode, or creates a new durable table.

Sources folded in (each already authoritative; none is re-derived):
- Fabric node registry  -> ``operator_fabric.load_node_registry``
- Fleet authority manifest -> ``<root>/config/fleet-authority.json``
- Profile toolsets & skills -> per-profile ``skills/`` dir + config
- (provider dimension derives from each profile's configured model/provider)

INV-9 (data containment) holds throughout: only refs, bounded metadata,
and content-addressed hashes cross the surface. No raw prompt, transcript,
memory body, credential, or secret-path content is ever emitted.

Read-only by construction: every SQLite/JSON source is opened read-only
(``mode=ro`` / ``r``) and no mutation path is exposed. The in-process
``placement_cache`` is a TTL-bounded derived snapshot that is *rebuilt*
from the registries when it expires; it is not a durable store.

Conventions mirror ``operator_events`` / ``operator_mission``:
- public functions return a JSON ``str`` of a bounded envelope;
- every call is audited (``op.audit_record``);
- an allowlist env gates the derived sources;
- output is bounded with ``truncated`` / ``count_total``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_diagnostics as op_diag
import operator_fabric as op_fabric
import operator_fleet as op_fleet
import operator_policy as op

SCHEMA_VERSION = "0.1-cm.1"
MANIFEST_SCHEMA = "hermes.capability-manifest/v1"

# Allowlist env: comma list of source kinds (unset = all, empty = none).
CAPABILITY_SOURCES_ENV = "HERMES_GPT_CAPABILITY_ALLOWED_SOURCES"
CAPABILITY_SOURCES: tuple[str, ...] = ("fabric", "fleet", "profile", "provider")

# Placement-cache TTL env (seconds), floor, and default.
CACHE_TTL_ENV = "HERMES_GPT_CAPABILITY_CACHE_TTL"
CACHE_TTL_DEFAULT = 60
CACHE_TTL_FLOOR = 5
CACHE_TTL_CEIL = 3600

MAX_ENTITIES_PER_SOURCE = 256
_MAX_STRING = 128
_ERROR_STRING_CAP = 500

# PII-like tokens stripped from free-text summaries (mirrors operator_events).
_PII_STRIP = __import__("re").compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)


def _sanitize(text: Any, limit: int = _ERROR_STRING_CAP) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _resolve_root(hermes_root: Path | None) -> Path:
    """Resolve a concrete Hermes data root (falls back to the default)."""
    if hermes_root is not None:
        return Path(hermes_root)
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [Path.home() / ".hermes"]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Allowlist + TTL
# ---------------------------------------------------------------------------


def _allowed_sources() -> set[str]:
    raw = os.environ.get(CAPABILITY_SOURCES_ENV)
    if raw is None:
        return set(CAPABILITY_SOURCES)
    allowed: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if item in CAPABILITY_SOURCES:
            allowed.add(item)
    return allowed


def _source_allowed(source: str) -> bool:
    return source in _allowed_sources()


def _cache_ttl() -> int:
    raw = os.environ.get(CACHE_TTL_ENV, "").strip()
    if not raw:
        return CACHE_TTL_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return CACHE_TTL_DEFAULT
    return max(CACHE_TTL_FLOOR, min(value, CACHE_TTL_CEIL))


# ---------------------------------------------------------------------------
# In-process TTL placement cache (derived snapshot; NOT a durable store)
# ---------------------------------------------------------------------------


def _cache_key(root: Path, sources: tuple[str, ...]) -> str:
    return f"{root!s}::{','.join(sorted(sources))}"


# value: (expires_at, snapshot)
_placement_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _placement_cache_get(root: Path, sources: tuple[str, ...]) -> dict[str, Any]:
    """Return a TTL-bounded derived snapshot, rebuilding it when expired."""
    key = _cache_key(root, sources)
    entry = _placement_cache.get(key)
    now = time.time()
    if entry is not None and now < entry[0]:
        entry[1]["built_at"] = entry[1].get("built_at")
        return entry[1]
    snapshot = _build_placement_cache(root, sources)
    _placement_cache[key] = (now + _cache_ttl(), snapshot)
    return snapshot


def _placement_cache_invalidate() -> None:
    _placement_cache.clear()


# ---------------------------------------------------------------------------
# Per-source readers (read-only; redacted; bounded)
# ---------------------------------------------------------------------------


def _read_fabric_entities(root: Path) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    try:
        nodes = op_fabric.load_node_registry(hermes_root=root)
    except Exception:  # noqa: BLE001 - missing/invalid registry is a state
        return entities
    for name in sorted(nodes):
        node = nodes[name]
        canon = {
            "entity_kind": "fabric_node",
            "name": _sanitize(name, _MAX_STRING),
            "enabled": bool(node.enabled),
            "allowed_profiles": list(node.allowed_profiles)[:MAX_ENTITIES_PER_SOURCE],
            "backends": list(node.allowed_remote_backends)[:MAX_ENTITIES_PER_SOURCE],
            "workspaces": list(node.logical_workspaces)[:MAX_ENTITIES_PER_SOURCE],
            "features": list(node.required_features)[:MAX_ENTITIES_PER_SOURCE],
            "authorization_ceiling": _sanitize(node.max_authorization, 32),
        }
        entities.append(_entity("fabric", name, canon, ["fabric-nodes.json"]))
        if len(entities) >= MAX_ENTITIES_PER_SOURCE:
            break
    return entities


def _read_fleet_entities(root: Path) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    manifest = root / "config" / "fleet-authority.json"
    if not manifest.is_file():
        return entities
    try:
        peers = op_fleet._load_authority(manifest)
    except Exception:  # noqa: BLE001 - invalid/missing manifest is a state
        return entities
    for name in sorted(peers):
        peer = peers[name]
        # INV-9: never emit the raw card identity; report only that it is
        # configured and expose the (non-secret) role + ceiling.
        canon = {
            "entity_kind": "fleet_peer",
            "name": _sanitize(name, _MAX_STRING),
            "host_role": _sanitize(peer.expected_host_role, _MAX_STRING),
            "identity_configured": bool(peer.expected_card_identity),
            "allowed_profiles": list(peer.allowed_profiles)[:MAX_ENTITIES_PER_SOURCE],
            "authorization_ceiling": _sanitize(peer.max_authorization, 32),
            "allow_public_actions": bool(peer.allow_public_actions),
        }
        entities.append(_entity("fleet", name, canon, ["fleet-authority.json"]))
        if len(entities) >= MAX_ENTITIES_PER_SOURCE:
            break
    return entities


def _list_profile_skills(profile_home: Path) -> list[str]:
    skills: list[str] = []
    root = profile_home / "skills"
    if not root.is_dir():
        return skills
    try:
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "SKILL.md").is_file():
                skills.append(_sanitize(d.name, _MAX_STRING))
    except OSError:
        return skills
    return skills


def _read_profile_entities(root: Path) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for profile in op.list_existing_profiles(root):
        try:
            home = op.resolve_profile_home(profile, root)
        except Exception:  # noqa: BLE001, S112 - missing profile is a state
            continue
        model: str | None = None
        provider: str | None = None
        try:
            cfg = op_diag._read_config_safe(home)
            if isinstance(cfg, dict):
                model = cfg.get("model")
                provider = cfg.get("provider")
                model = str(model) if isinstance(model, str) and model else None
                provider = (
                    str(provider) if isinstance(provider, str) and provider else None
                )
        except Exception:  # noqa: BLE001, S110 - unreadable config is a state
            pass
        skills = _list_profile_skills(home)
        canon = {
            "entity_kind": "profile",
            "name": _sanitize(profile, _MAX_STRING),
            "model": _sanitize(model or "", _MAX_STRING),
            "provider": _sanitize(provider or "", _MAX_STRING),
            "skills": skills[:MAX_ENTITIES_PER_SOURCE],
            "skill_count": len(skills),
        }
        entities.append(
            _entity("profile", profile, canon, [f"profiles/{profile}/config.yaml"])
        )
        if len(entities) >= MAX_ENTITIES_PER_SOURCE:
            break
    return entities


def _read_provider_entities(root: Path) -> list[dict[str, Any]]:
    """Derive the provider dimension from per-profile configured model/provider.

    This is a derived read model: it folds the profile registry into a
    provider-capability view, it is not a separate source of truth.
    """
    by_provider: dict[str, list[str]] = {}
    by_provider_model: dict[str, list[str]] = {}
    for profile in op.list_existing_profiles(root):
        try:
            home = op.resolve_profile_home(profile, root)
        except Exception:  # noqa: BLE001, S112 - missing profile is a state
            continue
        try:
            cfg = op_diag._read_config_safe(home)
        except Exception:  # noqa: BLE001, S112 - unreadable config is a state
            continue
        if not isinstance(cfg, dict):
            continue
        provider = cfg.get("provider")
        model = cfg.get("model")
        provider = str(provider) if isinstance(provider, str) and provider else ""
        model = str(model) if isinstance(model, str) and model else ""
        if not provider:
            continue
        by_provider.setdefault(provider, []).append(profile)
        if model:
            by_provider_model.setdefault(provider, []).append(model)
    entities: list[dict[str, Any]] = []
    for provider in sorted(by_provider):
        profiles = sorted(set(by_provider[provider]))
        models = sorted(set(by_provider_model.get(provider, [])))
        canon = {
            "entity_kind": "provider",
            "name": _sanitize(provider, _MAX_STRING),
            "profiles": profiles[:MAX_ENTITIES_PER_SOURCE],
            "models": models[:MAX_ENTITIES_PER_SOURCE],
            "profile_count": len(profiles),
            "model_count": len(models),
        }
        entities.append(
            _entity("provider", provider, canon, ["profiles/*/config.yaml"])
        )
        if len(entities) >= MAX_ENTITIES_PER_SOURCE:
            break
    return entities


def _entity(
    source: str, name: str, canon: dict[str, Any], derived_from: list[str]
) -> dict[str, Any]:
    """Content-address a canonical capability record (INV-9 provenance)."""
    canonical = {
        "entity_kind": canon["entity_kind"],
        "name": _sanitize(name, _MAX_STRING),
    }
    # Only bounded, deterministic metadata is hashed; never raw bodies.
    readable = {k: v for k, v in canon.items() if k not in {"entity_kind", "name"}}
    canonical["attributes"] = readable
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "entity_id": f"{source}:{_sanitize(name, _MAX_STRING)}",
        "source": source,
        "capability_sha256": digest,
        **canon,
        "derived_from": derived_from,
    }


_SOURCE_READERS: dict[str, Any] = {
    "fabric": _read_fabric_entities,
    "fleet": _read_fleet_entities,
    "profile": _read_profile_entities,
    "provider": _read_provider_entities,
}


# ---------------------------------------------------------------------------
# Placement cache (derived snapshot build)
# ---------------------------------------------------------------------------


def _build_placement_cache(root: Path, sources: tuple[str, ...]) -> dict[str, Any]:
    """Build the derived, TTL-bounded placement cache snapshot.

    The snapshot is a flat index of *placeable targets* filtered to
    enabled/available entities. Placement scoring (a later, non-mutating
    phase) will read this index and apply the hard/soft filters; this phase
    only produces the derived index and records its build provenance.
    """
    now = datetime.now(timezone.utc)
    entries: list[dict[str, Any]] = []
    for source in sources:
        if not _source_allowed(source):
            continue
        entries.extend(_SOURCE_READERS[source](root))
    # Enablement filter (derived): only enabled fabric nodes / valid peers /
    # configured profiles appear as placeable candidates.
    placeable: list[dict[str, Any]] = []
    for ent in entries:
        if ent["entity_kind"] == "fabric_node" and not ent.get("enabled"):
            continue
        placeable.append(
            {
                "entity_id": ent["entity_id"],
                "entity_kind": ent["entity_kind"],
                "name": ent["name"],
                "capability_sha256": ent["capability_sha256"],
                "authorization_ceiling": ent.get("authorization_ceiling", ""),
                "features": ent.get("features", []),
                "allowed_profiles": ent.get("allowed_profiles", []),
                "skills": ent.get("skills", []),
                "model": ent.get("model", ""),
                "provider": ent.get("provider", ""),
            }
        )
    snapshot = {
        "built_at": now.isoformat(),
        "ttl_seconds": _cache_ttl(),
        "expires_at": (
            datetime.fromtimestamp(time.time() + _cache_ttl(), tz=timezone.utc)
        ).isoformat(),
        "candidate_count": len(placeable),
        "candidates": placeable[:MAX_ENTITIES_PER_SOURCE],
    }
    return snapshot


# ---------------------------------------------------------------------------
# Manifest assembly + public tool
# ---------------------------------------------------------------------------


def _audit(
    tool: str, *, success: bool, summary: str, extra: dict[str, Any] | None = None
) -> None:
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=True,
            success=success,
            changed=False,
            summary=_sanitize(summary, 300),
            extra=extra or {},
        )
    except Exception:  # noqa: BLE001, S110 - audit must never break the call
        pass


def _envelope(
    *,
    tool: str,
    sources: list[str],
    queried: list[str],
    warnings: list[str],
    entities: list[dict[str, Any]],
    cache: dict[str, Any],
    limit: int,
    trace_id: str,
) -> dict[str, Any]:
    truncated = len(entities) > limit
    visible = entities[:limit]
    return {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "tool": tool,
        "surface": "capability_manifest",
        "trace_id": trace_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count_returned": len(visible),
        "count_total": len(entities),
        "truncated": truncated,
        "sources_queried": queried,
        "sources_allowed": sorted(_allowed_sources()),
        "warnings": warnings,
        "placement_cache": cache,
        "entities": visible,
    }


def hermes_capability_manifest(
    source: str = "",
    include_cache: bool = True,
    limit: int = 100,
    hermes_root: Path | None = None,
) -> str:
    """Query the derived capability manifest (read-only, INV-9, bounded).

    ``source`` one of fabric|fleet|profile|provider (empty = all allowed).
    ``include_cache`` toggles the TTL-bounded derived placement-cache
    snapshot. Returns a bounded JSON envelope. No mutation path exists.
    """
    tool = "hermes_capability_manifest"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    warnings: list[str] = []

    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    sources = [source] if source else list(CAPABILITY_SOURCES)
    if source and source not in CAPABILITY_SOURCES:
        warnings.append(f"unknown source {source!r}")
        sources = []
    queried = [s for s in sources if _source_allowed(s)]
    if len(queried) < len(sources):
        warnings.append("some sources filtered by allowlist")

    entities: list[dict[str, Any]] = []
    for s in queried:
        if s in _SOURCE_READERS:
            entities.extend(_SOURCE_READERS[s](root))

    cache: dict[str, Any] = {}
    if include_cache:
        cache = _placement_cache_get(root, tuple(queried))
        if cache:
            cache = dict(cache)
    else:
        cache = {
            "built_at": "",
            "expires_at": "",
            "candidate_count": 0,
            "candidates": [],
        }

    _audit(
        tool,
        success=True,
        summary=f"capability manifest source={source or 'all'} count={len(entities)}",
        extra={"source": source[:64] or "all", "count": len(entities)},
    )
    return json.dumps(
        _envelope(
            tool=tool,
            sources=sources,
            queried=queried,
            warnings=warnings,
            entities=entities,
            cache=cache,
            limit=limit,
            trace_id=tid,
        ),
        ensure_ascii=False,
        indent=2,
    )
