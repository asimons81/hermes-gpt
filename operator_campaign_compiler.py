"""Campaign → MissionPlan compiler for maintenance campaigns (AUTO-01).

An **additive**, deterministic compiler that turns a campaign definition
(the native-rails migration campaign shape: tracks of tasks with typed
evidence gates) into a canonical ``hermes.mission-plan/v1`` document.

Design rules (BOUNDARY.md: reuse-first, thin additive controller surface):

- **No new store.** The output is a plan document accepted verbatim by
  ``operator_mission_plan.hermes_plan_create`` (after canonicalization) —
  this module never touches SQLite, the Mission lifecycle, or dispatch.
- **No LLM.** Compilation is deterministic: campaign JSON → bounded DAG.
- **Typed evidence gates.** Every campaign task carries its evidence-type
  requirements; the compiler maps each task to a plan node whose
  ``capability_req`` records the required evidence classes, and inserts a
  validation node per track so completion can only be claimed with the
  declared typed evidence.
- **INV-9 data containment.** Task titles/objectives are hashed, never
  stored raw (delegated to ``_canonical_node`` via the ``objective`` field).
- **Bounded.** Node count is capped at ``MAX_NODES``; campaign tasks beyond
  the cap fail closed with a clear error instead of silently truncating.

Campaign schema (``hermes.campaign/v1``)::

    {
      "schema": "hermes.campaign/v1",
      "campaign_id": "native-rails-migration",     # [a-z][a-z0-9_-]{0,63}
      "mission_id": "msn-native-rails",            # valid Mission id
      "objective": "Migrate execution to native rails",  # hashed only
      "tracks": [
        {"track": "a", "profile": "hermes-dev",
         "tasks": [
           {"task": "OPS-01", "kind": "single",
            "evidence": ["sustained"],             # typed evidence required
            "budget": {"est_minutes": 60, "est_tokens": 200000},
            "artifacts": ["evidence.json"],
            "depends_on": []}                      # task ids in this campaign
         ]}
      ]
    }

The compiled DAG shape per track: ``<track>-tasks (parallel) ->
<track>-validate (single, typed-evidence gate)`` and finally
``campaign-validate (single)`` joining all track validators. An explicit
``"approval": true`` on the campaign adds the mandatory owner approval node
(the only node allowed to carry owner authority).
"""

from __future__ import annotations

import json
import re
from typing import Any

import operator_mission_plan as plan_store
from operator_mission_plan import (
    KIND_APPROVAL,
    KIND_PARALLEL,
    KIND_SINGLE,
    MAX_NODES,
    _canonical_plan,
    _objective_meta,
    _plan_sha256,
)

CAMPAIGN_SCHEMA = "hermes.campaign/v1"
DECOMPOSITION_ID = "campaign-compiler-v1"

CAMPAIGN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
TRACK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")

MAX_TRACKS = 8
MAX_TASKS_PER_TRACK = 24
MAX_TASKS_TOTAL = 48

#: Evidence classes a campaign task may declare (typed-evidence gates).
EVIDENCE_TYPES = (
    "implementation",
    "ci",
    "merge",
    "deployment",
    "runtime",
    "sustained",
)

_BUDGET_BOUNDS = {"est_minutes": (0, 10_000), "est_tokens": (0, 10_000_000)}


class CampaignSpecError(ValueError):
    """Raised when a campaign document violates the campaign schema."""


def _req(value: Any, field: str) -> Any:
    if value is None:
        raise CampaignSpecError(f"campaign field {field!r} is required")
    return value


def _clean_id(value: Any, regex: re.Pattern[str], field: str) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if not text or not regex.fullmatch(text):
        raise CampaignSpecError(f"campaign field {field!r} is invalid: {value!r}")
    return text


def _clean_evidence(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise CampaignSpecError(f"campaign field {field!r} must be a non-empty list")
    if len(value) > len(EVIDENCE_TYPES):
        raise CampaignSpecError(f"campaign field {field!r} lists too many evidence types")
    out: list[str] = []
    for item in value:
        if item not in EVIDENCE_TYPES:
            raise CampaignSpecError(
                f"campaign evidence type {item!r} is not one of {list(EVIDENCE_TYPES)}"
            )
        out.append(item)
    if len(set(out)) != len(out):
        raise CampaignSpecError(f"campaign field {field!r} contains duplicates")
    return out


def _clean_budget(value: Any, field: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CampaignSpecError(f"campaign field {field!r} must be an object")
    out: dict[str, int] = {}
    for key, (low, high) in _BUDGET_BOUNDS.items():
        if key not in value:
            continue
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
            raise CampaignSpecError(
                f"campaign budget {field}.{key} must be an int in {low}..{high}"
            )
        out[key] = raw
    return out


def _clean_artifacts(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CampaignSpecError(f"campaign field {field!r} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CampaignSpecError(f"campaign artifact in {field!r} must be a non-empty string")
        out.append(item.strip())
    if len(set(out)) != len(out):
        raise CampaignSpecError(f"campaign field {field!r} contains duplicate artifacts")
    return out


def compile_campaign(campaign_json: str) -> dict[str, Any]:
    """Compile a ``hermes.campaign/v1`` document into a canonical MissionPlan.

    Returns the plan dict (same shape ``decompose_mission`` produces: hashed
    objective metadata, ``plan_sha256``, ``node_count``, no raw objective).
    Raises :class:`CampaignSpecError` on any schema violation — this compiler
    fails closed rather than dropping or truncating campaign work.
    """
    try:
        raw = json.loads(campaign_json)
    except json.JSONDecodeError as exc:
        raise CampaignSpecError(f"campaign_json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CampaignSpecError("campaign must be a JSON object")
    if raw.get("schema") != CAMPAIGN_SCHEMA:
        raise CampaignSpecError(f"campaign schema must be {CAMPAIGN_SCHEMA!r}")

    _clean_id(raw.get("campaign_id"), CAMPAIGN_ID_RE, "campaign_id")
    mission_id = _clean_id(raw.get("mission_id"), plan_store.MISSION_ID_RE, "mission_id")
    objective = raw.get("objective")
    if objective is not None and not isinstance(objective, str):
        raise CampaignSpecError("campaign objective must be a string")
    approval = bool(raw.get("approval", False))

    tracks_raw = _req(raw.get("tracks"), "tracks")
    if not isinstance(tracks_raw, list) or not tracks_raw:
        raise CampaignSpecError("campaign tracks must be a non-empty list")
    if len(tracks_raw) > MAX_TRACKS:
        raise CampaignSpecError(f"campaign exceeds {MAX_TRACKS} tracks")

    total_tasks = 0
    task_ids: set[str] = set()
    track_specs: list[dict[str, Any]] = []
    for tr in tracks_raw:
        if not isinstance(tr, dict):
            raise CampaignSpecError("each campaign track must be an object")
        track_id = _clean_id(tr.get("track"), TRACK_ID_RE, "track")
        profile = tr.get("profile", "default")
        if not isinstance(profile, str) or not profile.strip():
            raise CampaignSpecError(f"track {track_id!r} profile must be a non-empty string")
        tasks_raw = _req(tr.get("tasks"), f"track {track_id!r} tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise CampaignSpecError(f"track {track_id!r} tasks must be a non-empty list")
        if len(tasks_raw) > MAX_TASKS_PER_TRACK:
            raise CampaignSpecError(
                f"track {track_id!r} exceeds {MAX_TASKS_PER_TRACK} tasks"
            )
        tasks: list[dict[str, Any]] = []
        for tk in tasks_raw:
            if not isinstance(tk, dict):
                raise CampaignSpecError(f"track {track_id!r} task must be an object")
            task_id = _clean_id(tk.get("task"), TASK_ID_RE, "task")
            if task_id in task_ids:
                raise CampaignSpecError(f"duplicate campaign task id {task_id!r}")
            task_ids.add(task_id)
            total_tasks += 1
            if total_tasks > MAX_TASKS_TOTAL:
                raise CampaignSpecError(f"campaign exceeds {MAX_TASKS_TOTAL} tasks total")
            kind = tk.get("kind", KIND_SINGLE)
            if kind == KIND_APPROVAL:
                raise CampaignSpecError(
                    f"task {task_id!r}: approval is a campaign-level property, not a task kind"
                )
            if kind not in (KIND_SINGLE, KIND_PARALLEL):
                raise CampaignSpecError(f"task {task_id!r} kind must be single or parallel")
            tasks.append(
                {
                    "task": task_id,
                    "kind": kind,
                    "evidence": _clean_evidence(tk.get("evidence"), f"task {task_id} evidence"),
                    "budget": _clean_budget(tk.get("budget"), f"task {task_id}"),
                    "artifacts": _clean_artifacts(tk.get("artifacts"), f"task {task_id}"),
                    "depends_on": _clean_depends(tk.get("depends_on"), task_id),
                }
            )
        track_specs.append({"track": track_id, "profile": profile, "tasks": tasks})

    # Cross-task dependency closure (deps may reference tasks in other tracks).
    for spec in track_specs:
        for task in spec["tasks"]:
            for dep in task["depends_on"]:
                if dep not in task_ids:
                    raise CampaignSpecError(
                        f"task {task['task']!r} depends on unknown task {dep!r}"
                    )
                if dep == task["task"]:
                    raise CampaignSpecError(f"task {task['task']!r} cannot depend on itself")

    # Node budget: tasks + per-track validators + campaign validator (+approval).
    projected = total_tasks + len(track_specs) + 1 + (1 if approval else 0)
    if projected > MAX_NODES:
        raise CampaignSpecError(
            f"compiled plan would need {projected} nodes (cap {MAX_NODES}); "
            "split the campaign"
        )

    # ---- Build the DAG -------------------------------------------------
    campaign_objective = _objective_meta(objective or "")
    nodes: list[dict[str, Any]] = []

    def _node(
        node_id: str,
        kind: str,
        owner: str,
        parents: list[str],
        objective: str,
        *,
        evidence: list[str] | None = None,
        budget: dict[str, int] | None = None,
        artifacts: list[str] | None = None,
    ) -> None:
        capability: dict[str, Any] = {
            "profile": owner,
            "skills": [],
            "authorization_class": "high_impact"
            if kind == KIND_APPROVAL
            else "reversible_write",
        }
        if artifacts is None:
            # Typed-evidence gate: one verifiable receipt artifact per required
            # evidence class (the plan-node schema's contract carrier — the
            # capability_req canonicalizer admits no extension keys).
            artifacts = [f"evidence-{ev}.json" for ev in (evidence or [])] or [
                "evidence.json"
            ]
        nodes.append(
            {
                "node_id": node_id,
                "kind": kind,
                "owner": owner,
                "parents": parents,
                "objective": objective,
                "capability_req": capability,
                "budget": budget or {"est_minutes": 60, "est_tokens": 200_000},
                "expected_artifacts": artifacts,
                "contract_ref": f"contract:{node_id}",
            }
        )

    validator_parents: list[str] = []
    for spec in track_specs:
        track_id = spec["track"]
        profile = spec["profile"]
        task_node_ids: dict[str, str] = {}
        for task in spec["tasks"]:
            node_id = f"{track_id}-{task['task'].lower()}"
            parents = [
                task_node_ids[dep] if dep in task_node_ids else _task_node_id(track_specs, dep)
                for dep in task["depends_on"]
            ]
            _node(
                node_id,
                task["kind"],
                profile,
                parents,
                f"{task['task']} task",
                evidence=task["evidence"] or None,
                budget=task["budget"] or None,
                artifacts=task["artifacts"] or None,
            )
            task_node_ids[task["task"]] = node_id
        track_evidence = sorted(
            {ev for task in spec["tasks"] for ev in task["evidence"]}
        )
        validator_id = f"{track_id}-validate"
        _node(
            validator_id,
            KIND_SINGLE,
            profile,
            sorted(task_node_ids.values()),
            f"track {track_id} typed-evidence validation",
            evidence=track_evidence or None,
        )
        validator_parents.append(validator_id)

    _node(
        "campaign-validate",
        KIND_SINGLE,
        "default",
        sorted(validator_parents),
        "campaign typed-evidence validation",
    )
    if approval:
        _node(
            "campaign-approval",
            KIND_APPROVAL,
            "owner",
            ["campaign-validate"],
            "campaign owner approval",
            budget={"est_minutes": 0, "est_tokens": 0},
            artifacts=[],
        )

    plan_raw = {
        "schema": plan_store.PLAN_SCHEMA,
        "mission_id": mission_id,
        "version": 1,
        "decomposition": DECOMPOSITION_ID,
        "objective": objective or "",
        "nodes": nodes,
    }
    canonical, compiled = _canonical_plan(plan_raw)
    compiled["objective_len"] = campaign_objective["objective_len"]
    compiled["objective_sha256"] = campaign_objective["objective_sha256"]
    compiled["node_count"] = len(compiled["nodes"])
    compiled["plan_sha256"] = _plan_sha256(canonical)
    compiled.pop("objective", None)
    return compiled


def _clean_depends(value: Any, task_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CampaignSpecError(f"task {task_id!r} depends_on must be a list")
    out = []
    for item in value:
        if not isinstance(item, str) or not TASK_ID_RE.fullmatch(item):
            raise CampaignSpecError(f"task {task_id!r} has invalid depends_on entry {item!r}")
        out.append(item)
    if len(set(out)) != len(out):
        raise CampaignSpecError(f"task {task_id!r} depends_on contains duplicates")
    return out


def _task_node_id(track_specs: list[dict[str, Any]], task_id: str) -> str:
    for spec in track_specs:
        for task in spec["tasks"]:
            if task["task"] == task_id:
                return f"{spec['track']}-{task_id.lower()}"
    raise CampaignSpecError(f"task {task_id!r} has no node")  # pragma: no cover
