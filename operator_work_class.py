"""Owner-approval work-class policy table (AUTO-04).

Thin additive classifier over the native authorization axis
(``operator_mission_plan.AUTH_CLASSES``). Reuse-first per BOUNDARY.md:
caller of mission_runtime primitives only; no new store; the ONLY write
path in the whole feature is the existing owner-only
``hermes_mission_approve``.

Policy-table semantics (fail closed everywhere):

- **Routine work classes** are the closed tuple ``ROUTINE_AUTH_CLASSES``
  (``read_only``, ``reversible_write``). Everything else — ``high_impact``,
  approval-kind nodes, missing/unknown ``authorization_class``, malformed
  nodes — is **owner-gated** and can never be preauthorized.
- A Mission may resolve its final Owner gate without a human click only
  when BOTH hold: (1) the campaign document explicitly declares
  ``"preauthorized": true`` (or the equivalent closed ``"work_class"``
  object), and (2) EVERY plan node classifies as routine. A declaration can
  never elevate: any approval-kind or high-impact node forces the ``owner``
  policy regardless of the declaration.
- ``effective_approval_policy`` only ever SETS the mint-time MissionSpec
  ``approval_policy`` field (the documented native feature,
  ``test_mission_approval_policy``): the Mission still parks at
  ``awaiting_approval`` and still completes only through
  ``hermes_mission_approve``. ``final_approval_required`` stays ``True``
  for every controller mint.
- ``resolve_owner_gate`` is the owner-side resolver the native feature
  documents: it re-verifies evidence through read primitives, probes Owner
  Mode WITHOUT fabricating it (no owner env vars, no ack string — ever),
  and then delegates the single mutation to ``hermes_mission_approve``.
  It grants no new authority inside Autopilot: the frontier and Autopilot
  still never approve anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import operator_mission_plan as plan_store
import operator_mission_runtime as mission_runtime
import operator_policy as op

POLICY_TABLE_ID = "work-class-policy/v1"

#: Closed set of authorization classes that count as routine work. Anything
#: else on the native axis — notably ``high_impact`` — is owner-gated.
ROUTINE_AUTH_CLASSES = ("read_only", "reversible_write")

#: Native ``approval_policy.max_wait_seconds`` default (mirrors
#: ``operator_mission_runtime._normalize_approval_policy``).
DEFAULT_MAX_WAIT_SECONDS = 900

#: Owner gate resolution outcomes (closed vocabulary).
REASON_OWNER_GATED = "owner_gated"
REASON_EVIDENCE_NOT_VERIFIED = "evidence_not_verified"
REASON_OWNER_MODE_UNAVAILABLE = "owner_mode_unavailable"
REASON_APPROVE_REJECTED = "approve_rejected"
REASON_MISSION_NOT_FOUND = "mission_not_found"

_OWNER_POLICY = {"policy": "owner"}


class WorkClassError(ValueError):
    """Raised when a campaign work-class declaration is invalid."""


# ---------------------------------------------------------------------------
# Node classification (pure)
# ---------------------------------------------------------------------------

def classify_plan_node(node: Any) -> dict[str, Any]:
    """Classify one plan node against the work-class policy table.

    Returns ``{"work_class": "routine" | "owner_gated", "auth_class": str,
    "preauthorized_eligible": bool, "reason": str}``. Fail closed: anything
    not positively routine is owner-gated.
    """
    if not isinstance(node, dict):
        return {
            "work_class": "owner_gated",
            "auth_class": "",
            "preauthorized_eligible": False,
            "reason": "malformed node",
        }
    if node.get("kind") == plan_store.KIND_APPROVAL:
        capability = node.get("capability_req")
        auth_class = (
            str(capability.get("authorization_class", ""))
            if isinstance(capability, dict)
            else ""
        )
        return {
            "work_class": "owner_gated",
            "auth_class": auth_class,
            "preauthorized_eligible": False,
            "reason": "approval node",
        }
    capability = node.get("capability_req")
    auth_class = (
        str(capability.get("authorization_class", ""))
        if isinstance(capability, dict)
        else ""
    )
    if not auth_class:
        return {
            "work_class": "owner_gated",
            "auth_class": "",
            "preauthorized_eligible": False,
            "reason": "missing authorization_class",
        }
    if auth_class not in ROUTINE_AUTH_CLASSES:
        return {
            "work_class": "owner_gated",
            "auth_class": auth_class,
            "preauthorized_eligible": False,
            "reason": f"authorization_class {auth_class!r} is not a routine work class",
        }
    return {
        "work_class": "routine",
        "auth_class": auth_class,
        "preauthorized_eligible": True,
        "reason": "routine work class",
    }


def mission_approval_policy_for_nodes(nodes: Any) -> dict[str, Any]:
    """Policy implied by a node set: auto only when EVERY node is routine.

    Empty / malformed input fails closed to the ``owner`` policy.
    """
    if not isinstance(nodes, list) or not nodes:
        return _OWNER_POLICY
    for node in nodes:
        if classify_plan_node(node)["work_class"] != "routine":
            return _OWNER_POLICY
    return {
        "policy": "auto_when_evidence_verified",
        "max_wait_seconds": DEFAULT_MAX_WAIT_SECONDS,
    }


# ---------------------------------------------------------------------------
# Campaign declaration (pure)
# ---------------------------------------------------------------------------

def campaign_preauthorized(campaign_json: str) -> bool:
    """Read the campaign's optional preauthorization declaration.

    Absent → ``False``. Honored shapes: top-level ``"preauthorized": bool``
    or a closed ``"work_class": {"preauthorized": bool}`` object mirroring
    the same semantics. Anything else raises :class:`WorkClassError`
    (invalid JSON, non-bool values, unknown ``work_class`` keys, or both
    shapes present at once — ambiguity fails closed).
    """
    try:
        raw = json.loads(campaign_json)
    except json.JSONDecodeError as exc:
        raise WorkClassError(f"campaign_json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkClassError("campaign document must be a JSON object")
    bare = raw.get("preauthorized")
    work_class = raw.get("work_class")
    if work_class is not None:
        if not isinstance(work_class, dict):
            raise WorkClassError('campaign "work_class" must be an object')
        unknown = sorted(set(work_class) - {"preauthorized"})
        if unknown:
            raise WorkClassError(f'campaign "work_class" has unknown keys: {unknown}')
        if bare is not None:
            raise WorkClassError(
                'campaign declares both "preauthorized" and "work_class.preauthorized"'
            )
        if "preauthorized" not in work_class:
            return False
        flag = work_class["preauthorized"]
    elif bare is None:
        return False
    else:
        flag = bare
    if not isinstance(flag, bool):
        raise WorkClassError('campaign "preauthorized" must be a boolean')
    return flag


def effective_approval_policy(campaign_json: str, nodes: Any) -> dict[str, Any]:
    """The policy-table conjunction the controller sets at mint time.

    Auto policy iff the campaign is preauthorized AND every node is routine;
    otherwise ``{"policy": "owner"}``. A declaration can never elevate: any
    approval-kind or high-impact node forces the owner policy regardless.
    """
    if not campaign_preauthorized(campaign_json):
        return _OWNER_POLICY
    return mission_approval_policy_for_nodes(nodes)


# ---------------------------------------------------------------------------
# Read-only surfacing
# ---------------------------------------------------------------------------

def _owner_mode_available(dry_run: bool) -> bool:
    """Probe (never fabricate) Owner Mode: no env writes, no ack string."""
    try:
        op.OperatorPolicy().require_owner(dry_run)
    except PermissionError:
        return False
    return True


def _children_evidence(mission: dict[str, Any]) -> dict[str, Any]:
    """Mirror ``hermes_mission_approve``'s validate() over read-primitive data."""
    attachments = [
        a for a in (mission.get("attachments") or []) if isinstance(a, dict)
    ]
    succeeded = [
        a for a in attachments
        if str(a.get("state")) == "succeeded" and bool(a.get("verified"))
    ]
    invalid = [
        a for a in attachments
        if str(a.get("state")) not in {"succeeded", "cancelled"}
        or (str(a.get("state")) == "succeeded" and not bool(a.get("verified")))
    ]
    return {
        "children": [
            {"kind": str(a.get("kind")), "ref": str(a.get("ref")),
             "state": str(a.get("state")), "verified": bool(a.get("verified"))}
            for a in attachments
        ],
        "terminal_with_verified_success": not invalid and bool(succeeded),
    }


def _surface(mission_id: str, hermes_root: Path | None) -> dict[str, Any]:
    """Bundle the owner-gate picture from read primitives only."""
    mission = json.loads(
        mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root)
    )
    if not mission.get("success") or mission.get("found") is False:
        return {
            "success": False,
            "tool": "surface_owner_gate",
            "mission_id": mission_id,
            "found": False,
            "error": str(mission.get("error", "mission not found")),
        }
    review = json.loads(
        plan_store.hermes_plan_review(mission_id, hermes_root=hermes_root)
    )
    nodes = review.get("nodes") if isinstance(review, dict) else None
    classifications = [
        {"node_id": str(n.get("node_id")), "state": str(n.get("state")),
         **classify_plan_node(n)}
        for n in (nodes or [])
        if isinstance(n, dict)
    ]
    policy = mission.get("approval_policy") or {}
    policy_name = str(policy.get("policy", "owner")) if isinstance(policy, dict) else "owner"
    evidence = _children_evidence(mission)
    owner_ready = _owner_mode_available(False)
    if str(mission.get("status")) != "awaiting_approval":
        hint = "await_mission_reconcile"
    elif not evidence["terminal_with_verified_success"]:
        hint = "verify_children"
    elif policy_name != "auto_when_evidence_verified":
        hint = "owner_approve"  # human click: hermes_mission_approve directly
    elif not owner_ready:
        hint = "enable_owner_mode"
    else:
        hint = "owner_resolve_gate"
    return {
        "success": True,
        "tool": "surface_owner_gate",
        "mission_id": mission_id,
        "found": True,
        "status": str(mission.get("status")),
        "version": int(mission.get("version", 0)),
        "final_approval_required": bool(mission.get("final_approval_required", True)),
        "approval_policy": policy,
        "node_classifications": classifications,
        "children": evidence["children"],
        "children_terminal_with_verified_success": evidence["terminal_with_verified_success"],
        "owner_mode_available": owner_ready,
        "next": hint,
    }


def surface_owner_gate(mission_id: str, hermes_root: Path | None = None) -> str:
    """READ-ONLY owner-gate bundle: status, policy, node classes, children,
    Owner Mode availability, next hint. No mutation, no env writes."""
    return json.dumps(_surface(mission_id, hermes_root))


# ---------------------------------------------------------------------------
# The owner-side resolver (the native feature's documented consumer)
# ---------------------------------------------------------------------------

def resolve_owner_gate(
    mission_id: str,
    hermes_root: Path | None = None,
    approval_reference: str | None = None,
    confirm: bool = True,
    dry_run: bool = False,
) -> str:
    """Resolve a preauthorized Mission's final Owner gate, or park + surface.

    Refusal is ALWAYS non-mutating (owner_gated / evidence_not_verified /
    owner_mode_unavailable): the mission row, its version and its children
    are untouched. Success delegates the single mutation to the owner-only
    ``hermes_mission_approve`` — this resolver never writes on its own and
    never fabricates Owner Mode (no owner env vars, no ack string).
    """
    mission = json.loads(
        mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root)
    )
    if not mission.get("success") or mission.get("found") is False:
        return json.dumps({
            "success": False,
            "tool": "resolve_owner_gate",
            "mission_id": mission_id,
            "reason": REASON_MISSION_NOT_FOUND,
            "surfaced": _surface(mission_id, hermes_root),
        })

    def _parked(reason: str) -> str:
        return json.dumps({
            "success": False,
            "tool": "resolve_owner_gate",
            "mission_id": mission_id,
            "reason": reason,
            "surfaced": _surface(mission_id, hermes_root),
        })

    policy = mission.get("approval_policy") or {}
    policy_name = str(policy.get("policy", "owner")) if isinstance(policy, dict) else "owner"
    if policy_name != "auto_when_evidence_verified":
        return _parked(REASON_OWNER_GATED)

    evidence = _children_evidence(mission)
    if str(mission.get("status")) != "awaiting_approval" or not evidence["terminal_with_verified_success"]:
        return _parked(REASON_EVIDENCE_NOT_VERIFIED)

    if not _owner_mode_available(dry_run):
        return _parked(REASON_OWNER_MODE_UNAVAILABLE)

    if not approval_reference:
        routine = sorted({
            c["auth_class"] for c in _surface(mission_id, hermes_root).get(
                "node_classifications", []
            ) if c["work_class"] == "routine"
        })
        approval_reference = (
            f"{POLICY_TABLE_ID}:{','.join(routine)}" if routine else POLICY_TABLE_ID
        )
    result = json.loads(mission_runtime.hermes_mission_approve(
        mission_id,
        approval_reference,
        confirm=confirm,
        dry_run=dry_run,
        hermes_root=hermes_root,
    ))
    out = dict(result)
    out["tool"] = "resolve_owner_gate"
    out["work_class"] = {
        "policy_table": "v1",
        "resolved_via": "policy_table_preauthorization",
    }
    if not result.get("success"):
        out["reason"] = REASON_APPROVE_REJECTED
    return json.dumps(out)
