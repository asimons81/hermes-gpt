"""Outcome / Work Contracts for hermes-gpt v0.6 (M1).

Implements the approved Work Contracts design (v0.6 M1,
feat/m1-work-contracts): structured work orders whose objective, assigned
agent/profile, allowed scope, forbidden actions, expected artifacts, tests, review
requirements, and completion criteria are declared up front, and whose completion is
verified against **observed** Mission Control state (run/outcome/artifacts) rather
than a worker's claim (success criterion S2, risk R4).

The surface is the ``hermes_contract_*`` tool group:

- ``hermes_contract_define``   — read-only, pure: validate + canonicalize a contract
  document (``hermes.work-contract/v1``) and return its canonical form + sha256.
- ``hermes_contract_dispatch`` — workspace level, dry-run-first: submit a contract as
  a fleet work order (reuses ``operator_fleet`` authority, live peer verification,
  dry-run/confirm gates).
- ``hermes_contract_validate`` — read-only by default; may run allowlisted tests when
  operator policy grants workspace + direct. Returns a deterministic verdict
  (``SATISFIED`` / ``NOT_SATISFIED`` / ``INCONCLUSIVE`` / ``INVALID_CONTRACT``) against
  observed Mission Control state, never the worker's self-report.
- ``hermes_contract_status``   — read-only: link a contract to its observed run state.

Design invariants enforced here (D1-D11, §9):

- Observed-only evidence (D4): the validator reads kanban ``task_runs``, async
  delegations, on-disk artifacts, and the audit trail. A worker-supplied
  ``result``/``completion_bundle`` is never accepted as proof.
- Fail-closed verdicts (D7): any required check that cannot be positively verified
  yields ``NOT_SATISFIED`` or ``INCONCLUSIVE``; a missing observed run is
  ``INCONCLUSIVE``, never ``SATISFIED``.
- Tests only through the workspace allowlist (D6): ``hermes_workspace_run_test``,
  ``shell=False``, bounded timeout, workdir under allowed paths; test execution
  individually gated at workspace + direct.
- Forbidden actions are detected, not prevented (D5/NG-WC4): the validator scans the
  audit trail (and artifact set) for forbidden-action signals.
- Redaction by class (D8): objective appears only as ``{prompt_len, prompt_sha256}``;
  forbidden-action labels are class-level; artifact paths are basenames on surfaces;
  validation evidence is summary-based.
- Every contract call is audited (D9) with ``contract_sha256`` + ``task_id``.
- No new persistent store (D10): contracts are declarative JSON documents; the
  validator reads existing Mission Control sources.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import operator_policy as op
import operator_fleet as op_fleet
import operator_mission as mission
import operator_workspace as op_workspace
import operator_runners as op_runners

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "0.6-wc.1"
CONTRACT_SCHEMA = "hermes.work-contract/v1"

# Fleet regexes reused for identity fields.
_AGENT_RE = op_fleet._AGENT_RE
_PROFILE_RE = op_fleet._PROFILE_RE
_TASK_ID_RE = op_fleet._TASK_ID_RE

_FORBIDDEN_CLASSES = frozenset({"LOW", "MED", "HIGH"})
_AUTH_CLASSES = op_fleet._AUTH_CLASSES

_MAX_OBJECTIVE_BYTES = 8_000
_MAX_ARTIFACTS = 32
_MAX_TESTS = 16
_MAX_FORBIDDEN_ACTIONS = 32
_MAX_SCOPE_WORKSPACES = 8
_MAX_SCOPE_PROFILES = 16
_MAX_REVIEW_EVIDENCE_SCAN = 500

_VERDICT_SATISFIED = "SATISFIED"
_VERDICT_NOT_SATISFIED = "NOT_SATISFIED"
_VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
_VERDICT_INVALID = "INVALID_CONTRACT"

_CHECK_KINDS = ("run_state", "artifacts", "tests", "review", "forbidden", "authorization")


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
# Error / envelope helpers
# ---------------------------------------------------------------------------


def _contract_error(
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


def _prompt_meta(text: str | None) -> dict[str, Any]:
    """Return ``{prompt_len, prompt_sha256}`` for a contract objective."""
    if text is None:
        return {"prompt_len": 0, "prompt_sha256": ""}
    data = text.encode("utf-8", errors="replace")
    return {
        "prompt_len": len(data),
        "prompt_sha256": hashlib.sha256(data).hexdigest(),
    }


def _truncate(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _audit_call(
    *,
    tool: str,
    dry_run: bool,
    success: bool,
    changed: bool,
    summary: str,
    contract_sha256: str = "",
    task_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a contract call in the operator audit log (D9).

    Never includes the objective text; only ``contract_sha256`` + ``task_id``.
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
                "contract_sha256": contract_sha256,
                "task_id": task_id,
                **(extra or {}),
            },
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Canonicalization (design §6)
# ---------------------------------------------------------------------------


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    """Bound + strip a text field (mirrors operator_fleet._clean_text)."""
    return op_fleet._clean_text(value, field=field, maximum=maximum, required=required)


def _string_list(value: Any, *, field: str) -> list[str]:
    return op_fleet._string_list(value, field=field)


def _workspace_list(value: Any) -> list[Path]:
    """Validate allowed_scope.workspaces: absolute, non-denied paths."""
    if not isinstance(value, list) or not value or len(value) > _MAX_SCOPE_WORKSPACES:
        raise ValueError("allowed_scope.workspaces must be a non-empty list (<= 8)")
    workspaces: list[Path] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("workspace must be a non-empty path string")
        p = op._normalize_path(item)
        if op.is_denied_path(p):
            raise PermissionError(f"workspace {item!r} is denied by the secret-path policy")
        if not p.is_absolute():
            raise ValueError(f"workspace {item!r} must be an absolute path")
        workspaces.append(p)
    return workspaces


def _profile_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_SCOPE_PROFILES:
        raise ValueError("allowed_scope.profiles must be a non-empty list (<= 16)")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("profile must be a string")
        out.append(op.validate_profile_name(item))
    return out


def _forbidden_list(value: Any) -> list[dict[str, Any]]:
    """Normalize forbidden_actions to ``{action, reason, class}`` (LOW/MED/HIGH)."""
    if not isinstance(value, list) or len(value) > _MAX_FORBIDDEN_ACTIONS:
        raise ValueError(f"forbidden_actions must be a list (<= {_MAX_FORBIDDEN_ACTIONS})")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("forbidden action must be an object")
        action = _clean_text(item.get("action"), field="forbidden action", maximum=128)
        reason = _clean_text(item.get("reason", ""), field="forbidden reason", maximum=500, required=False)
        klass = str(item.get("class", "HIGH")).upper()
        if klass not in _FORBIDDEN_CLASSES:
            raise ValueError(f"forbidden action class must be one of {sorted(_FORBIDDEN_CLASSES)}")
        out.append({"action": action, "reason": reason, "class": klass})
    return out


def _resolve_artifact_paths(path: str, workspaces: list[Path]) -> list[Path]:
    """Resolve a (possibly relative) artifact path against every workspace.

    No ``..`` escaping: the resolved path must remain under its workspace.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("artifact path must be a non-empty string")
    resolved: list[Path] = []
    for ws in workspaces:
        p = Path(path)
        if not p.is_absolute():
            p = ws / p
        r = p.resolve()
        if op.is_denied_path(r):
            raise PermissionError(f"artifact path {path!r} is denied by the secret-path policy")
        if not op.path_under_allowed(r, [ws]):
            raise ValueError(f"artifact path {path!r} escapes its workspace")
        resolved.append(r)
    return resolved


def _artifact_list(value: Any, workspaces: list[Path]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _MAX_ARTIFACTS:
        raise ValueError(f"expected_artifacts must be a list (<= {_MAX_ARTIFACTS})")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected artifact must be an object")
        path = _clean_text(item.get("path"), field="artifact path", maximum=1000)
        _resolve_artifact_paths(path, workspaces)  # validates no escape / no denied
        must_exist = bool(item.get("must_exist", True))
        min_bytes = int(item.get("min_bytes", 0))
        if min_bytes < 0:
            raise ValueError("min_bytes must be >= 0")
        out.append({"path": path, "must_exist": must_exist, "min_bytes": min_bytes})
    return out


def _test_list(value: Any) -> list[dict[str, Any]]:
    """Validate tests against the workspace allowlist (D6)."""
    if not isinstance(value, list) or len(value) > _MAX_TESTS:
        raise ValueError(f"tests must be a list (<= {_MAX_TESTS})")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("test must be an object")
        name = _clean_text(item.get("name"), field="test name", maximum=200)
        command = _clean_text(item.get("command"), field="test command", maximum=1000)
        try:
            argv = op_workspace._split_command_argv(command)
        except ValueError as exc:
            raise ValueError(f"test command is not parseable: {exc}") from exc
        allowed, reason = op_workspace._is_allowed_test_command(argv)
        if not allowed:
            raise ValueError(f"test {name!r} is not in the allowlist: {reason}")
        workdir = _clean_text(item.get("workdir", ""), field="test workdir", maximum=1000, required=False)
        out.append({"name": name, "command": command, "workdir": workdir})
    return out


def _review_requirements(value: Any) -> dict[str, Any]:
    if value is None:
        return {"required": False, "reviewer": "", "evidence": "", "approval_required": False}
    if not isinstance(value, dict):
        raise ValueError("review_requirements must be an object")
    required = bool(value.get("required", False))
    reviewer = _clean_text(value.get("reviewer", ""), field="reviewer", maximum=128, required=False)
    evidence = _clean_text(value.get("evidence", ""), field="review evidence", maximum=500, required=False)
    approval_required = bool(value.get("approval_required", required))
    return {
        "required": required,
        "reviewer": reviewer,
        "evidence": evidence,
        "approval_required": approval_required,
    }


def _completion_criteria(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("completion_criteria must be an object")
    run_state = value.get("run_state")
    if not isinstance(run_state, dict):
        raise ValueError("completion_criteria.run_state must be an object")
    outcome_ok = run_state.get("outcome_ok")
    if not isinstance(outcome_ok, list) or not outcome_ok or any(not isinstance(x, str) for x in outcome_ok):
        raise ValueError("completion_criteria.run_state.outcome_ok must be a non-empty string list")
    terminal = bool(run_state.get("terminal", True))
    return {
        "run_state": {"terminal": terminal, "outcome_ok": [str(x) for x in outcome_ok]},
        "artifacts_present": bool(value.get("artifacts_present", True)),
        "tests_pass": bool(value.get("tests_pass", False)),
        "review_satisfied": bool(value.get("review_satisfied", False)),
        "no_forbidden_actions": bool(value.get("no_forbidden_actions", True)),
    }


def _canonical_contract(raw: Any) -> tuple[str, dict[str, Any]]:
    """Validate + canonicalize a contract document.

    Returns ``(canonical_json, contract_dict)``. Raises ValueError /
    PermissionError on schema, scope, or authorization violations.
    """
    if not isinstance(raw, dict):
        raise ValueError("contract must be a JSON object")
    if raw.get("schema") != CONTRACT_SCHEMA:
        raise ValueError(f"contract schema must be {CONTRACT_SCHEMA!r}")

    task_id = _clean_text(raw.get("task_id"), field="task_id", maximum=128)
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id has an invalid format")
    assigned_agent = _clean_text(raw.get("assigned_agent"), field="assigned_agent", maximum=64)
    if not _AGENT_RE.fullmatch(assigned_agent):
        raise ValueError("assigned_agent is invalid (must be a fleet peer name)")
    assigned_profile = _clean_text(raw.get("assigned_profile"), field="assigned_profile", maximum=64)
    if not _PROFILE_RE.fullmatch(assigned_profile):
        raise ValueError("assigned_profile is invalid")
    objective = _clean_text(raw.get("objective"), field="objective", maximum=_MAX_OBJECTIVE_BYTES)

    scope = raw.get("allowed_scope")
    if not isinstance(scope, dict):
        raise ValueError("allowed_scope must be an object")
    workspaces = _workspace_list(scope.get("workspaces"))
    profiles = _profile_list(scope.get("profiles"))

    forbidden = _forbidden_list(raw.get("forbidden_actions") or [])
    artifacts = _artifact_list(raw.get("expected_artifacts") or [], workspaces)
    tests = _test_list(raw.get("tests") or [])
    review = _review_requirements(raw.get("review_requirements"))
    criteria = _completion_criteria(raw.get("completion_criteria"))

    # Authorization is the fleet authorization metadata (class/approved/approved_by).
    auth_value = raw.get("authorization")
    if auth_value is None and isinstance(raw.get("completion_criteria"), dict):
        auth_value = raw["completion_criteria"].get("authorization")
    authorization = op_fleet._authorization(auth_value)
    execution = op_runners.normalize_execution(raw.get("execution"))

    contract: dict[str, Any] = {
        "schema": CONTRACT_SCHEMA,
        "task_id": task_id,
        "assigned_agent": assigned_agent,
        "assigned_profile": assigned_profile,
        "objective": objective,
        "allowed_scope": {
            "workspaces": [str(w) for w in workspaces],
            "profiles": profiles,
        },
        "forbidden_actions": forbidden,
        "expected_artifacts": artifacts,
        "tests": tests,
        "review_requirements": review,
        "completion_criteria": criteria,
        "inputs": _string_list(raw.get("inputs") or [], field="inputs"),
        "constraints": _string_list(raw.get("constraints") or [], field="constraints"),
        "authorization": authorization,
    }
    # Backward compatibility: omit the default fleet selector from canonical
    # contracts unless the caller explicitly supplied an execution block. This
    # preserves hashes for pre-runner work contracts.
    if execution is not None:
        contract["execution"] = execution
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, contract


def _contract_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _parse_contract(contract_json: str) -> tuple[str, dict[str, Any], str]:
    """Parse + canonicalize a contract JSON string.

    Returns ``(canonical_json, contract, contract_sha256)``.
    """
    if not isinstance(contract_json, str) or not contract_json.strip():
        raise ValueError("contract_json is required")
    try:
        raw = json.loads(contract_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"contract_json is not valid JSON: {exc}") from exc
    canonical, contract = _canonical_contract(raw)
    return canonical, contract, _contract_sha256(canonical)


# ---------------------------------------------------------------------------
# Surface redaction (D8)
# ---------------------------------------------------------------------------


def _surface_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Redact a canonical contract for surfaces.

    - objective -> ``{prompt_len, prompt_sha256}``
    - forbidden actions -> ``{action, class}`` (+ redacted reason)
    - artifacts -> basename + must_exist + min_bytes
    """
    surf = dict(contract)
    surf["objective"] = _prompt_meta(contract.get("objective"))
    surf["forbidden_actions"] = [
        {
            "action": fa["action"],
            "class": fa["class"],
            "reason": op.redact_output(fa.get("reason", "")),
        }
        for fa in contract.get("forbidden_actions", [])
    ]
    surf["expected_artifacts"] = [
        {
            "basename": Path(a["path"]).name,
            "path": a["path"],
            "must_exist": a["must_exist"],
            "min_bytes": a["min_bytes"],
        }
        for a in contract.get("expected_artifacts", [])
    ]
    if isinstance(contract.get("execution"), dict):
        options = contract["execution"].get("options") or {}
        surf["execution"] = {
            "backend": contract["execution"].get("backend"),
            "option_keys": sorted(str(k) for k in options.keys()),
        }
    return surf


# ---------------------------------------------------------------------------
# Observed state helpers (validator evidence, D4)
# ---------------------------------------------------------------------------


def _observed_kanban_runs(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    warnings: list[str] = []
    try:
        runs = mission._kanban_runs_for(hermes_root, warnings)
    except Exception:
        return []
    return [
        {
            "task_id": r.get("task_id"),
            "board": r.get("board"),
            "assignee": r.get("assignee"),
            "status": r.get("status"),
            "outcome": r.get("outcome"),
            "error": r.get("error"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
        }
        for r in runs
        if r.get("task_id") == task_id
    ]


def _observed_delegations(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    for profile in op.list_existing_profiles(hermes_root):
        try:
            home = op.resolve_profile_home(profile, hermes_root)
            for d in mission._async_delegations_for(home, warnings, profile):
                if d.get("delegation_id") == task_id:
                    out.append(
                        {
                            "delegation_id": d.get("delegation_id"),
                            "state": d.get("state"),
                            "dispatched_at": d.get("dispatched_at"),
                            "completed_at": d.get("completed_at"),
                            "scope": d.get("scope"),
                        }
                    )
        except Exception:
            continue
    return out


def _observed_runs(task_id: str, hermes_root: Path) -> list[dict[str, Any]]:
    """All observed run/outcome records for a task id.

    Existing Mission Control sources remain authoritative for legacy/fleet work;
    pluggable runner jobs contribute their own bounded durable state.
    """
    return (
        _observed_kanban_runs(task_id, hermes_root)
        + _observed_delegations(task_id, hermes_root)
        + op_runners.observed_runs(task_id, hermes_root=hermes_root)
    )


def _observed_audit(limit: int = _MAX_REVIEW_EVIDENCE_SCAN) -> list[dict[str, Any]]:
    try:
        return op.audit_tail(limit=limit)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Validator checks (design §7.2)
# ---------------------------------------------------------------------------


def _check_run_state(contract: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    task_id = contract["task_id"]
    outcome_ok = set(contract["completion_criteria"]["run_state"]["outcome_ok"])
    runs = _observed_runs(task_id, hermes_root)
    if not runs:
        return {
            "kind": "run_state",
            "status": "UNVERIFIED",
            "detail": f"no observed run/outcome for task_id {task_id}",
        }
    # Retries produce multiple records for a task. Select the latest observed
    # record by stable keys rather than relying on SQLite/list traversal order.
    # A newer retry is authoritative; an older successful attempt cannot mask a
    # currently-running or failed retry.
    def run_sort_key(run: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
        return (
            str(run.get("started_at") or run.get("dispatched_at") or ""),
            str(run.get("ended_at") or run.get("completed_at") or ""),
            str(run.get("status") or ""),
            str(run.get("outcome") or run.get("state") or ""),
            str(run.get("board") or run.get("scope") or ""),
            str(run.get("error") or ""),
        )

    primary = max(runs, key=run_sort_key)
    status = str(primary.get("status") or "")
    outcome = str(primary.get("outcome") or primary.get("state") or "")
    error = primary.get("error")
    source = primary.get("board") or primary.get("scope") or "observed"
    if error:
        return {
            "kind": "run_state",
            "status": "FAIL",
            "detail": f"observed {source} run errored: {_truncate(str(error), 200)}",
        }
    if outcome:
        outcome_passes = outcome in outcome_ok
    else:
        outcome_passes = status in outcome_ok
    if outcome_passes:
        return {
            "kind": "run_state",
            "status": "PASS",
            "detail": f"observed {source} status={status} outcome={outcome}",
        }
    return {
        "kind": "run_state",
        "status": "FAIL",
        "detail": f"observed {source} status={status} outcome={outcome} not in outcome_ok",
    }


def _admitted_artifact_evidence(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> list[dict[str, Any]]:
    """Read coordinator-verified remote artifact metadata from capable runners.

    This is intentionally metadata-only. Active remote content remains in the
    Fabric admission store and is never copied into the Work Contract workspace
    merely to make the artifact check pass.
    """
    task_id = str(contract.get("task_id") or "")
    try:
        backend = op_runners.get_backend("fabric")
    except LookupError:
        return []
    observer = getattr(backend, "observed_artifacts", None)
    if not callable(observer):
        return []
    try:
        value = observer(
            task_id,
            contract_sha256=contract_sha256,
            hermes_root=hermes_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _check_artifacts(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> dict[str, Any]:
    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    artifacts = contract["expected_artifacts"]
    if not artifacts:
        return {"kind": "artifacts", "status": "PASS", "detail": "no artifacts required"}
    if not workspaces:
        return {"kind": "artifacts", "status": "UNVERIFIED", "detail": "no allowed workspace"}

    admitted = _admitted_artifact_evidence(contract, contract_sha256, hermes_root)
    admitted_by_name: dict[str, list[dict[str, Any]]] = {}
    for item in admitted:
        name = item.get("logical_name")
        if isinstance(name, str):
            admitted_by_name.setdefault(name, []).append(item)

    missing: list[str] = []
    evidence: list[dict[str, Any]] = []
    for art in artifacts:
        if not art["must_exist"]:
            continue
        candidates = []
        try:
            candidates = _resolve_artifact_paths(art["path"], workspaces)
        except (ValueError, PermissionError):
            missing.append(art["path"])
            continue
        found = None
        for cand in candidates:
            try:
                if cand.is_file() and cand.stat().st_size >= art["min_bytes"]:
                    found = cand
                    break
            except OSError:
                continue
        if found is not None:
            evidence.append({"basename": found.name, "size": found.stat().st_size})
            continue

        remote = next(
            (
                item
                for item in admitted_by_name.get(art["path"], [])
                if isinstance(item.get("size_bytes"), int)
                and item["size_bytes"] >= art["min_bytes"]
                and item.get("provenance") == "coordinator_verified_artifact"
            ),
            None,
        )
        if remote is not None:
            evidence.append(
                {
                    "basename": Path(art["path"]).name,
                    "size": remote["size_bytes"],
                    "sha256": remote.get("sha256", ""),
                    "provenance": "coordinator_verified_artifact",
                }
            )
        else:
            missing.append(art["path"])
    if missing:
        return {
            "kind": "artifacts",
            "status": "FAIL",
            "detail": f"missing artifacts: {', '.join(missing)}",
            "evidence": evidence,
        }
    return {
        "kind": "artifacts",
        "status": "PASS",
        "detail": f"{len(artifacts)} artifact(s) present",
        "evidence": evidence,
    }


def _check_tests(contract: dict[str, Any], runner: Callable[..., tuple[int, str, str]] | None, hermes_root: Path) -> dict[str, Any]:
    tests = contract["tests"]
    tests_required = contract["completion_criteria"]["tests_pass"]
    if not tests:
        return {
            "kind": "tests",
            "status": "PASS" if not tests_required else "UNVERIFIED",
            "detail": "no tests declared" if not tests_required else "tests required but none declared",
        }
    if not tests_required:
        return {"kind": "tests", "status": "PASS", "detail": "tests optional (tests_pass=false)"}

    # Test execution individually gated at workspace + direct (D6).
    policy = op.OperatorPolicy()
    can_run = op.has_level("workspace", policy.level) and policy.apply_mode == "direct"
    if not can_run:
        return {
            "kind": "tests",
            "status": "UNVERIFIED",
            "detail": "tests required but workspace+direct not granted (D6)",
        }

    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    results: list[dict[str, Any]] = []
    for t in tests:
        workdir = t.get("workdir") or (str(workspaces[0]) if workspaces else None)
        if workdir and workspaces and not op.path_under_allowed(workdir, workspaces):
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} workdir is not under an allowed workspace",
            }
        try:
            out = op_workspace.hermes_workspace_run_test(
                command=t["command"],
                workdir=workdir,
                timeout=120,
                dry_run=False,
                runner=runner,
            )
            payload = json.loads(out)
        except Exception as exc:
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} refused/errored: {_truncate(op.redact_output(str(exc)), 200)}",
            }
        rc = payload.get("returncode")
        results.append({"name": t["name"], "rc": rc, "command": t["command"]})
        if rc != 0:
            return {
                "kind": "tests",
                "status": "FAIL",
                "detail": f"test {t['name']!r} failed rc={rc}",
                "evidence": results,
            }
    return {
        "kind": "tests",
        "status": "PASS",
        "detail": f"{len(tests)} test(s) passed",
        "evidence": results,
    }


def _assignee_identity(contract: dict[str, Any]) -> str:
    """Return the executing profile identity for attribution-sensitive checks.

    ``assigned_agent`` identifies placement: it may be ``auto`` before routing
    or a Fabric node name after remote placement. ``assigned_profile`` is the
    authority-bearing actor that actually executes the contract, so review
    distinctness and audit attribution must use it whenever present. The
    assigned-agent fallback preserves compatibility with legacy callers that
    construct an incomplete contract outside the normal parser.
    """
    assigned_profile = str(contract.get("assigned_profile") or "").strip()
    if assigned_profile:
        return assigned_profile
    return str(contract["assigned_agent"])


def _check_review(contract: dict[str, Any], contract_sha256: str, hermes_root: Path) -> dict[str, Any]:
    review = contract["review_requirements"]
    assignee_identities = _attributable_identities(contract)
    if not review["required"]:
        return {"kind": "review", "status": "PASS", "detail": "review not required"}

    declared_reviewer = review.get("reviewer") or ""
    if declared_reviewer and declared_reviewer in assignee_identities:
        return {
            "kind": "review",
            "status": "FAIL",
            "detail": "self-review: declared reviewer == assignee identity",
        }

    # Evidence 1: an audit hermes_contract_validate acceptance by a distinct reviewer.
    for rec in _observed_audit():
        if rec.get("tool") != "hermes_contract_validate":
            continue
        if rec.get("contract_sha256") != contract_sha256:
            continue
        verdict = rec.get("verdict")
        if verdict not in ("SATISFIED", "accept", "ACCEPT"):
            continue
        reviewer = rec.get("reviewer") or rec.get("profile") or ""
        if reviewer and reviewer not in assignee_identities:
            return {
                "kind": "review",
                "status": "PASS",
                "detail": f"audit acceptance by reviewer {reviewer} != assignee",
            }
    # Evidence 1b (v0.7 S3): a ReviewAcceptanceRecord in the review-evidence
    # store written via hermes_review_accept. Distinct reviewer re-checked at
    # validate time (ADR-003): the record's reviewer must differ from the
    # contract's assigned_agent.
    try:
        import operator_review as op_review

        for rec in op_review.read_review_acceptances(hermes_root):
            if rec.get("contract_sha256") != contract_sha256:
                continue
            if rec.get("verdict") != "SATISFIED":
                continue
            reviewer = rec.get("reviewer") or ""
            if reviewer and reviewer not in assignee_identities:
                return {
                    "kind": "review",
                    "status": "PASS",
                    "detail": f"review-evidence acceptance by reviewer {reviewer} != assignee",
                }
    except Exception:
        pass
    # Evidence 2: human approval reference by someone other than the assignee.
    auth = contract.get("authorization") or {}
    approved_by = auth.get("approved_by") or ""
    if approved_by and approved_by not in assignee_identities and auth.get("approval_reference"):
        return {
            "kind": "review",
            "status": "PASS",
            "detail": f"human approval by {approved_by} (ref {auth.get('approval_reference')})",
        }
    return {
        "kind": "review",
        "status": "FAIL",
        "detail": "review required but no evidence by a reviewer distinct from the assignee",
    }


def _attributable_identities(contract: dict[str, Any]) -> set[str]:
    """Identities whose audit records are attributable to this contract's execution.

    ``assigned_profile`` is the effective assignee (authority-bearing actor).
    ``assigned_agent`` is the placement identity: after remote auto placement it
    is the Fabric node / dispatcher name that physically executed on the
    assignee's behalf. Task-scoped records recorded under either identity must
    be attributed; records under any other concrete profile stay unattributed
    so unrelated concurrent actors cannot fail or satisfy this contract.
    """
    identities: set[str] = set()
    for value in (contract.get("assigned_profile"), contract.get("assigned_agent")):
        text = str(value or "").strip()
        if text:
            identities.add(text)
    return identities


def _auto_fabric_lineage(
    contract: dict[str, Any],
    contract_sha256: str,
    hermes_root: Path,
) -> bool | None:
    """Return whether an auto contract has durable remote Fabric placement lineage.

    ``True`` means at least one matching routing decision selected remote Fabric.
    ``False`` means matching durable routing evidence exists and is local-only.
    ``None`` means placement provenance cannot be proven, which callers must treat
    as unverified rather than assuming local execution.
    """
    execution = contract.get("execution")
    if not isinstance(execution, dict) or str(execution.get("backend") or "").strip().lower() != "auto":
        return False

    journal = _resolve_root(hermes_root) / "fabric" / "routing-decisions.jsonl"
    matched = False
    try:
        with journal.open("rb") as fh:
            for raw_line in fh:
                if not raw_line or len(raw_line) > 128_000:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if (
                    record.get("schema") != "hermes.fabric-routing-decision/v1"
                    or record.get("task_id") != contract.get("task_id")
                    or record.get("original_contract_sha256") != contract_sha256
                ):
                    continue
                selected = record.get("selected")
                if not isinstance(selected, dict):
                    continue
                matched = True
                if selected.get("remote") is True and selected.get("transport_backend") == "fabric":
                    return True
    except OSError:
        return None
    return False if matched else None


def _check_forbidden(
    contract: dict[str, Any],
    hermes_root: Path,
    contract_sha256: str = "",
) -> dict[str, Any]:
    forbidden = contract["forbidden_actions"]
    if not forbidden:
        return {"kind": "forbidden", "status": "PASS", "detail": "no forbidden actions declared"}

    identities = _attributable_identities(contract)
    task_id = contract["task_id"]
    labels = [fa["action"].lower() for fa in forbidden]
    signals: list[dict[str, Any]] = []

    # Audit trail scan (D5): scope strictly to this contract's task identity.
    # Profile-only matching allowed an unrelated concurrent contract to fail this
    # one; records without a matching task_id are intentionally ignored.
    for rec in _observed_audit():
        if str(rec.get("task_id") or "") != task_id:
            continue
        profile = str(rec.get("profile") or "")
        source = str(rec.get("source_profile") or "").strip()
        attributable = (
            profile in identities
            or profile in ("", "unknown")
            # Records written by a dispatcher/peer on behalf of the assignee.
            or (bool(source) and source in identities)
        )
        if not attributable:
            continue
        tool = str(rec.get("tool") or "").lower()
        summary = str(rec.get("summary") or "").lower()
        extra_forbidden = str(rec.get("forbidden_action") or "").lower()
        for fa in forbidden:
            label = fa["action"].lower()
            if label in tool or label in summary or (extra_forbidden and label == extra_forbidden):
                signals.append({"action": fa["action"], "class": fa["class"], "tool": rec.get("tool"), "summary": _truncate(rec.get("summary"))})
                break

    # Artifact set scan: a forbidden-action label appearing in a produced artifact
    # path is a weak signal (detection, not prevention; NG-WC4).
    workspaces = [Path(w) for w in contract["allowed_scope"]["workspaces"]]
    for art in contract["expected_artifacts"]:
        try:
            candidates = _resolve_artifact_paths(art["path"], workspaces)
        except (ValueError, PermissionError):
            continue
        for cand in candidates:
            try:
                if cand.is_file():
                    low = str(cand).lower()
                    for fa in forbidden:
                        if fa["action"].lower() in low:
                            signals.append({"action": fa["action"], "class": fa["class"], "tool": "artifact", "summary": cand.name})
                            break
            except OSError:
                continue

    if signals:
        detail = "; ".join(f"{s['action']} ({s['class']}) via {s['tool']}" for s in signals[:5])
        return {"kind": "forbidden", "status": "FAIL", "detail": f"forbidden action detected: {detail}", "evidence": signals[:10]}

    # Fabric v1 admits bounded remote run-state evidence but does not admit a
    # coordinator-verifiable forbidden-action audit trail. New dispatches with
    # non-empty forbidden_actions are rejected at the Fabric boundary. Historical
    # explicit-Fabric contracts therefore remain unverified regardless of whether
    # the observer is currently healthy: absence of remote evidence is not proof.
    execution = contract.get("execution")
    execution_backend = (
        str(execution.get("backend") or "").strip().lower()
        if isinstance(execution, dict)
        else ""
    )
    if execution_backend == "fabric":
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric execution has no coordinator-verifiable forbidden-action evidence",
        }

    # Auto contracts need durable placement lineage before absence can be trusted.
    # A missing/unreadable journal is itself unverified; a competing local runner
    # record for the same task_id must never launder unknown Fabric provenance.
    auto_lineage = _auto_fabric_lineage(contract, contract_sha256, hermes_root)
    if auto_lineage is True:
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric auto placement has no coordinator-verifiable forbidden-action evidence",
        }
    if auto_lineage is None:
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "auto placement provenance is unavailable for forbidden-action verification",
        }

    # Legacy/non-auto callers may still have Fabric history discoverable by the
    # registered backend. Observation failure does not prove Fabric involvement,
    # but any positively observed Fabric run remains fail-closed.
    try:
        fabric_backend = op_runners.get_backend("fabric")
        observer = getattr(fabric_backend, "observed_runs", None)
        fabric_runs = observer(task_id, hermes_root=hermes_root) if callable(observer) else []
    except (LookupError, OSError, RuntimeError, TypeError, ValueError):
        fabric_runs = []
    if any(
        isinstance(run, dict)
        and (
            run.get("backend") == "fabric"
            or str(run.get("scope") or "").startswith("fabric:")
        )
        for run in fabric_runs
    ):
        return {
            "kind": "forbidden",
            "status": "UNVERIFIED",
            "detail": "remote Fabric run has no coordinator-verifiable forbidden-action evidence",
        }

    return {"kind": "forbidden", "status": "PASS", "detail": "no forbidden actions detected in audit/artifacts"}


def _check_authorization(contract: dict[str, Any]) -> dict[str, Any]:
    auth = contract.get("authorization") or {}
    approved = auth.get("approved") is True
    if not approved:
        return {"kind": "authorization", "status": "FAIL", "detail": "authorization not approved"}
    if auth.get("class") == "high_impact" and not auth.get("approved_by"):
        return {"kind": "authorization", "status": "FAIL", "detail": "high_impact requires approved_by"}
    return {"kind": "authorization", "status": "PASS", "detail": f"authorized ({auth.get('class')})"}


# ---------------------------------------------------------------------------
# Define
# ---------------------------------------------------------------------------


def hermes_contract_define(contract_json: str, hermes_root: Path | None = None) -> str:
    """Validate + canonicalize a contract document (read-only, pure).

    Returns the canonical contract (redacted per D8) + ``contract_sha256``.
    """
    tool = "hermes_contract_define"
    tid = op.new_trace_id()
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except PermissionError as exc:
        payload = _contract_error(
            code="CONTRACT_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the denied path / authorization and re-define.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="define denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and re-define.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="invalid contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    result = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "contract_define",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "contract": _surface_contract(contract),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(
        tool=tool,
        dry_run=True,
        success=True,
        changed=False,
        summary=f"defined contract task={contract['task_id']}",
        contract_sha256=sha,
        task_id=contract["task_id"],
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Dispatch bridge (design §8; reuse operator_fleet)
# ---------------------------------------------------------------------------


def hermes_contract_dispatch(
    contract_json: str,
    confirm: bool = False,
    dry_run: bool = True,
    timeout: int = 30,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_bin: str | None = None,
    authority_manifest: Path | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Submit a contract as a fleet work order (workspace level, dry-run-first).

    Reuses ``operator_fleet.hermes_fleet_dispatch_work_order`` for authority,
    live peer verification, dry-run/confirm gates, and audit.
    """
    tool = "hermes_contract_dispatch"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        policy = op.OperatorPolicy()
        policy.require_level("workspace")
    except PermissionError as exc:
        payload = _contract_error(
            code="FLEET_POLICY_DENIED",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Enable workspace-level Operator Mode before dispatching contracts.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="dispatch denied")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and re-dispatch.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="invalid contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    task_id = contract["task_id"]
    # Uniqueness invariant (design §6.3): task_id must be unique for the dispatch.
    if _observed_runs(task_id, root):
        payload = _contract_error(
            code="CONTRACT_ALREADY_DISPATCHED",
            safe_message=f"task_id {task_id!r} already has an observed run; use a unique task_id.",
            suggested_action="Pick a new task_id for this contract.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="duplicate task_id", contract_sha256=sha, task_id=task_id)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Dispatch through the selected execution backend. Contracts without an
    # explicit selector continue to use the legacy fleet path.
    payload = op_runners.dispatch_contract(
        contract,
        confirm=confirm,
        dry_run=dry_run,
        timeout=timeout,
        hermes_root=root,
        runner=runner,
        hermes_bin=hermes_bin,
        authority_manifest=authority_manifest,
    )

    # Augment the backend envelope with the contract identity; keep shape bounded.
    payload["contract_sha256"] = sha
    payload["contract_task_id"] = task_id
    _audit_call(
        tool=tool,
        dry_run=bool(payload.get("dry_run", True)),
        success=bool(payload.get("success", False)),
        changed=bool(payload.get("changed", False)),
        summary=(
            f"contract dispatch plan task={task_id}"
            if payload.get("dry_run")
            else f"contract dispatched task={task_id}"
        ),
        contract_sha256=sha,
        task_id=task_id,
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Validator (S2 heart)
# ---------------------------------------------------------------------------


def _validate_impl(contract: dict[str, Any], sha: str, runner: Callable[..., tuple[int, str, str]] | None, hermes_root: Path) -> dict[str, Any]:
    """Run the six checks and aggregate a deterministic verdict (D7)."""
    checks: list[dict[str, Any]] = [
        _check_run_state(contract, hermes_root),
        _check_artifacts(contract, sha, hermes_root),
        _check_tests(contract, runner, hermes_root),
        _check_review(contract, sha, hermes_root),
        _check_forbidden(contract, hermes_root, sha),
        _check_authorization(contract),
    ]

    required = {
        "run_state": True,
        "artifacts": contract["completion_criteria"]["artifacts_present"],
        "tests": contract["completion_criteria"]["tests_pass"],
        "review": contract["review_requirements"]["required"],
        "forbidden": contract["completion_criteria"]["no_forbidden_actions"],
        "authorization": True,
    }

    failed = [c for c in checks if c["status"] == "FAIL" and required[c["kind"]]]
    unverified = [c for c in checks if c["status"] == "UNVERIFIED" and required[c["kind"]]]

    rejected: list[str] = []
    false_done = False
    if failed:
        verdict = _VERDICT_NOT_SATISFIED
        false_done = True
        rejected = [f"{c['kind']} FAIL: {c['detail']}" for c in failed]
    elif unverified:
        # Fail-closed: required checks that cannot be positively verified never pass.
        run_unverified = any(c["kind"] == "run_state" for c in unverified)
        other_unverified = [c for c in unverified if c["kind"] != "run_state"]
        if run_unverified and not other_unverified:
            # Valid contract + no observed run at all -> INCONCLUSIVE (D7 §7.3).
            verdict = _VERDICT_INCONCLUSIVE
            rejected = [
                "no observed run/outcome for "
                f"{contract['task_id']}; cannot confirm completion (fail-closed)"
            ]
            false_done = True
        else:
            verdict = _VERDICT_NOT_SATISFIED
            false_done = True
            rejected = [f"{c['kind']} UNVERIFIED: {c['detail']}" for c in unverified]
    else:
        verdict = _VERDICT_SATISFIED
        false_done = False

    evidence: dict[str, Any] = {
        "run": _observed_runs(contract["task_id"], hermes_root)[:5],
        "artifacts": [],
        "audit_count": len(_observed_audit()),
    }
    for c in checks:
        if c.get("evidence"):
            evidence["artifacts"].extend(c["evidence"] if isinstance(c["evidence"], list) else [c["evidence"]])
    evidence["artifacts"] = evidence["artifacts"][:10]

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "hermes_contract_validate",
        "surface": "contract_validate",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "verdict": verdict,
        "satisfied": verdict == _VERDICT_SATISFIED,
        "checks": [
            {
                "kind": c["kind"],
                "status": c["status"],
                "detail": c.get("detail", ""),
            }
            for c in checks
        ],
        "false_done_detected": false_done,
        "rejected_reasons": rejected,
        "evidence": evidence,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": op.new_trace_id(),
    }


def hermes_contract_validate(
    contract_json: str,
    runner: Callable[..., tuple[int, str, str]] | None = None,
    hermes_root: Path | None = None,
) -> str:
    """Return a deterministic verdict against **observed** Mission Control state.

    The worker's own ``result``/``completion_bundle`` is never accepted as proof
    (D4). Test execution is individually gated at workspace + direct (D6).
    """
    tool = "hermes_contract_validate"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "surface": "contract_validate",
            "task_id": "",
            "contract_sha256": "",
            "verdict": _VERDICT_INVALID,
            "satisfied": False,
            "checks": [],
            "false_done_detected": False,
            "rejected_reasons": [f"INVALID_CONTRACT: {op.redact_output(str(exc))[:300]}"],
            "evidence": {},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": tid,
        }
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="invalid contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    payload = _validate_impl(contract, sha, runner, root)
    _audit_call(
        tool=tool,
        dry_run=True,
        success=payload["satisfied"],
        changed=False,
        summary=f"validate task={contract['task_id']} verdict={payload['verdict']}",
        contract_sha256=sha,
        task_id=contract["task_id"],
        extra={"verdict": payload["verdict"]},
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def hermes_contract_status(contract_json: str, hermes_root: Path | None = None) -> str:
    """Link a contract to its observed run/delegation state (read-only)."""
    tool = "hermes_contract_status"
    tid = op.new_trace_id()
    root = _resolve_root(hermes_root)
    try:
        canonical, contract, sha = _parse_contract(contract_json)
    except (ValueError, TypeError, PermissionError) as exc:
        payload = _contract_error(
            code="INVALID_CONTRACT",
            safe_message=op.redact_output(str(exc))[:300],
            suggested_action="Correct the contract schema and retry.",
            trace_id=tid,
        )
        _audit_call(tool=tool, dry_run=True, success=False, changed=False, summary="invalid contract")
        return json.dumps(payload, ensure_ascii=False, indent=2)

    kanban = _observed_kanban_runs(contract["task_id"], root)
    delegations = _observed_delegations(contract["task_id"], root)
    payload = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "surface": "contract_status",
        "task_id": contract["task_id"],
        "contract_sha256": sha,
        "observed": {
            "kanban_runs": kanban[:10],
            "delegations": delegations[:10],
        },
        "summary": (
            f"{len(kanban)} kanban run(s), {len(delegations)} delegation(s) observed "
            f"for task_id {contract['task_id']}"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
    }
    _audit_call(
        tool=tool,
        dry_run=True,
        success=True,
        changed=False,
        summary=f"status task={contract['task_id']} runs={len(kanban)} delegations={len(delegations)}",
        contract_sha256=sha,
        task_id=contract["task_id"],
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
