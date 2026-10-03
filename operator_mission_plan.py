"""Deterministic MissionPlan (bounded decomposition DAG) for Hermes GPT v0.9.

Implements architecture proposal §17 item 2 (mirrors §5.2 / §6.2 / §4):
an **additive** ``mission_plans`` + ``plan_nodes`` store attached to a Mission,
used to turn a MissionSpec into a bounded, operator-reviewable DAG of work
units. This is the Phase-1 **read-only** slice: the surfaces *read* mission /
plan state and *write plan rows*, but never dispatch work and never mutate a
Mission's lifecycle.

Properties enforced here:

- **Additive, no competing store.** ``mission_plans`` / ``plan_nodes`` live in
  the same ``missions/missions.db`` as the Mission runtime (FK to ``missions``),
  so a Mission keeps one authoritative store.
- **Node state machine (design §5.2).** ``pending -> blockable -> dispatched ->
  running -> awaiting_review -> validated -> awaiting_approval -> completed |
  failed | paused``. Transitions are validated; only the state machine may move a
  node, and it never dispatches or completes a Mission.
- **Deterministic-first decomposition.** ``decompose_mission`` builds the
  bounded DAG from the canonical Swarm shape (reuse of
  ``operator_swarm_workflows``) and Work Contract refs, with no LLM required.
  An operator-provided plan (``use_canonical`` set aside) is accepted verbatim
  after canonicalization.
- **INV-9 data containment.** No raw objective / prompt / transcript is stored
  in ``plan_json`` or ``plan_nodes`` — only ``{objective_len,
  objective_sha256}`` redaction metadata plus refs / hashes / bounded caps.
- **Authority boundary.** Reads require ``read_only``; additive plan writes
  (create / node transition / status) require ``workspace`` + ``direct`` and are
  dry-run-first; they never touch a Mission's status or dispatch a worker.

Every public call is audited (refs / hashes / a bounded summary only).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op
import operator_skill_resolution as skill_resolution
from operator_contract import artifact_constraints
from operator_swarm_workflows import CANONICAL_STAGE_SPECS, DEFAULT_OWNERS

SCHEMA_VERSION = "0.9-plan.1"
PLAN_SCHEMA = "hermes.mission-plan/v1"
NODE_SCHEMA = "hermes.plan-node/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")

KIND_SINGLE = "single"
KIND_PARALLEL = "parallel"
KIND_APPROVAL = "approval"
KINDS = (KIND_SINGLE, KIND_PARALLEL, KIND_APPROVAL)

MAX_NODES = 64
MAX_DEPTH_DEPENDS = 8
MAX_OBJECTIVE_BYTES = 8_000
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BASENAME = 200
MAX_SKILLS = 32
MAX_SKILL_NAME = 128
MAX_BUDGET_MINUTES = 100_000
MAX_BUDGET_TOKENS = 100_000_000
MAX_DECOMP = 64
AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")

# Plan-level review status (operator-reviewable; distinct from a Mission status).
PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_REVIEW = "review"
PLAN_STATUS_APPROVED = "approved"
PLAN_STATUS_REJECTED = "rejected"
PLAN_STATUSES = (PLAN_STATUS_DRAFT, PLAN_STATUS_REVIEW, PLAN_STATUS_APPROVED, PLAN_STATUS_REJECTED)

# Plan node state machine (design §5.2).
NODE_STATES = (
    "pending",
    "blockable",
    "dispatched",
    "running",
    "awaiting_review",
    "validated",
    "awaiting_approval",
    "completed",
    "failed",
    "paused",
)
NODE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"blockable", "dispatched", "failed", "paused"},
    "blockable": {"dispatched", "failed", "paused"},
    "dispatched": {"running", "failed", "paused"},
    "running": {"awaiting_review", "failed", "paused"},
    "awaiting_review": {"validated", "failed", "paused"},
    "validated": {"awaiting_approval", "failed", "paused"},
    "awaiting_approval": {"completed", "failed", "paused"},
    "completed": set(),
    "failed": set(),
    "paused": {"running", "blockable", "failed"},
}
TERMINAL_NODE_STATES = frozenset({"completed", "failed"})


class PlanVersionConflict(ValueError):
    """The plan was replaced (``hermes_plan_create``) after the caller read it.

    Raised by the optional ``expected_plan_version`` compare-and-swap on
    ``hermes_plan_node_transition``. ``hermes_plan_create`` replaces the whole
    plan (and resets every node to ``pending``) with no read-verify-write of its
    own, so an out-of-process writer such as Autopilot must be able to refuse a
    node write that was computed against a plan version that no longer exists.
    """

    def __init__(self, expected: int, actual: int | None):
        super().__init__(f"plan version changed (expected {expected}, found {actual})")
        self.expected = expected
        self.actual = actual


class PlanInFlightError(ValueError):
    """Replacing a plan would orphan an active scheduler or unfinished work."""


@contextlib.contextmanager
def _replacement_lease(mission_id: str, hermes_root: Path | None):
    # Serialize with both schedulers, including the dispatch-before-node-write
    # window. Checking node state alone cannot close that window.
    import operator_controller as controller

    lock = f"plan-replace-{os.getpid()}-{secrets.token_hex(8)}"
    with controller._connect(controller._db_path(hermes_root), write=True) as db:
        lease = controller.acquire_lease(db, mission_id, "operator_request", ttl=60, lease_lock=lock)
        if not lease.get("acquired"):
            raise PlanInFlightError("A scheduler pass is active; stop and drain work before replacing the plan")
        try:
            yield
        finally:
            controller.release_lease(db, mission_id, lock)


def _assert_replaceable(db: sqlite3.Connection, mission_id: str, hermes_root: Path | None) -> None:
    import operator_autopilot as autopilot

    active = db.execute(
        "SELECT node_id FROM plan_nodes WHERE mission_id=? AND state IN "
        "('dispatched','running','awaiting_review','validated','awaiting_approval','paused') LIMIT 1",
        (mission_id,),
    ).fetchone()
    run = autopilot._read_run(mission_id, hermes_root)
    if active or (run and run.get("state") not in autopilot.TERMINAL_STATES):
        raise PlanInFlightError("Stop Autopilot and resolve unfinished plan nodes before replacing the plan")
    current = mission._row_to_mission(db, mission._get_row(db, mission_id))
    root = mission._root(hermes_root)
    for attachment in current["attachments"]:
        if attachment["kind"] == "delegation":
            state, _, _ = mission._delegation_state(root, mission_id, attachment)
            if state not in ("succeeded", "failed", "cancelled"):
                raise PlanInFlightError("Resolve unfinished delegations before replacing the plan")


# ---------------------------------------------------------------------------
# Time / root / DB helpers (reuse the Mission runtime store)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_plan_tables(db: sqlite3.Connection) -> None:
    """Additive plan tables. IF NOT EXISTS, so coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS mission_plans (
            mission_id TEXT PRIMARY KEY,
            plan_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            plan_sha256 TEXT NOT NULL DEFAULT '',
            decomposition TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS plan_nodes (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            contract_sha256 TEXT NOT NULL DEFAULT '',
            capability_req TEXT NOT NULL DEFAULT '{}',
            budget TEXT NOT NULL DEFAULT '{}',
            deps TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL,
            lease_lock TEXT NOT NULL DEFAULT '',
            lease_expires TEXT NOT NULL DEFAULT '',
            epoch INTEGER NOT NULL DEFAULT 0,
            failure_kind TEXT NOT NULL DEFAULT '',
            retries INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id),
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_plan_nodes_mission_state ON plan_nodes(mission_id, state);
        CREATE INDEX IF NOT EXISTS idx_plans_status ON mission_plans(status, updated_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_plan_tables(db)
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


def _error(
    exc: Exception,
    code: str,
    action: str,
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    return json.dumps(
        op.error_from_exception(
            exc,
            layer="operator",
            code=code,
            suggested_action=action,
            extra=extra,
        )
    )


# ---------------------------------------------------------------------------
# Bounded canonicalization helpers
# ---------------------------------------------------------------------------


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


def _objective_meta(text: str | None) -> dict[str, Any]:
    """Redaction metadata for an objective; never the raw text (INV-9)."""
    if not text:
        return {"objective_len": 0, "objective_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "objective_len": len(data),
        "objective_sha256": hashlib.sha256(data).hexdigest(),
    }


def _clean_profile(value: Any, *, field: str, allow_owner: bool = False) -> str:
    name = _clean_text(value, field=field, maximum=64, required=True)
    if allow_owner and name in ("owner", "tony"):
        return name
    return op.validate_profile_name(name)


def _clean_skills(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SKILLS:
        raise ValueError(f"capability_req.skills must be a list (<= {MAX_SKILLS})")
    out: list[str] = []
    for item in value:
        name = _clean_text(item, field="capability skill", maximum=MAX_SKILL_NAME, required=True)
        if name in out:
            raise ValueError(f"duplicate capability skill {name!r}")
        out.append(name)
    return out


def _clean_capability_req(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("capability_req must be an object")
    profile = _clean_profile(value.get("profile"), field="capability_req.profile")
    skills = _clean_skills(value.get("skills"))
    klass = str(value.get("authorization_class", "reversible_write")).strip().lower()
    if klass not in AUTH_CLASSES:
        raise ValueError(f"capability_req.authorization_class must be one of {list(AUTH_CLASSES)}")
    return {"profile": profile, "skills": skills, "authorization_class": klass}


def _clean_budget(value: Any) -> dict[str, int]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("budget must be an object")
    minutes = int(value.get("est_minutes", 0) or 0)
    tokens = int(value.get("est_tokens", 0) or 0)
    if minutes < 0 or minutes > MAX_BUDGET_MINUTES:
        raise ValueError(f"budget.est_minutes out of range (0..{MAX_BUDGET_MINUTES})")
    if tokens < 0 or tokens > MAX_BUDGET_TOKENS:
        raise ValueError(f"budget.est_tokens out of range (0..{MAX_BUDGET_TOKENS})")
    return {"est_minutes": minutes, "est_tokens": tokens}


def _clean_artifacts(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS:
        raise ValueError(f"expected_artifacts must be a list (<= {MAX_ARTIFACTS})")
    out: list[str] = []
    for item in value:
        name = _clean_text(item, field="expected artifact", maximum=MAX_ARTIFACT_BASENAME, required=True)
        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"expected artifact {name!r} must be a basename")
        out.append(name)
    return out


def _node_contract_signature(node: dict[str, Any]) -> str:
    """Deterministic contract signature for a node (a hash, never raw content)."""
    skeleton = {
        "node_id": node["node_id"],
        "kind": node["kind"],
        "owner": node["owner"],
        "parents": node["parents"],
        "objective_sha256": node["objective_sha256"],
        "capability_req": node["capability_req"],
        "budget": node["budget"],
        "expected_artifacts": node["expected_artifacts"],
    }
    if node.get("artifact_requirements"):
        skeleton["artifact_requirements"] = node["artifact_requirements"]
    enc = json.dumps(skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def _clean_artifact_requirements(value: Any, artifacts: list[str]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARTIFACTS:
        raise ValueError("artifact_requirements must be a bounded list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"path", "min_bytes", "max_bytes", "sha256"}:
            raise ValueError("artifact requirement fields must be path/min_bytes/max_bytes/sha256")
        name = _clean_artifacts([item.get("path")])[0]
        if name not in artifacts or name in seen:
            raise ValueError("artifact requirement must match one unique expected_artifact")
        minimum = item.get("min_bytes", 1)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 2**63 - 1:
            raise ValueError("artifact min_bytes must be a positive bounded integer")
        out.append({"path": name, "min_bytes": minimum, **artifact_constraints(item, minimum)})
        seen.add(name)
    return sorted(out, key=lambda item: item["path"])


def _validate_node_dag(nodes: list[dict[str, Any]], *, uuid: set[str]) -> None:
    """Validate node ids, deps known, no self/dupe, and acyclicity (Kahn)."""
    for idx, node in enumerate(nodes):
        node_id = node["node_id"]
        parents = node["parents"]
        for parent in parents:
            if parent not in uuid:
                raise ValueError(f"node {node_id!r} references unknown parent {parent!r}")
    indegree = {n["node_id"]: 0 for n in nodes}
    edges: dict[str, list[str]] = {n["node_id"]: [] for n in nodes}
    for node in nodes:
        for parent in node["parents"]:
            if parent == node["node_id"]:
                raise ValueError(f"node {node['node_id']!r} cannot be its own parent")
            edges[parent].append(node["node_id"])
            indegree[node["node_id"]] += 1
    queue = [nid for nid in indegree if indegree[nid] == 0]
    ordered: list[str] = []
    while queue:
        cur = queue.pop(0)
        ordered.append(cur)
        for nxt in edges[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(nodes):
        cycle = sorted(nid for nid in indegree if indegree[nid] > 0)
        raise ValueError(f"plan DAG contains a cycle involving nodes: {cycle}")


def _canonical_node(raw: Any) -> dict[str, Any]:
    """Validate + canonicalize one plan node (definition only — no state)."""
    if not isinstance(raw, dict):
        raise ValueError("each plan node must be an object")
    node_id = _clean_text(raw.get("node_id"), field="node_id", maximum=64, required=True)
    if not NODE_ID_RE.fullmatch(node_id):
        raise ValueError(f"node_id {node_id!r} is invalid")
    kind = str(raw.get("kind", KIND_SINGLE)).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"node {node_id!r} kind must be one of {list(KINDS)}")
    owner = _clean_profile(raw.get("owner"), field=f"node {node_id} owner", allow_owner=(kind == KIND_APPROVAL))
    if kind == KIND_APPROVAL and owner not in ("owner", "tony"):
        raise ValueError(f"approval node {node_id!r} owner must be 'owner' or 'tony'")

    parents = raw.get("parents") or []
    if not isinstance(parents, list) or len(parents) > MAX_DEPTH_DEPENDS:
        raise ValueError(f"node {node_id!r} parents must be a list (<= {MAX_DEPTH_DEPENDS})")
    parents = [_clean_text(p, field=f"node {node_id} parent", maximum=64, required=True) for p in parents]
    for p in parents:
        if not NODE_ID_RE.fullmatch(p):
            raise ValueError(f"node {node_id!r} has invalid parent {p!r}")
    if len(set(parents)) != len(parents):
        raise ValueError(f"node {node_id!r} parents contain duplicates")

    # INV-9: never store the raw objective. If raw text is present, hash it;
    # if only hash metadata is present (an already-canonical node), preserve it.
    if raw.get("objective") is not None:
        objective_meta = _objective_meta(raw["objective"])
    else:
        o_sha = _clean_text(raw.get("objective_sha256"), field=f"node {node_id} objective_sha256", maximum=64)
        if o_sha:
            if not SHA_RE.fullmatch(o_sha):
                raise ValueError(f"node {node_id!r} objective_sha256 must be lowercase SHA-256")
            objective_meta = {"objective_len": int(raw.get("objective_len") or 0), "objective_sha256": o_sha}
        else:
            objective_meta = {"objective_len": 0, "objective_sha256": ""}
    cap = _clean_capability_req(raw.get("capability_req"))
    budget = _clean_budget(raw.get("budget"))
    artifacts = _clean_artifacts(raw.get("expected_artifacts"))
    requirements = _clean_artifact_requirements(raw.get("artifact_requirements"), artifacts)
    if kind == KIND_APPROVAL and artifacts:
        raise ValueError(f"approval node {node_id!r} must not declare expected_artifacts")

    contract_ref = _clean_text(raw.get("contract_ref"), field=f"node {node_id} contract_ref", maximum=256)
    if contract_ref and not REF_RE.fullmatch(contract_ref):
        raise ValueError(f"node {node_id!r} contract_ref contains unsupported characters")
    contract_sha = _clean_text(raw.get("contract_sha256"), field=f"node {node_id} contract_sha256", maximum=64)
    if contract_sha and not SHA_RE.fullmatch(contract_sha):
        raise ValueError(f"node {node_id!r} contract_sha256 must be lowercase SHA-256")

    node: dict[str, Any] = {
        "schema": NODE_SCHEMA,
        "node_id": node_id,
        "kind": kind,
        "owner": owner,
        "parents": parents,
        **objective_meta,
        "contract_ref": contract_ref,
        "contract_sha256": contract_sha or _node_contract_signature(
            {
                "node_id": node_id,
                "kind": kind,
                "owner": owner,
                "parents": parents,
                "objective_sha256": objective_meta["objective_sha256"],
                "capability_req": cap,
                "budget": budget,
                "expected_artifacts": artifacts,
                "artifact_requirements": requirements,
            }
        ),
        "capability_req": cap,
        "budget": budget,
        "expected_artifacts": artifacts,
    }
    if requirements:
        node["artifact_requirements"] = requirements
    return node


def _canonical_plan(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a MissionPlan document.

    Returns ``(canonical_json, plan_dict)``. Raises ValueError / PermissionError
    on schema, DAG, cap, or containment violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")
    if raw.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA!r}")

    mission_id = _clean_text(raw.get("mission_id"), field="mission_id", maximum=68, required=True)
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    version = int(raw.get("version", 1) or 1)
    if version < 1:
        raise ValueError("plan version must be >= 1")
    decomposition = _clean_text(raw.get("decomposition") or "operator-provided", field="decomposition", maximum=MAX_DECOMP)

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError("plan nodes must be a non-empty list")
    if len(nodes_raw) > MAX_NODES:
        raise ValueError(f"plan node count {len(nodes_raw)} exceeds cap ({MAX_NODES})")

    if raw.get("objective") is not None:
        mission_meta = _objective_meta(raw["objective"])
    else:
        o_sha = _clean_text(raw.get("objective_sha256"), field="objective_sha256", maximum=64)
        if o_sha:
            if not SHA_RE.fullmatch(o_sha):
                raise ValueError("objective_sha256 must be lowercase SHA-256")
            mission_meta = {"objective_len": int(raw.get("objective_len") or 0), "objective_sha256": o_sha}
        else:
            mission_meta = {"objective_len": 0, "objective_sha256": ""}
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(nodes_raw):
        node = _canonical_node(item)
        if node["node_id"] in seen:
            raise ValueError(f"duplicate node id {node['node_id']!r}")
        seen.add(node["node_id"])
        nodes.append(node)
    _validate_node_dag(nodes, uuid=seen)

    canonical: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mission_id": mission_id,
        "version": version,
        "decomposition": decomposition,
        **mission_meta,
        "nodes": nodes,
    }
    # INV-9: a stored plan must never carry a raw objective / prompt-like field.
    if op.redact_output(json.dumps(canonical)) != json.dumps(canonical):
        raise PermissionError("plan contains secret-like durable values")
    enc = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(enc.encode("utf-8")) > 64_000:
        raise ValueError("canonical plan exceeds 64 KB")
    return enc, canonical


def _validate_plan_skill_requirements(
    plan: dict[str, Any], hermes_root: Path | None
) -> None:
    """Reject required skills that a node's logical profile cannot load."""
    for node in plan.get("nodes", []):
        capability = node.get("capability_req") or {}
        rejection = skill_resolution.validate_required_skills(
            capability.get("profile", ""),
            capability.get("skills", []),
            hermes_root,
        )
        if rejection is not None:
            rejection = dict(rejection)
            rejection["node_id"] = node.get("node_id", "")
            raise skill_resolution.SkillRequirementsError(rejection)


def _plan_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_plan(plan_json: str) -> tuple[str, dict[str, Any], str]:
    if not isinstance(plan_json, str) or not plan_json.strip():
        raise ValueError("plan_json is required")
    try:
        raw = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc
    canonical, plan = _canonical_plan(raw)
    return canonical, plan, _plan_sha256(canonical)


# ---------------------------------------------------------------------------
# Deterministic decomposition (MissionSpec -> bounded DAG; reuse Swarm shape)
# ---------------------------------------------------------------------------


def decompose_mission(
    mission_id: str,
    *,
    objective: str = "",
    owner_profile: str = "default",
    use_canonical: bool = True,
) -> dict[str, Any]:
    """Deterministically decompose a MissionSpec into a bounded plan DAG.

    Reuses the canonical Swarm stage shape (research -> architecture ->
    implementation/tests/docs -> integration_review -> codex_review ->
    acceptance_validation -> human_approval) and Work Contract refs. No LLM is
    used; each node carries bounded metadata and hashes only (INV-9).
    """
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    if not use_canonical:
        raise ValueError("decomposition requires use_canonical=True; an operator-provided plan is accepted via plan_create")

    mission_objective = _objective_meta(objective)
    nodes: list[dict[str, Any]] = []
    for stage_id, kind, parents in CANONICAL_STAGE_SPECS:
        if len(nodes) >= MAX_NODES:
            break
        owner = DEFAULT_OWNERS.get(stage_id, owner_profile)
        if kind == KIND_APPROVAL:
            owner = "owner"
        default_budget = _default_budget(kind)
        node_raw = {
            "node_id": stage_id,
            "kind": kind,
            "owner": owner,
            "parents": list(parents),
            "objective": f"{stage_id.replace('_', ' ')} stage",
            "capability_req": {
                "profile": owner if kind != KIND_APPROVAL else "owner",
                "skills": [],
                "authorization_class": "high_impact" if kind == KIND_APPROVAL else "reversible_write",
            },
            "budget": default_budget,
            "expected_artifacts": [] if kind == KIND_APPROVAL else _default_artifacts(stage_id),
            "contract_sha256": "",
            "contract_ref": f"contract:{stage_id}",
        }
        nodes.append(_canonical_node(node_raw))
    # The canonical nodes carry redaction metadata from the stage objective; the
    # mission-level objective is attached separately.
    raw: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "mission_id": mission_id,
        "version": 1,
        "decomposition": "canonical-swarm-v1",
        "objective": objective,
        "nodes": nodes,
    }
    canonical, plan = _canonical_plan(raw)
    # Recompute mission/plan objective metadata from the raw input objective.
    plan["objective_len"] = mission_objective["objective_len"]
    plan["objective_sha256"] = mission_objective["objective_sha256"]
    plan["node_count"] = len(plan["nodes"])
    plan["plan_sha256"] = _plan_sha256(canonical)
    # Remove raw objective from the returned plan (it is redacted in canonical).
    plan.pop("objective", None)
    return plan


def _default_budget(kind: str) -> dict[str, int]:
    if kind == KIND_APPROVAL:
        return {"est_minutes": 0, "est_tokens": 0}
    if kind == KIND_PARALLEL or kind == KIND_SINGLE:
        return {"est_minutes": 60, "est_tokens": 200_000}
    return {"est_minutes": 30, "est_tokens": 50_000}


def _default_artifacts(stage_id: str) -> list[str]:
    return ["work-contract.json", "evidence.json"]


# ---------------------------------------------------------------------------
# Row / plan read helpers
# ---------------------------------------------------------------------------


def _get_plan_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute("SELECT * FROM mission_plans WHERE mission_id=?", (mission_id,)).fetchone()
    if row is None:
        raise LookupError(f"mission plan {mission_id!r} not found")
    return row


def _nodes_rows(db: sqlite3.Connection, mission_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM plan_nodes WHERE mission_id=? ORDER BY node_id", (mission_id,)
    ).fetchall()


def _plan_view(db: sqlite3.Connection, row: sqlite3.Row, *, include_nodes: bool = True) -> dict[str, Any]:
    plan = json.loads(row["plan_json"])
    value: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        **plan,
        "version": int(row["version"]),
        "status": row["status"],
        "plan_sha256": row["plan_sha256"],
        "decomposition": row["decomposition"] or plan.get("decomposition", ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_nodes:
        node_rows = {r["node_id"]: r for r in _nodes_rows(db, row["mission_id"])}
        nodes: list[dict[str, Any]] = []
        for defn in plan.get("nodes", []):
            nid = defn["node_id"]
            n = node_rows.get(nid)
            if n is None:
                continue
            nodes.append(
                {
                    "schema": NODE_SCHEMA,
                    "node_id": nid,
                    **defn,
                    "state": n["state"],
                    "lease_lock": n["lease_lock"],
                    "lease_expires": n["lease_expires"],
                    "epoch": int(n["epoch"]),
                    "failure_kind": n["failure_kind"],
                    "retries": int(n["retries"]),
                }
            )
        value["nodes"] = nodes
        value["node_count"] = len(nodes)
    return value


# ---------------------------------------------------------------------------
# CRUD + operator review surfaces
# ---------------------------------------------------------------------------


def hermes_plan_create(
    mission_id: str,
    plan_json: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    status: str = PLAN_STATUS_DRAFT,
    hermes_root: Path | None = None,
) -> str:
    """Create (or replace-version) a MissionPlan for a mission. Additive.

    Never dispatches work or mutates the Mission lifecycle. If ``plan_json`` is
    empty, the plan is derived deterministically from the mission spec
    (canonical Swarm shape). ``status`` is the plan review status.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if status not in PLAN_STATUSES:
            raise ValueError(f"plan status must be one of {list(PLAN_STATUSES)}")

        if plan_json.strip():
            _canonical, plan, plan_sha = _parse_plan(plan_json)
        else:
            # MissionSpec -> bounded DAG, deterministic.
            raw = json.loads(_read_spec(mission_id, hermes_root))
            plan = decompose_mission(
                mission_id,
                objective=raw.get("objective", ""),
                owner_profile=raw.get("owner_profile", "default"),
                use_canonical=True,
            )
            _canonical, plan, plan_sha = _parse_plan(json.dumps(plan))

        # Capability requirements are validated against the target profile
        # before any plan row or node row can be created/replaced.
        _validate_plan_skill_requirements(plan, hermes_root)

        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan creation requires confirm=true")

        if effective_dry:
            path = _db_path(hermes_root)
            if path.exists():
                with _connect(path, write=False) as db:
                    tables = db.execute("SELECT name FROM sqlite_master WHERE name='mission_plans'").fetchone()
                    if tables and db.execute("SELECT 1 FROM mission_plans WHERE mission_id=?", (mission_id,)).fetchone():
                        _assert_replaceable(db, mission_id, hermes_root)
            _audit("hermes_plan_create", policy, dry_run=True, success=True, changed=False,
                   mission_id=mission_id, extra={"node_count": len(plan["nodes"]), "plan_sha256": plan_sha})
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_create",
                "mission_id": mission_id, "version": int(plan.get("version", 1)),
                "plan_sha256": plan_sha, "node_count": len(plan["nodes"]),
                "status": status, "changed": False, "dry_run": True,
            })

        path = _db_path(hermes_root)
        with _replacement_lease(mission_id, hermes_root), _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists (raises LookupError)
            now = _now()
            existing = db.execute(
                "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
            if existing:
                _assert_replaceable(db, mission_id, hermes_root)
            new_version = int(existing["version"]) + 1 if existing else int(plan.get("version", 1))
            store_plan = dict(plan)
            store_plan.pop("status", None)
            if existing:
                db.execute(
                    "UPDATE mission_plans SET plan_json=?, version=?, status=?, plan_sha256=?, decomposition=?, updated_at=? WHERE mission_id=?",
                    (json.dumps(store_plan, sort_keys=True), new_version, status, plan_sha,
                     plan.get("decomposition", ""), now, mission_id),
                )
            else:
                db.execute(
                    "INSERT INTO mission_plans(mission_id,plan_json,version,status,plan_sha256,decomposition,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (mission_id, json.dumps(store_plan, sort_keys=True), new_version, status, plan_sha,
                     plan.get("decomposition", ""), now, now),
                )
            db.execute("DELETE FROM plan_nodes WHERE mission_id=?", (mission_id,))
            for node in plan["nodes"]:
                db.execute(
                    "INSERT INTO plan_nodes(mission_id,node_id,contract_sha256,capability_req,budget,deps,state,lease_lock,lease_expires,epoch,failure_kind,retries,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mission_id, node["node_id"], node["contract_sha256"],
                        json.dumps(node["capability_req"], sort_keys=True),
                        json.dumps(node["budget"], sort_keys=True),
                        json.dumps(node["parents"], sort_keys=True),
                        node.get("state", "pending"),
                        "", "", 0, "", 0, now, now,
                    ),
                )
            db.commit()

        _audit("hermes_plan_create", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, extra={"version": new_version, "node_count": len(plan["nodes"]), "plan_sha256": plan_sha})
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_create",
            "mission_id": mission_id, "version": new_version, "plan_sha256": plan_sha,
            "node_count": len(plan["nodes"]), "status": status, "changed": True, "dry_run": False,
        })
    except PlanInFlightError as exc:
        _audit("hermes_plan_create", policy, dry_run=dry_run, success=False, changed=False, mission_id=mission_id)
        return _error(exc, "PLAN_IN_FLIGHT", "Stop the scheduler and resolve unfinished work before replacing the plan.")
    except skill_resolution.SkillRequirementsError as exc:
        _audit(
            "hermes_plan_create",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "PLAN_SKILL_REQUIREMENTS_REJECTED",
            "Install the required skills in the requested Hermes profile before creating the plan.",
            extra={"skill_validation": exc.rejection},
        )
    except (ValueError, TypeError, PermissionError, LookupError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        _audit("hermes_plan_create", policy, dry_run=dry_run, success=False, changed=False, mission_id=mission_id)
        return _error(exc, "PLAN_CREATE_REJECTED", "Check mission id, plan schema, and Operator workspace/direct policy.")


def _read_spec(mission_id: str, hermes_root: Path | None) -> str:
    """Return a mission spec JSON string (used by decompose). Read-only."""
    with mission._connect(_db_path(hermes_root), write=False) as db:
        row = mission._get_row(db, mission_id)
        return row["spec_json"]


def hermes_plan_get(mission_id: str, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
        with _connect(path, write=False) as db:
            row = _get_plan_row(db, mission_id)
            value = _plan_view(db, row)
        value["success"] = True
        _audit("hermes_plan_get", policy, dry_run=True, success=True, changed=False, mission_id=mission_id)
        return json.dumps(value)
    except FileNotFoundError:
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "PLAN_READ_FAILED", "Check mission id and Operator read access.")


def hermes_plan_list(status: str = "", limit: int = 50, hermes_root: Path | None = None) -> str:
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if status and status not in PLAN_STATUSES:
            raise ValueError("plan status filter is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "plans": [], "count": 0})
        with _connect(path, write=False) as db:
            if status:
                rows = db.execute("SELECT * FROM mission_plans WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM mission_plans ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            plans = []
            for row in rows:
                view = _plan_view(db, row, include_nodes=False)
                view.pop("nodes", None)
                plans.append(view)
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "plans": plans, "count": len(plans)})
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "PLAN_LIST_FAILED", "Check status/limit and Operator read access.")


def hermes_plan_validate(
    plan_json: str, *, hermes_root: Path | None = None
) -> str:
    """Pure read-only validation of a MissionPlan. Never writes."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        _canonical, plan, plan_sha = _parse_plan(plan_json)
        _validate_plan_skill_requirements(plan, hermes_root)
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_validate",
            "valid": True, "plan_sha256": plan_sha, "mission_id": plan["mission_id"],
            "version": int(plan["version"]), "decomposition": plan.get("decomposition", ""),
            "node_count": len(plan["nodes"]),
            "nodes": [{"node_id": n["node_id"], "kind": n["kind"], "state": "pending"} for n in plan["nodes"]],
        })
    except skill_resolution.SkillRequirementsError as exc:
        payload = json.loads(
            _error(
                exc,
                "PLAN_SKILL_REQUIREMENTS_REJECTED",
                "Install the required skills in the requested Hermes profile before validating the plan.",
                extra={"skill_validation": exc.rejection},
            )
        )
        payload.update({
            "schema_version": SCHEMA_VERSION,
            "tool": "hermes_plan_validate",
            "valid": False,
        })
        return json.dumps(payload)
    except (ValueError, PermissionError, json.JSONDecodeError) as exc:
        return json.dumps({
            "success": False, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_validate",
            "valid": False, "error": _error(exc, "PLAN_VALIDATE_FAILED", "Check plan schema."),
        })


def hermes_plan_decompose(mission_id: str, hermes_root: Path | None = None) -> str:
    """Read-only: deterministic MissionSpec -> bounded plan DAG (not persisted)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        raw = json.loads(_read_spec(mission_id, hermes_root))
        plan = decompose_mission(
            mission_id,
            objective=raw.get("objective", ""),
            owner_profile=raw.get("owner_profile", "default"),
            use_canonical=True,
        )
        plan["success"] = True
        plan["schema_version"] = SCHEMA_VERSION
        plan["tool"] = "hermes_plan_decompose"
        _audit("hermes_plan_decompose", policy, dry_run=True, success=True, changed=False, mission_id=mission_id,
               extra={"node_count": len(plan["nodes"])})
        return json.dumps(plan)
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return _error(exc, "PLAN_DECOMPOSE_FAILED", "Check mission id and Operator read access.")


def _ready_node_ids(nodes: list[dict[str, Any]]) -> list[str]:
    """Return pending nodes whose complete parent set succeeded."""
    by_id = {n["node_id"]: n for n in nodes}
    return [
        n["node_id"]
        for n in nodes
        if n["state"] == "pending"
        and all(
            parent_id in by_id and by_id[parent_id]["state"] == "completed"
            for parent_id in n.get("parents", [])
        )
    ]


def hermes_plan_review(mission_id: str, hermes_root: Path | None = None) -> str:
    """Operator review surface (read-only): the bounded DAG + node state."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
        with _connect(path, write=False) as db:
            row = _get_plan_row(db, mission_id)
            value = _plan_view(db, row)
            nodes = value["nodes"]
            # Topological order (Kahn) for operator readability.
            value["topological_order"] = _topological_order(
                [{"node_id": n["node_id"], "parents": n["parents"]} for n in nodes]
            )
            value["terminal_nodes"] = [n["node_id"] for n in nodes if n["state"] in TERMINAL_NODE_STATES]
            # Ready means every declared parent completed successfully.
            value["ready_nodes"] = _ready_node_ids(nodes)
        value["success"] = True
        value["tool"] = "hermes_plan_review"
        _audit("hermes_plan_review", policy, dry_run=True, success=True, changed=False, mission_id=mission_id)
        return json.dumps(value)
    except FileNotFoundError:
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "mission_id": mission_id, "found": False})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "PLAN_REVIEW_FAILED", "Check mission id and Operator read access.")


def _topological_order(nodes: list[dict[str, Any]]) -> list[str]:
    indegree: dict[str, int] = {n["node_id"]: 0 for n in nodes}
    for n in nodes:
        for parent in n.get("parents") or []:
            indegree[n["node_id"]] += 1
    queue = [nid for nid in indegree if indegree[nid] == 0]
    order: list[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for n in nodes:
            if cur in (n.get("parents") or []):
                indegree[n["node_id"]] -= 1
                if indegree[n["node_id"]] == 0:
                    queue.append(n["node_id"])
    return order


def hermes_plan_node_transition(
    mission_id: str,
    node_id: str,
    target_state: str,
    reason: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    expected_plan_version: int | None = None,
    bump_retries: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Advance a plan node through the validated state machine (design §5.2).

    This mutates only the plan node's own state; it never dispatches a worker
    and never completes/approves a Mission (read-only slice).

    ``expected_plan_version`` is an optional compare-and-swap: when given, the
    transition is refused with ``PLAN_VERSION_CONFLICT`` unless
    ``mission_plans.version`` still equals it, checked inside the same
    ``BEGIN IMMEDIATE`` transaction as the write (and before the dry-run
    return, so a dry run can pre-validate it). ``None`` keeps the historical
    unchecked behavior.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if target_state not in NODE_STATES:
            raise ValueError(f"target node state must be one of {list(NODE_STATES)}")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct node transition requires confirm=true")

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            if expected_plan_version is not None:
                plan_row = db.execute(
                    "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
                ).fetchone()
                actual_version = int(plan_row["version"]) if plan_row else None
                if actual_version != int(expected_plan_version):
                    raise PlanVersionConflict(int(expected_plan_version), actual_version)
            row = db.execute(
                "SELECT state FROM plan_nodes WHERE mission_id=? AND node_id=?", (mission_id, node_id)
            ).fetchone()
            if row is None:
                raise LookupError(f"plan node {node_id!r} not found for mission {mission_id!r}")
            current = row["state"]
            if current not in NODE_TRANSITIONS or target_state not in NODE_TRANSITIONS[current]:
                raise ValueError(f"illegal node transition {current} -> {target_state} for node {node_id!r}")
            if effective_dry:
                db.rollback()
                _audit("hermes_plan_node_transition", policy, dry_run=True, success=True, changed=False,
                       mission_id=mission_id, node_id=node_id, extra={"from": current, "to": target_state})
                return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_node_transition",
                                   "mission_id": mission_id, "node_id": node_id, "from_state": current,
                                   "to_state": target_state, "changed": False, "dry_run": True})
            now = _now()
            db.execute(
                "UPDATE plan_nodes SET state=?, lease_lock='', lease_expires='', retries=retries+?, updated_at=? WHERE mission_id=? AND node_id=?",
                (target_state, 1 if bump_retries else 0, now, mission_id, node_id),
            )
            db.commit()
        _audit("hermes_plan_node_transition", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, node_id=node_id, extra={"from": current, "to": target_state, "reason": _clean_text(reason, field="reason", maximum=200)})
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_node_transition",
                           "mission_id": mission_id, "node_id": node_id, "from_state": current,
                           "to_state": target_state, "changed": True, "dry_run": False})
    except PlanVersionConflict as exc:
        _audit("hermes_plan_node_transition", policy, dry_run=dry_run, success=False, changed=False,
               mission_id=mission_id, node_id=node_id)
        return _error(
            exc, "PLAN_VERSION_CONFLICT",
            "The plan was replaced since it was read; re-read the plan and recompute before writing.",
            extra={"expected_plan_version": exc.expected, "actual_plan_version": exc.actual},
        )
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit("hermes_plan_node_transition", policy, dry_run=dry_run, success=False, changed=False,
               mission_id=mission_id, node_id=node_id)
        return _error(exc, "PLAN_NODE_TRANSITION_REJECTED", "Check node id, target state, and state-machine legality.")


def hermes_plan_set_status(
    mission_id: str,
    status: str,
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Set the plan-level review status (operator-reviewable). Read-only re mission."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if status not in PLAN_STATUSES:
            raise ValueError(f"plan status must be one of {list(PLAN_STATUSES)}")
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan status update requires confirm=true")
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            row = db.execute("SELECT status FROM mission_plans WHERE mission_id=?", (mission_id,)).fetchone()
            if row is None:
                raise LookupError(f"mission plan {mission_id!r} not found")
            if effective_dry:
                db.rollback()
                _audit("hermes_plan_set_status", policy, dry_run=True, success=True, changed=False,
                       mission_id=mission_id, extra={"to": status})
                return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_set_status",
                                   "mission_id": mission_id, "status": status, "changed": False, "dry_run": True})
            db.execute("UPDATE mission_plans SET status=?, updated_at=? WHERE mission_id=?",
                       (status, _now(), mission_id))
            db.commit()
        _audit("hermes_plan_set_status", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, extra={"to": status})
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_set_status",
                           "mission_id": mission_id, "status": status, "changed": True, "dry_run": False})
    except (ValueError, LookupError, PermissionError, OSError, sqlite3.Error) as exc:
        _audit("hermes_plan_set_status", policy, dry_run=dry_run, success=False, changed=False, mission_id=mission_id)
        return _error(exc, "PLAN_STATUS_REJECTED", "Check mission id and plan status.")


# ---------------------------------------------------------------------------
# Bounded rework patch (v0.13 Autopilot PR5, Level B) — internal bridge, no tool
# ---------------------------------------------------------------------------


def _rework_clone_id(existing: set[str], failed_id: str) -> str:
    for n in range(1, 100):
        candidate = f"{failed_id[:58]}-r{n}"
        if candidate not in existing:
            return candidate
    raise ValueError("too many rework clones for this node")


def _assert_rework_invariants(
    old_nodes: list[dict[str, Any]], new_nodes: list[dict[str, Any]], states: dict[str, str],
    *, failed_id: str, clone_id: str, child_ids: set[str],
) -> None:
    """Belt-and-braces re-check that the patch touched only what it may touch.

    The patch is built to satisfy these by construction; asserting them again
    on the finished result means a future edit that breaks one fails loudly
    instead of silently rewriting history.
    """
    old_by = {n["node_id"]: n for n in old_nodes}
    new_by = {n["node_id"]: n for n in new_nodes}
    if set(new_by) != set(old_by) | {clone_id}:
        raise ValueError("rework patch may only add the clone node")
    for node_id, old in old_by.items():
        new = new_by[node_id]
        if states.get(node_id) == "completed" and new != old:
            raise ValueError(f"rework patch may not rewrite completed node {node_id!r}")
        if node_id == failed_id and new != old:
            raise ValueError("rework patch may not rewrite the failed node (its history is preserved)")
        if node_id not in child_ids and new != old:
            raise ValueError(f"rework patch may not change unrelated node {node_id!r}")
        for key in ("kind", "owner", "capability_req", "budget", "expected_artifacts", "objective_sha256"):
            if new[key] != old[key]:
                raise ValueError(f"rework patch may not change {key} of node {node_id!r}")
        if new.get("artifact_requirements") != old.get("artifact_requirements"):
            raise ValueError("rework patch may not change artifact acceptance requirements")
    clone, failed = new_by[clone_id], old_by[failed_id]
    if clone.get("artifact_requirements") != failed.get("artifact_requirements"):
        raise ValueError("rework clone must preserve artifact acceptance requirements")
    for key in ("kind", "owner", "capability_req", "budget", "expected_artifacts", "objective_sha256", "parents"):
        if clone[key] != failed[key]:
            raise ValueError(f"rework clone must copy {key} of the failed node")
    if sum(1 for n in new_nodes if n["kind"] == KIND_APPROVAL) != sum(1 for n in old_nodes if n["kind"] == KIND_APPROVAL):
        raise ValueError("rework patch may not add or remove an approval node")


def apply_rework_patch(
    mission_id: str,
    failed_node_id: str,
    *,
    expected_plan_version: int,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Replace a *failed* node by a fresh clone, in place, without touching history.

    Level B recovery. The failed node stays ``failed`` (evidence preserved); a
    clone ``<node>-rN`` with the same definition and parents is added as
    ``pending``; the failed node's direct children are re-parented onto the
    clone. ``mission_plans.version`` is deliberately **not** bumped: the plan was
    extended, not replaced, so dispatch identities of in-flight nodes stay
    valid. Concurrent replacement by ``hermes_plan_create`` still bumps the
    version and is caught by ``expected_plan_version``.

    Refused (``ValueError``/``PlanVersionConflict``) unless: the version matches;
    the node is ``failed`` and not an approval node or ``high_impact``; every
    direct child is still ``pending``; and the patched plan re-validates as a
    whole (DAG, caps, secret scan) with the clone's skills re-resolved. Never
    touches a completed node, an authorization class, or an approval node.
    """
    path = _db_path(hermes_root)
    with _connect(path, write=True) as db:
        _begin_write(db)
        row = _get_plan_row(db, mission_id)
        if int(row["version"]) != int(expected_plan_version):
            raise PlanVersionConflict(int(expected_plan_version), int(row["version"]))
        plan = json.loads(row["plan_json"])
        old_nodes = plan["nodes"]
        states = {r["node_id"]: r["state"] for r in _nodes_rows(db, mission_id)}
        by_id = {n["node_id"]: n for n in old_nodes}
        failed = by_id.get(failed_node_id)
        if failed is None or states.get(failed_node_id) != "failed":
            raise ValueError("rework requires an existing failed node")
        if failed["kind"] == KIND_APPROVAL or failed["capability_req"].get("authorization_class") == "high_impact":
            raise ValueError("a human-gated node is never replanned around")
        children = [n for n in old_nodes if failed_node_id in n["parents"]]
        started = [n["node_id"] for n in children if states.get(n["node_id"]) != "pending"]
        if started:
            raise ValueError(f"rework refused: descendant already started: {started}")
        clone_id = _rework_clone_id(set(by_id), failed_node_id)
        child_ids = {c["node_id"] for c in children}

        clone = _canonical_node({**failed, "node_id": clone_id, "contract_sha256": ""})
        new_nodes: list[dict[str, Any]] = []
        for node in old_nodes:
            if node["node_id"] in child_ids:
                reparented = [clone_id if p == failed_node_id else p for p in node["parents"]]
                node = _canonical_node({**node, "parents": reparented, "contract_sha256": ""})
            new_nodes.append(node)
        new_nodes.append(clone)
        _assert_rework_invariants(old_nodes, new_nodes, states, failed_id=failed_node_id,
                                  clone_id=clone_id, child_ids=child_ids)

        raw_plan = {k: v for k, v in plan.items() if k != "nodes"}
        raw_plan["nodes"] = new_nodes
        canonical_json, canonical = _canonical_plan(raw_plan)  # DAG, caps, secret scan on the whole result
        _validate_plan_skill_requirements({"nodes": [clone]}, hermes_root)
        canon_by = {n["node_id"]: n for n in canonical["nodes"]}
        now = _now()
        db.execute(
            "UPDATE mission_plans SET plan_json=?, plan_sha256=?, updated_at=? WHERE mission_id=?",
            (json.dumps({k: v for k, v in canonical.items() if k != "status"}, sort_keys=True),
             _plan_sha256(canonical_json), now, mission_id),
        )
        db.execute(
            "INSERT INTO plan_nodes(mission_id,node_id,contract_sha256,capability_req,budget,deps,state,lease_lock,lease_expires,epoch,failure_kind,retries,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mission_id, clone_id, clone["contract_sha256"], json.dumps(clone["capability_req"], sort_keys=True),
             json.dumps(clone["budget"], sort_keys=True), json.dumps(clone["parents"], sort_keys=True),
             "pending", "", "", 0, "", 0, now, now),
        )
        for child_id in sorted(child_ids):
            child = canon_by[child_id]
            db.execute(
                "UPDATE plan_nodes SET deps=?, contract_sha256=?, updated_at=? WHERE mission_id=? AND node_id=?",
                (json.dumps(child["parents"], sort_keys=True), child["contract_sha256"], now, mission_id, child_id),
            )
        db.commit()
    _audit("hermes_plan_rework_patch", op.OperatorPolicy(), dry_run=False, success=True, changed=True,
           mission_id=mission_id, node_id=failed_node_id,
           extra={"clone": clone_id, "reparented": sorted(child_ids), "plan_version": int(expected_plan_version)})
    return {"clone_node_id": clone_id, "reparented": sorted(child_ids), "plan_version": int(expected_plan_version)}


# ---------------------------------------------------------------------------
# State-machine predicate (exposed for tests / controllers)
# ---------------------------------------------------------------------------


def validate_node_transition(from_state: str, to_state: str) -> bool:
    """Return whether ``from_state -> to_state`` is a legal §5.2 transition."""
    return from_state in NODE_TRANSITIONS and to_state in NODE_TRANSITIONS[from_state]
