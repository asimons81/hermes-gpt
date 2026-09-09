"""Deterministic filter-and-score placement over the derived capability manifest.

Implements architecture proposal §8.2 ("Where") / D7 / §17 item 5: a
deterministic, auditable placement decision that filters a work unit's
capability requirement over the derived capability-manifest index (built by
``operator_capability_manifest``), then soft-scores the survivors.

**Phase 2 — dry-run only.** This slice never assigns, never dispatches, never
mutates a Mission or a plan node's lifecycle, and never writes outside its own
allowlisted surface. The decision is *recorded* (``score_breakdown`` +
``candidate_set`` + ``filter_optouts``) both in the returned envelope and, when
the operator policy allows a direct write, in a durable ``placement_decisions``
row in the same ``missions/missions.db`` as the Mission runtime.

Determinism + auditability (D7):
- No LLM in the scoring hot path. Only bounded arithmetic over manifest data.
- The same inputs always produce the same output; iteration is always over
  sorted keys, and ties break on ``(authorization_excess asc, entity_id asc)``.
- ``assigned_agent="auto"`` is kept distinct from the stage/plan node ``owner``;
  the controller proposes, actual dispatch goes through the existing
  ``hermes_contract_dispatch`` / delegation surfaces (authority never overridden).

Invariants:
- **INV-9 data containment.** Only refs, bounded metadata, and content-addressed
  hashes cross the surface. No raw prompt, transcript, memory body, credential,
  or secret-path content is ever emitted; a redaction check rejects
  secret-like requirement/context input.
- **No candidate passes ⇒ ``no_capable_target``** classification (escalate path
  stubbed: a bounded ``would_escalate`` signal, never an actual action).
- Every public call is audited (bounded summary + counts only).

Conventions mirror ``operator_mission_plan`` / ``operator_mission_budget``/
``operator_capability_manifest``: public functions return a JSON ``str`` of a
bounded envelope, read surfaces require ``read_only``, and decision recording
requires ``workspace`` + ``direct`` (dry-run-first).

The pure scoring core (``score_targets`` / ``_apply_hard_filters`` /
``_evaluate_score``) is I/O-free and directly testable with in-memory targets.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_capability_manifest as cm
import operator_mission_budget as budget
import operator_mission_runtime as mission
import operator_policy as op

SCHEMA_VERSION = "0.9-placement.1"
PLACEMENT_SCHEMA = "hermes.placement/v1"
DECISION_SCHEMA = "hermes.placement-decision/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Authorization classes in increasing rank (self-contained; matches the plan /
# fabric vocabulary: none < read_only < reversible_write < high_impact).
AUTH_ORDER = ("read_only", "reversible_write", "high_impact")
AUTH_RANK = {name: i for i, name in enumerate(AUTH_ORDER)}
AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")

# Candidate executor kinds that can directly run a work unit.
EXECUTOR_KINDS = ("profile", "fabric_node")

# Soft-score weights (default). Must sum to 1.0 over the supported dimensions.
WEIGHTS: dict[str, float] = {
    "capability_fit": 0.30,
    "authorization_match": 0.20,
    "affinity": 0.15,
    "load_headroom": 0.15,
    "health": 0.10,
    "cost_priority": 0.10,
}

# Classification outcomes.
CLASS_ASSIGNED = "assigned"
CLASS_NO_TARGET = "no_capable_target"
CLASS_HUMAN = "human_approval"
CLASSES = (CLASS_ASSIGNED, CLASS_NO_TARGET, CLASS_HUMAN)

# Bounds.
MAX_SKILLS = 32
MAX_FEATURES = 64
MAX_TARGETS = 256
MAX_STRING = 128
_MISSION_MAX = 68
_NODE_MAX = 64

# PII-ish tokens stripped from free-text summaries (mirrors operator_events).
_PII_STRIP = __import__("re").compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(text: Any, limit: int = MAX_STRING) -> str:
    if text is None:
        return ""
    value = " ".join(str(text).split())
    value = _PII_STRIP.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[:limit] + "…[truncated]"
    return value


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _clean_str_list(
    value: Any, *, field: str, maximum_items: int, item_max: int
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{field} must be a list (<= {maximum_items})")
    out: list[str] = []
    for item in value:
        item = _clean_text(item, field=field, maximum=item_max, required=True)
        if item not in out:
            out.append(item)
    return out


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_placement_tables(db: sqlite3.Connection) -> None:
    """Additive placement table. IF NOT EXISTS, coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS placement_decisions (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            classification TEXT NOT NULL,
            top_candidate TEXT NOT NULL DEFAULT '',
            assigned_agent TEXT NOT NULL DEFAULT '',
            decision_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_placement_mission ON placement_decisions(mission_id, updated_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_placement_tables(db)
        return db
    return mission._connect(path, write=False)


def _begin_write(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    mission_id: str = "",
    node_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id} node={node_id}",
            extra={"mission_id": mission_id, "node_id": node_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


# ---------------------------------------------------------------------------
# Instrument the pure scoring core
# ---------------------------------------------------------------------------


def _normalize_auth_class(value: Any) -> str:
    klass = str(value or "").strip().lower()
    if klass not in AUTH_CLASSES:
        raise ValueError(f"authorization_class must be one of {list(AUTH_CLASSES)}")
    return klass


def _stance(requirement: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical, bounded ``requirement`` for a work unit.

    Accepts a plan-node ``capability_req`` (profile / skills /
    authorization_class) plus optional ``features`` / ``workspace`` /
    ``backends`` and the node's bounded ``budget`` / ``kind`` / ``owner``.
    """
    profile = _clean_text(
        requirement.get("profile"), field="profile", maximum=64, required=True
    )
    if profile not in ("owner", "tony"):
        op.validate_profile_name(profile)
    skills = _clean_str_list(
        requirement.get("skills"),
        field="skills",
        maximum_items=MAX_SKILLS,
        item_max=128,
    )
    auth_class = _normalize_auth_class(
        requirement.get("authorization_class", "reversible_write")
    )
    features = _clean_str_list(
        requirement.get("features"),
        field="features",
        maximum_items=MAX_FEATURES,
        item_max=128,
    )
    workspace = _clean_text(
        requirement.get("workspace"), field="workspace", maximum=128
    )
    backends = _clean_str_list(
        requirement.get("backends"), field="backends", maximum_items=64, item_max=64
    )
    kind = _clean_text(requirement.get("kind"), field="kind", maximum=16) or "single"
    owner = _clean_text(requirement.get("owner"), field="owner", maximum=64)
    raw_budget = requirement.get("budget") or {}
    if not isinstance(raw_budget, dict):
        raise TypeError("budget must be an object")
    minutes = int(raw_budget.get("est_minutes", 0) or 0)
    tokens = int(raw_budget.get("est_tokens", 0) or 0)
    budget_req = {
        "est_minutes": max(0, min(minutes, 100_000)),
        "est_tokens": max(0, min(tokens, 100_000_000)),
    }
    stance = {
        "profile": _sanitize(profile, MAX_STRING),
        "skills": skills,
        "authorization_class": auth_class,
        "features": features,
        "workspace": workspace,
        "backends": backends,
        "kind": kind,
        "owner": _sanitize(owner, MAX_STRING),
        "budget": budget_req,
    }
    if op.redact_output(json.dumps(stance)) != json.dumps(stance):
        raise PermissionError(
            "placement requirement contains secret-like durable values"
        )
    return stance


def _target_from_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Normalize one capability-manifest entity into a placement target.

    Fields that are not available for a kind are carried as neutral defaults so
    filters/scores degrade deterministically (and mark ``*_unavailable`` flags)
    rather than failing open or breaking determinism.
    """
    kind = entity.get("entity_kind", "")
    name = _sanitize(entity.get("name", ""), MAX_STRING)
    if kind == "fabric_node":
        enabled = bool(entity.get("enabled"))
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": enabled,
            "reachable": enabled,
            "identity_configured": enabled,
            "authorization_ceiling": _sanitize(
                entity.get("authorization_ceiling", ""), 64
            ),
            "allowed_profiles": _clean_str_list(
                entity.get("allowed_profiles"),
                field="allowed_profiles",
                maximum_items=64,
                item_max=128,
            ),
            "features": _clean_str_list(
                entity.get("features"), field="features", maximum_items=64, item_max=128
            ),
            "workspaces": _clean_str_list(
                entity.get("workspaces", []),
                field="workspaces",
                maximum_items=64,
                item_max=128,
            ),
            "backends": _clean_str_list(
                entity.get("backends", []),
                field="backends",
                maximum_items=64,
                item_max=64,
            ),
            "skills": [],
            "model": "",
            "provider": "",
            "host_role": "",
            "allow_public_actions": False,
        }
    if kind == "fleet_peer":
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(entity.get("identity_configured")),
            "identity_configured": bool(entity.get("identity_configured")),
            "authorization_ceiling": _sanitize(
                entity.get("authorization_ceiling", ""), 64
            ),
            "allowed_profiles": _clean_str_list(
                entity.get("allowed_profiles"),
                field="allowed_profiles",
                maximum_items=64,
                item_max=128,
            ),
            "features": [],
            "workspaces": [],
            "backends": [],
            "skills": [],
            "model": "",
            "provider": "",
            "host_role": _sanitize(entity.get("host_role", ""), MAX_STRING),
            "allow_public_actions": bool(entity.get("allow_public_actions")),
        }
    if kind == "profile":
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(
                entity.get("model") or entity.get("skills") or entity.get("skill_count")
            ),
            "identity_configured": True,
            "authorization_ceiling": "",
            "allowed_profiles": [name],
            "features": [],
            "workspaces": ["host"],
            "backends": ["local"],
            "skills": _clean_str_list(
                entity.get("skills", []),
                field="skills",
                maximum_items=MAX_SKILLS,
                item_max=128,
            ),
            "model": _sanitize(entity.get("model", ""), MAX_STRING),
            "provider": _sanitize(entity.get("provider", ""), MAX_STRING),
            "host_role": "worker",
            "allow_public_actions": False,
        }
    if kind == "provider":
        profiles = _clean_str_list(
            entity.get("profiles", []), field="profiles", maximum_items=64, item_max=128
        )
        return {
            "entity_id": _sanitize(entity.get("entity_id", ""), MAX_STRING),
            "kind": kind,
            "name": name,
            "enabled": True,
            "reachable": bool(profiles),
            "identity_configured": bool(profiles),
            "authorization_ceiling": "",
            "allowed_profiles": profiles,
            "features": [],
            "workspaces": [],
            "backends": ["remote"],
            "skills": [],
            "model": "",
            "provider": name,
            "host_role": "provider",
            "allow_public_actions": False,
        }
    raise ValueError(f"unknown manifest entity_kind {kind!r}")


def load_manifest_targets(
    hermes_root: Path | None,
    *,
    source: str = "",
    limit: int = MAX_TARGETS,
) -> list[dict[str, Any]]:
    """Read the derived capability manifest and normalize to placement targets.

    Read-only. Respects the manifest's per-source allowlist env. Returns
    targets sorted by ``entity_id`` for determinism.
    """
    root = cm._resolve_root(hermes_root)
    readers = {
        "fabric": cm._read_fabric_entities,
        "fleet": cm._read_fleet_entities,
        "profile": cm._read_profile_entities,
        "provider": cm._read_provider_entities,
    }
    sources = [source] if source else list(readers.keys())
    if source and source not in readers:
        raise ValueError(f"unknown manifest source {source!r}")
    targets: list[dict[str, Any]] = []
    for src in sources:
        if not cm._source_allowed(src):
            continue
        for ent in readers[src](root):
            try:
                targets.append(_target_from_entity(ent))
            except ValueError:
                continue
            if len(targets) >= MAX_TARGETS:
                break
        if len(targets) >= MAX_TARGETS:
            break
    return sorted(targets, key=lambda t: t["entity_id"])


# ---------------------------------------------------------------------------
# Hard filters (any fail => excluded; reason recorded)
# ---------------------------------------------------------------------------


def _auth_rank_of(target: dict[str, Any]) -> int | None:
    ceiling = target.get("authorization_ceiling", "")
    if not ceiling:
        return None
    return AUTH_RANK.get(ceiling)


def _apply_hard_filters(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> list[str]:
    """Return the list of hard-filter optout codes for a target (empty = pass).

    Codes (stable, testable):
    - disabled / unreachable / identity_unconfigured
    - required_features_missing
    - auth_ceiling_exceeded
    - profile_out_of_scope
    - workspace_out_of_scope
    - concurrency_full
    - forbidden_action
    """
    optouts: list[str] = []

    # F1 enabled + reachable + identity.
    if not target.get("enabled"):
        optouts.append("disabled")
    if not target.get("reachable"):
        optouts.append("unreachable")
    if not target.get("identity_configured"):
        optouts.append("identity_unconfigured")

    # F2 required features present.
    required_features = requirement.get("features", [])
    if required_features:
        target_features = set(target.get("features") or [])
        if not set(required_features) <= target_features:
            optouts.append("required_features_missing")

    # F3 authorization ceiling not exceeded.
    ceiling_rank = _auth_rank_of(target)
    required_rank = AUTH_RANK[
        requirement.get("authorization_class", "reversible_write")
    ]
    if ceiling_rank is not None and ceiling_rank < required_rank:
        optouts.append("auth_ceiling_exceeded")

    # F4: the profile must be in scope.
    required_profile = requirement.get("profile", "")
    if required_profile and required_profile not in (
        target.get("allowed_profiles") or []
    ):
        optouts.append("profile_out_of_scope")

    # F5 workspace in scope (known workspaces only; unavailable passes with a flag).
    required_workspace = requirement.get("workspace", "")
    if required_workspace:
        workspaces = target.get("workspaces") or []
        if workspaces and required_workspace not in workspaces:
            optouts.append("workspace_out_of_scope")

    # F6 per-profile concurrency headroom.
    concurrency = ctx.get("concurrency", {})
    key = target.get("name") or target.get("entity_id")
    entry = concurrency.get(key) or concurrency.get(target.get("profile", ""))
    if isinstance(entry, dict):
        cap = float(entry.get("max_in_progress", 0))
        running = float(entry.get("in_progress", 0) or 0)
        if cap > 0 and running >= cap:
            optouts.append("concurrency_full")

    # F7 forbidden-action policy.
    forbidden = ctx.get("forbidden_actions") or []
    if "public" in forbidden and target.get("allow_public_actions"):
        optouts.append("forbidden_action")
    return optouts


# ---------------------------------------------------------------------------
# Soft scores (0..1 among survivors)
# ---------------------------------------------------------------------------


def _score_capability_fit(target: dict[str, Any], requirement: dict[str, Any]) -> float:
    features = requirement.get("features", [])
    if features:
        have = set(target.get("features") or [])
        return round(len(have & set(features)) / len(features), 6)
    # No explicit feature requirement: an executor kind is position-capable.
    return 1.0 if target.get("kind") in EXECUTOR_KINDS else 0.4


def _score_authorization_match(
    target: dict[str, Any], requirement: dict[str, Any]
) -> float:
    ceiling_rank = _auth_rank_of(target)
    required_rank = AUTH_RANK[
        requirement.get("authorization_class", "reversible_write")
    ]
    if ceiling_rank is None:
        return 0.5  # ceiling unknown -> neutral (recorded in caveats)
    excess = ceiling_rank - required_rank
    if excess <= 0:
        return 1.0  # just enough
    return round(max(0.0, 1.0 - 0.4 * excess), 6)


def _score_affinity(target: dict[str, Any], requirement: dict[str, Any]) -> float:
    skills = requirement.get("skills", [])
    if skills:
        have = set(target.get("skills") or [])
        return round(len(have & set(skills)) / len(skills), 6)
    # No explicit skill requirement: reward the manifest owner of this scope.
    return 1.0 if target.get("name") == requirement.get("profile") else 0.5


def _score_load_headroom(target: dict[str, Any], ctx: dict[str, Any]) -> float:
    entry = ctx.get("concurrency", {}).get(target.get("name")) or ctx.get(
        "concurrency", {}
    ).get(target.get("entity_id"))
    if not isinstance(entry, dict):
        return 1.0  # no concurrency data -> assume idle (recorded)
    cap = float(entry.get("max_in_progress", 0) or 0)
    running = float(entry.get("in_progress", 0) or 0)
    if cap <= 0:
        return 1.0  # unlimited
    return round(max(0.0, 1.0 - running / cap), 6)


def _score_health(target: dict[str, Any], ctx: dict[str, Any]) -> float:
    entry = ctx.get("health", {}).get(target.get("name")) or ctx.get("health", {}).get(
        target.get("entity_id")
    )
    if not isinstance(entry, dict) or float(entry.get("samples", 0) or 0) <= 0:
        return 0.5  # no reputation -> neutral
    rate = float(entry.get("success_rate", 0.0) or 0.0)
    return round(max(0.0, min(1.0, rate)), 6)


def _score_cost_priority(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> float:
    bctx = ctx.get("budget")
    if not isinstance(bctx, dict):
        return 1.0  # no budget constraint -> no cost pressure
    est = int(requirement.get("budget", {}).get("est_tokens", 0) or 0)
    remaining = float(bctx.get("remaining_tokens") or 0.0)
    if est <= 0:
        return 1.0
    if remaining >= est:
        budget_fit = 1.0
    else:
        budget_fit = round(max(0.0, remaining / est), 6)
    priority = int(ctx.get("priority", 0) or 0)
    priority_norm = round(min(1.0, max(0.0, priority / 9.0)), 6)
    # A high-priority, budget-fitting candidate is worth the spend.
    return round(budget_fit * (0.5 + 0.5 * priority_norm), 6)


# ---------------------------------------------------------------------------
# Deterministic verdict
# ---------------------------------------------------------------------------


def _score_dims(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, float]:
    return {
        "capability_fit": _score_capability_fit(target, requirement),
        "authorization_match": _score_authorization_match(target, requirement),
        "affinity": _score_affinity(target, requirement),
        "load_headroom": _score_load_headroom(target, ctx),
        "health": _score_health(target, ctx),
        "cost_priority": _score_cost_priority(target, requirement, ctx),
    }


def _weigh(scores: dict[str, float], weights: dict[str, float]) -> float:
    total = 0.0
    for dim, weight in weights.items():
        total += weight * scores.get(dim, 0.0)
    return round(total, 6)


def _normalize_weights(raw: dict[str, Any] | None) -> dict[str, float]:
    weights = dict(WEIGHTS)
    if isinstance(raw, dict):
        for key in weights:
            if key in raw:
                try:
                    value = float(raw[key])
                except (TypeError, ValueError):
                    raise ValueError(f"weight {key} must be a number") from None
                if value < 0:
                    raise ValueError(f"weight {key} must be non-negative")
                weights[key] = value
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    # Never return a zero total (would collapse ordering); deterministic scale.
    return weights


def _caveats(
    target: dict[str, Any], requirement: dict[str, Any], ctx: dict[str, Any]
) -> list[str]:
    caveats: list[str] = []
    if _auth_rank_of(target) is None:
        caveats.append("authorization_ceiling_unavailable")
    if requirement.get("workspace") and not (target.get("workspaces") or []):
        caveats.append("workspace_scope_unavailable")
    if not ctx.get("concurrency"):
        caveats.append("load_headroom_unavailable")
    if not ctx.get("health"):
        caveats.append("health_unavailable")
    return caveats


def score_targets(
    requirement: dict[str, Any],
    targets: list[dict[str, Any]],
    ctx: dict[str, Any] | None = None,
    *,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic filter-and-score over candidate targets.

    ``requirement`` is a bounded work-unit requirement (see ``_stance``);
    ``targets`` are normalized manifest targets (``_target_from_entity``);
    ``ctx`` optionally carries ``concurrency`` / ``health`` / ``budget`` /
    ``priority`` / ``forbidden_actions`` / ``weights``. Pure and I/O-free.

    Returns: classification, assigned_agent, top_candidate, candidate_set,
    score_breakdown, filter_optouts.
    """
    req = dict(requirement)
    if "profile" not in req:
        req = _stance(requirement)
    ctx = dict(ctx or {})
    wts = _normalize_weights(ctx.get("weights") or weights)

    # Sort targets for determinism.
    ordered = sorted(targets, key=lambda t: t.get("entity_id", ""))
    optouts: dict[str, list[str]] = {}
    survivors: list[dict[str, Any]] = []
    for target in ordered:
        codes = _apply_hard_filters(target, req, ctx)
        if codes:
            optouts[target.get("entity_id", "")] = codes
        else:
            survivors.append(target)

    breakdown: dict[str, dict[str, Any]] = {}
    scored: list[dict[str, Any]] = []
    for target in survivors:
        dims = _score_dims(target, req, ctx)
        total = _weigh(dims, wts)
        ceiling_rank = _auth_rank_of(target)
        required_rank = AUTH_RANK[req.get("authorization_class", "reversible_write")]
        excess = (ceiling_rank - required_rank) if ceiling_rank is not None else None
        entry = {
            "entity_id": target.get("entity_id", ""),
            "name": target.get("name", ""),
            "kind": target.get("kind", ""),
            "capability_sha256": _sanitize(target.get("capability_sha256", ""), 64),
            "scores": dims,
            "total": total,
            "authorization_excess": excess if excess is not None else None,
            "caveats": _caveats(target, req, ctx),
        }
        entry["_tie"] = excess if excess is not None else 9999
        breakdown[target.get("entity_id", "")] = entry
        scored.append(entry)

    # Deterministic rank: total desc, authorization_excess asc, entity_id asc.
    scored.sort(key=lambda e: (-e["total"], e["_tie"], e["entity_id"]))
    for e in scored:
        e.pop("_tie", None)

    top = scored[0] if scored else None
    agent = "auto" if top else ""
    if req.get("kind") == "approval":
        classification = CLASS_HUMAN
        agent = "owner"
    elif top is None:
        classification = CLASS_NO_TARGET
    else:
        classification = CLASS_ASSIGNED

    return {
        "classification": classification,
        "assigned_agent": agent,
        "top_candidate": top,
        "candidate_set": scored,  # survivors with scores
        "score_breakdown": breakdown,
        "filter_optouts": optouts,
    }


def _decision_digest(
    mission_id: str, node_id: str, requirement: dict[str, Any], verdict: dict[str, Any]
) -> str:
    skeleton = {
        "mission_id": mission_id,
        "node_id": node_id,
        "requirement": requirement,
        "classification": verdict["classification"],
        "assigned_agent": verdict["assigned_agent"],
        "top_candidate": verdict["top_candidate"],
        "candidate_set": verdict["candidate_set"],
        "filter_optouts": verdict["filter_optouts"],
    }
    enc = json.dumps(
        skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def build_decision(
    mission_id: str,
    node_id: str,
    requirement: dict[str, Any],
    targets: list[dict[str, Any]],
    ctx: dict[str, Any] | None = None,
    *,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete, deterministic placement decision envelope (dry-run)."""
    req = _stance(requirement)
    verdict = score_targets(req, targets, ctx, weights=weights)
    digest = _decision_digest(mission_id, node_id, req, verdict)
    escalate = None
    if verdict["classification"] == CLASS_NO_TARGET:
        escalate = {
            "would_raise_signal": "capability",
            "classification": CLASS_NO_TARGET,
            "path": "stubbed",
            "note": "no_capable_target: candidate set empty after hard filters; escalate to Orchestrator (never auto-resolve).",
        }
    return {
        "schema": DECISION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_placement_score",
        "mission_id": mission_id,
        "node_id": node_id,
        "requirement": req,
        "classification": verdict["classification"],
        "assigned_agent": verdict["assigned_agent"],
        "owner": req.get("owner", ""),
        "assigned_agent_note": "auto is distinct from stage owner; the controller proposes, dispatch goes through existing authority",
        "top_candidate": verdict["top_candidate"],
        "candidate_set": verdict["candidate_set"],
        "score_breakdown": verdict["score_breakdown"],
        "filter_optouts": verdict["filter_optouts"],
        "decision_sha256": digest,
        "would_assign": False,
        "dry_run": True,
        "escalate": escalate,
        "generated_at": _now(),
    }


# ---------------------------------------------------------------------------
# Read a plan node's requirement (reuse the plan store)
# ---------------------------------------------------------------------------


def _read_node_requirement(
    db: sqlite3.Connection, mission_id: str, node_id: str
) -> dict[str, Any]:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    if not NODE_ID_RE.fullmatch(node_id):
        raise ValueError("node_id is invalid")
    row = db.execute(
        "SELECT capability_req, budget, state FROM plan_nodes WHERE mission_id=? AND node_id=?",
        (mission_id, node_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"plan node {node_id!r} not found for mission {mission_id!r}")
    cap_req = json.loads(row["capability_req"] or "{}")
    budget_req = json.loads(row["budget"] or "{}")
    return {
        "profile": cap_req.get("profile", ""),
        "skills": cap_req.get("skills", []),
        "authorization_class": cap_req.get("authorization_class", "reversible_write"),
        "budget": budget_req,
    }


def _read_mission_priority(db: sqlite3.Connection, mission_id: str) -> int:
    try:
        row = db.execute(
            "SELECT spec_json FROM missions WHERE mission_id=?", (mission_id,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        spec = json.loads(row["spec_json"])
    except (ValueError, TypeError):
        return 0
    priority = spec.get("priority", 0) if isinstance(spec, dict) else 0
    try:
        return max(0, min(int(priority), 9))
    except (TypeError, ValueError):
        return 0


def _read_budget_context(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    try:
        acct = budget._read_account(db, mission_id)
    except (LookupError, ValueError, sqlite3.Error):
        return {}
    env = acct.get("envelope", {})
    return {
        "remaining_tokens": float(env.get("remaining", 0.0) or 0.0),
        "quota_tokens": float(acct.get("quota", 0.0) or 0.0),
        "unit": acct.get("unit", ""),
        "crosses_envelope": bool(env.get("crosses_envelope")),
    }


# ---------------------------------------------------------------------------
# Durable decision read/write
# ---------------------------------------------------------------------------


def _placement_table_exists(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='placement_decisions'"
    ).fetchone()
    return row is not None


def _upsert_decision(db: sqlite3.Connection, decision: dict[str, Any]) -> None:
    now = _now()
    top = decision.get("top_candidate") or {}
    db.execute(
        """
        INSERT INTO placement_decisions
            (mission_id, node_id, decision_json, classification, top_candidate,
             assigned_agent, decision_sha256, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mission_id, node_id) DO UPDATE SET
            decision_json=excluded.decision_json,
            classification=excluded.classification,
            top_candidate=excluded.top_candidate,
            assigned_agent=excluded.assigned_agent,
            decision_sha256=excluded.decision_sha256,
            updated_at=excluded.updated_at
        """,
        (
            decision["mission_id"],
            decision["node_id"],
            json.dumps(decision, ensure_ascii=False, sort_keys=True),
            decision["classification"],
            top.get("entity_id", ""),
            decision["assigned_agent"],
            decision["decision_sha256"],
            now,
            now,
        ),
    )


def _decision_view(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["decision_json"])


# ---------------------------------------------------------------------------
# Public MCP tools
# ---------------------------------------------------------------------------


def hermes_placement_score(
    mission_id: str,
    node_id: str,
    *,
    source: str = "",
    features: str = "",
    workspace: str = "",
    backends: str = "",
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Score + record a plan node's placement (deterministic, dry-run-first).

    Reads the plan node's capability requirement and the derived capability
    manifest, applies the materialized hard filters, soft-scores the survivors,
    and records the decision (``score_breakdown`` + ``candidate_set`` +
    ``filter_optouts``). ``assigned_agent="auto"`` is never a dispatch — this
    slice is dry-run only and returns ``would_assign=False``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        if not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct placement record requires confirm=true")

        path = _db_path(hermes_root)
        with _connect(path, write=False) as db:
            base = _read_node_requirement(db, mission_id, node_id)
            priority = _read_mission_priority(db, mission_id)
            budget_ctx = _read_budget_context(db, mission_id)

        targets = load_manifest_targets(hermes_root, source=source)
        overrides: dict[str, Any] = {}
        if features:
            overrides["features"] = _clean_str_list(
                [f for f in features.split(",") if f.strip()],
                field="features",
                maximum_items=MAX_FEATURES,
                item_max=128,
            )
        if workspace:
            overrides["workspace"] = _clean_text(
                workspace, field="workspace", maximum=128
            )
        if backends:
            overrides["backends"] = _clean_str_list(
                [b for b in backends.split(",") if b.strip()],
                field="backends",
                maximum_items=64,
                item_max=64,
            )
        requirement = dict(base)
        requirement.update(overrides)
        node_def = _node_def(path, mission_id, node_id)
        requirement["kind"] = node_def["kind"]
        requirement["owner"] = node_def["owner"]

        ctx = {"priority": priority}
        if budget_ctx:
            ctx["budget"] = budget_ctx

        decision = build_decision(mission_id, node_id, requirement, targets, ctx)

        if effective_dry:
            _audit(
                "hermes_placement_score",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                node_id=node_id,
                extra={
                    "classification": decision["classification"],
                    "assigned_agent": decision["assigned_agent"],
                    "candidate_count": len(decision["candidate_set"]),
                    "optout_count": len(decision["filter_optouts"]),
                    "decision_sha256": decision["decision_sha256"],
                },
            )
            return json.dumps(decision, ensure_ascii=False, indent=2)

        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists
            _upsert_decision(db, decision)
            db.commit()
            persisted = {"node_id": node_id, "mission_id": mission_id}

        _audit(
            "hermes_placement_score",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            node_id=node_id,
            extra={
                "classification": decision["classification"],
                "assigned_agent": decision["assigned_agent"],
                "candidate_count": len(decision["candidate_set"]),
                "optout_count": len(decision["filter_optouts"]),
                "decision_sha256": decision["decision_sha256"],
            },
        )
        decision["changed"] = True
        decision["dry_run"] = False
        decision["persisted"] = persisted
        return json.dumps(decision, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        _audit(
            "hermes_placement_score",
            op.OperatorPolicy(),
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
        )
        return _error(
            exc,
            "PLACEMENT_SCORE_REJECTED",
            "Check mission/node id, manifest source, and Operator workspace/direct policy.",
        )


def _node_def(path: Path, mission_id: str, node_id: str) -> dict[str, str]:
    """Derive a plan node's ``kind``/``owner`` from the stored plan JSON (read-only)."""
    default = {"kind": "single", "owner": ""}
    try:
        with _connect(path, write=False) as db:
            row = db.execute(
                "SELECT plan_json FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
    except sqlite3.Error:
        return default
    if row is None:
        return default
    try:
        plan_doc = json.loads(row["plan_json"])
    except (ValueError, TypeError):
        return default
    for node in plan_doc.get("nodes", []) or []:
        if node.get("node_id") == node_id:
            return {
                "kind": _clean_text(
                    node.get("kind", "single"), field="kind", maximum=16
                )
                or "single",
                "owner": _clean_text(node.get("owner", ""), field="owner", maximum=64),
            }
    return default


def hermes_placement_candidates(
    profile: str,
    *,
    skills: str = "",
    authorization_class: str = "reversible_write",
    features: str = "",
    workspace: str = "",
    backends: str = "",
    source: str = "",
    hermes_root: Path | None = None,
) -> str:
    """Read-only probe: list candidate targets + per-candidate filter/score.

    Never records, never writes. Enforces ``read_only``. Useful as the
    transparent, deterministic first step before any decision is recorded.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        requirement: dict[str, Any] = {
            "profile": _clean_text(profile, field="profile", maximum=64, required=True),
            "skills": _clean_str_list(
                [s for s in skills.split(",") if s.strip()],
                field="skills",
                maximum_items=MAX_SKILLS,
                item_max=128,
            ),
            "authorization_class": _normalize_auth_class(authorization_class),
        }
        if features:
            requirement["features"] = _clean_str_list(
                [f for f in features.split(",") if f.strip()],
                field="features",
                maximum_items=MAX_FEATURES,
                item_max=128,
            )
        if workspace:
            requirement["workspace"] = _clean_text(
                workspace, field="workspace", maximum=128
            )
        if backends:
            requirement["backends"] = _clean_str_list(
                [b for b in backends.split(",") if b.strip()],
                field="backends",
                maximum_items=64,
                item_max=64,
            )
        targets = load_manifest_targets(hermes_root, source=source)
        verdict = score_targets(_stance(requirement), targets, {})
        envelope = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "surface": "placement_candidates",
            "tool": "hermes_placement_candidates",
            "requirement": _stance(requirement),
            "classification": verdict["classification"],
            "candidate_set": verdict["candidate_set"],
            "score_breakdown": verdict["score_breakdown"],
            "filter_optouts": verdict["filter_optouts"],
            "top_candidate": verdict["top_candidate"],
            "count_candidates": len(verdict["candidate_set"]),
            "count_filtered": len(verdict["filter_optouts"]),
            "count_total": len(targets),
        }
        _audit(
            "hermes_placement_candidates",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            extra={
                "profile": profile[:64],
                "classification": verdict["classification"],
                "candidates": len(verdict["candidate_set"]),
                "filtered": len(verdict["filter_optouts"]),
            },
        )
        return json.dumps(envelope, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        return _error(
            exc,
            "PLACEMENT_CANDIDATES_FAILED",
            "Check profile/authorization_class and Operator read access.",
        )


def hermes_placement_get(
    mission_id: str, node_id: str, *, hermes_root: Path | None = None
) -> str:
    """Read a recorded placement decision (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        if not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
            )
        with _connect(path, write=False) as db:
            if not _placement_table_exists(db):
                return json.dumps(
                    {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
                )
            row = db.execute(
                "SELECT * FROM placement_decisions WHERE mission_id=? AND node_id=?",
                (mission_id, node_id),
            ).fetchone()
        if row is None:
            return json.dumps(
                {"success": True, "schema_version": SCHEMA_VERSION, "found": False}
            )
        view = _decision_view(row)
        view["success"] = True
        view["found"] = True
        _audit(
            "hermes_placement_get",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
            extra={"classification": view.get("classification", "")},
        )
        return json.dumps(view, ensure_ascii=False, indent=2)
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc,
            "PLACEMENT_GET_FAILED",
            "Check mission/node id and Operator read access.",
        )


def hermes_placement_list(
    mission_id: str, limit: int = 50, *, hermes_root: Path | None = None
) -> str:
    """List recorded placement decisions for a mission (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "decisions": [],
                    "count": 0,
                }
            )
        with _connect(path, write=False) as db:
            if not _placement_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "decisions": [],
                        "count": 0,
                    }
                )
            rows = db.execute(
                "SELECT * FROM placement_decisions WHERE mission_id=? ORDER BY updated_at DESC, node_id ASC LIMIT ?",
                (mission_id, limit),
            ).fetchall()
        decisions = []
        for row in rows:
            view = _decision_view(row)
            view.pop("candidate_set", None)
            view.pop("score_breakdown", None)
            view.pop("filter_optouts", None)
            decisions.append(view)
        _audit(
            "hermes_placement_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"count": len(decisions)},
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "decisions": decisions,
                "count": len(decisions),
            },
            ensure_ascii=False,
            indent=2,
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "PLACEMENT_LIST_FAILED", "Check mission id and Operator read access."
        )


# ---------------------------------------------------------------------------
# Pure predicate (exposed for tests / controllers)
# ---------------------------------------------------------------------------


def validate_requirement(requirement: dict[str, Any]) -> bool:
    """Return whether a requirement canonicalizes without raising."""
    try:
        _stance(requirement)
        return True
    except (ValueError, TypeError, PermissionError):
        return False
