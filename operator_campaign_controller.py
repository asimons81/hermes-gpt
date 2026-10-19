"""Persistent campaign controller for maintenance campaigns (AUTO-02).

AUTO-01 (``operator_campaign_compiler``) compiles ONE campaign document into
ONE MissionPlan. Real campaigns are bigger than one Mission and must survive
restarts, so this module adds the thin, additive controller that drives a
campaign as a sequence of FINITE Missions — "finite Missions driven by a
persistent campaign controller (not one immortal Mission)" (PLAN.md AUTO-02):

- A **campaign manifest** (``hermes.campaign-manifest/v1``) declares ordered
  segments; each segment carries its own ``hermes.campaign/v1`` document
  (compiled by AUTO-01) plus inter-segment dependencies.
- The controller's only writable surface is a small durable state document
  under ``<data_root>/campaigns/<campaign_id>/state.json`` (orchestration
  metadata ONLY — mission/plan/node/delegation truth stays authoritative in
  the existing stores, per BOUNDARY.md "a caller, not a competing owner").
- ``campaign_controller_tick`` is an idempotent reconcile pass: for every
  segment it re-reads authoritative truth (mission store, plan store,
  autopilot run store) and drives at most one phase transition per segment
  per tick — ``pending -> minted -> active -> terminal`` — reusing the
  existing primitives verbatim: ``hermes_mission_create``,
  ``compile_campaign`` + ``hermes_plan_create``,
  ``hermes_mission_transition`` and ``hermes_autopilot_start``. There is no
  second scheduler, no worker process and no dispatch path in this module:
  execution inside a segment is Autopilot's job, sequencing across segments
  is this module's job.
- **Read-then-adopt** (restart ⇒ never duplicate): the tick never trusts the
  recorded phase — every segment's phase is re-derived from the
  authoritative stores (mission exists? plan exists? autopilot run alive?
  mission terminal?) and the state document is repaired before acting.
  Deleting ``state.json`` is safe: the next tick rebuilds it from truth.
- Owner gates stay owner gates: a Mission at ``awaiting_approval`` parks the
  segment (``parked_owner_gate``) and the controller proceeds with
  independent segments; approval is never forged or bypassed. The Autopilot
  machine gate (``HERMES_GPT_AUTOPILOT``, default OFF) likewise parks a
  segment at ``parked_autopilot_gate`` — flipping it is an operator act.
- All mutations go through the standard OperatorPolicy gate with
  dry-run-first semantics: ``apply=False`` plans the tick (every phase
  decision computed, nothing written).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_campaign_compiler as compiler
import operator_mission_plan as mission_plan
import operator_mission_runtime as mission_runtime
import operator_policy as op

SCHEMA_VERSION = "hermes.campaign-controller/v1"
MANIFEST_SCHEMA = "hermes.campaign-manifest/v1"
STATE_SCHEMA = "hermes.campaign-state/v1"

MAX_SEGMENTS = 24
MAX_MANIFEST_BYTES = 262_144

SEQ_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Segment phases (monotone per segment; terminal set is closed).
PHASE_PENDING = "pending"
PHASE_MINTED = "minted"
PHASE_ACTIVE = "active"
PHASE_PARKED_OWNER_GATE = "parked_owner_gate"
PHASE_PARKED_AUTOPILOT_GATE = "parked_autopilot_gate"
PHASE_SUCCEEDED = "succeeded"
PHASE_FAILED = "failed"
PHASE_ORPHANED = "orphaned"  # segment vanished from a revised manifest

TERMINAL_PHASES = frozenset({PHASE_SUCCEEDED, PHASE_FAILED, PHASE_ORPHANED})
PARKED_PHASES = frozenset({PHASE_PARKED_OWNER_GATE, PHASE_PARKED_AUTOPILOT_GATE})


class CampaignManifestError(ValueError):
    """Raised when a campaign manifest violates the manifest schema."""


def _data_root(hermes_root: Path | None = None) -> Path:
    configured = hermes_root or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    normalized = op.normalize_hermes_data_root(configured)
    return Path(normalized or configured).expanduser().resolve()


def _campaigns_root(hermes_root: Path | None = None) -> Path:
    return _data_root(hermes_root) / "campaigns"


def _state_path(campaign_id: str, hermes_root: Path | None) -> Path:
    return _campaigns_root(hermes_root) / campaign_id / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Manifest validation (fail closed; compiles every segment before any write)
# ---------------------------------------------------------------------------

def _validate_manifest(manifest_json: str) -> dict[str, Any]:
    if not isinstance(manifest_json, str) or len(manifest_json.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise CampaignManifestError("manifest_json must be a string under 256KiB")
    try:
        raw = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise CampaignManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CampaignManifestError("manifest must be a JSON object")
    if raw.get("schema") != MANIFEST_SCHEMA:
        raise CampaignManifestError(f"manifest schema must be {MANIFEST_SCHEMA!r}")
    campaign_id = raw.get("campaign_id")
    if not isinstance(campaign_id, str) or not compiler.CAMPAIGN_ID_RE.fullmatch(campaign_id):
        raise CampaignManifestError(f"manifest campaign_id is invalid: {campaign_id!r}")
    segments = raw.get("segments")
    if not isinstance(segments, list) or not segments:
        raise CampaignManifestError("manifest segments must be a non-empty list")
    if len(segments) > MAX_SEGMENTS:
        raise CampaignManifestError(f"manifest exceeds {MAX_SEGMENTS} segments")
    seqs: set[str] = set()
    mission_ids: set[str] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            raise CampaignManifestError("each manifest segment must be an object")
        seq = seg.get("seq")
        if not isinstance(seq, str) or not SEQ_RE.fullmatch(seq):
            raise CampaignManifestError(f"segment seq is invalid: {seq!r}")
        if seq in seqs:
            raise CampaignManifestError(f"duplicate segment seq {seq!r}")
        seqs.add(seq)
        doc = seg.get("campaign_json")
        if not isinstance(doc, str) or not doc.strip():
            raise CampaignManifestError(f"segment {seq!r} campaign_json must be a non-empty string")
        try:
            campaign = json.loads(doc)
        except json.JSONDecodeError as exc:
            raise CampaignManifestError(f"segment {seq!r} campaign_json is not valid JSON: {exc}") from exc
        try:
            plan = compiler.compile_campaign(doc)
        except compiler.CampaignSpecError as exc:
            raise CampaignManifestError(f"segment {seq!r} does not compile: {exc}") from exc
        if campaign.get("campaign_id") != campaign_id:
            raise CampaignManifestError(
                f"segment {seq!r} campaign_id {campaign.get('campaign_id')!r} "
                f"!= manifest campaign_id {campaign_id!r}"
            )
        mid = campaign.get("mission_id")
        if not isinstance(mid, str) or not mission_runtime.MISSION_ID_RE.fullmatch(mid):
            raise CampaignManifestError(f"segment {seq!r} mission_id is invalid: {mid!r}")
        if mid in mission_ids:
            raise CampaignManifestError(f"duplicate mission_id {mid!r} across segments")
        mission_ids.add(mid)
        deps = seg.get("depends_on", [])
        if not isinstance(deps, list):
            raise CampaignManifestError(f"segment {seq!r} depends_on must be a list")
        for dep in deps:
            if not isinstance(dep, str) or not SEQ_RE.fullmatch(dep):
                raise CampaignManifestError(f"segment {seq!r} has invalid depends_on entry {dep!r}")
        seg["_compiled_plan_sha256"] = plan["plan_sha256"]
        seg["_mission_id"] = mid
    for seg in segments:  # dependency closure: known, non-self, acyclic
        for dep in seg.get("depends_on", []):
            if dep not in seqs:
                raise CampaignManifestError(f"segment {seg['seq']!r} depends on unknown segment {dep!r}")
            if dep == seg["seq"]:
                raise CampaignManifestError(f"segment {seg['seq']!r} cannot depend on itself")
    _assert_acyclic({seg["seq"]: list(seg.get("depends_on", [])) for seg in segments})
    return raw


def _assert_acyclic(deps: dict[str, list[str]]) -> None:
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        mark = state.get(node, 0)
        if mark == 1:
            raise CampaignManifestError(f"manifest dependency cycle through segment {node!r}")
        if mark == 2:
            return
        state[node] = 1
        for dep in deps[node]:
            visit(dep)
        state[node] = 2

    for seq in deps:
        visit(seq)


# ---------------------------------------------------------------------------
# Durable controller state (orchestration metadata only)
# ---------------------------------------------------------------------------

def _load_state(campaign_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    try:
        raw = json.loads(_state_path(campaign_id, hermes_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA or raw.get("campaign_id") != campaign_id:
        return None
    return raw


def _save_state(state: dict[str, Any], hermes_root: Path | None) -> None:
    path = _state_path(state["campaign_id"], hermes_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _fresh_state(campaign_id: str, manifest_sha256: str) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "created_at": _now(),
        "updated_at": _now(),
        "ticks": 0,
        "segments": {},
    }


# ---------------------------------------------------------------------------
# Authoritative truth readers (per-tick memo; never a cached belief)
# ---------------------------------------------------------------------------

def _mission_view(mission_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    payload = json.loads(mission_runtime.hermes_mission_get(mission_id, hermes_root=hermes_root))
    if not payload.get("success") or payload.get("found") is False:
        return None
    return payload


def _plan_review(mission_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    payload = json.loads(mission_plan.hermes_plan_review(mission_id, hermes_root=hermes_root))
    if not payload.get("success") or payload.get("found") is False:
        return None
    return payload


class _Truth:
    """Per-tick memo of authoritative reads (one pass, consistent snapshot)."""

    def __init__(self, hermes_root: Path | None) -> None:
        self.hermes_root = hermes_root
        self._missions: dict[str, dict[str, Any] | None] = {}
        self._plans: dict[str, dict[str, Any] | None] = {}
        self._runs: dict[str, dict[str, Any] | None] = {}

    def mission(self, mission_id: str) -> dict[str, Any] | None:
        if mission_id not in self._missions:
            self._missions[mission_id] = _mission_view(mission_id, self.hermes_root)
        return self._missions[mission_id]

    def plan(self, mission_id: str) -> dict[str, Any] | None:
        if mission_id not in self._plans:
            self._plans[mission_id] = _plan_review(mission_id, self.hermes_root)
        return self._plans[mission_id]

    def run(self, mission_id: str) -> dict[str, Any] | None:
        if mission_id not in self._runs:
            self._runs[mission_id] = _autopilot_run(mission_id, self.hermes_root)
        return self._runs[mission_id]

    def invalidate_mission(self, mission_id: str) -> None:
        self._missions.pop(mission_id, None)
        self._plans.pop(mission_id, None)
        self._runs.pop(mission_id, None)


def _autopilot_run(mission_id: str, hermes_root: Path | None) -> dict[str, Any] | None:
    import operator_autopilot as autopilot

    payload = json.loads(autopilot.hermes_autopilot_status(mission_id, hermes_root=hermes_root))
    if not payload.get("success"):
        return None
    run = payload.get("run")
    return run if isinstance(run, dict) else None


def _reconcile_phase(seg: dict[str, Any], truth: _Truth, recorded: dict[str, Any] | None) -> str:
    """Phase from authoritative truth; the recorded phase is only a hint.

    - mission absent -> pending
    - mission completed -> succeeded; failed/cancelled -> failed
    - mission awaiting_approval -> parked_owner_gate
    - autopilot run non-terminal -> active
    - otherwise -> minted (mission row exists; plan/start still owed)
    """
    mid = seg["_mission_id"]
    mission = truth.mission(mid)
    if mission is None:
        return PHASE_PENDING
    status = str(mission.get("status") or "")
    if status == "completed":
        return PHASE_SUCCEEDED
    if status in ("failed", "cancelled"):
        return PHASE_FAILED
    if status == "awaiting_approval":
        return PHASE_PARKED_OWNER_GATE
    run = truth.run(mid)
    if run is not None and str(run.get("state") or "") not in _autopilot_terminal_states():
        return PHASE_ACTIVE
    if recorded is not None and recorded.get("phase") == PHASE_PARKED_AUTOPILOT_GATE:
        # Parking on the machine gate is controller knowledge, not mission
        # truth: keep the park until a start actually succeeds.
        return PHASE_PARKED_AUTOPILOT_GATE
    return PHASE_MINTED


def _autopilot_terminal_states() -> frozenset[str]:
    import operator_autopilot as autopilot

    return autopilot.TERMINAL_STATES


def _plan_all_nodes_terminal(review: dict[str, Any]) -> bool:
    nodes = review.get("nodes") or []
    return bool(nodes) and all(str(n.get("state")) in ("completed", "failed", "cancelled", "skipped")
                               for n in nodes)


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def campaign_controller_tick(
    manifest_json: str,
    *,
    confirm: bool = False,
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """One idempotent campaign reconcile pass. Safe to call repeatedly.

    ``apply=False`` (default) is a dry run: every phase decision is computed
    against authoritative truth and reported, but no mission/plan/run/state
    mutation happens. ``apply=True`` additionally requires ``confirm=True``
    and direct apply mode, and drives at most one phase transition per
    segment.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        if apply and not confirm:
            raise PermissionError("campaign tick apply requires confirm=true")
        policy.require_mutation(not apply)
        manifest = _validate_manifest(manifest_json)
        effective_apply = apply and confirm and not policy.effective_dry_run(False)
        campaign_id = manifest["campaign_id"]
        manifest_sha = _sha256(manifest_json)

        state = _load_state(campaign_id, hermes_root)
        if state is None:
            state = _fresh_state(campaign_id, manifest_sha)
        elif state.get("manifest_sha256") != manifest_sha:
            # Revised manifest: known seqs keep their recorded phase
            # (validated against truth below); removed seqs become orphaned
            # (containment only — the controller never mutates their
            # missions, the owner decides their fate).
            state["manifest_sha256"] = manifest_sha
            known = {s["seq"] for s in manifest["segments"]}
            for seq, entry in state["segments"].items():
                if seq not in known and entry.get("phase") not in TERMINAL_PHASES:
                    entry["phase"] = PHASE_ORPHANED
                    entry["updated_at"] = _now()

        truth = _Truth(hermes_root)
        actions: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        segment_views: list[dict[str, Any]] = []

        # Pass 1: re-derive every segment's phase from truth (read-then-adopt).
        for seg in manifest["segments"]:
            seq = seg["seq"]
            recorded = state["segments"].get(seq)
            derived = _reconcile_phase(seg, truth, recorded)
            if recorded is None:
                state["segments"][seq] = {
                    "phase": derived,
                    "mission_id": seg["_mission_id"],
                    "plan_sha256": seg["_compiled_plan_sha256"],
                    "updated_at": _now(),
                }
            elif recorded.get("phase") != derived:
                repairs.append({"seq": seq, "recorded": recorded.get("phase"), "derived": derived})
                recorded["phase"] = derived
                recorded["updated_at"] = _now()

        # Pass 2: drive segments whose dependencies allow work. Independent
        # segments proceed in parallel; a failed dependency parks dependents
        # instead of monopolizing the campaign.
        for seg in manifest["segments"]:
            seq = seg["seq"]
            entry = state["segments"][seq]
            phase = entry["phase"]
            deps = list(seg.get("depends_on", []))
            dep_phases = {d: state["segments"].get(d, {}).get("phase") for d in deps}
            mission = truth.mission(seg["_mission_id"])
            run = truth.run(seg["_mission_id"])
            view: dict[str, Any] = {
                "seq": seq,
                "mission_id": seg["_mission_id"],
                "phase": phase,
                "mission_status": str(mission.get("status")) if mission else None,
                "autopilot_run_state": str(run.get("state")) if run else None,
                "dependencies": dep_phases,
            }
            failed_deps = sorted(d for d, s in dep_phases.items() if s == PHASE_FAILED)
            blocked_deps = sorted(d for d, s in dep_phases.items() if s != PHASE_SUCCEEDED)

            if phase in TERMINAL_PHASES:
                view["next_action"] = "none"
            elif failed_deps:
                entry["phase"] = PHASE_FAILED
                entry["reason"] = f"dependency failed: {','.join(failed_deps)}"
                entry["updated_at"] = _now()
                view.update(phase=PHASE_FAILED, next_action="none")
            elif blocked_deps:
                view["next_action"] = "wait_dependencies"
            elif phase == PHASE_PENDING:
                view["next_action"] = "mint_mission"
                if effective_apply:
                    out = _mint_mission(seg, hermes_root)
                    actions.append({"seq": seq, "action": "mission_create", **out})
                    if out["ok"]:
                        entry["phase"] = PHASE_MINTED
                        entry["updated_at"] = _now()
                        view["phase"] = PHASE_MINTED
                        truth.invalidate_mission(seg["_mission_id"])
            elif phase in (PHASE_MINTED, PHASE_PARKED_AUTOPILOT_GATE):
                view["next_action"] = "retry_start" if phase == PHASE_PARKED_AUTOPILOT_GATE else "create_plan_and_start"
                if effective_apply:
                    outcome = _mint_plan_and_start(seg, hermes_root, actions)
                    truth.invalidate_mission(seg["_mission_id"])
                    if outcome == "active":
                        entry["phase"] = PHASE_ACTIVE
                        entry.pop("reason", None)
                    elif outcome == "parked_gate":
                        entry["phase"] = PHASE_PARKED_AUTOPILOT_GATE
                        entry["reason"] = "HERMES_GPT_AUTOPILOT gate off"
                        view["next_action"] = "retry_start"
                    elif outcome == "drained":
                        # plan already fully terminal: mission reconcile will
                        # terminalize; nothing to start (no duplicate run)
                        entry["phase"] = PHASE_ACTIVE
                        entry["reason"] = "plan drained; awaiting mission reconcile"
                    view["phase"] = entry["phase"]
            elif phase == PHASE_ACTIVE:
                review = truth.plan(seg["_mission_id"])
                if review is not None and _plan_all_nodes_terminal(review):
                    view["next_action"] = "await_mission_reconcile"
                else:
                    view["next_action"] = "observe"
            elif phase == PHASE_PARKED_OWNER_GATE:
                view["next_action"] = "wait_owner"
            segment_views.append(view)

        terminal = all(
            state["segments"].get(s["seq"], {}).get("phase") in TERMINAL_PHASES
            for s in manifest["segments"]
        )
        if terminal:
            state.setdefault("finished_at", _now())
        else:
            state.pop("finished_at", None)

        state["ticks"] = int(state.get("ticks", 0)) + 1
        state["updated_at"] = _now()
        if effective_apply:
            _save_state(state, hermes_root)

        op.audit_record(
            tool="campaign_controller_tick",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=not effective_apply,
            success=True,
            changed=bool(actions),
            summary="campaign tick",
            extra={"campaign_id": campaign_id, "actions": len(actions), "repairs": len(repairs)},
        )
        return json.dumps({
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "tool": "campaign_controller_tick",
            "campaign_id": campaign_id,
            "dry_run": not effective_apply,
            "ticks": state["ticks"],
            "segments": segment_views,
            "actions": actions,
            "state_repairs": repairs,
            "finished": terminal,
            "changed": bool(actions),
        })
    except (CampaignManifestError, compiler.CampaignSpecError, ValueError, TypeError,
            PermissionError, LookupError, OSError, sqlite3.Error) as exc:
        op.audit_record(
            tool="campaign_controller_tick",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=not apply,
            success=False,
            changed=False,
            summary=f"campaign tick rejected: {exc}",
        )
        return json.dumps({
            "success": False,
            "schema_version": SCHEMA_VERSION,
            "tool": "campaign_controller_tick",
            "error": str(exc),
            "error_class": exc.__class__.__name__,
        })


def _mint_mission(seg: dict[str, Any], hermes_root: Path | None) -> dict[str, Any]:
    """Mint the segment Mission.

    Always ``final_approval_required=True``: mission completion parks at the
    Owner gate (``hermes_mission_approve`` is owner-only by design), and a
    workspace-level controller must never forge that approval. Preauthorized
    completion classes are AUTO-04's policy-table work, not a mint-time
    shortcut here.
    """
    campaign = json.loads(seg["campaign_json"])
    spec = {
        "schema": mission_runtime.MISSION_SPEC_SCHEMA,
        "mission_id": campaign["mission_id"],
        "title": f"Campaign segment {seg['seq']}",
        "objective": str(campaign.get("objective") or seg["seq"]),
        "owner_profile": "default",
        "acceptance_criteria": [
            f"campaign-validate node reached with typed evidence (segment {seg['seq']})"
        ],
        "context_refs": [],
        "skills": [],
        "final_approval_required": True,
    }
    payload = json.loads(mission_runtime.hermes_mission_create(
        json.dumps(spec), confirm=True, dry_run=False, hermes_root=hermes_root
    ))
    ok = bool(payload.get("success"))
    return {"ok": ok, "detail": "" if ok else str(payload.get("error", ""))}


def _mint_plan_and_start(
    seg: dict[str, Any], hermes_root: Path | None, actions: list[dict[str, Any]]
) -> str:
    """Create the compiled plan, activate the mission, start Autopilot.

    Returns ``"active"`` | ``"parked_gate"`` | ``"drained"`` | ``"failed"``.
    """
    import operator_autopilot as autopilot

    mid = seg["_mission_id"]
    review = _plan_review(mid, hermes_root)
    if review is None:
        plan = compiler.compile_campaign(seg["campaign_json"])
        created = json.loads(mission_plan.hermes_plan_create(
            mid, json.dumps(plan), confirm=True, dry_run=False, hermes_root=hermes_root,
        ))
        actions.append({"seq": seg["seq"], "action": "plan_create", "ok": bool(created.get("success")),
                        "detail": str(created.get("plan_sha256") or created.get("error", ""))})
        if not created.get("success"):
            return "failed"
        review = _plan_review(mid, hermes_root)
    if review is not None and _plan_all_nodes_terminal(review):
        # Finished work: drive the Mission to its authoritative desired
        # status (awaiting_approval for approval-required missions) through
        # the existing reconcile primitive — never start a second run.
        rec = json.loads(mission_runtime.hermes_mission_reconcile(
            mid, confirm=True, dry_run=False, hermes_root=hermes_root,
        ))
        actions.append({"seq": seg["seq"], "action": "mission_reconcile", "ok": bool(rec.get("success")),
                        "detail": str(rec.get("status") or rec.get("error", ""))})
        return "drained"

    mission = _mission_view(mid, hermes_root)
    if mission is None:
        return "failed"
    if str(mission.get("status")) == "draft":
        moved = json.loads(mission_runtime.hermes_mission_transition(
            mid, "running", reason="campaign controller activation", confirm=True, dry_run=False,
            hermes_root=hermes_root,
        ))
        actions.append({"seq": seg["seq"], "action": "mission_running", "ok": bool(moved.get("success")),
                        "detail": str(moved.get("to_status") or moved.get("error", ""))})
        if not moved.get("success"):
            return "failed"

    started = json.loads(autopilot.hermes_autopilot_start(
        mid, confirm=True, dry_run=False, hermes_root=hermes_root,
    ))
    if started.get("success") or started.get("idempotent"):
        actions.append({"seq": seg["seq"], "action": "autopilot_start", "ok": True, "detail": ""})
        return "active"
    detail = str(started.get("error", ""))
    if "HERMES_GPT_AUTOPILOT" in detail:
        actions.append({"seq": seg["seq"], "action": "autopilot_start", "ok": False,
                        "parked": True, "detail": detail})
        return "parked_gate"
    actions.append({"seq": seg["seq"], "action": "autopilot_start", "ok": False, "detail": detail})
    return "failed"


def campaign_controller_status(campaign_id: str, hermes_root: Path | None = None) -> str:
    """Read-only campaign state (durable document only; no reconciliation)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        state = _load_state(campaign_id, hermes_root)
        if state is None:
            return json.dumps({"success": True, "schema_version": SCHEMA_VERSION,
                               "tool": "campaign_controller_status", "campaign_id": campaign_id,
                               "found": False})
        return json.dumps({"success": True, "schema_version": SCHEMA_VERSION,
                           "tool": "campaign_controller_status", "campaign_id": campaign_id,
                           "found": True, "state": state})
    except (PermissionError, OSError, json.JSONDecodeError) as exc:
        return json.dumps({"success": False, "schema_version": SCHEMA_VERSION,
                           "tool": "campaign_controller_status", "error": str(exc)})
