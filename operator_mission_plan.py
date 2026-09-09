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

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op
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


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(exc, layer="operator", code=code, suggested_action=action)
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
    enc = json.dumps(skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


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
            }
        ),
        "capability_req": cap,
        "budget": budget,
        "expected_artifacts": artifacts,
    }
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

        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct plan creation requires confirm=true")

        if effective_dry:
            _audit("hermes_plan_create", policy, dry_run=True, success=True, changed=False,
                   mission_id=mission_id, extra={"node_count": len(plan["nodes"]), "plan_sha256": plan_sha})
            return json.dumps({
                "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_create",
                "mission_id": mission_id, "version": int(plan.get("version", 1)),
                "plan_sha256": plan_sha, "node_count": len(plan["nodes"]),
                "status": status, "changed": False, "dry_run": True,
            })

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists (raises LookupError)
            now = _now()
            existing = db.execute(
                "SELECT version FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
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


def hermes_plan_validate(plan_json: str) -> str:
    """Pure read-only validation of a MissionPlan. Never writes."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        _canonical, plan, plan_sha = _parse_plan(plan_json)
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_validate",
            "valid": True, "plan_sha256": plan_sha, "mission_id": plan["mission_id"],
            "version": int(plan["version"]), "decomposition": plan.get("decomposition", ""),
            "node_count": len(plan["nodes"]),
            "nodes": [{"node_id": n["node_id"], "kind": n["kind"], "state": "pending"} for n in plan["nodes"]],
        })
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
    hermes_root: Path | None = None,
) -> str:
    """Advance a plan node through the validated state machine (design §5.2).

    This mutates only the plan node's own state; it never dispatches a worker
    and never completes/approves a Mission (read-only slice).
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
                "UPDATE plan_nodes SET state=?, lease_lock='', lease_expires='', updated_at=? WHERE mission_id=? AND node_id=?",
                (target_state, now, mission_id, node_id),
            )
            db.commit()
        _audit("hermes_plan_node_transition", policy, dry_run=False, success=True, changed=True,
               mission_id=mission_id, node_id=node_id, extra={"from": current, "to": target_state, "reason": _clean_text(reason, field="reason", maximum=200)})
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION, "tool": "hermes_plan_node_transition",
                           "mission_id": mission_id, "node_id": node_id, "from_state": current,
                           "to_state": target_state, "changed": True, "dry_run": False})
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
# State-machine predicate (exposed for tests / controllers)
# ---------------------------------------------------------------------------


def validate_node_transition(from_state: str, to_state: str) -> bool:
    """Return whether ``from_state -> to_state`` is a legal §5.2 transition."""
    return from_state in NODE_TRANSITIONS and to_state in NODE_TRANSITIONS[from_state]
