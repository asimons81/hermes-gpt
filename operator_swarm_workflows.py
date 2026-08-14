"""Canonical Swarm Orchestration workflow shapes (v0.6 M2).

Holds the validated stage DAGs the swarm engine ships: the canonical
research -> architecture -> (implementation/tests/docs parallel) ->
integration review -> Codex review -> acceptance validation -> human
approval shape from ``docs/design/v0.6-swarm-orchestration.md`` §5.1.

The engine (``operator_swarm.py``) is DAG-generic: any workflow document
that passes ``hermes_swarm_workflow_validate`` may run. This module is the
optional split for the canonical template (design §5.4) so the engine stays
focused on scheduling, contract dispatch, validation hooks, and handoffs.

Owners are the design's defaults (§5.1); a caller may override any owner
when building a workflow. The canonical shape never assigns Codex as an
implementation owner (risk-review P2-3): Codex appears only as the review
stage owner, read-only.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Canonical stage definitions
# ---------------------------------------------------------------------------

# Stage kind values understood by the engine.
KIND_SINGLE = "single"
KIND_PARALLEL = "parallel"
KIND_APPROVAL = "approval"

# Default owners from design §5.1.
DEFAULT_OWNERS = {
    "research": "hermes-researcher",
    "architecture": "hermes-dev",
    "implementation": "hermes-dev",
    "tests": "hermes-dev",
    "docs": "hermes-dev",
    "integration_review": "default",
    "codex_review": "codex",
    "acceptance_validation": "hermes-dev",
    "human_approval": "owner",
}

# Forbidden-action defaults carried into every stage contract (fleet model:
# no raw secrets, no vault/fleet-policy edits, no public actions, plus the
# existing catastrophic git guards from Owner Mode).
DEFAULT_FORBIDDEN_ACTIONS: list[dict[str, Any]] = [
    {"action": "raw_secret_request", "reason": "no raw secret values on any surface", "class": "HIGH"},
    {"action": "vault_policy_edit", "reason": "no vault policy edits", "class": "HIGH"},
    {"action": "fleet_policy_edit", "reason": "no fleet authority edits", "class": "HIGH"},
    {"action": "public_action", "reason": "no public posting/publishing", "class": "HIGH"},
    {"action": "git_add_all", "reason": "no git add -A (catastrophic guard)", "class": "MED"},
    {"action": "force_push", "reason": "no force-push / history rewrite", "class": "HIGH"},
]

# The canonical DAG as (stage_id, kind, parents).
CANONICAL_STAGE_SPECS: list[tuple[str, str, list[str]]] = [
    ("research", KIND_SINGLE, []),
    ("architecture", KIND_SINGLE, ["research"]),
    ("implementation", KIND_PARALLEL, ["architecture"]),
    ("tests", KIND_PARALLEL, ["architecture"]),
    ("docs", KIND_PARALLEL, ["architecture"]),
    ("integration_review", KIND_SINGLE, ["implementation", "tests", "docs"]),
    ("codex_review", KIND_SINGLE, ["integration_review"]),
    ("acceptance_validation", KIND_SINGLE, ["codex_review"]),
    ("human_approval", KIND_APPROVAL, ["acceptance_validation"]),
]


def _stage_objective(stage_id: str, title: str) -> str:
    """A bounded default objective for a canonical stage."""
    return f"{title} — {stage_id.replace('_', ' ')} stage"


def _canonical_stage(
    *,
    stage_id: str,
    kind: str,
    parents: list[str],
    owner: str,
    title: str,
    workspace: str,
    worktree: bool,
    objective: str | None,
) -> dict[str, Any]:
    """Build one canonical stage definition dict.

    Implementation-stage artifacts/tests are left to the caller (per-file
    owners know their files); review stages carry review requirements.
    """
    stage: dict[str, Any] = {
        "id": stage_id,
        "kind": kind,
        "owner": owner,
        "parents": list(parents),
        "objective": objective or _stage_objective(stage_id, title),
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {"required": False, "reviewer": "", "evidence": "", "approval_required": False},
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
            "artifacts_present": bool(kind != KIND_APPROVAL),
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "authorization": {
            "class": "reversible_write" if kind != KIND_APPROVAL else "high_impact",
            "approved": True,
            "approved_by": "Tony",
            "approval_reference": "G4-swarm-approval",
        },
    }
    if worktree:
        # NG5: implementation stages run in an upstream kanban worktree. The
        # engine records the plan; upstream materializes it.
        stage["worktree"] = {"enabled": True, "repo": workspace}
    return stage


def canonical_workflow(
    *,
    title: str,
    workspace: str,
    workflow_id: str | None = None,
    owners: dict[str, str] | None = None,
    objectives: dict[str, str] | None = None,
    implementation_artifacts: list[dict[str, Any]] | None = None,
    tests_artifacts: list[dict[str, Any]] | None = None,
    docs_artifacts: list[dict[str, Any]] | None = None,
    implementation_tests: list[dict[str, Any]] | None = None,
    max_parallel: int | None = None,
    board_cap: int | None = None,
    max_stages: int | None = None,
    project: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical v0.6 workflow document (design §5.1).

    ``workspace`` is the base repo/workspace path; implementation/tests/docs
    stages get ``worktree={"enabled": True, "repo": workspace}`` so the
    engine plans separate upstream worktrees for them (D-SW2/NG5).
    """
    owners = dict(DEFAULT_OWNERS, **(owners or {}))
    objectives = objectives or {}
    stages: list[dict[str, Any]] = []
    for stage_id, kind, parents in CANONICAL_STAGE_SPECS:
        owner = owners.get(stage_id, DEFAULT_OWNERS[stage_id])
        worktree = stage_id in {"implementation", "tests", "docs"}
        stage = _canonical_stage(
            stage_id=stage_id,
            kind=kind,
            parents=parents,
            owner=owner,
            title=title,
            workspace=workspace,
            worktree=worktree,
            objective=objectives.get(stage_id),
        )
        if stage_id == "implementation" and implementation_artifacts:
            stage["expected_artifacts"] = list(implementation_artifacts)
            if implementation_tests:
                stage["tests"] = list(implementation_tests)
        elif stage_id == "tests" and tests_artifacts:
            stage["expected_artifacts"] = list(tests_artifacts)
        elif stage_id == "docs" and docs_artifacts:
            stage["expected_artifacts"] = list(docs_artifacts)
        elif stage_id == "codex_review":
            # Codex review stage: read-only review; completion requires the
            # review verdict (P2-1: bounded verdict JSON, no transcripts).
            stage["review_requirements"] = {
                "required": True,
                "reviewer": "codex",
                "evidence": "codex review verdict JSON",
                "approval_required": False,
            }
            stage["completion_criteria"] = {
                "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
                "artifacts_present": False,
                "tests_pass": False,
                "review_satisfied": True,
                "no_forbidden_actions": True,
            }
        elif stage_id == "human_approval":
            # The final gate never runs as a contract; it waits on
            # hermes_swarm_approve (D-SW8). No artifacts expected.
            stage["expected_artifacts"] = []
            stage["completion_criteria"] = {
                "run_state": {"terminal": True, "outcome_ok": ["completed", "done"]},
                "artifacts_present": False,
                "tests_pass": False,
                "review_satisfied": True,
                "no_forbidden_actions": True,
            }
        stages.append(stage)

    doc: dict[str, Any] = {
        "schema": "hermes.swarm-workflow/v1",
        "title": title,
        "workspace": workspace,
        "stages": stages,
    }
    if workflow_id:
        doc["workflow_id"] = workflow_id
    if max_parallel is not None:
        doc["max_parallel"] = int(max_parallel)
    if board_cap is not None:
        doc["board_cap"] = int(board_cap)
    if max_stages is not None:
        doc["max_stages"] = int(max_stages)
    if project:
        doc["project"] = dict(project)
    return doc
