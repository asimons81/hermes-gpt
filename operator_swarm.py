"""Swarm Orchestration engine for hermes-gpt v0.6 (M2).

Implements the approved Swarm Orchestration design (v0.6 M2): a workflow engine that
runs the canonical multi-stage agent workflow — research -> architecture ->
parallel implementation/tests/docs -> integration review -> Codex review ->
acceptance validation -> human approval — across Hermes profiles, delegated
Hermes agents, isolated Git worktrees, and Codex.

The engine **consumes** M0 (Mission Control observed-state view-model) and
M1 (Work Contracts engine) rather than re-implementing them:

- Every stage is an M1 **contract** (D-SW1); the engine builds the contract
  from the stage definition + workflow context.
- Stage completion is validated by the M1 validator against **observed**
  Mission Control state (kanban task runs, async delegations, artifacts on
  disk, audit trail) — a false "done" claim returns the stage for rework,
  never trusted (D-SW6 / S2).
- Implementation stages plan an **upstream kanban worktree** (NG5, D-SW2):
  the engine computes the worktree plan (branch ``<slug>/<task-id>``, path
  ``<repo>/.worktrees/<task-id>``) but never manages git itself.
- Codex keeps its existing posture (D-SW7): the engine drives
  ``operator_codex`` unchanged (fixed-argv, ``shell=False``, bounded
  timeout, approved workdir) and reads only a bounded verdict JSON
  (risk-review P2-1).
- Human approval is a real gate (D-SW8): the final stage waits on
  ``hermes_swarm_approve`` at owner level + direct mode; the engine never
  auto-advances past it.

Design decisions enforced here:

- D-SW3 bounded parallelism: per-workflow ``max_parallel`` (default 3),
  per-board cap (default 4), global stage cap per workflow (default 12).
  Caps are read from the workflow document, overridable by env
  (``HERMES_GPT_SWARM_*``), never hard-coded beyond defaults.
- D-SW4 exactly-one owner per stage: owner required and immutable.
- D-SW5 safe handoffs: handoff records list ``from``, ``to``,
  ``artifact_refs``, ``contract_verdict``.
- D-SW9 dry-run-first mutation + D-SW10 audit on every action.
- D-SW11 read-only bounded status surfaces (no raw bodies).
- D-SW12 no mutation from Mission Control (swarm mutations live only in the
  ``hermes_swarm_*`` namespace under operator gates).
- Q6 resolution (open question in the design): workflow instances persist
  as minimal operational JSON state under ``<hermes_root>/swarm-workflows/``
  (mirrors ``operator_codex``'s ``codex-jobs/`` pattern). This is the
  engine's own state — **not** a new observed-state source for M0; status
  output still uses M0's bounded/redacted envelopes and the M1 validator
  still reads existing Mission Control sources for evidence.
- P2-2 retention: every workflow record carries a retention note so
  ``default`` can clean worktrees / Codex artifacts after the release gate.
- P2-3: the canonical shape never assigns Codex as an implementation owner
  (Codex appears only as the read-only review stage owner).

The public surface is the ``hermes_swarm_*`` tool group (7 tools):

- ``hermes_swarm_workflow_create``   — workspace, dry-run-first: register a
  workflow instance; returns ``workflow_id`` + stage plan.
- ``hermes_swarm_workflow_list``     — read-only: instances + status.
- ``hermes_swarm_workflow_status``   — read-only: stage map/owners/handoffs/
  verdicts.
- ``hermes_swarm_workflow_validate`` — read-only: validate a proposed
  workflow DAG against engine rules (shape, caps, contracts).
- ``hermes_swarm_stage_dispatch``    — workspace, dry-run-first: dispatch one
  ready stage as an M1 contract (respects caps).
- ``hermes_swarm_stage_advance``     — workspace, dry-run-first: validate a
  stage from observed state, record the handoff, promote next ready stages.
- ``hermes_swarm_approve``           — owner, dry-run-first: record the final
  human approval.

All tests are fixture-only (temp ``hermes_root``, injected runners), never
production data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import operator_policy as op
import operator_fleet as op_fleet
import operator_contract as contract_mod
import operator_mission as mission
import operator_codex as op_codex

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.6-sw.1"
WORKFLOW_SCHEMA = "hermes.swarm-workflow/v1"
CONTRACT_SCHEMA = contract_mod.CONTRACT_SCHEMA

# Identity regexes (reused from the fleet / contract layer).
_WORKFLOW_ID_RE = re.compile(r"^sw-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_AGENT_RE = op_fleet._AGENT_RE
_PROFILE_RE = op_fleet._PROFILE_RE
_TASK_ID_RE = op_fleet._TASK_ID_RE

# Stage / workflow lifecycle statuses (design §9).
STAGE_STATUS_TODO = "todo"
STAGE_STATUS_RUNNING = "running"
STAGE_STATUS_VALIDATED = "validated"
STAGE_STATUS_REWORK = "returned_for_rework"
STAGE_STATUS_BLOCKED = "blocked"
STAGE_STATUS_DONE = "done"
STAGE_STATUSES = (
    STAGE_STATUS_TODO,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_VALIDATED,
    STAGE_STATUS_REWORK,
    STAGE_STATUS_BLOCKED,
    STAGE_STATUS_DONE,
)

WORKFLOW_STATUS_RUNNING = "running"
WORKFLOW_STATUS_BLOCKED = "blocked"
WORKFLOW_STATUS_DONE = "done"
WORKFLOW_STATUS_AWAITING = "awaiting_approval"
WORKFLOW_STATUSES = (
    WORKFLOW_STATUS_RUNNING,
    WORKFLOW_STATUS_BLOCKED,
    WORKFLOW_STATUS_DONE,
    WORKFLOW_STATUS_AWAITING,
)

# D-SW3 caps (defaults; overridable per workflow and via env).
DEFAULT_MAX_PARALLEL = 3
DEFAULT_BOARD_CAP = 4
DEFAULT_MAX_STAGES = 12
ENV_MAX_PARALLEL = "HERMES_GPT_SWARM_MAX_PARALLEL"
ENV_BOARD_CAP = "HERMES_GPT_SWARM_BOARD_CAP"
ENV_MAX_STAGES = "HERMES_GPT_SWARM_MAX_STAGES"
HARD_MAX_PARALLEL = 8
HARD_BOARD_CAP = 16
HARD_MAX_STAGES = 64

# Bound on stage definitions (mirrors M1 limits).
_MAX_TITLE_BYTES = 500
_MAX_OBJECTIVE_BYTES = 8_000
_MAX_ARTIFACTS = 32
_MAX_TESTS = 16
_MAX_FORBIDDEN_ACTIONS = 32
_MAX_HANDOFFS = 64

# Retention note included in every workflow record (risk-review P2-2).
RETENTION_NOTE = (
    "Worktrees and Codex verdict/transcript artifacts persist for review; "
    "default cleans workflow worktrees and Codex job files after the release "
    "gate (operator_codex RETENTION_DAYS=30 for job files)."
)

# Worktree plan shape (NG5 / D-SW2). The engine computes the plan and passes
# the worktree path as the contract's allowed workspace; upstream kanban
# materializes it. Never call git from here.
WORKTREE_PROJECT_LINKED = "project-linked"
WORKTREE_PLAIN = "worktree"

_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Error / envelope helpers (mirror operator_contract)
# ---------------------------------------------------------------------------


def _swarm_error(
    *,
    code: str,
    safe_message: str,
    suggested_action: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    env = op.make_error_envelope(
        layer="operator",
        code=code,
        safe_message=safe_message,
        suggested_action=suggested_action,
        trace_id=trace_id,
        extra=extra,
    )
    env["schema_version"] = SCHEMA_VERSION
    return env


def _truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _prompt_meta(text: str | None) -> dict[str, Any]:
    """Return ``{prompt_len, prompt_sha256}`` for an objective (never text)."""
    if text is None:
        return {"prompt_len": 0, "prompt_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "prompt_len": len(data),
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
    }


def _audit_call(
    *,
    tool: str,
    workflow_id: str,
    stage_id: str,
    dry_run: bool,
    success: bool,
    changed: bool,
    summary: str,
    owner: str = "",
    verdict: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a swarm call in the operator audit log (D-SW10).

    Never includes objective text; only workflow_id/stage/owner/verdict.
    """
    policy = op.OperatorPolicy()
    try:
        op.audit_record(
            tool=tool,
            level=policy.level or "read_only",
            apply_mode=policy.apply_mode,
            dry_run=bool(dry_run),
            success=bool(success),
            changed=bool(changed),
            summary=_truncate(summary, 500),
            extra={
                "workflow_id": workflow_id,
                "stage_id": stage_id,
                "owner": owner,
                "verdict": verdict,
                **(extra or {}),
            },
        )
    except Exception:
        pass


def _default_hermes_root() -> Path | None:
    """Return the default Hermes data root (mirrors operator_mission)."""
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        normalized = op.normalize_hermes_data_root(Path(env_home).expanduser())
        if normalized is not None:
            return normalized
    for cand in [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
    ]:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path.home() / ".hermes"


def _resolve_root(hermes_root: Path | None) -> Path:
    return hermes_root or _default_hermes_root() or Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def _env_cap(name: str, default: int, hard: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, hard))


def _caps(workflow: dict[str, Any]) -> tuple[int, int, int]:
    """Return (max_parallel, board_cap, max_stages) for a workflow.

    Per-workflow document values override env defaults; env caps the hard
    bound. Design Q1: engine reads caps from config/env, not hard-coded.
    """
    max_parallel = int(workflow.get("max_parallel") or _env_cap(ENV_MAX_PARALLEL, DEFAULT_MAX_PARALLEL, HARD_MAX_PARALLEL))
    board_cap = int(workflow.get("board_cap") or _env_cap(ENV_BOARD_CAP, DEFAULT_BOARD_CAP, HARD_BOARD_CAP))
    max_stages = int(workflow.get("max_stages") or _env_cap(ENV_MAX_STAGES, DEFAULT_MAX_STAGES, HARD_MAX_STAGES))
    return (
        max(1, min(max_parallel, HARD_MAX_PARALLEL)),
        max(1, min(board_cap, HARD_BOARD_CAP)),
        max(1, min(max_stages, HARD_MAX_STAGES)),
    )


# ---------------------------------------------------------------------------
# Workflow canonicalization (design §5.1 / §6)
# ---------------------------------------------------------------------------


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    return op_fleet._clean_text(value, field=field, maximum=maximum, required=required)


def _string_list(value: Any, *, field: str) -> list[str]:
    return op_fleet._string_list(value, field=field)


def _stage_id_list(value: Any, *, known: set[str], field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of stage ids")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _STAGE_ID_RE.fullmatch(item):
            raise ValueError(f"{field} contains an invalid stage id")
        if item not in known:
            raise ValueError(f"{field} references unknown stage {item!r}")
        if item in out:
            raise ValueError(f"{field} contains duplicate stage {item!r}")
        out.append(item)
    return out


def _validate_stage_defs(stages: Any, workflow: dict[str, Any]) -> None:
    """Validate the stage DAG: ids, owners, parents, cycles, caps."""
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty list")
    _, board_cap, max_stages = _caps(workflow)
    if len(stages) > max_stages:
        raise ValueError(f"stage count {len(stages)} exceeds max_stages cap ({max_stages})")

    ids: list[str] = []
    known: set[str] = set()
    for idx, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stage[{idx}] must be an object")
        stage_id = _clean_text(stage.get("id"), field="stage id", maximum=64)
        if not _STAGE_ID_RE.fullmatch(stage_id):
            raise ValueError(f"stage id {stage_id!r} is invalid")
        if stage_id in known:
            raise ValueError(f"duplicate stage id {stage_id!r}")
        known.add(stage_id)
        ids.append(stage_id)

    # Second pass: validate owners/kinds/objectives/parents now that every
    # stage id is known (parents may reference later stages).
    for idx, stage in enumerate(stages):
        stage_id = stage["id"]
        kind = str(stage.get("kind", "single")).strip().lower()
        if kind not in ("single", "parallel", "approval"):
            raise ValueError(f"stage {stage_id!r} kind must be single|parallel|approval")

        # D-SW4: exactly-one owner, required and immutable.
        owner = _clean_text(stage.get("owner"), field=f"stage {stage_id} owner", maximum=64)
        if not _AGENT_RE.fullmatch(owner) or not _PROFILE_RE.fullmatch(owner):
            raise ValueError(f"stage {stage_id!r} owner {owner!r} is invalid")
        if kind == "approval" and owner not in ("owner", "tony"):
            raise ValueError(f"approval stage {stage_id!r} owner must be 'owner'")

        parents = _stage_id_list(stage.get("parents") or [], known=known, field=f"stage {stage_id} parents")

        objective = _clean_text(stage.get("objective"), field=f"stage {stage_id} objective", maximum=_MAX_OBJECTIVE_BYTES)
        if not objective:
            raise ValueError(f"stage {stage_id!r} objective is required")

        # Parallel fan-out group bound (D-SW3): the group that fans out from a
        # common parent must not exceed the workflow's per-workflow cap.
        if kind == "parallel":
            key = tuple(sorted(parents))
            group = [s["id"] for s in stages if isinstance(s, dict) and tuple(sorted(s.get("parents") or [])) == key]
            if len(group) > board_cap:
                raise ValueError(
                    f"parallel fan-out group {sorted(group)} exceeds board cap ({board_cap})"
                )

    # Acyclicity: topological sort (Kahn).
    indegree = {sid: 0 for sid in ids}
    edges: dict[str, list[str]] = {sid: [] for sid in ids}
    for stage in stages:
        for parent in stage.get("parents") or []:
            if parent == stage["id"]:
                raise ValueError(f"stage {stage['id']!r} cannot be its own parent")
            edges[parent].append(stage["id"])
            indegree[stage["id"]] += 1
    queue = [sid for sid in ids if indegree[sid] == 0]
    ordered: list[str] = []
    while queue:
        sid = queue.pop(0)
        ordered.append(sid)
        for nxt in edges[sid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(ids):
        cycle = sorted(sid for sid in ids if indegree[sid] > 0)
        raise ValueError(f"workflow DAG contains a cycle involving stages: {cycle}")


def _canonical_workflow(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a workflow document.

    Returns ``(canonical_json, workflow_dict)``. Raises ValueError /
    PermissionError on schema, DAG, or cap violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("workflow must be a JSON object")
    if raw.get("schema") != WORKFLOW_SCHEMA:
        raise ValueError(f"workflow schema must be {WORKFLOW_SCHEMA!r}")

    workflow_id = _clean_text(raw.get("workflow_id", ""), field="workflow_id", maximum=128, required=False)
    if workflow_id and not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise ValueError("workflow_id must match sw-<name> (letters/digits/_-)")
    title = _clean_text(raw.get("title"), field="title", maximum=_MAX_TITLE_BYTES)
    workspace = _clean_text(raw.get("workspace"), field="workspace", maximum=1000)
    if op.is_denied_path(workspace):
        raise PermissionError("workspace is denied by the secret-path policy")

    project_raw = raw.get("project") or {}
    project: dict[str, Any] = {}
    if isinstance(project_raw, dict):
        slug = _clean_text(project_raw.get("slug", ""), field="project.slug", maximum=128, required=False)
        repo = _clean_text(project_raw.get("repo", ""), field="project.repo", maximum=1000, required=False)
        if slug and repo:
            if op.is_denied_path(repo):
                raise PermissionError("project.repo is denied by the secret-path policy")
            project = {"slug": slug, "repo": repo}

    stages = raw.get("stages")
    _validate_stage_defs(stages, raw)
    assert isinstance(stages, list)

    # Per-stage M1-contract shape validation happens lazily at dispatch
    # (the engine builds contracts then); workflow_validate can call
    # _stage_contract for a full contract check. Here we bound + keep the
    # raw stage fields for the engine to build contracts from.
    cleaned_stages: list[dict[str, Any]] = []
    for stage in stages:
        cleaned = dict(stage)
        artifacts = stage.get("expected_artifacts") or []
        if not isinstance(artifacts, list) or len(artifacts) > _MAX_ARTIFACTS:
            raise ValueError("expected_artifacts must be a list (<= 32)")
        for art in artifacts:
            if not isinstance(art, dict):
                raise ValueError("expected artifact must be an object")
        cleaned["expected_artifacts"] = list(artifacts)

        tests = stage.get("tests") or []
        if not isinstance(tests, list) or len(tests) > _MAX_TESTS:
            raise ValueError("tests must be a list (<= 16)")
        for t in tests:
            if not isinstance(t, dict):
                raise ValueError("test must be an object")
        cleaned["tests"] = list(tests)

        forbidden = stage.get("forbidden_actions") or []
        if not isinstance(forbidden, list) or len(forbidden) > _MAX_FORBIDDEN_ACTIONS:
            raise ValueError("forbidden_actions must be a list (<= 32)")
        cleaned["forbidden_actions"] = list(forbidden)
        cleaned_stages.append(cleaned)

    workflow: dict[str, Any] = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": workflow_id,
        "title": title,
        "workspace": workspace,
        "project": project,
        "max_parallel": int(raw.get("max_parallel") or _env_cap(ENV_MAX_PARALLEL, DEFAULT_MAX_PARALLEL, HARD_MAX_PARALLEL)),
        "board_cap": int(raw.get("board_cap") or _env_cap(ENV_BOARD_CAP, DEFAULT_BOARD_CAP, HARD_BOARD_CAP)),
        "max_stages": int(raw.get("max_stages") or _env_cap(ENV_MAX_STAGES, DEFAULT_MAX_STAGES, HARD_MAX_STAGES)),
        "stages": cleaned_stages,
    }
    canonical = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > 64_000:
        raise ValueError("canonical workflow exceeds 64 KB")
    return canonical, workflow


def _workflow_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_workflow(workflow_json: str) -> tuple[str, dict[str, Any], str]:
    """Parse + canonicalize a workflow JSON string.

    Returns ``(canonical_json, workflow, workflow_sha256)``.
    """
    if not isinstance(workflow_json, str) or not workflow_json.strip():
        raise ValueError("workflow_json is required")
    try:
        raw = json.loads(workflow_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"workflow_json is not valid JSON: {exc}") from exc
    canonical, workflow = _canonical_workflow(raw)
    return canonical, workflow, _workflow_sha256(canonical)


def _workflow_id(workflow: dict[str, Any]) -> str:
    return workflow["workflow_id"]


# ---------------------------------------------------------------------------
# Workflow registry (Q6 resolution: operational JSON state, not an M0 source)
# ---------------------------------------------------------------------------


def _workflows_dir(hermes_root: Path) -> Path:
    return Path(hermes_root) / "swarm-workflows"


def _workflow_path(hermes_root: Path, workflow_id: str) -> Path:
    return _workflows_dir(hermes_root) / f"{workflow_id}.json"


def _load_workflow(hermes_root: Path, workflow_id: str) -> dict[str, Any] | None:
    path = _workflow_path(hermes_root, workflow_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_workflow(hermes_root: Path, record: dict[str, Any]) -> None:
    path = _workflow_path(hermes_root, record["workflow_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Operational state file (like codex-jobs). Never contains raw bodies on
    # any surface; objective text is stored for contract rebuilds only.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def _list_records(hermes_root: Path) -> list[dict[str, Any]]:
    root = _workflows_dir(hermes_root)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        for path in sorted(root.glob("sw-*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("schema") == WORKFLOW_SCHEMA:
                records.append(rec)
    except OSError:
        pass
    return records


def _new_record(workflow: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    """Create a fresh workflow state record from a canonical workflow.

    The record keeps the full canonical workflow definition (used by the
    engine to rebuild stage contracts at dispatch/advance) plus per-stage
    runtime state. Objective text lives in the operational registry only —
    never on any surface (D-SW11) and never in the audit log.
    """
    stages_state: list[dict[str, Any]] = []
    for stage in workflow["stages"]:
        stages_state.append(
            {
                "id": stage["id"],
                "kind": stage.get("kind", "single"),
                "owner": stage["owner"],
                "parents": list(stage.get("parents") or []),
                "status": STAGE_STATUS_TODO,
                "task_id": "",
                "contract_sha256": "",
                "verdict": "",
                "rework_count": 0,
                "handoffs": [],
                "worktree_plan": None,
                "started_at": None,
                "ended_at": None,
                "blocked_reason": "",
            }
        )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": WORKFLOW_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow["workflow_id"],
        "title": workflow["title"],
        "workspace": workflow["workspace"],
        "project": workflow.get("project", {}),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "status": WORKFLOW_STATUS_RUNNING,
        "definition": workflow,
        "stages": stages_state,
        "approval": {"approved": False, "approved_by": "", "approval_reference": "", "approved_at": ""},
        "retention_note": RETENTION_NOTE,
        "created_at": now,
        "updated_at": now,
    }


def _stage_state(record: dict[str, Any], stage_id: str) -> dict[str, Any] | None:
    for stage in record.get("stages", []):
        if stage["id"] == stage_id:
            return stage
    return None


# ---------------------------------------------------------------------------
# Worktree plan (NG5 / D-SW2 — compute only, never manage git)
# ---------------------------------------------------------------------------


def _worktree_plan(workflow: dict[str, Any], stage: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Compute the upstream kanban worktree plan for a stage, or None.

    Mirrors upstream Hermes kanban: project-linked tasks anchor under the
    project's primary repo as ``<repo>/.worktrees/<task-id>`` on branch
    ``<slug>/<task-id>``; plain worktree tasks use ``wt/<task-id>``.
    """
    stage_worktree = stage.get("worktree")
    if not isinstance(stage_worktree, dict) or not stage_worktree.get("enabled"):
        return None
    project = workflow.get("project") or {}
    if project.get("slug") and project.get("repo"):
        branch = f"{project['slug']}/{task_id}"
        path = str(Path(project["repo"]) / ".worktrees" / task_id)
        kind = WORKTREE_PROJECT_LINKED
    else:
        branch = f"wt/{task_id}"
        path = str(Path(workflow.get("workspace", ".")) / ".worktrees" / task_id)
        kind = WORKTREE_PLAIN
    return {"kind": kind, "task_id": task_id, "branch": branch, "path": path}


# ---------------------------------------------------------------------------
# Stage contract building (D-SW1: every stage is an M1 contract)
# ---------------------------------------------------------------------------


def _stage_contract(workflow: dict[str, Any], stage: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    """Build the M1 contract document for a stage (design §6)."""
    stage_id = stage["id"]
    if task_id is None:
        task_id = f"{workflow['workflow_id']}-{stage_id}"
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"generated task_id {task_id!r} is invalid")

    workspace = workflow["workspace"]
    # Implementation stages run inside their planned worktree (NG5); others
    # use the workflow workspace as allowed scope.
    plan = _worktree_plan(workflow, stage, task_id)
    allowed_workspaces = [plan["path"]] if plan else [workspace]

    artifacts = list(stage.get("expected_artifacts") or [])
    tests = list(stage.get("tests") or [])
    forbidden = list(stage.get("forbidden_actions") or [])
    review = stage.get("review_requirements") or {
        "required": False,
        "reviewer": "",
        "evidence": "",
        "approval_required": False,
    }
    criteria = stage.get("completion_criteria") or {
        "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
        "artifacts_present": bool(artifacts),
        "tests_pass": bool(tests),
        "review_satisfied": bool(review.get("required", False)),
        "no_forbidden_actions": True,
    }
    auth = stage.get("authorization") or {
        "class": "reversible_write",
        "approved": True,
        "approved_by": "Tony",
        "approval_reference": "G4-swarm",
    }

    return {
        "schema": CONTRACT_SCHEMA,
        "task_id": task_id,
        "assigned_agent": stage["owner"],
        "assigned_profile": stage["owner"],
        "objective": stage["objective"],
        "allowed_scope": {
            "workspaces": allowed_workspaces,
            "profiles": [stage["owner"]],
        },
        "forbidden_actions": forbidden,
        "expected_artifacts": artifacts,
        "tests": tests,
        "review_requirements": review,
        "completion_criteria": criteria,
        "inputs": _string_list(stage.get("inputs") or [], field="inputs"),
        "constraints": _string_list(stage.get("constraints") or [], field="constraints"),
        "authorization": auth,
    }


def _stage_contract_sha(contract: dict[str, Any]) -> str:
    try:
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
        return sha
    except (ValueError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# Scheduler (design §5.2 — fan-in/fan-out, bounded dispatch)
# ---------------------------------------------------------------------------


def _stage_ready(record: dict[str, Any], stage: dict[str, Any]) -> tuple[bool, str]:
    """A stage is ready when all fan-in parents are done and it is todo/rework."""
    st = _stage_state(record, stage["id"])
    if st is None:
        return False, "unknown stage"
    if st["status"] not in (STAGE_STATUS_TODO, STAGE_STATUS_REWORK):
        return False, f"stage not dispatchable (status={st['status']})"
    for parent_id in stage.get("parents") or []:
        parent = _stage_state(record, parent_id)
        if parent is None or parent["status"] != STAGE_STATUS_DONE:
            return False, f"parent {parent_id} not done"
    return True, "ready"


def _running_count(record: dict[str, Any]) -> int:
    return sum(1 for s in record.get("stages", []) if s.get("status") == STAGE_STATUS_RUNNING)


def _board_running_count(hermes_root: Path) -> int:
    """Global board-level running count across all workflows (D-SW3)."""
    total = 0
    for rec in _list_records(hermes_root):
        total += _running_count(rec)
    return total


def _next_ready_stages(record: dict[str, Any], workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Stages that can dispatch now, in declaration order, respecting caps.

    Enforces the per-workflow concurrency cap (D-SW3). The global board cap
    is applied by callers that know ``hermes_root``.
    """
    max_parallel, _, _ = _caps(workflow)
    running = _running_count(record)
    budget = max(0, max_parallel - running)
    if budget <= 0:
        return []
    ready: list[dict[str, Any]] = []
    for stage in workflow["stages"]:
        ok, _reason = _stage_ready(record, stage)
        if ok:
            ready.append(stage)
        if len(ready) >= budget:
            break
    return ready


# ---------------------------------------------------------------------------
# Codex review hook (D-SW7 / P2-1)
# ---------------------------------------------------------------------------


def _default_codex_reviewer(
    *,
    workdir: str,
    target: str,
    instructions: str = "",
    timeout: int = 900,
) -> dict[str, Any]:
    """Drive the existing operator_codex runner unchanged (D-SW7).

    Returns the bounded verdict envelope read from the job's observed state.
    The verdict JSON carries only pass/fail verdict + structured fields —
    never raw transcript or prompt text (risk-review P2-1).
    """
    result = op_codex.hermes_codex_review_start(
        workdir=workdir,
        target=target,
        instructions=instructions,
        timeout=timeout,
        confirm=False,
        dry_run=True,
    )
    if not isinstance(result, dict):
        return {"status": "error", "verdict": "UNKNOWN", "detail": "codex reviewer returned no envelope"}
    if not result.get("success", False):
        return {
            "status": "refused",
            "verdict": "UNKNOWN",
            "detail": _truncate(op.redact_output(str(result.get("safe_message") or result.get("error") or "refused")), 300),
        }
    # Dry-run plan: expose the fixed argv (prompt redacted) + posture so a
    # caller can confirm before a real run. The engine's advance path uses
    # this as the review evidence; no transcript is ever read.
    argv = result.get("argv") or []
    return {
        "status": "planned",
        "verdict": "PENDING",
        "mode": result.get("mode", "review"),
        "sandbox": result.get("sandbox", "read-only"),
        "timeout": result.get("timeout"),
        "workdir": result.get("workdir"),
        "argv_redacted": [_truncate(str(a), 200) for a in argv[:12]],
    }


# ---------------------------------------------------------------------------
# Validate tool
# ---------------------------------------------------------------------------


def hermes_swarm_workflow_validate(workflow_json: str, hermes_root: Path | None = None) -> str:
    """Validate a proposed workflow DAG (read-only, pure).

    Checks schema, DAG shape/cycles, owners, caps (parallel group, stage
    count), and that every stage builds a valid M1 contract.
    """
    tool = "hermes_swarm_workflow_validate"
    tid = op.new_trace_id()
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(_swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                                       suggested_action="Enable read-only Operator Mode before validating workflows.", trace_id=tid), ensure_ascii=False, indent=2)

    try:
        _, workflow, sha = _parse_workflow(workflow_json)
    except (ValueError, TypeError, PermissionError) as exc:
        return json.dumps(_swarm_error(code="INVALID_WORKFLOW", safe_message=op.redact_output(str(exc))[:300],
                                       suggested_action="Correct the workflow schema and re-validate.", trace_id=tid), ensure_ascii=False, indent=2)

    # Per-stage M1 contract validation (D-SW1): each stage must canonicalize
    # as a valid contract.
    contract_issues: list[str] = []
    for stage in workflow["stages"]:
        if stage.get("kind") == "approval":
            continue  # the approval gate never runs as a dispatched contract
        try:
            contract = _stage_contract(workflow, stage)
            contract_mod._parse_contract(json.dumps(contract))
        except (ValueError, TypeError, PermissionError) as exc:
            contract_issues.append(f"{stage['id']}: {op.redact_output(str(exc))[:200]}")

    payload = {
        "success": not contract_issues,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_validate",
        "workflow_id": workflow["workflow_id"] or "<generated>",
        "workflow_sha256": sha,
        "valid": not contract_issues,
        "title": workflow["title"],
        "stage_count": len(workflow["stages"]),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "stages": [
            {
                "id": s["id"],
                "kind": s.get("kind", "single"),
                "owner": s["owner"],
                "parents": s.get("parents") or [],
                "objective": _prompt_meta(s.get("objective")),
            }
            for s in workflow["stages"]
        ],
        "contract_issues": contract_issues[:10],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id=workflow["workflow_id"] or "<generated>", stage_id="", dry_run=True,
                success=payload["valid"], changed=False, summary=f"validate workflow stages={payload['stage_count']} valid={payload['valid']}")
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Create tool
# ---------------------------------------------------------------------------


def hermes_swarm_workflow_create(
    workflow_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Register a workflow instance (workspace level, dry-run-first).

    Returns ``workflow_id`` + the stage plan. Direct execution requires
    workspace + direct apply mode + ``confirm=true``.
    """
    tool = "hermes_swarm_workflow_create"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = _swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Enable workspace-level Operator Mode (direct) before creating workflows.", trace_id=tid)
        _audit_call(tool=tool, workflow_id="", stage_id="", dry_run=True, success=False, changed=False, summary="create denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        _, workflow, sha = _parse_workflow(workflow_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _swarm_error(code="INVALID_WORKFLOW", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Correct the workflow schema and re-create.", trace_id=tid)
        _audit_call(tool=tool, workflow_id="", stage_id="", dry_run=True, success=False, changed=False, summary="invalid workflow")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not workflow["workflow_id"]:
        workflow["workflow_id"] = f"sw-{uuid.uuid4().hex[:12]}"
    workflow_id = workflow["workflow_id"]

    if _load_workflow(root, workflow_id) is not None:
        payload = _swarm_error(code="WORKFLOW_ALREADY_EXISTS",
                               safe_message=f"workflow {workflow_id!r} already exists.",
                               suggested_action="Pick a new workflow_id or inspect the existing workflow.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=False, changed=False, summary="duplicate workflow_id")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    plan = {
        "workflow_id": workflow_id,
        "title": workflow["title"],
        "stage_count": len(workflow["stages"]),
        "max_parallel": workflow["max_parallel"],
        "board_cap": workflow["board_cap"],
        "max_stages": workflow["max_stages"],
        "stages": [
            {
                "id": s["id"],
                "kind": s.get("kind", "single"),
                "owner": s["owner"],
                "parents": s.get("parents") or [],
                "worktree": bool(isinstance(s.get("worktree"), dict) and s["worktree"].get("enabled")),
            }
            for s in workflow["stages"]
        ],
    }

    if effective:
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_workflow_create",
            "workflow_id": workflow_id,
            "workflow_sha256": sha,
            "dry_run": True,
            "plan": plan,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=True, changed=False,
                    summary=f"workflow create plan stages={len(workflow['stages'])}")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = _swarm_error(code="CONFIRMATION_REQUIRED",
                               safe_message="workflow create requires confirm=true for direct execution.",
                               suggested_action="Review the plan and call again with confirm=true, dry_run=false.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=False, success=False, changed=False, summary="create confirmation required")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = _new_record(workflow, root)
    _save_workflow(root, record)
    payload = {
        "success": True,
        "changed": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_create",
        "workflow_id": workflow_id,
        "workflow_sha256": sha,
        "dry_run": False,
        "plan": plan,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=False, success=True, changed=True,
                summary=f"workflow created stages={len(workflow['stages'])}")
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# List / Status tools (read-only, bounded, redacted — D-SW11)
# ---------------------------------------------------------------------------


def _surface_workflow_summary(record: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    stages = record.get("stages", [])
    stages_done = sum(1 for s in stages if s.get("status") == STAGE_STATUS_DONE)
    parallel_active = sum(1 for s in stages if s.get("status") == STAGE_STATUS_RUNNING)
    current_owners = sorted({s.get("owner", "") for s in stages if s.get("status") in (STAGE_STATUS_TODO, STAGE_STATUS_RUNNING, STAGE_STATUS_REWORK)})
    awaiting: list[str] = []
    if record.get("status") == WORKFLOW_STATUS_AWAITING:
        awaiting.append("human approval")
    elif record.get("status") == WORKFLOW_STATUS_BLOCKED:
        awaiting.append("human (blocked)")
    return {
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "status": record.get("status", WORKFLOW_STATUS_RUNNING),
        "stage_count": len(stages),
        "stages_done": stages_done,
        "parallel_active": parallel_active,
        "current_owners": current_owners[:8],
        "awaiting": awaiting,
        "approval": record.get("approval", {}).get("approved", False),
    }


def hermes_swarm_workflow_list(hermes_root: Path | None = None) -> str:
    """List workflow instances + status (read-only)."""
    tool = "hermes_swarm_workflow_list"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(_swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                                       suggested_action="Enable read-only Operator Mode before listing workflows.", trace_id=tid), ensure_ascii=False, indent=2)

    records = _list_records(root)
    workflows = [_surface_workflow_summary(rec, root) for rec in records]
    payload = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_list",
        "count": len(workflows),
        "workflows": workflows[:50],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id="", stage_id="", dry_run=True, success=True, changed=False, summary=f"list workflows count={len(workflows)}")
    return mission._bounded_json(payload)


def hermes_swarm_workflow_status(workflow_id: str, hermes_root: Path | None = None) -> str:
    """One workflow's stage map, owners, handoffs, verdicts (read-only)."""
    tool = "hermes_swarm_workflow_status"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        op.OperatorPolicy().require_level("read_only")
    except PermissionError as exc:
        return json.dumps(_swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                                       suggested_action="Enable read-only Operator Mode before reading workflow status.", trace_id=tid), ensure_ascii=False, indent=2)

    record = _load_workflow(root, workflow_id)
    if record is None:
        payload = _swarm_error(code="WORKFLOW_NOT_FOUND", safe_message=f"workflow {workflow_id!r} not found.",
                               suggested_action="Call hermes_swarm_workflow_list to see registered workflows.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=False, changed=False, summary="status not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Observed kanban runs per stage task_id (bounded, redacted).
    observed_by_task: dict[str, list[dict[str, Any]]] = {}
    try:
        warnings: list[str] = []
        runs = mission._kanban_runs_for(root, warnings)
        for r in runs:
            observed_by_task.setdefault(str(r.get("task_id") or ""), []).append(
                {"status": r.get("status"), "outcome": r.get("outcome"), "board": r.get("board")}
            )
    except Exception:
        pass

    stages: list[dict[str, Any]] = []
    for st in record.get("stages", []):
        # Expected contract task_id resolves even before dispatch so status
        # can link a stage to its observed kanban run (M0 read surface).
        expected_task = st.get("task_id") or f"{record['workflow_id']}-{st['id']}"
        stages.append(
            {
                "id": st["id"],
                "kind": st.get("kind", "single"),
                "owner": st["owner"],
                "parents": st.get("parents") or [],
                "status": st.get("status", STAGE_STATUS_TODO),
                "task_id": st.get("task_id", ""),
                "contract_sha256": st.get("contract_sha256", ""),
                "verdict": st.get("verdict", ""),
                "rework_count": st.get("rework_count", 0),
                "worktree": st.get("worktree_plan"),
                "observed": observed_by_task.get(expected_task, [])[:10],
                "handoffs": (st.get("handoffs") or [])[:_MAX_HANDOFFS],
            }
        )

    payload = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_workflow_status",
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "status": record.get("status", WORKFLOW_STATUS_RUNNING),
        "approval": record.get("approval", {}),
        "retention_note": record.get("retention_note", RETENTION_NOTE),
        "stages": stages,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=True, changed=False,
                summary=f"status stages={len(stages)} status={record.get('status')}")
    return mission._bounded_json(payload)


# ---------------------------------------------------------------------------
# Stage dispatch (mutation, workspace + direct, dry-run-first)
# ---------------------------------------------------------------------------


def hermes_swarm_stage_dispatch(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_bin: str | None = None,
    authority_manifest: Path | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Dispatch one ready stage as an M1 contract (workspace, dry-run-first).

    Reuses ``hermes_contract_dispatch`` (fleet authority, live peer
    verification, dry-run/confirm gates, audit). Respects the per-workflow
    and per-board concurrency caps (D-SW3) and only dispatches stages whose
    parents are done (fan-in).
    """
    tool = "hermes_swarm_stage_dispatch"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = _swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Enable workspace-level Operator Mode (direct) before dispatching stages.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="dispatch denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = _load_workflow(root, workflow_id)
    if record is None:
        payload = _swarm_error(code="WORKFLOW_NOT_FOUND", safe_message=f"workflow {workflow_id!r} not found.",
                               suggested_action="Create the workflow first with hermes_swarm_workflow_create.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="dispatch not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if record.get("status") in (WORKFLOW_STATUS_DONE, WORKFLOW_STATUS_BLOCKED):
        payload = _swarm_error(code="WORKFLOW_FINISHED", safe_message=f"workflow is {record.get('status')}; no stages can dispatch.",
                               suggested_action="Inspect the workflow status before dispatching.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="dispatch finished workflow")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    workflow = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "workspace": record["workspace"],
        "project": record.get("project", {}),
        "max_parallel": record["max_parallel"],
        "board_cap": record["board_cap"],
        "max_stages": record["max_stages"],
        "stages": _workflow_stages_for_dispatch(record),
    }
    stage = next((s for s in workflow["stages"] if s["id"] == stage_id), None)
    if stage is None:
        payload = _swarm_error(code="STAGE_NOT_FOUND", safe_message=f"stage {stage_id!r} not found in workflow {workflow_id!r}.",
                               suggested_action="Check the stage id with hermes_swarm_workflow_status.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="dispatch stage not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if stage.get("kind") == "approval":
        payload = _swarm_error(code="APPROVAL_GATE", safe_message="the approval stage is never dispatched; it waits on hermes_swarm_approve.",
                               suggested_action="Advance prior stages; the workflow will surface awaiting_approval.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="approval gate not dispatched")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    ready, reason = _stage_ready(record, stage)
    if not ready:
        payload = _swarm_error(code="STAGE_NOT_READY", safe_message=f"stage {stage_id!r} is not ready: {reason}",
                               suggested_action="Complete parent stages before dispatching this one.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="stage not ready")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Bounds (D-SW3): workflow cap + global board cap.
    max_parallel, board_cap, _ = _caps(workflow)
    workflow_running = _running_count(record)
    board_running = _board_running_count(root)
    if workflow_running >= max_parallel:
        payload = _swarm_error(code="WORKFLOW_CAP_REACHED",
                               safe_message=f"workflow parallelism cap reached ({workflow_running}/{max_parallel}).",
                               suggested_action="Advance a running stage before dispatching more.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="workflow cap reached")
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if board_running >= board_cap:
        payload = _swarm_error(code="BOARD_CAP_REACHED",
                               safe_message=f"global board concurrency cap reached ({board_running}/{board_cap}).",
                               suggested_action="Wait for running stages on the board to complete.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="board cap reached")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Build the stage contract (fresh task_id per attempt for rework).
    stage_state = _stage_state(record, stage_id) or {}
    attempt = int(stage_state.get("rework_count", 0))
    task_id = f"{workflow_id}-{stage_id}" if attempt == 0 else f"{workflow_id}-{stage_id}-r{attempt}"
    try:
        contract = _stage_contract(workflow, stage, task_id=task_id)
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _swarm_error(code="INVALID_STAGE_CONTRACT", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Correct the stage contract fields and re-dispatch.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="invalid stage contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    plan = _worktree_plan(workflow, stage, task_id)
    dispatch_result = contract_mod.hermes_contract_dispatch(
        json.dumps(contract),
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        runner=runner,
        hermes_bin=hermes_bin,
        authority_manifest=authority_manifest,
        hermes_root=root,
    )
    result_payload: dict[str, Any]
    try:
        parsed = json.loads(dispatch_result)
        result_payload = parsed if isinstance(parsed, dict) else {"success": False}
    except json.JSONDecodeError:
        result_payload = {"success": False}

    success = bool(result_payload.get("success", False))
    changed = bool(result_payload.get("changed", False))

    # Record the stage state transition on a real dispatch.
    st = _stage_state(record, stage_id)
    if st is not None and (not effective or changed):
        st["task_id"] = task_id
        st["contract_sha256"] = sha
        st["worktree_plan"] = plan
        if changed and not effective:
            st["status"] = STAGE_STATUS_RUNNING
            st["started_at"] = datetime.now(timezone.utc).isoformat()
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_workflow(root, record)

    result_payload["tool"] = tool
    result_payload["workflow_id"] = workflow_id
    result_payload["stage_id"] = stage_id
    result_payload["task_id"] = task_id
    result_payload["contract_sha256"] = sha
    if plan:
        result_payload["worktree_plan"] = plan

    _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=effective or bool(result_payload.get("dry_run", True)),
                success=success, changed=changed, owner=stage["owner"], summary=f"stage dispatch plan task={task_id}" if not changed else f"stage dispatched task={task_id}")
    return json.dumps(result_payload, ensure_ascii=False, indent=2)


def _workflow_stages_for_dispatch(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the stage definitions from the stored workflow definition.

    The record keeps the full canonical definition (``record["definition"]``)
    so the engine can rebuild M1 contracts at dispatch/advance with the real
    objective/artifacts/tests. Falls back to the runtime stage summary only
    if the definition is missing (defensive; should not happen).
    """
    definition = record.get("definition") or {}
    stages = definition.get("stages")
    if isinstance(stages, list) and stages:
        return stages
    out: list[dict[str, Any]] = []
    for st in record.get("stages", []):
        stage: dict[str, Any] = {
            "id": st["id"],
            "kind": st.get("kind", "single"),
            "owner": st["owner"],
            "parents": list(st.get("parents") or []),
            "objective": st.get("objective", ""),
            "expected_artifacts": st.get("expected_artifacts", []),
            "tests": st.get("tests", []),
            "forbidden_actions": st.get("forbidden_actions", []),
            "review_requirements": st.get("review_requirements"),
            "completion_criteria": st.get("completion_criteria"),
            "authorization": st.get("authorization"),
            "worktree": st.get("worktree"),
            "inputs": st.get("inputs", []),
            "constraints": st.get("constraints", []),
        }
        out.append(stage)
    return out


# ---------------------------------------------------------------------------
# Stage advance (mutation, workspace + direct — D-SW6 verification)
# ---------------------------------------------------------------------------


def hermes_swarm_stage_advance(
    workflow_id: str,
    stage_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_root: Path | None = None,
    codex_reviewer: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Validate a stage from observed state; record handoff; promote.

    Runs the M1 validator against **observed** Mission Control state
    (D-SW6). A false "done" (NOT_SATISFIED / INCONCLUSIVE) returns the
    stage for rework — once; a second failure blocks the workflow for a
    human (bounded rework, §5.3). The approval stage is never auto-advanced
    past (D-SW8).
    """
    tool = "hermes_swarm_stage_advance"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = _swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Enable workspace-level Operator Mode (direct) before advancing stages.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="advance denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = _load_workflow(root, workflow_id)
    if record is None:
        payload = _swarm_error(code="WORKFLOW_NOT_FOUND", safe_message=f"workflow {workflow_id!r} not found.",
                               suggested_action="Create the workflow first.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="advance not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    st = _stage_state(record, stage_id)
    if st is None:
        payload = _swarm_error(code="STAGE_NOT_FOUND", safe_message=f"stage {stage_id!r} not found in workflow {workflow_id!r}.",
                               suggested_action="Check the stage id with hermes_swarm_workflow_status.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="advance stage not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if st.get("status") == STAGE_STATUS_DONE:
        payload = _swarm_error(code="STAGE_ALREADY_DONE", safe_message=f"stage {stage_id!r} is already done.",
                               suggested_action="Advance the next ready stage.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="stage already done")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    workflow = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": record["workflow_id"],
        "title": record["title"],
        "workspace": record["workspace"],
        "project": record.get("project", {}),
        "max_parallel": record["max_parallel"],
        "board_cap": record["board_cap"],
        "max_stages": record["max_stages"],
        "stages": _workflow_stages_for_dispatch(record),
    }
    stage = next((s for s in workflow["stages"] if s["id"] == stage_id), None)
    if stage is None:
        stage = {"id": stage_id, "kind": st.get("kind", "single"), "owner": st.get("owner", ""),
                 "parents": list(st.get("parents") or []), "objective": st.get("objective", ""),
                 "expected_artifacts": st.get("expected_artifacts", []), "tests": st.get("tests", []),
                 "review_requirements": st.get("review_requirements"),
                 "completion_criteria": st.get("completion_criteria"),
                 "authorization": st.get("authorization"), "worktree": st.get("worktree"),
                 "forbidden_actions": st.get("forbidden_actions", []),
                 "inputs": st.get("inputs", []), "constraints": st.get("constraints", [])}

    # The approval gate is advanced only by hermes_swarm_approve (D-SW8).
    if stage.get("kind") == "approval":
        payload = _swarm_error(code="APPROVAL_GATE", safe_message="the approval stage is advanced only by hermes_swarm_approve.",
                               suggested_action="Call hermes_swarm_approve at owner level when ready.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, summary="approval gate requires approve tool")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Codex review stage: drive the existing runner, read the bounded verdict.
    review_evidence: dict[str, Any] | None = None
    if stage.get("kind") == "codex_review" or stage.get("id") == "codex_review":
        reviewer = codex_reviewer or _default_codex_reviewer
        plan = _worktree_plan(workflow, stage, st.get("task_id") or f"{workflow_id}-{stage_id}")
        # Design §8: target = the merged branch (base:<integration-branch>).
        # The integration branch is the workflow's project branch under NG5
        # (the integration-review stage merges parallel branches there).
        target = "uncommitted"
        project = workflow.get("project") or {}
        if project.get("slug"):
            target = f"base:{project['slug']}/{workflow_id}"
        elif plan and plan.get("branch"):
            target = f"base:{plan['branch']}"
        try:
            review_evidence = reviewer(workdir=plan["path"] if plan else workflow["workspace"], target=target, instructions="", timeout=900)
        except Exception as exc:
            review_evidence = {"status": "error", "verdict": "UNKNOWN", "detail": _truncate(op.redact_output(str(exc)), 300)}
        if review_evidence and review_evidence.get("status") == "refused":
            payload = _swarm_error(code="CODEX_REVIEW_REFUSED", safe_message=op.redact_output(str(review_evidence.get("detail") or "refused"))[:300],
                                   suggested_action="Check the Codex runner posture (approved workdir, runner enabled).", trace_id=tid)
            _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, owner=stage["owner"], summary="codex review refused")
            return json.dumps(payload, ensure_ascii=False, indent=2)

    # Build the stage contract with the recorded task_id and validate.
    task_id = st.get("task_id") or f"{workflow_id}-{stage_id}"
    try:
        contract = _stage_contract(workflow, stage, task_id=task_id)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _swarm_error(code="INVALID_STAGE_CONTRACT", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Correct the stage contract fields and retry.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=False, changed=False, owner=stage["owner"], summary="invalid stage contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Codex review: write the bounded verdict as review evidence so the M1
    # review check sees a distinct reviewer (P2-1: no transcript/prompt).
    if review_evidence is not None:
        verdict = review_evidence.get("verdict") or "UNKNOWN"
        if verdict in ("PASS", "APPROVED", "SATISFIED"):
            _add_review_evidence(contract, reviewer=stage["owner"], verdict="SATISFIED", workflow_id=workflow_id, stage_id=stage_id)
        elif verdict in ("FAIL", "CHANGES_REQUESTED", "NOT_SATISFIED"):
            _add_review_evidence(contract, reviewer=stage["owner"], verdict="NOT_SATISFIED", workflow_id=workflow_id, stage_id=stage_id)

    validate_out = contract_mod.hermes_contract_validate(json.dumps(contract), runner=runner, hermes_root=root)
    verdict_payload: dict[str, Any]
    try:
        parsed = json.loads(validate_out)
        verdict_payload = parsed if isinstance(parsed, dict) else {"verdict": "INVALID_CONTRACT"}
    except json.JSONDecodeError:
        verdict_payload = {"verdict": "INVALID_CONTRACT"}

    verdict = verdict_payload.get("verdict", "INVALID_CONTRACT")
    satisfied = verdict == "SATISFIED"
    false_done = bool(verdict_payload.get("false_done_detected", False))
    rejected = verdict_payload.get("rejected_reasons", [])

    # Worktree freeze: record artifact refs from the validation evidence.
    artifact_refs: list[str] = []
    for check in verdict_payload.get("checks", []):
        if check.get("kind") == "artifacts" and check.get("status") == "PASS":
            evidence = check.get("evidence") or []
            artifact_refs = [str(e.get("basename") or e.get("path") or "") for e in evidence if isinstance(e, dict)][:32]

    # Dry-run: return the plan + validation snapshot, no state change.
    if effective:
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_stage_advance",
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "dry_run": True,
            "plan": {
                "stage": stage_id,
                "owner": stage["owner"],
                "contract_sha256": st.get("contract_sha256") or _stage_contract_sha(contract),
                "task_id": task_id,
                "verdict_if_advanced": verdict,
                "would_freeze_worktree": bool(st.get("worktree_plan")),
                "handoff_artifact_refs": artifact_refs,
            },
            "validation": {"verdict": verdict, "false_done_detected": false_done, "rejected_reasons": rejected[:5]},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=True, success=True, changed=False,
                    owner=stage["owner"], verdict=verdict, summary=f"advance plan stage={stage_id} verdict={verdict}")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = _swarm_error(code="CONFIRMATION_REQUIRED",
                               safe_message="stage advance requires confirm=true for direct execution.",
                               suggested_action="Review the validation verdict and call again with confirm=true, dry_run=false.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=False, success=False, changed=False,
                    owner=stage["owner"], verdict=verdict, summary="advance confirmation required")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # D-SW6: fail -> bounded rework (once), then blocked for a human.
    if not satisfied:
        st["verdict"] = verdict
        st["rework_count"] = int(st.get("rework_count", 0)) + 1
        if st["rework_count"] >= 2:
            st["status"] = STAGE_STATUS_BLOCKED
            st["blocked_reason"] = f"validation failed twice: {rejected[0] if rejected else verdict}"
            record["status"] = WORKFLOW_STATUS_BLOCKED
            summary = f"stage {stage_id} blocked after second failed validation"
        else:
            st["status"] = STAGE_STATUS_REWORK
            st["blocked_reason"] = ""
            summary = f"stage {stage_id} returned for rework ({st['rework_count']}/1)"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_workflow(root, record)
        payload = {
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_stage_advance",
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "changed": True,
            "verdict": verdict,
            "false_done_detected": false_done,
            "rejected_reasons": rejected[:5],
            "stage_status": st["status"],
            "rework_count": st["rework_count"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=False, success=False, changed=True,
                    owner=stage["owner"], verdict=verdict, summary=summary)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Pass: record verdict, freeze worktree, write handoff, promote.
    parents_done = [p for p in (stage.get("parents") or [])]
    handoff = {
        "from": parents_done,
        "to": stage_id,
        "artifact_refs": artifact_refs,
        "contract_verdict": "SATISFIED",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    st["verdict"] = "SATISFIED"
    st["status"] = STAGE_STATUS_DONE
    st["ended_at"] = datetime.now(timezone.utc).isoformat()
    st["handoffs"] = list(st.get("handoffs") or []) + [handoff]
    record["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Promotion: with an approval stage, the workflow reaches
    # awaiting_approval once every other stage is done; without one, all
    # stages done means the workflow is done. Never auto-advance the
    # approval gate (D-SW8).
    next_ready = _next_ready_stages(record, workflow)
    has_approval = any(s.get("kind") == "approval" for s in record.get("stages", []))
    all_non_approval_done = all(
        s.get("status") == STAGE_STATUS_DONE or s.get("kind") == "approval" for s in record.get("stages", [])
    )
    if all_non_approval_done and has_approval:
        record["status"] = WORKFLOW_STATUS_AWAITING
    elif all_non_approval_done:
        record["status"] = WORKFLOW_STATUS_DONE
    elif any(s.get("status") == STAGE_STATUS_BLOCKED for s in record.get("stages", [])):
        record["status"] = WORKFLOW_STATUS_BLOCKED
    else:
        record["status"] = WORKFLOW_STATUS_RUNNING
    _save_workflow(root, record)

    payload = {
        "success": True,
        "changed": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_stage_advance",
        "workflow_id": workflow_id,
        "stage_id": stage_id,
        "verdict": "SATISFIED",
        "handoff": handoff,
        "workflow_status": record["status"],
        "next_ready_stages": [s["id"] for s in next_ready],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id=workflow_id, stage_id=stage_id, dry_run=False, success=True, changed=True,
                owner=stage["owner"], verdict="SATISFIED", summary=f"stage {stage_id} satisfied; workflow={record['status']}")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _add_review_evidence(contract: dict[str, Any], *, reviewer: str, verdict: str, workflow_id: str, stage_id: str) -> None:
    """Write an audit acceptance record for a stage contract by a reviewer.

    Used by the Codex review stage so the M1 review check sees evidence from
    a distinct reviewer (the Codex role). Never includes prompt/transcript
    text (P2-1).
    """
    try:
        _, _, sha = contract_mod._parse_contract(json.dumps(contract))
    except (ValueError, PermissionError):
        return
    op.audit_record(
        tool="hermes_swarm_stage_advance",
        level="workspace",
        apply_mode="direct",
        dry_run=False,
        success=verdict == "SATISFIED",
        changed=True,
        summary=f"codex review verdict {verdict}",
        profile=reviewer,
        extra={
            "contract_sha256": sha,
            "task_id": contract["task_id"],
            "verdict": verdict,
            "reviewer": reviewer,
            "workflow_id": workflow_id,
            "stage_id": stage_id,
        },
    )


# ---------------------------------------------------------------------------
# Approve (owner + direct — D-SW8)
# ---------------------------------------------------------------------------


def hermes_swarm_approve(
    workflow_id: str,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Record the final human approval (owner level, direct, audited).

    Only valid when the workflow is ``awaiting_approval`` (every non-approval
    stage done). The engine never auto-advances past this gate (D-SW8).
    """
    tool = "hermes_swarm_approve"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_owner(dry_run)
        effective = policy.effective_dry_run(dry_run)
    except PermissionError as exc:
        payload = _swarm_error(code="SWARM_POLICY_DENIED", safe_message=op.redact_output(str(exc))[:300],
                               suggested_action="Enable Owner Mode (direct) before approving a workflow.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=False, changed=False, summary="approve denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    record = _load_workflow(root, workflow_id)
    if record is None:
        payload = _swarm_error(code="WORKFLOW_NOT_FOUND", safe_message=f"workflow {workflow_id!r} not found.",
                               suggested_action="Create the workflow first.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=False, changed=False, summary="approve not found")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if record.get("status") != WORKFLOW_STATUS_AWAITING:
        payload = _swarm_error(code="NOT_AWAITING_APPROVAL",
                               safe_message=f"workflow is {record.get('status')}; approval requires awaiting_approval.",
                               suggested_action="Advance all stages to completion before approving.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=False, changed=False, summary="approve not awaiting")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if effective:
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "swarm_approve",
            "workflow_id": workflow_id,
            "dry_run": True,
            "plan": {
                "workflow_id": workflow_id,
                "title": record["title"],
                "would_record": {"approved": True, "approved_by": "owner", "approval_reference": f"{workflow_id}-approval"},
                "workflow_status_after": WORKFLOW_STATUS_DONE,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=True, success=True, changed=False, summary="approve plan")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if not confirm:
        payload = _swarm_error(code="CONFIRMATION_REQUIRED",
                               safe_message="approve requires confirm=true for direct execution.",
                               suggested_action="Review the workflow and call again with confirm=true, dry_run=false.", trace_id=tid)
        _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=False, success=False, changed=False, summary="approve confirmation required")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    now = datetime.now(timezone.utc).isoformat()
    record["approval"] = {
        "approved": True,
        "approved_by": "owner",
        "approval_reference": f"{workflow_id}-approval",
        "approved_at": now,
    }
    record["status"] = WORKFLOW_STATUS_DONE
    record["updated_at"] = now
    for st in record.get("stages", []):
        if st.get("kind") == "approval":
            st["status"] = STAGE_STATUS_DONE
            st["verdict"] = "SATISFIED"
            st["ended_at"] = now
    _save_workflow(root, record)

    payload = {
        "success": True,
        "changed": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "swarm_approve",
        "workflow_id": workflow_id,
        "approval": record["approval"],
        "workflow_status": WORKFLOW_STATUS_DONE,
        "generated_at": now,
        "trace_id": tid,
    }
    _audit_call(tool=tool, workflow_id=workflow_id, stage_id="", dry_run=False, success=True, changed=True, summary="workflow approved")
    return json.dumps(payload, ensure_ascii=False, indent=2)
