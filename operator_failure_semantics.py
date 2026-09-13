"""Semantic failure classification + deterministic recovery matrix (decision output only).

Implements architecture proposal §17 item 7 / D8 / §11 (failure model): the Ops
8-class semantic failure taxonomy as a **thin classifier over authoritative
observations only**, plus the recovery matrix as a **deterministic
smallest-first lookup** (§11.2).

**Decision output only.** This module classifies an observation envelope and
proposes the smallest recovery action; it never executes anything:
``would_execute`` is always ``False``, no dispatch/reclaim/redispatch/approval
path exists, and the only durable write is a ``controller_plan`` row recording
the decision itself (workspace + direct + confirm, dry-run-first) — the
telemetry surface §6.2 calls ``controller_plan``.

Observation inputs are authoritative channels only (proposal §11.1):
- delegation ``state`` / ``backend_state`` / ``outcome`` + ``validation_verdict``
  (``operator_delegations``);
- runner ``observed_runs`` latest status/outcome/error (``operator_runners``);
- worker exit classification mirroring the host dispatcher's
  ``_classify_worker_exit`` vocabulary (``clean_exit`` / ``rate_limited`` /
  ``nonzero_exit`` / ``signaled`` / ``unknown``);
- respawn-blocker token matching over ``last_failure_error`` mirroring (and
  refining) the host ``_RESPAWN_BLOCKER_RE``;
- live capability negotiation result (ok/not-ok + bounded reasons).

Invariants:
- **Fail-closed.** Unclassifiable or malformed observations classify
  ``unknown`` → park ``blocked`` + ``need_attention`` with a recorded
  ``classification_uncertainty`` reason. A claimed-in-flight subject with no
  observable run channel is ``unknown``, never an optimistic class. A
  ``nonzero_exit`` with no flavor evidence is ``unknown`` (no basis to pick
  semantic vs transient).
- **Deterministic.** Pure function over the envelope; fixed check ladder;
  canonical-JSON ``decision_sha256``; same input → identical decision.
- **INV-9 data containment.** Raw ``last_failure_error``/runner error text is
  matched against a fixed token vocabulary and **never stored** — only the
  matched token ids, bounded enums, counts, and hashes are recorded.
  Secret-shaped input is redacted from any echoed summary.
- **D8 replan bound.** A replan is *proposed* only for ``semantic_failure``
  and only while ``replan_attempts_used < max`` (default 1); it routes through
  the existing decompose/advance tools and is never executed here.
- **Breaker respected (§8.1).** When the circuit breaker is open
  (``gave_up`` or ``consecutive_failures >= limit``) no retry action is
  proposed — the row becomes ``breaker_exhausted`` (blocked + human signal).

Conventions mirror ``operator_mission_budget`` / ``operator_placement``:
public functions return a JSON ``str`` of a bounded envelope; read surfaces
require ``read_only``; decision recording requires ``workspace`` + ``direct``
(dry-run-first). Every call is audited (bounded summary + counts only).
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

SCHEMA_VERSION = "0.9-failure-semantics.1"
DECISION_SCHEMA = "hermes.failure-decision/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# §11.1 taxonomy — the 8 semantic failure classes (authoritative, Ops model)
# ---------------------------------------------------------------------------

CLASS_TRANSIENT = "transient"
CLASS_WAITING = "waiting"
CLASS_SEMANTIC = "semantic_failure"
CLASS_AUTHORITY = "authority"
CLASS_CAPABILITY = "capability"
CLASS_ENVIRONMENT = "environment"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_TERMINAL = "cancelled"  # cancelled / operator-terminal: no action

FAILURE_CLASSES = (
    CLASS_TRANSIENT,
    CLASS_WAITING,
    CLASS_SEMANTIC,
    CLASS_AUTHORITY,
    CLASS_CAPABILITY,
    CLASS_ENVIRONMENT,
    CLASS_AMBIGUOUS,
    CLASS_TERMINAL,
)

# Non-failure classifications (recovery-matrix progress rows, §11.2).
CLASS_NONE_DISPATCHABLE = "none_dispatchable"  # ready child waiting for dispatch
CLASS_NONE_APPROVAL = "none_awaiting_approval"  # all terminal, approval required
CLASS_NONE_COMPLETION = "none_completion"  # all terminal, verified, no approval
CLASS_NONE = "none"  # healthy in-flight / steady state

CLASS_UNKNOWN = "unknown"  # fail-closed bucket (not one of the 8)

TAXONOMY: dict[str, dict[str, Any]] = {
    CLASS_TRANSIENT: {
        "meaning": "rate_limit, provider 5xx/429 throughput, transient fleet/network",
        "action": "backoff + retry (base 30s, cap 15min, jitter; breaker respected)",
        "auto_retry": True,
    },
    CLASS_WAITING: {
        "meaning": "parent not done, approval pending, child not terminal, provider replenishing",
        "action": "wait; no retry; recheck on next trigger",
        "auto_retry": False,
    },
    CLASS_SEMANTIC: {
        "meaning": "implementation/QA defect, test failure, forbidden action, artifact mismatch, protocol violation",
        "action": "escalate; bounded replan proposal (D8, <=1 default); route to Developer",
        "auto_retry": False,
    },
    CLASS_AUTHORITY: {
        "meaning": "policy deny, confirm/approval gate, auth/quota/billing credential blocker",
        "action": "park blocked, escalate to owner; never auto-retry",
        "auto_retry": False,
    },
    CLASS_CAPABILITY: {
        "meaning": "no capable node, missing skill, node not enrolled, backend unsupported",
        "action": "park blocked, escalate; no auto-retry",
        "auto_retry": False,
    },
    CLASS_ENVIRONMENT: {
        "meaning": "missing workspace, unreadable store, corrupt manifest, stale lease",
        "action": "fail-closed to reconciling/blocked; recover around or escalate",
        "auto_retry": False,
    },
    CLASS_AMBIGUOUS: {
        "meaning": "backend accepted but outcome unknown (submission_may_have_succeeded), reconciling",
        "action": "observe + report; never auto-redispatch; do not fabricate",
        "auto_retry": False,
    },
    CLASS_TERMINAL: {
        "meaning": "cancelled / operator-terminal state",
        "action": "terminal; no action",
        "auto_retry": False,
    },
}

# ---------------------------------------------------------------------------
# §11.2 recovery matrix — deterministic smallest-first lookup.
# Every action is a *proposed request*; nothing here executes (would_execute
# is always False). "smallest" ordering is embodied in the ladder + rows.
# ---------------------------------------------------------------------------

MATRIX: dict[str, dict[str, Any]] = {
    "dispatch_ready_child": {
        "observed": "mission running, child pending, parent ready",
        "smallest_action": "dispatch ready child",
        "proposed_tool": "hermes_swarm_stage_dispatch | hermes_contract_dispatch",
        "verify": "deployment durable (delegation dispatched, task running)",
        "auto_retry": False,
    },
    "reclaim_dead_worker": {
        "observed": "child running, worker dead",
        "smallest_action": "reclaim + bounded retry",
        "proposed_tool": "host kanban reclaim_task",
        "verify": "task back in ready/blocked; no duplicate spawn",
        "auto_retry": True,
    },
    "observe_reconciling": {
        "observed": "child reconciling / ambiguous backend outcome",
        "smallest_action": "observe + report; do NOT redispatch",
        "proposed_tool": "hermes_delegation_reconcile",
        "verify": "resolves or escalates; never auto-redispatched",
        "auto_retry": False,
    },
    "retry_transient_backoff": {
        "observed": "child failed (transient)",
        "smallest_action": "backoff + retry",
        "proposed_tool": "dispatcher retry (breaker respected)",
        "verify": "run reaches terminal",
        "auto_retry": True,
    },
    "breaker_exhausted": {
        "observed": "retry ceiling reached (consecutive_failures/failure_limit, gave_up)",
        "smallest_action": "blocked + gave_up + human signal; no further retry",
        "proposed_tool": "controller signal (broker) + operator",
        "verify": "human action; breaker state recorded",
        "auto_retry": False,
    },
    "escalate_semantic": {
        "observed": "child failed (semantic)",
        "smallest_action": "escalate + bounded replan proposal (D8)",
        "proposed_tool": "hermes_swarm_stage_advance rework | hermes_plan_decompose | Developer card",
        "verify": "plan revised only through decompose/advance; evidence attached",
        "auto_retry": False,
    },
    "signal_awaiting_approval": {
        "observed": "all children terminal, approval required",
        "smallest_action": "signal awaiting_approval and stop",
        "proposed_tool": "hermes_mission_reconcile",
        "verify": "status awaiting_approval; controller stops; owner approves",
        "auto_retry": False,
    },
    "request_completion": {
        "observed": "all children terminal, no approval required, verified evidence",
        "smallest_action": "request completion via verified lifecycle",
        "proposed_tool": "hermes_mission_reconcile",
        "verify": "completed; evidence ref recorded",
        "auto_retry": False,
    },
    "fail_closed_evidence": {
        "observed": "missing / corrupt / unverified evidence",
        "smallest_action": "fail-closed to blocked/reconciling; need_attention",
        "proposed_tool": "reconcile tools (hermes_mission_reconcile)",
        "verify": "not success; need_attention raised",
        "auto_retry": False,
    },
    "park_authority": {
        "observed": "authority / policy gate",
        "smallest_action": "park blocked, escalate",
        "proposed_tool": "controller signal + operator/owner",
        "verify": "human action; no auto-retry",
        "auto_retry": False,
    },
    "park_capability": {
        "observed": "no capable target",
        "smallest_action": "park blocked, escalate capability",
        "proposed_tool": "controller signal + Orchestrator",
        "verify": "placement reviewed; not auto-resolved",
        "auto_retry": False,
    },
    "recover_environment": {
        "observed": "environment fault (workspace/store/manifest/lease)",
        "smallest_action": "fail-closed to reconciling/blocked; recover around or escalate",
        "proposed_tool": "controller signal + operator",
        "verify": "environment recovered or escalated; never silently retried",
        "auto_retry": False,
    },
    "terminal_no_action": {
        "observed": "cancelled / operator terminal",
        "smallest_action": "none (terminal)",
        "proposed_tool": "",
        "verify": "terminal state stands; no action",
        "auto_retry": False,
    },
    "wait_recheck": {
        "observed": "waiting on dependency / approval / replenish",
        "smallest_action": "no action; recheck next trigger",
        "proposed_tool": "",
        "verify": "next trigger re-evaluates",
        "auto_retry": False,
    },
    "unknown_fail_closed": {
        "observed": "unclassifiable / missing observation",
        "smallest_action": "fail-closed: blocked + need_attention; record classification_uncertainty",
        "proposed_tool": "controller signal (broker)",
        "verify": "human review; no guessed class ever acted on",
        "auto_retry": False,
    },
}

MATRIX_ROW_KEYS = tuple(sorted(MATRIX))

# Which classes are *failure* classes vs non-failure progress classes.
_CLASS_TO_ROW: dict[str, str] = {
    CLASS_TRANSIENT: "retry_transient_backoff",
    CLASS_WAITING: "wait_recheck",
    CLASS_SEMANTIC: "escalate_semantic",
    CLASS_AUTHORITY: "park_authority",
    CLASS_CAPABILITY: "park_capability",
    CLASS_ENVIRONMENT: "recover_environment",
    CLASS_AMBIGUOUS: "observe_reconciling",
    CLASS_TERMINAL: "terminal_no_action",
    CLASS_UNKNOWN: "unknown_fail_closed",
    CLASS_NONE_DISPATCHABLE: "dispatch_ready_child",
    CLASS_NONE_APPROVAL: "signal_awaiting_approval",
    CLASS_NONE_COMPLETION: "request_completion",
    CLASS_NONE: "wait_recheck",
}

# ---------------------------------------------------------------------------
# Authoritative-vocabulary enums (inputs are validated against these; an
# unknown enum value is fail-closed to `unknown`, never guessed).
# ---------------------------------------------------------------------------

DELEGATION_STATES = frozenset(
    {"reserved", "queued", "running", "reconciling", "succeeded", "failed", "cancelled"}
)
DELEGATION_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MISSION_STATES = frozenset(
    {"draft", "running", "awaiting_approval", "completed", "paused", "blocked"}
)
NODE_STATES = frozenset(
    {
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
    }
)
NODE_TERMINAL = frozenset({"completed", "failed"})
WORKER_EXIT_KINDS = frozenset(
    {"clean_exit", "rate_limited", "nonzero_exit", "signaled", "unknown"}
)
VERDICTS = frozenset(
    {"", "SATISFIED", "NOT_SATISFIED", "INCONCLUSIVE", "INVALID_CONTRACT"}
)
INFLIGHT_DELEGATION_STATES = frozenset({"queued", "running", "reconciling"})

# ---------------------------------------------------------------------------
# Fixed token vocabularies (INV-9: only matched token ids are ever recorded).
#
# These refine the host `_RESPAWN_BLOCKER_RE` (kanban_db_dispatch), which lumps
# throughput (rate limit / 429) together with account/credential walls
# (quota / auth / billing). §11.1 splits them: throughput → `transient`
# (backoff + retry), account/credential/policy → `authority` (park + escalate,
# never auto-retry). Both refinements are subsets of the host pattern set.
# ---------------------------------------------------------------------------

AUTHORITY_TOKENS: tuple[str, ...] = (
    "quota",
    "auth",
    "unauthorized",
    "forbidden",
    "billing",
    "subscription",
    "access_denied",
    "permission_denied",
    "invalid_api_key",
    "invalid_key",
    "403",
    "credentials",
    "confirm_required",
    "approval_required",
    "policy_denied",
)
TRANSIENT_TOKENS: tuple[str, ...] = (
    "rate_limit",
    "ratelimit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "bad_gateway",
    "service_unavailable",
    "overloaded",
    "temporarily_unavailable",
    "connection_error",
    "connection_reset",
    "connection_refused",
    "network_error",
    "timeout",
)
ENVIRONMENT_TOKENS: tuple[str, ...] = (
    "workspace_missing",
    "workspace_unavailable",
    "store_unreadable",
    "db_locked",
    "database_locked",
    "manifest_corrupt",
    "corrupt_manifest",
    "lease_stale",
    "stale_lease",
    "disk_full",
)
SEMANTIC_TOKENS: tuple[str, ...] = (
    "test_failure",
    "tests_failed",
    "assertion",
    "assertionerror",
    "forbidden_action",
    "artifact_mismatch",
    "missing_artifact",
    "protocol_violation",
    "compile_error",
    "syntax_error",
    "import_error",
    "regression",
)
CAPABILITY_TOKENS: tuple[str, ...] = (
    "no_capable_target",
    "missing_skill",
    "skill_not_found",
    "node_not_enrolled",
    "backend_unsupported",
    "no_backend",
    "no_peer",
    "capability_denied",
)

MAX_REPLAN_ATTEMPTS_DEFAULT = 1  # D8: replan bounded (<=1 by default)

# Envelope bounds.
MAX_ERROR_TEXT = 512  # matched, never stored raw
MAX_REASONS = 16
MAX_OBSERVATION_JSON = 8192
MAX_STRING = 128

# PII-ish tokens stripped from any echoed summary (mirrors operator_placement).
_PII_STRIP = re.compile(
    r"(?i)(sk-[a-zA-Z0-9]{20,}|[A-Za-z0-9._~-]{43,128}@[A-Za-z0-9._-]+|"
    r"Bearer\s+[A-Za-z0-9._~-]{20,}|ghp_[A-Za-z0-9]{20,})"
)


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


def _normalize_tokens(text: Any) -> set[str]:
    """Word and underscore-joined phrase n-grams of an error string (matching only).

    ``quota exceeded`` yields ``quota``, ``exceeded``, ``quota_exceeded`` so
    both single-word vocabulary (``quota``) and multiword vocabulary
    (``rate_limit`` ← "rate limit") match deterministically.
    """
    if not text:
        return set()
    folded = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")
    if not folded:
        return set()
    words = [w for w in folded.split("_") if w]
    grams: set[str] = set(words)
    for n in (2, 3, 4):
        for i in range(0, len(words) - n + 1):
            grams.add("_".join(words[i : i + n]))
    return grams


def _match_tokens(text: Any, vocabulary: tuple[str, ...]) -> list[str]:
    tokens = _normalize_tokens(text)
    return sorted({v for v in vocabulary if v in tokens})


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(exc, layer="operator", code=code, suggested_action=action)
    )


def _audit(
    tool: str,
    policy: op.OperatorPolicy | None,
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
            level=policy.level if policy else "read_only",
            apply_mode=policy.apply_mode if policy else "dry_run",
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id} node={node_id}",
            extra={"mission_id": mission_id, "node_id": node_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


# ---------------------------------------------------------------------------
# Durable store: controller_plan rows (decision output only). Additive,
# IF NOT EXISTS, coexists with the Mission runtime store (§6.2).
# ---------------------------------------------------------------------------


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_tables(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS controller_plan (
            mission_id TEXT NOT NULL,
            node_id TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL,
            classification TEXT NOT NULL,
            failure_class TEXT NOT NULL DEFAULT '',
            row_key TEXT NOT NULL DEFAULT '',
            proposed_action TEXT NOT NULL DEFAULT '',
            would_execute INTEGER NOT NULL DEFAULT 0,
            need_attention INTEGER NOT NULL DEFAULT 0,
            decision_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (mission_id, node_id, decision_sha256)
        );
        CREATE INDEX IF NOT EXISTS idx_controller_plan_mission
            ON controller_plan(mission_id, created_at);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_tables(db)
        return db
    return mission._connect(path, write=False)


def _record_decision(db: sqlite3.Connection, decision: dict[str, Any]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO controller_plan("
        "mission_id,node_id,decision_json,classification,failure_class,row_key,"
        "proposed_action,would_execute,need_attention,decision_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision["mission_id"],
            decision["node_id"],
            _canonical(decision),
            decision["classification"],
            decision.get("failure_class", ""),
            decision.get("row_key", ""),
            _sanitize(decision.get("proposed_action", ""), 200),
            0,
            1 if decision.get("need_attention") else 0,
            decision["decision_sha256"],
            decision.get("generated_at") or _now(),
        ),
    )


# ---------------------------------------------------------------------------
# Observation envelope validation (fail-closed on malformed input)
# ---------------------------------------------------------------------------


class ObservationError(ValueError):
    """Raised when the envelope is structurally unusable (caller error, not a
    classification outcome). Malformed *enum values inside* a structurally
    valid envelope are NOT this — they classify `unknown` (fail-closed)."""


def _validate_envelope(observation: dict[str, Any]) -> dict[str, Any]:
    """Structural validation. Returns the normalized envelope.

    Raises ObservationError for structurally invalid input (wrong types,
    unknown top-level keys, oversized payloads).
    """
    if not isinstance(observation, dict):
        raise ObservationError("observation must be a JSON object")
    allowed = {
        "delegation",
        "runner",
        "worker_exit",
        "last_failure_error",
        "capability",
        "plan",
        "mission",
        "breaker",
    }
    unknown_keys = set(observation) - allowed
    if unknown_keys:
        raise ObservationError(f"unknown observation keys: {sorted(unknown_keys)}")

    env: dict[str, Any] = {}

    delegation = observation.get("delegation")
    if delegation is not None:
        if not isinstance(delegation, dict):
            raise ObservationError("delegation must be an object or null")
        for key in delegation:
            if key not in {"state", "backend_state", "outcome", "validation_verdict"}:
                raise ObservationError(f"unknown delegation key: {key}")
        env["delegation"] = {
            "state": delegation.get("state", ""),
            "backend_state": delegation.get("backend_state", ""),
            "outcome": delegation.get("outcome", ""),
            "validation_verdict": delegation.get("validation_verdict", ""),
        }

    runner = observation.get("runner")
    if runner is not None:
        if not isinstance(runner, dict):
            raise ObservationError("runner must be an object or null")
        for key in runner:
            if key not in {"status", "outcome", "error"}:
                raise ObservationError(f"unknown runner key: {key}")
        env["runner"] = {
            "status": runner.get("status", ""),
            "outcome": runner.get("outcome", ""),
            "error": runner.get("error", ""),
        }

    worker_exit = observation.get("worker_exit")
    if worker_exit is not None:
        if not isinstance(worker_exit, dict):
            raise ObservationError("worker_exit must be an object or null")
        for key in worker_exit:
            if key not in {"kind", "code"}:
                raise ObservationError(f"unknown worker_exit key: {key}")
        env["worker_exit"] = {
            "kind": worker_exit.get("kind", ""),
            "code": worker_exit.get("code"),
        }

    error_text = observation.get("last_failure_error") or ""
    if not isinstance(error_text, str):
        raise ObservationError("last_failure_error must be a string")
    env["last_failure_error"] = error_text[:MAX_ERROR_TEXT]

    capability = observation.get("capability")
    if capability is not None:
        if not isinstance(capability, dict):
            raise ObservationError("capability must be an object or null")
        for key in capability:
            if key not in {"ok", "reasons"}:
                raise ObservationError(f"unknown capability key: {key}")
        reasons = capability.get("reasons") or []
        if not isinstance(reasons, list) or len(reasons) > MAX_REASONS:
            raise ObservationError("capability.reasons must be a list (<=16)")
        env["capability"] = {
            "ok": bool(capability.get("ok", True)),
            "reasons": [_sanitize(r, 64) for r in reasons],
        }

    plan = observation.get("plan") or {}
    if not isinstance(plan, dict):
        raise ObservationError("plan must be an object")
    for key in plan:
        if key not in {
            "node_state",
            "parent_done",
            "all_children_terminal",
            "retries",
            "replan_attempts_used",
        }:
            raise ObservationError(f"unknown plan key: {key}")
    env["plan"] = {
        "node_state": plan.get("node_state", ""),
        "parent_done": bool(plan.get("parent_done", False)),
        "all_children_terminal": bool(plan.get("all_children_terminal", False)),
        "retries": int(plan.get("retries", 0) or 0),
        "replan_attempts_used": int(plan.get("replan_attempts_used", 0) or 0),
    }

    mission_obs = observation.get("mission") or {}
    if not isinstance(mission_obs, dict):
        raise ObservationError("mission must be an object")
    for key in mission_obs:
        if key not in {"status", "final_approval_required"}:
            raise ObservationError(f"unknown mission key: {key}")
    env["mission"] = {
        "status": mission_obs.get("status", ""),
        "final_approval_required": bool(mission_obs.get("final_approval_required", True)),
    }

    breaker = observation.get("breaker") or {}
    if not isinstance(breaker, dict):
        raise ObservationError("breaker must be an object")
    for key in breaker:
        if key not in {"consecutive_failures", "limit", "gave_up"}:
            raise ObservationError(f"unknown breaker key: {key}")
    env["breaker"] = {
        "consecutive_failures": int(breaker.get("consecutive_failures", 0) or 0),
        "limit": int(breaker.get("limit", 3) or 3),
        "gave_up": bool(breaker.get("gave_up", False)),
    }

    return env


def _enums_valid(env: dict[str, Any]) -> tuple[bool, str]:
    """True when every populated enum value is inside the authoritative vocab."""
    delegation = env.get("delegation")
    if delegation:
        if delegation["state"] and delegation["state"] not in DELEGATION_STATES:
            return False, f"delegation.state={delegation['state']!r}"
        if (
            delegation["validation_verdict"]
            and delegation["validation_verdict"] not in VERDICTS
        ):
            return False, "delegation.validation_verdict invalid"
    worker_exit = env.get("worker_exit")
    if worker_exit and worker_exit["kind"] and worker_exit["kind"] not in WORKER_EXIT_KINDS:
        return False, f"worker_exit.kind={worker_exit['kind']!r}"
    node_state = env["plan"]["node_state"]
    if node_state and node_state not in NODE_STATES:
        return False, f"plan.node_state={node_state!r}"
    mission_status = env["mission"]["status"]
    if mission_status and mission_status not in MISSION_STATES:
        return False, f"mission.status={mission_status!r}"
    return True, ""


def _token_evidence(env: dict[str, Any]) -> dict[str, list[str]]:
    """Matched token ids per class (the ONLY thing retained from raw error text)."""
    texts = [env["last_failure_error"]]
    runner = env.get("runner")
    if runner:
        texts.append(runner.get("error") or "")
    blob = "\n".join(t for t in texts if t)
    return {
        "authority": _match_tokens(blob, AUTHORITY_TOKENS),
        "transient": _match_tokens(blob, TRANSIENT_TOKENS),
        "environment": _match_tokens(blob, ENVIRONMENT_TOKENS),
        "semantic": _match_tokens(blob, SEMANTIC_TOKENS),
        "capability": _match_tokens(blob, CAPABILITY_TOKENS),
    }


# ---------------------------------------------------------------------------
# The classifier — pure function, fixed ladder, fail-closed.
# ---------------------------------------------------------------------------


def classify(mission_id: str, node_id: str, env: dict[str, Any]) -> dict[str, Any]:
    """Classify a validated observation envelope.

    Pure: no I/O, no clock inside the hashed core. Deterministic ladder
    (fail-closed first, then never-auto-retry classes, then flavors, then
    progress rows). Returns the decision dict (without generated_at).
    """
    evidence = _token_evidence(env)
    matched: list[str] = []
    for cls in sorted(evidence):
        matched.extend(f"{cls}:{t}" for t in evidence[cls])

    delegation = env.get("delegation")
    runner = env.get("runner")
    worker_exit = env.get("worker_exit")
    capability = env.get("capability")
    plan = env["plan"]
    mission_obs = env["mission"]
    breaker = env["breaker"]

    def decide(
        classification: str,
        *,
        uncertainty: str = "",
        reason: str = "",
        replan: bool = False,
        failure: bool = True,
        row_override: str = "",
    ) -> dict[str, Any]:
        failure_class = classification if failure else ""
        row_key = row_override or _CLASS_TO_ROW[classification]
        if row_override:
            if row_override not in MATRIX:
                raise AssertionError(f"unknown matrix row override: {row_override}")
        row = MATRIX[row_key]
        breaker_open = breaker["gave_up"] or (
            breaker["limit"] > 0 and breaker["consecutive_failures"] >= breaker["limit"]
        )
        # §8.1: the hard retry ceiling overrides any retry-flavored proposal.
        # Only `transient` proposes an auto-retry; every other class already
        # parks/escalates/observes. A *dispatch* progress row is not a retry
        # and is never breaker-gated here.
        if classification == CLASS_TRANSIENT and breaker_open:
            row_key = "breaker_exhausted"
            row = MATRIX[row_key]
            reason = reason or "retry ceiling reached"
        decision: dict[str, Any] = {
            "schema": DECISION_SCHEMA,
            "mission_id": mission_id,
            "node_id": node_id,
            "classification": classification,
            "failure_class": failure_class,
            "row_key": row_key,
            "proposed_action": row["smallest_action"],
            "proposed_tool": row["proposed_tool"],
            "verify": row["verify"],
            "auto_retry": bool(row["auto_retry"]) and not breaker_open,
            "would_execute": False,  # decision output only — hard constant
            "need_attention": classification
            in (CLASS_UNKNOWN, CLASS_AUTHORITY, CLASS_CAPABILITY, CLASS_ENVIRONMENT)
            or row_key in ("breaker_exhausted", "fail_closed_evidence"),
            "matched_tokens": matched,
            "reason": _sanitize(reason, 200),
            "breaker_open": breaker_open,
        }
        if uncertainty:
            decision["classification_uncertainty"] = uncertainty
        if replan:
            decision["replan_proposal"] = {
                "eligible": True,
                "attempts_used": plan["replan_attempts_used"],
                "max_attempts": MAX_REPLAN_ATTEMPTS_DEFAULT,
                "path": "hermes_plan_decompose / hermes_swarm_stage_advance (existing tools; audited)",
                "executed": False,  # D8: proposal only, never executed here
            }
        return decision

    # ---- 0. Malformed enum anywhere → fail-closed unknown -------------------
    ok, bad = _enums_valid(env)
    if not ok:
        return decide(
            CLASS_UNKNOWN,
            uncertainty=f"invalid_observation_enum:{_sanitize(bad, 64)}",
            reason="observation carries a value outside the authoritative vocabulary",
        )

    dl_state = delegation["state"] if delegation else ""
    dl_backend = delegation["backend_state"] if delegation else ""
    dl_outcome = delegation["outcome"] if delegation else ""
    dl_verdict = delegation["validation_verdict"] if delegation else ""
    node_state = plan["node_state"]
    mission_status = mission_obs["status"]

    # ---- 1. Operator terminal intent ---------------------------------------
    if dl_state == "cancelled" or node_state == "cancelled":
        return decide(CLASS_TERMINAL, failure=True, reason="operator cancelled")
    if mission_status in ("paused", "blocked") and not node_state:
        return decide(
            CLASS_TERMINAL,
            failure=True,
            reason=f"mission {mission_status} by operator; terminal, no action",
        )

    # ---- 2. Missing observation fail-closed --------------------------------
    # A subject that claims in-flight progress must be observable through at
    # least one run channel; otherwise the state is un-verifiable → unknown.
    claims_inflight = (
        dl_state in INFLIGHT_DELEGATION_STATES
        or node_state in {"dispatched", "running"}
    )
    has_channel = runner is not None or worker_exit is not None or dl_state != ""
    if claims_inflight and not has_channel:
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:no_run_channel",
            reason="in-flight subject with no runner/worker-exit/delegation observation",
        )
    if (
        worker_exit is not None
        and worker_exit["kind"] == "nonzero_exit"
        and not env["last_failure_error"]
        and not (runner and runner.get("error"))
    ):
        # rc != 0 with zero flavor evidence: no basis to choose semantic vs
        # transient vs environment. Fail closed rather than guess.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:no_failure_flavor",
            reason="nonzero worker exit with no error text to flavor the failure",
        )

    # §11.2 row 2: child running + worker dead (exit un-observable) → the
    # smallest action is reclaim + bounded retry via the host kanban surface.
    if (
        node_state == "running"
        and worker_exit is not None
        and worker_exit["kind"] == "unknown"
    ):
        return decide(
            CLASS_TRANSIENT,
            reason="worker dead with unobservable exit; reclaim + bounded retry is smallest",
            row_override="reclaim_dead_worker",
        )

    # ---- 3. Ambiguous (never auto-redispatch; do not fabricate) -------------
    if (
        dl_state == "reconciling"
        or dl_backend == "ambiguous"
        or dl_outcome == "submission_may_have_succeeded"
    ):
        return decide(
            CLASS_AMBIGUOUS,
            reason="delegation outcome unknown; observe only (§7.4 non-idempotent)",
        )

    # ---- 4. Authority / policy (never auto-retry) ---------------------------
    if evidence["authority"]:
        return decide(
            CLASS_AUTHORITY,
            reason="credential/quota/policy blocker in authoritative error channel",
        )

    # ---- 5. Capability (no capable target; park + escalate) -----------------
    cap_reasons = capability.get("reasons", []) if capability else []
    if (capability is not None and not capability["ok"]) or evidence["capability"]:
        return decide(
            CLASS_CAPABILITY,
            reason="capability negotiation failed or no capable target",
        )

    # ---- 6. Environment ------------------------------------------------------
    if evidence["environment"]:
        return decide(
            CLASS_ENVIRONMENT,
            reason="environment fault (workspace/store/manifest/lease)",
        )

    # ---- 7. Semantic failure (implementation/QA defect) ---------------------
    semantic_evidence = (
        dl_verdict in ("NOT_SATISFIED", "INVALID_CONTRACT")
        or bool(evidence["semantic"])
        or (worker_exit is not None and worker_exit["kind"] == "clean_exit")
    )
    if semantic_evidence:
        replan_ok = plan["replan_attempts_used"] < MAX_REPLAN_ATTEMPTS_DEFAULT
        return decide(
            CLASS_SEMANTIC,
            reason=(
                "validation verdict not satisfied"
                if dl_verdict in ("NOT_SATISFIED", "INVALID_CONTRACT")
                else "worker protocol violation (clean exit while running)"
                if worker_exit is not None and worker_exit["kind"] == "clean_exit"
                else "defect-flavored failure evidence"
            ),
            replan=replan_ok,
        )

    # ---- 8. Transient (backoff + retry, breaker respected) ------------------
    # Only exit kinds + explicit throughput token evidence flavor transient.
    # A bare runner "failed" outcome carries no class basis → fail closed
    # (unknown), never an optimistic transient retry.
    if worker_exit is not None and worker_exit["kind"] in ("rate_limited", "signaled"):
        return decide(
            CLASS_TRANSIENT,
            reason="throughput/network-flavored failure; backoff + bounded retry",
        )
    if evidence["transient"]:
        return decide(
            CLASS_TRANSIENT,
            reason="throughput/network-flavored failure; backoff + bounded retry",
        )
    if runner is not None and runner.get("outcome") == "failed":
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_runner_failure",
            reason="runner failed with no classifiable evidence; refusing to guess a class",
        )

    # ---- 9. Evidence gate (fail-closed before any success row) --------------
    if dl_state == "failed":
        # Delegation failed but nothing flavored it above → unclassifiable.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_delegation_failure",
            reason="delegation failed with no classifiable evidence",
        )
    if node_state == "failed" and dl_state == "":
        # Node failed but no delegation/runner/exit observation flavored it.
        return decide(
            CLASS_UNKNOWN,
            uncertainty="missing_observation:unflavored_node_failure",
            reason="plan node failed with no classifiable evidence",
        )
    if dl_verdict == "INCONCLUSIVE":
        return decide(
            CLASS_UNKNOWN,
            uncertainty="inconclusive_validation",
            reason="validation INCONCLUSIVE is never success; fail closed",
            row_override="fail_closed_evidence",
        )

    # ---- 10. Waiting --------------------------------------------------------
    if mission_status == "awaiting_approval":
        return decide(CLASS_WAITING, failure=False, reason="owner approval pending")
    if node_state and not plan["all_children_terminal"]:
        if node_state == "pending" and not plan["parent_done"]:
            return decide(
                CLASS_WAITING, failure=False, reason="parent dependency not done"
            )
        if node_state in ("dispatched", "running", "awaiting_review", "validated"):
            return decide(
                CLASS_WAITING, failure=False, reason="work in flight; no failure observed"
            )
    if dl_state in ("queued", "running", "reserved"):
        return decide(
            CLASS_WAITING, failure=False, reason="delegation in flight; no failure observed"
        )

    # ---- 11. Progress rows (non-failure; §11.2 R1/R6/R7) --------------------
    if plan["all_children_terminal"]:
        if dl_verdict == "" and dl_state == "succeeded":
            return decide(
                CLASS_UNKNOWN,
                uncertainty="missing_observation:unverified_evidence",
                reason="terminal success without validation verdict (§11.2 fail-closed row)",
                row_override="fail_closed_evidence",
            )
        if mission_obs["final_approval_required"]:
            return decide(
                CLASS_NONE_APPROVAL,
                failure=False,
                reason="all children terminal; owner approval required (controller stops)",
            )
        return decide(
            CLASS_NONE_COMPLETION,
            failure=False,
            reason="all children terminal; verified evidence; no approval flag",
        )
    if node_state in ("pending", "blockable") and plan["parent_done"] and mission_status == "running":
        return decide(
            CLASS_NONE_DISPATCHABLE,
            failure=False,
            reason="ready child with satisfied dependencies; dispatch is the smallest action",
        )

    # ---- 12. Steady state ----------------------------------------------------
    return decide(
        CLASS_NONE,
        failure=False,
        reason="no failure signal and no progress row applies; recheck next trigger",
    )


def finalize(decision: dict[str, Any]) -> dict[str, Any]:
    """Stamp the decision digest over the canonical core (no clock inside).

    ``schema_version`` participates in the digest so the dry-run and recorded
    paths produce the identical ``decision_sha256`` for identical inputs.
    """
    decision["schema_version"] = SCHEMA_VERSION
    core = {k: v for k, v in decision.items() if k != "generated_at"}
    decision["decision_sha256"] = hashlib.sha256(_canonical(core).encode()).hexdigest()
    decision["generated_at"] = _now()
    return decision


# ---------------------------------------------------------------------------
# Public tool surfaces (JSON envelopes; read_only / workspace+direct gated)
# ---------------------------------------------------------------------------


def hermes_failure_classify(
    mission_id: str,
    node_id: str,
    observation_json: str,
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Classify an observation envelope and propose the smallest recovery action.

    Decision output only: ``would_execute`` is always ``False`` and no action
    is taken. Dry-run (default) requires ``read_only`` and records nothing;
    recording the decision to ``controller_plan`` requires ``workspace`` +
    ``direct`` + ``confirm``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        if node_id and not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node_id is invalid")
        if not isinstance(observation_json, str) or len(observation_json) > MAX_OBSERVATION_JSON:
            raise ValueError("observation_json is missing or exceeds the size bound")
        try:
            observation = json.loads(observation_json)
        except json.JSONDecodeError as exc:
            raise ObservationError(f"observation_json is not valid JSON: {exc}") from exc

        env = _validate_envelope(observation)
        decision = finalize(classify(mission_id, node_id or "", env))
        decision["dry_run"] = True
        decision["changed"] = False

        effective_dry = policy.effective_dry_run(dry_run)
        if effective_dry:
            _audit(
                "hermes_failure_classify",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                node_id=node_id,
                extra={
                    "classification": decision["classification"],
                    "row_key": decision["row_key"],
                    "decision_sha256": decision["decision_sha256"],
                },
            )
            return json.dumps(decision, ensure_ascii=False, indent=2)

        # Recording path: workspace + direct + confirm; writes ONLY a
        # controller_plan row. No mission/plan/delegation mutation exists here.
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not confirm:
            raise PermissionError("recording a failure decision requires confirm=true")
        decision["dry_run"] = False
        decision["changed"] = True
        decision["persisted"] = {"mission_id": mission_id, "node_id": node_id or ""}
        decision["decision_sha256"] = hashlib.sha256(
            _canonical({k: v for k, v in decision.items() if k != "generated_at"}).encode()
        ).hexdigest()
        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            mission._get_row(db, mission_id)  # verify the mission exists
            _record_decision(db, decision)
            db.commit()

        _audit(
            "hermes_failure_classify",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            node_id=node_id,
            extra={
                "classification": decision["classification"],
                "row_key": decision["row_key"],
                "decision_sha256": decision["decision_sha256"],
            },
        )
        return json.dumps(decision, ensure_ascii=False, indent=2)
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
        ObservationError,
    ) as exc:
        _audit(
            "hermes_failure_classify",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
            node_id=node_id,
        )
        return _error(
            exc,
            "FAILURE_CLASSIFY_REJECTED",
            "Check mission/node ids, the observation envelope shape, and Operator policy.",
        )


def hermes_failure_taxonomy() -> str:
    """Read-only: the authoritative 8-class taxonomy (§11.1) + unknown bucket."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "classes": {
                name: {
                    "meaning": spec["meaning"],
                    "action": spec["action"],
                    "auto_retry": spec["auto_retry"],
                }
                for name, spec in TAXONOMY.items()
            },
            "non_failure_classifications": {
                CLASS_NONE_DISPATCHABLE: "ready child; dispatch is the smallest action",
                CLASS_NONE_APPROVAL: "all children terminal; approval gate",
                CLASS_NONE_COMPLETION: "all children terminal; verified; completion request",
                CLASS_NONE: "steady state; recheck next trigger",
            },
            "unknown_bucket": {
                "classification": CLASS_UNKNOWN,
                "policy": "fail-closed: blocked + need_attention + classification_uncertainty",
            },
            "replan_bound": {
                "max_attempts": MAX_REPLAN_ATTEMPTS_DEFAULT,
                "only_for_class": CLASS_SEMANTIC,
                "path": "proposal through existing decompose/advance tools (D8)",
            },
        }
        _audit(
            "hermes_failure_taxonomy",
            policy,
            dry_run=True,
            success=True,
            changed=False,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError) as exc:
        return _error(exc, "FAILURE_TAXONOMY_REJECTED", "Operator policy must be enabled.")


def hermes_recovery_matrix(row_key: str = "") -> str:
    """Read-only: the deterministic smallest-first recovery matrix (§11.2)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if row_key:
            if row_key not in MATRIX:
                raise LookupError(f"unknown matrix row: {row_key}")
            rows = {row_key: MATRIX[row_key]}
        else:
            rows = dict(MATRIX)
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "smallest_first": True,
            "every_action_is_a_request": True,
            "would_execute": False,
            "rows": rows,
        }
        _audit(
            "hermes_recovery_matrix",
            policy,
            dry_run=True,
            success=True,
            changed=False,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, LookupError, OSError) as exc:
        return _error(exc, "RECOVERY_MATRIX_REJECTED", "Check the row key and Operator policy.")


def hermes_controller_plan_list(
    mission_id: str,
    limit: int = 50,
    *,
    hermes_root: Path | None = None,
) -> str:
    """Read-only: recorded failure decisions (controller_plan rows)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        if not MISSION_ID_RE.fullmatch(mission_id or ""):
            raise ValueError("mission_id is invalid")
        limit = max(1, min(int(limit), 200))
        path = _db_path(hermes_root)
        rows: list[dict[str, Any]] = []
        if path.is_file():
            with _connect(path, write=False) as db:
                try:
                    cursor = db.execute(
                        "SELECT node_id,classification,failure_class,row_key,proposed_action,"
                        "would_execute,need_attention,decision_sha256,created_at "
                        "FROM controller_plan WHERE mission_id=? ORDER BY created_at DESC LIMIT ?",
                        (mission_id, limit),
                    )
                except sqlite3.Error:
                    cursor = []
                rows = [
                    {
                        "node_id": r["node_id"],
                        "classification": r["classification"],
                        "failure_class": r["failure_class"],
                        "row_key": r["row_key"],
                        "proposed_action": r["proposed_action"],
                        "would_execute": bool(r["would_execute"]),
                        "need_attention": bool(r["need_attention"]),
                        "decision_sha256": r["decision_sha256"],
                        "created_at": r["created_at"],
                    }
                    for r in cursor
                ]
        payload = {
            "success": True,
            "schema_version": SCHEMA_VERSION,
            "mission_id": mission_id,
            "count": len(rows),
            "decisions": rows,
        }
        _audit(
            "hermes_controller_plan_list",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"count": len(rows)},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(exc, "CONTROLLER_PLAN_LIST_REJECTED", "Check the mission id and Operator policy.")
