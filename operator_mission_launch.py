"""One-call Mission launch for Hermes GPT v0.16 (``hermes_mission_launch``).

This is a caller, not an owner. It sequences the existing public tools that
already own each step, in order, and passes each tool's own dry-run and confirm
semantics through unchanged:

1. ``operator_mission_runtime.hermes_mission_create``  (``spec_json``)
2. ``operator_mission_plan.hermes_plan_create``        (plan derived from the spec)
3. ``operator_mission_budget.hermes_budget_set``       (only when ``budget_json`` is given)
4. ``operator_autopilot.hermes_autopilot_start``       (``autopilot_limits_json``)

The launcher writes no store directly, reimplements no validator, bypasses no
gate, and approves nothing. Its own checks are limited to input shape, the
refusal of an approval-disabling spec, and an Operator/Autopilot preflight that
mirrors the checks ``hermes_autopilot_start`` itself makes.

Dry run (the default) previews every step without writing. Because a later step
needs the Mission id that an earlier step creates, a dry run validates those
steps with their own validators against a preview id; it never fabricates an id
for the Owner to act on.

Apply (``dry_run=False``) requires ``confirm=True``. The preflight and the full
preview must both be clean before the first write. The chain then stops at the
first refusal or error and reports which steps completed, which did not, and
which ids exist. Nothing is rolled back: no deletion surface exists, so the
report describes what is on disk.

Re-running with the same spec creates a new Mission. There is no idempotency
key; the created ``mission_id`` is returned prominently.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_autopilot as autopilot
import operator_mission_budget as budget
import operator_mission_plan as mission_plan
import operator_mission_runtime as mission_runtime
import operator_policy as op
import operator_skill_resolution as skill_resolution

SCHEMA_VERSION = "hermes.mission-launch/v1"
TOOL = "hermes_mission_launch"
APPROVAL_FIELD = "final_approval_required"
LIMIT_KEYS = ("max_concurrency", "max_replans", "max_attempts_per_node", "max_runtime_seconds")
BUDGET_KEYS = frozenset({"quota", "policy"})
DETAIL_MAX = 300

STEP_INPUT = "input"
STEP_PREFLIGHT = "preflight"
STEP_MISSION = "mission_create"
STEP_PLAN = "plan_create"
STEP_BUDGET = "budget_set"
STEP_AUTOPILOT = "autopilot_start"
CHAIN = (STEP_MISSION, STEP_PLAN, STEP_BUDGET, STEP_AUTOPILOT)

TOOL_NAMES = {
    STEP_MISSION: "hermes_mission_create",
    STEP_PLAN: "hermes_plan_create",
    STEP_BUDGET: "hermes_budget_set",
    STEP_AUTOPILOT: "hermes_autopilot_start",
}

# Bounds mirror hermes_autopilot_start's own range checks. Its defaults are read
# from its signature so the launcher never drifts from them.
_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "max_concurrency": (1, autopilot.MAX_CONCURRENCY_LIMIT),
    "max_replans": (0, autopilot.MAX_REPLANS_LIMIT),
    "max_attempts_per_node": (1, autopilot.MAX_ATTEMPTS_LIMIT),
    "max_runtime_seconds": (autopilot.MIN_MAX_RUNTIME_SECONDS, autopilot.MAX_MAX_RUNTIME_SECONDS),
}
_AUTOPILOT_SIGNATURE = inspect.signature(autopilot.hermes_autopilot_start)
_LIMIT_DEFAULTS: dict[str, int] = {key: _AUTOPILOT_SIGNATURE.parameters[key].default for key in LIMIT_KEYS}

BUDGET_SKIPPED_DETAIL = (
    "no budget requested, so no envelope is created. Autopilot holds dispatch only for a "
    "crossing, invalid, or unreadable envelope; a Mission with no budget account dispatches "
    "unrestricted."
)
NOTE = "Re-running with the same spec creates a new Mission; there is no idempotency key."


class _InputError(ValueError):
    """Malformed launch input (shape, unknown keys). Refused as step ``input``."""


class _LimitsError(ValueError):
    """Invalid Autopilot limits. Refused as step ``autopilot_start``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bound(text: Any) -> str:
    value = op.redact_output(str(text))
    return value if len(value) <= DETAIL_MAX else value[: DETAIL_MAX - 3] + "..."


def _refusal(code: str, message: str, *, step: str, action: str) -> dict[str, str]:
    return {"code": code, "step": step, "message": _bound(message), "suggested_action": action}


def _message_of(out: dict[str, Any], fallback: str) -> tuple[str, str]:
    """Return ``(code, message)`` from a step's failure payload, unwrapping nested envelopes."""
    code = str(out.get("code") or "")
    message = out.get("safe_message") or ""
    error = out.get("error")
    if not message and isinstance(error, str) and error.startswith("{"):
        try:
            inner = json.loads(error)
            code = code or str(inner.get("code") or "")
            message = str(inner.get("safe_message") or "")
        except json.JSONDecodeError:
            message = ""
    if not message:
        message = error if isinstance(error, str) and error else fallback
    return code or "LAUNCH_STEP_REFUSED", str(message)


def _json_object(text: str | None, field: str, *, required: bool) -> dict[str, Any] | None:
    if text is None or (isinstance(text, str) and not text.strip()):
        if required:
            raise _InputError(f"{field} is required")
        return None
    if not isinstance(text, str):
        raise _InputError(f"{field} must be a JSON string")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _InputError(f"{field} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise _InputError(f"{field} must be a JSON object")
    return value


def _budget_request(text: str | None) -> dict[str, Any] | None:
    raw = _json_object(text, "budget_json", required=False)
    if raw is None:
        return None
    unknown = sorted(set(raw) - BUDGET_KEYS)
    if unknown:
        raise _InputError(f"budget_json has unknown keys: {unknown}")
    if "quota" not in raw:
        raise _InputError("budget_json requires quota")
    policy = raw.get("policy")
    # hermes_budget_set's own _clean_policy validates the policy document.
    return {"quota": raw["quota"], "policy_json": "" if policy is None else json.dumps(policy, sort_keys=True)}


def _limits_request(text: str | None) -> dict[str, int]:
    """Return only the limits the caller supplied; omitted keys keep Autopilot's defaults."""
    try:
        raw = _json_object(text, "autopilot_limits_json", required=False)
    except _InputError as exc:
        raise _LimitsError(str(exc)) from exc
    if raw is None:
        return {}
    unknown = sorted(set(raw) - set(LIMIT_KEYS))
    if unknown:
        raise _LimitsError(f"autopilot_limits_json has unknown keys: {unknown}")
    provided: dict[str, int] = {}
    for key in LIMIT_KEYS:
        if key in raw:
            minimum, maximum = _LIMIT_BOUNDS[key]
            try:
                provided[key] = autopilot._bounded_int(raw[key], minimum=minimum, maximum=maximum, field=key)
            except ValueError as exc:
                raise _LimitsError(str(exc)) from exc
    return provided


def _effective_limits(provided: dict[str, int]) -> dict[str, int]:
    return {**_LIMIT_DEFAULTS, **provided}


def _preflight(policy: op.OperatorPolicy) -> dict[str, dict[str, Any]]:
    """The same checks hermes_autopilot_start makes before a direct start, read-only."""
    checks: dict[str, dict[str, Any]] = {}

    gate_on = autopilot._autopilot_enabled()
    checks["autopilot_gate"] = {
        "ok": gate_on,
        "code": "AUTOPILOT_GATE_OFF",
        "detail": f"{autopilot.AUTOPILOT_ENV}=1 is required for a direct Autopilot start",
    }

    level_ok, level_code, level_detail = True, "", "Operator level is workspace or higher"
    try:
        policy.require_level("workspace")
    except PermissionError as exc:
        level_ok = False
        level_code = "OPERATOR_DISABLED" if not policy.enabled else "OPERATOR_LEVEL_INSUFFICIENT"
        level_detail = str(exc)
    checks["operator_level"] = {"ok": level_ok, "code": level_code, "detail": level_detail}

    # require_mutation(False) is a no-op in dry_run apply mode, and the underlying
    # tools then quietly return a preview for dry_run=false. Check the effective
    # mode directly so the launch refuses instead of reporting a write that never happened.
    mode_ok = not policy.effective_dry_run(False)
    mode_detail = ("Operator apply mode is direct" if mode_ok else
                   f"Operator apply mode is {policy.apply_mode!r}; set "
                   f"{op.OPERATOR_APPLY_MODE_ENV}=direct for a direct launch")
    checks["apply_mode"] = {"ok": mode_ok, "code": "APPLY_MODE_NOT_DIRECT", "detail": mode_detail}
    return checks


def _first_failed(checks: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    for name, check in checks.items():
        if not check["ok"]:
            return name, check
    return None


def _public_checks(checks: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {name: {"ok": c["ok"], "detail": _bound(c["detail"])} for name, c in checks.items()}


def _step(step: str, outcome: str, detail: str) -> dict[str, Any]:
    return {"step": step, "tool": TOOL_NAMES.get(step, ""), "outcome": outcome, "detail": _bound(detail)}


# ---------------------------------------------------------------------------
# Preview (dry-run validation of every step, no writes)
# ---------------------------------------------------------------------------


def _refused(step: str, code: str, message: str, action: str) -> tuple[dict[str, Any], dict[str, str]]:
    refusal = _refusal(code, message, step=step, action=action)
    return _step(step, "refused", f"{code}: {message}"), refusal


def _preview_mission(
    spec_json: str, hermes_root: Path | None, spec_has_id: bool,
) -> tuple[dict[str, Any], dict[str, str] | None, str | None]:
    out = json.loads(mission_runtime.hermes_mission_create(spec_json, confirm=False, dry_run=True, hermes_root=hermes_root))
    if not out.get("success"):
        code, message = _message_of(out, "mission spec was refused")
        step, refusal = _refused(STEP_MISSION, code, message,
                                 "Fix the MissionSpec; the spec is validated by hermes_mission_create.")
        return step, refusal, None
    preview_id = str(out.get("mission_id") or "")
    note = (
        f"spec validated; mission would be created as {preview_id}"
        if spec_has_id
        else "spec validated; mission_id is generated at apply and is not reported in a dry run"
    )
    return _step(STEP_MISSION, "preview", note), None, preview_id


def _preview_plan(
    raw_spec: dict[str, Any], preview_id: str, hermes_root: Path | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Validate the canonical plan the real plan_create would derive, without the Mission in the store.

    hermes_plan_create cannot dry-run before the Mission exists because it reads the
    spec from the store. This reuses the same derivation (decompose_mission) and the
    same read-only validator (hermes_plan_validate) that plan_create itself calls.
    """
    action = "Fix the MissionSpec objective or owner_profile; nothing was created."
    try:
        plan = mission_plan.decompose_mission(
            preview_id,
            objective=str(raw_spec.get("objective", "")),
            owner_profile=str(raw_spec.get("owner_profile") or "default"),
            use_canonical=True,
        )
    except (ValueError, TypeError) as exc:
        return _refused(STEP_PLAN, "PLAN_DERIVATION_REJECTED", str(exc), action)
    validated = json.loads(mission_plan.hermes_plan_validate(json.dumps(plan), hermes_root=hermes_root))
    if not validated.get("valid"):
        code, message = _message_of(validated, "plan was refused")
        return _refused(STEP_PLAN, code, message, action)
    try:
        autopilot._validate_plan_capabilities(plan, hermes_root)
    except skill_resolution.SkillRequirementsError as exc:
        return _refused(STEP_PLAN, "AUTOPILOT_SKILL_REQUIREMENTS_REJECTED", str(exc),
                        "Install the required skills in the requested Hermes profile before launching.")
    return _step(
        STEP_PLAN, "would_run",
        f"plan derived from spec: {validated.get('node_count')} nodes; created as a draft on apply",
    ), None


def _preview_budget(
    budget_req: dict[str, Any] | None, preview_id: str, hermes_root: Path | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    if budget_req is None:
        return _step(STEP_BUDGET, "skipped", BUDGET_SKIPPED_DETAIL), None
    out = json.loads(budget.hermes_budget_set(
        preview_id, budget_req["quota"], budget_req["policy_json"],
        confirm=False, dry_run=True, hermes_root=hermes_root,
    ))
    if not out.get("success"):
        code, message = _message_of(out, "budget was refused")
        return _refused(STEP_BUDGET, code, message, "Fix budget_json quota or policy; nothing was created.")
    return _step(STEP_BUDGET, "preview", f"budget envelope previewed: quota={out.get('quota')} unit={out.get('unit')}"), None


def _preview_autopilot(
    effective: dict[str, int], preflight_failure: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    limits = ", ".join(f"{k}={v}" for k, v in effective.items())
    if preflight_failure is not None:
        return _refused(STEP_AUTOPILOT, preflight_failure["code"], preflight_failure["detail"],
                        "Enable the Autopilot machine gate, Operator workspace level, and direct apply mode.")
    return _step(
        STEP_AUTOPILOT, "would_run",
        f"limits valid ({limits}); the Mission and plan come from earlier steps, so the start is validated on apply",
    ), None


# ---------------------------------------------------------------------------
# Apply (real chain, stop at first refusal)
# ---------------------------------------------------------------------------


def _describe(step: str, out: dict[str, Any]) -> str:
    if step == STEP_MISSION:
        return f"mission_id={out.get('mission_id')} status={out.get('status')}"
    if step == STEP_PLAN:
        return f"nodes={out.get('node_count')} version={out.get('version')}"
    if step == STEP_BUDGET:
        return f"quota={out.get('quota')} unit={out.get('unit')}"
    run = out.get("run") or {}
    return f"job_id={out.get('job_id', '')} state={run.get('state', '')}"


def _apply_chain(
    spec_json: str,
    budget_req: dict[str, Any] | None,
    limits: dict[str, int],
    hermes_root: Path | None,
    result: dict[str, Any],
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    ctx: dict[str, Any] = {"mission_id": ""}
    created: dict[str, Any] = {}
    result["steps"] = []  # replace the dry validation previews with the real chain outcomes

    def run_mission() -> dict[str, Any]:
        return json.loads(mission_runtime.hermes_mission_create(spec_json, confirm=True, dry_run=False, hermes_root=hermes_root))

    def run_plan() -> dict[str, Any]:
        return json.loads(mission_plan.hermes_plan_create(ctx["mission_id"], "", confirm=True, dry_run=False, hermes_root=hermes_root))

    def run_budget() -> dict[str, Any]:
        assert budget_req is not None
        return json.loads(budget.hermes_budget_set(
            ctx["mission_id"], budget_req["quota"], budget_req["policy_json"],
            confirm=True, dry_run=False, hermes_root=hermes_root,
        ))

    def run_autopilot() -> dict[str, Any]:
        return json.loads(autopilot.hermes_autopilot_start(
            ctx["mission_id"], confirm=True, dry_run=False, hermes_root=hermes_root, **limits,
        ))

    runners: dict[str, Callable[[], dict[str, Any]] | None] = {
        STEP_MISSION: run_mission,
        STEP_PLAN: run_plan,
        STEP_BUDGET: run_budget if budget_req is not None else None,
        STEP_AUTOPILOT: run_autopilot,
    }
    halted = False
    for step in CHAIN:
        runner = runners[step]
        if runner is None:
            result["steps"].append(_step(step, "skipped", BUDGET_SKIPPED_DETAIL))
            continue
        if halted:
            result["steps"].append(_step(step, "not_run", "an earlier step did not complete"))
            continue
        try:
            out = runner()
        except Exception as exc:  # noqa: BLE001 - report partial state rather than lose created ids
            result["steps"].append(_step(step, "error", f"{type(exc).__name__}: launch step raised"))
            result["refusal"] = _refusal("LAUNCH_STEP_ERROR", f"{step} raised {type(exc).__name__}",
                                         step=step, action="Inspect the created ids and continue manually.")
            halted = True
            continue
        if out.get("success") and out.get("dry_run") is True:
            # Defensive: a dry_run=false call must never come back as a preview.
            result["steps"].append(_step(step, "refused", "APPLY_DOWNGRADED_TO_PREVIEW: nothing was written"))
            result["refusal"] = _refusal("APPLY_DOWNGRADED_TO_PREVIEW",
                                         f"{step} returned a preview for dry_run=false",
                                         step=step, action="Set the Operator apply mode to direct and retry.")
            halted = True
            continue
        if not out.get("success"):
            code, message = _message_of(out, f"{step} was refused")
            result["steps"].append(_step(step, "refused", f"{code}: {message}"))
            result["refusal"] = _refusal(code, message, step=step,
                                         action=str(out.get("suggested_action") or "Resolve the refusal, then continue manually."))
            halted = True
            continue
        result["steps"].append(_step(step, "completed", _describe(step, out)))
        if step == STEP_MISSION:
            ctx["mission_id"] = str(out["mission_id"])
            created["mission_id"] = ctx["mission_id"]
            result["mission_id"] = ctx["mission_id"]
        elif step == STEP_PLAN:
            created["plan"] = {"node_count": out.get("node_count"), "plan_sha256": out.get("plan_sha256"),
                               "version": out.get("version")}
        elif step == STEP_BUDGET:
            created["budget"] = {"quota": out.get("quota"), "account_sha256": out.get("account_sha256")}
        elif step == STEP_AUTOPILOT:
            run = out.get("run") or {}
            created["autopilot"] = {"job_id": out.get("job_id"), "state": run.get("state")}
        if checkpoint is not None:
            result["created"] = created
            checkpoint(result)

    result["created"] = created
    result["completed_steps"] = [s["step"] for s in result["steps"] if s["outcome"] == "completed"]
    result["not_run_steps"] = [s["step"] for s in result["steps"] if s["outcome"] in ("not_run", "refused", "error")]
    result["autopilot_started"] = any(s["step"] == STEP_AUTOPILOT and s["outcome"] == "completed" for s in result["steps"])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _launch(
    policy: op.OperatorPolicy,
    spec_json: str,
    budget_json: str | None,
    autopilot_limits_json: str | None,
    dry_run: bool,
    confirm: bool,
    hermes_root: Path | None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    apply = not dry_run
    result: dict[str, Any] = {
        "success": False,
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "dry_run": not apply,
        "mission_id": None,
        "steps": [],
        "preflight": {},
        "refusal": None,
        "created": {},
        "completed_steps": [],
        "not_run_steps": [],
        "autopilot_started": False,
        "note": NOTE,
    }

    def block(refusal: dict[str, str] | None) -> None:
        if refusal is not None and result["refusal"] is None:
            result["refusal"] = refusal

    def all_not_run(reason: str) -> None:
        result["steps"] = [_step(name, "not_run", reason) for name in CHAIN]

    if apply and not confirm:
        block(_refusal("CONFIRM_REQUIRED", "dry_run=false requires confirm=true; nothing was created",
                       step=STEP_INPUT, action="Review the dry-run preview, then re-run with confirm=true."))
        all_not_run("confirm=true is required; nothing was created")
        return result

    try:
        raw_spec = _json_object(spec_json, "spec_json", required=True) or {}
        budget_req = _budget_request(budget_json)
    except _InputError as exc:
        block(_refusal("INVALID_INPUT", str(exc), step=STEP_INPUT,
                       action="Pass spec_json as a MissionSpec object and budget_json as {quota, policy}."))
        all_not_run("input refused; nothing was created")
        return result
    try:
        provided_limits = _limits_request(autopilot_limits_json)
    except _LimitsError as exc:
        block(_refusal("AUTOPILOT_LIMITS_INVALID", str(exc), step=STEP_AUTOPILOT,
                       action="Use the hermes_autopilot_start limit names and ranges."))
        all_not_run("input refused; nothing was created")
        return result
    effective = _effective_limits(provided_limits)
    result["effective_limits"] = effective

    if APPROVAL_FIELD in raw_spec and raw_spec[APPROVAL_FIELD] is False:
        block(_refusal(
            "APPROVAL_REQUIRED_CANNOT_BE_DISABLED",
            "final_approval_required must stay true; a launch never disables final Owner approval",
            step=STEP_INPUT,
            action="Remove final_approval_required=false. Completion still requires hermes_mission_approve in Owner mode.",
        ))
        all_not_run("input refused; nothing was created")
        return result

    checks = _preflight(policy)
    result["preflight"] = _public_checks(checks)
    failed = _first_failed(checks)
    preflight_failure = failed[1] if failed is not None else None
    if preflight_failure is not None:
        block(_refusal(preflight_failure["code"], preflight_failure["detail"], step=STEP_PREFLIGHT,
                       action="Enable the Autopilot machine gate, Operator workspace level, and direct apply mode."))
    if apply and result["refusal"] is not None:
        all_not_run("preflight refused; nothing was created")
        return result

    # Preview every step. A dry run reports these; an apply treats them as a
    # precondition so nothing is written unless the whole chain validates.
    spec_has_id = bool(raw_spec.get("mission_id"))
    mission_step, mission_refusal, preview_id = _preview_mission(spec_json, hermes_root, spec_has_id)
    block(mission_refusal)
    if preview_id is None:
        plan_step = _step(STEP_PLAN, "skipped", "depends on mission_create")
        budget_step = (_step(STEP_BUDGET, "skipped", BUDGET_SKIPPED_DETAIL) if budget_req is None
                       else _step(STEP_BUDGET, "skipped", "depends on mission_create"))
    else:
        result["mission_id"] = preview_id if spec_has_id else None
        plan_step, plan_refusal = _preview_plan(raw_spec, preview_id, hermes_root)
        block(plan_refusal)
        budget_step, budget_refusal = _preview_budget(budget_req, preview_id, hermes_root)
        block(budget_refusal)
    if preview_id is None and preflight_failure is None:
        autopilot_step, autopilot_refusal = _step(STEP_AUTOPILOT, "skipped", "depends on mission_create"), None
    else:
        autopilot_step, autopilot_refusal = _preview_autopilot(effective, preflight_failure)
    block(autopilot_refusal)
    result["steps"] = [mission_step, plan_step, budget_step, autopilot_step]

    if not apply:
        result["would_start_autopilot"] = result["refusal"] is None
        result["success"] = result["refusal"] is None
        return result

    if result["refusal"] is not None:
        for step in result["steps"]:
            if step["outcome"] in ("preview", "would_run"):
                step["outcome"] = "not_run"
                step["detail"] = "not run: the launch was refused before any write"
        result["mission_id"] = None  # a preview id is not a created id
        result["created"] = {}
        result["completed_steps"] = []
        result["not_run_steps"] = [s["step"] for s in result["steps"] if s["outcome"] != "skipped"]
        result["autopilot_started"] = False
        return result

    _apply_chain(spec_json, budget_req, effective, hermes_root, result, checkpoint)
    result["success"] = result["refusal"] is None and result["autopilot_started"]
    return result


def _audit(policy: op.OperatorPolicy, result: dict[str, Any]) -> None:
    """One launch-level audit record: ids, step outcomes, and refusal codes. No objective text."""
    try:
        op.audit_record(
            tool=TOOL,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=bool(result.get("dry_run")),
            success=bool(result.get("success")),
            changed=bool(result.get("created")) and not result.get("replayed", False),
            summary=f"{TOOL} mission={result.get('mission_id') or ''}",
            extra={
                "mission_id": result.get("mission_id") or "",
                "created": result.get("created") or {},
                "steps": [{"step": s["step"], "outcome": s["outcome"]} for s in result.get("steps", [])],
                "refusal_code": (result.get("refusal") or {}).get("code", ""),
                "autopilot_started": bool(result.get("autopilot_started")),
            },
        )
    except (OSError, TypeError, ValueError):
        return


# Receipts are additive and retained: deleting one allows its request id to be reused.
# Older versions ignore this store. It contains hashes/identifiers, never launch inputs.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_RECEIPTS = 10_000


def _receipt_path(root: Path | None) -> Path:
    return mission_runtime._root(root) / "missions" / "launch-receipts.db"


def _request_digest(request_id: str) -> str:
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must contain 16-64 letters, digits, hyphens or underscores")
    return hashlib.sha256(request_id.encode()).hexdigest()


def _receipt_error(code: str) -> dict[str, Any]:
    return {"success": False, "code": code, "retry_safe": False,
            "safe_message": "Inspect the original launch receipt before submitting new work."}


def _read_receipt(digest: str, root: Path | None) -> dict[str, Any] | None:
    path = _receipt_path(root)
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT input_digest,mission_id,state,result_json,created_at,updated_at FROM receipts WHERE request_digest=?", (digest,)).fetchone()
        if row is None:
            return None
        if len(row[3]) > 65536:
            raise ValueError("receipt exceeds bound")
        return {"input_digest": row[0], "mission_id": row[1], "state": row[2],
                "result": json.loads(row[3]), "created_at": row[4], "updated_at": row[5]}
    finally:
        db.close()


def launch_receipt(request_id: str, hermes_root: Path | None = None) -> dict[str, Any]:
    """Read a receipt without resuming work or creating a store."""
    try:
        op.OperatorPolicy().require_level("read_only")
        receipt = _read_receipt(_request_digest(request_id), hermes_root)
        return {"success": True, "found": receipt is not None, "receipt": receipt, "read_only": True}
    except PermissionError:
        return _receipt_error("OPERATOR_LEVEL_INSUFFICIENT")
    except (ValueError, OSError, sqlite3.Error):
        return _receipt_error("LAUNCH_RECEIPT_UNAVAILABLE")


def _record_receipt(digest: str, root: Path | None, result: dict[str, Any], state: str) -> None:
    # Closed projection: no objective, arbitrary validator error, or original request.
    summary = {"success": bool(result.get("success")), "mission_id": result.get("mission_id"),
               "created": result.get("created", {}), "autopilot_started": bool(result.get("autopilot_started")),
               "steps": [{"step": x["step"], "outcome": x["outcome"]} for x in result.get("steps", [])],
               "refusal_code": (result.get("refusal") or {}).get("code"), "retry_safe": False}
    db = sqlite3.connect(_receipt_path(root))
    try:
        with db:
            db.execute("UPDATE receipts SET state=?,result_json=?,updated_at=? WHERE request_digest=?",
                       (state, json.dumps(summary), datetime.now(timezone.utc).isoformat(), digest))
    finally:
        db.close()


def _keyed_launch(policy, spec_json, budget_json, limits_json, dry_run, confirm, root, request_id):
    try:
        policy.require_level("read_only")
        digest = _request_digest(request_id)
        raw = _json_object(spec_json, "spec_json", required=True) or {}
        # Stable Mission identity is reserved before effects, including after a crash.
        raw.setdefault("mission_id", "msn-launch-" + digest[:32])
        normalized = mission_runtime._normalize_spec(raw)
        budget_req = _budget_request(budget_json)
        limits = {**_LIMIT_DEFAULTS, **_limits_request(limits_json)}
        inputs = json.dumps([normalized, budget_req, limits], sort_keys=True, separators=(",", ":"), allow_nan=False)
        input_digest = hashlib.sha256(inputs.encode()).hexdigest()
        receipt = _read_receipt(digest, root)
        if receipt:
            if receipt["input_digest"] != input_digest:
                return _receipt_error("LAUNCH_REQUEST_CONFLICT")
            # An admitted attempt may not have checkpointed its first result yet.
            # Always return an explicit outcome; missing success must never be
            # interpreted as a successful mutation by a client adapter.
            outcome = {"success": False, **receipt["result"]}
            if not outcome["success"]:
                outcome.update(_receipt_error("LAUNCH_OUTCOME_UNKNOWN" if receipt["state"] == "in_progress" else "LAUNCH_STOPPED"))
            return {**outcome, "replayed": True, "dry_run": dry_run,
                    "receipt": receipt, "mission_id": receipt["mission_id"],
                    "outcome_unknown": receipt["state"] == "in_progress", "retry_safe": False}
        spec_json = json.dumps(raw)
        preview = _launch(policy, spec_json, budget_json, limits_json, True, False, root)
        if dry_run or not preview["success"]:
            return preview
        if not confirm:
            return _receipt_error("CONFIRM_REQUIRED")
        path = _receipt_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS receipts (request_digest TEXT PRIMARY KEY,input_digest TEXT NOT NULL,mission_id TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= MAX_RECEIPTS:
                    return _receipt_error("LAUNCH_RECEIPTS_FULL")
                now = datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?)", (digest, input_digest, raw["mission_id"], "in_progress", "{}", now, now))
        except sqlite3.IntegrityError:
            # Concurrent caller won admission. Read its receipt; never execute again.
            receipt = _read_receipt(digest, root)
            if receipt and receipt["input_digest"] != input_digest:
                return _receipt_error("LAUNCH_REQUEST_CONFLICT")
            return {**_receipt_error("LAUNCH_ALREADY_RESERVED"), "receipt": receipt, "mission_id": raw["mission_id"]}
        finally:
            db.close()
        try:
            result = _launch(policy, spec_json, budget_json, limits_json, False, True, root,
                             checkpoint=lambda result: _record_receipt(digest, root, result, "in_progress"))
            state = "completed" if result["success"] else "stopped"
            _record_receipt(digest, root, result, state)
            result["receipt"] = _read_receipt(digest, root)
            result["retry_safe"] = False
            result["note"] = "This request is reserved. Reuse its request_id to inspect the same attempt; never create a new id to retry uncertain work."
            return result
        except (OSError, ValueError, sqlite3.Error):
            return {**_receipt_error("LAUNCH_OUTCOME_UNKNOWN"), "mission_id": raw["mission_id"], "outcome_unknown": True}
    except PermissionError:
        return _receipt_error("OPERATOR_LEVEL_INSUFFICIENT")
    except (ValueError, TypeError, OSError, sqlite3.Error):
        return _receipt_error("LAUNCH_RECEIPT_UNAVAILABLE")


def hermes_mission_launch(
    spec_json: str,
    budget_json: str | None = None,
    autopilot_limits_json: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    hermes_root: Path | None = None,
    request_id: str | None = None,
) -> str:
    """Launch a Mission through existing gated steps; optional request_id deduplicates attempts.

    Dry runs write nothing. Reusing a request_id returns the existing receipt and
    never resumes or redispatches partial work. Omission preserves legacy behavior.
    """
    policy = op.OperatorPolicy()
    if request_id is None:
        result = _launch(policy, spec_json, budget_json, autopilot_limits_json, dry_run, confirm, hermes_root)
    else:
        result = _keyed_launch(policy, spec_json, budget_json, autopilot_limits_json, dry_run, confirm, hermes_root, request_id)
    _audit(policy, result)
    return json.dumps(result)
