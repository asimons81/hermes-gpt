"""Coverage test-matrix for semantic failure classification + recovery matrix.

Implements the card's acceptance criteria for t_49bbc143 (proposal §17 item 7 /
D8 / §11):

- full taxonomy coverage: every one of the 8 failure classes is reached and
  produces its §11.1 action semantics;
- the §11.2 recovery-matrix rows are exercised through the classifier and each
  proposed action is the smallest action for its observed state;
- fail-closed on missing observation (in-flight without channel, unflavored
  failures, unverified terminal success, INCONCLUSIVE validation) → unknown +
  blocked + need_attention + classification_uncertainty;
- zero side effects: dry-run records nothing; recording writes ONLY a
  controller_plan row; no mission/plan/delegation surface is touched;
- determinism (same input → identical decision digest) and INV-9 containment
  (raw error text and secret-shaped input never persisted).

Pytest-compatible AND standalone-runnable: the module defines a lightweight
harness (``run()``) so the matrix executes without pytest, which is not
installable in this sandbox (Tirith scan-blocked in -q mode — same caveat the
sibling slice-1 cards recorded).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operator_failure_semantics as fs
import operator_mission_runtime as mission
import operator_policy as op

# ---------------------------------------------------------------------------
# Minimal standalone harness (no pytest in this environment)
# ---------------------------------------------------------------------------

failures: list[str] = []
passed: list[str] = []


def check(cond, label: str) -> None:
    if cond:
        passed.append(label)
    else:
        failures.append(label)
        print(f"  FAIL: {label}")


def eq(actual, expected, label: str) -> None:
    check(actual == expected, f"{label} (got {actual!r}, want {expected!r})")


class Case:
    """A named classification case."""

    def __init__(self, label, observation, classification, *, row_key=None,
                 need_attention=None, uncertainty=None, replan=None, note=""):
        self.label = label
        self.observation = observation
        self.classification = classification
        self.row_key = row_key
        self.need_attention = need_attention
        self.uncertainty = uncertainty
        self.replan = replan
        self.note = note


# ---------------------------------------------------------------------------
# The taxonomy coverage matrix (8 classes + unknown + non-failure rows)
# ---------------------------------------------------------------------------

MATRIX_CASES: list[Case] = [
    # -- transient ---------------------------------------------------------
    Case(
        "transient: rate-limited worker exit",
        {"worker_exit": {"kind": "rate_limited", "code": 75}},
        fs.CLASS_TRANSIENT,
        row_key="retry_transient_backoff",
        note="host KANBAN_RATE_LIMIT_EXIT_CODE shape",
    ),
    Case(
        "transient: throughput tokens in error channel",
        {"runner": {"status": "failed", "outcome": "failed",
                     "error": "HTTP 503 service unavailable"}},
        fs.CLASS_TRANSIENT,
        row_key="retry_transient_backoff",
    ),
    Case(
        "transient: 429 in last_failure_error",
        {"worker_exit": {"kind": "nonzero_exit", "code": 1},
         "last_failure_error": "provider returned 429 rate limit"},
        fs.CLASS_TRANSIENT,
    ),
    Case(
        "transient → breaker exhausted when ceiling reached",
        {"worker_exit": {"kind": "rate_limited", "code": 75},
         "breaker": {"consecutive_failures": 3, "limit": 3, "gave_up": False}},
        fs.CLASS_TRANSIENT,
        row_key="breaker_exhausted",
        note="§8.1 hard ceiling overrides the retry proposal",
    ),
    Case(
        "transient → breaker exhausted on gave_up",
        {"worker_exit": {"kind": "rate_limited", "code": 75},
         "breaker": {"consecutive_failures": 1, "limit": 3, "gave_up": True}},
        fs.CLASS_TRANSIENT,
        row_key="breaker_exhausted",
    ),
    # -- waiting -----------------------------------------------------------
    Case(
        "waiting: parent dependency not done",
        {"plan": {"node_state": "pending", "parent_done": False}},
        fs.CLASS_WAITING,
        row_key="wait_recheck",
    ),
    Case(
        "waiting: delegation queued, no failure",
        {"delegation": {"state": "queued", "backend_state": "queued",
                         "outcome": "", "validation_verdict": ""}},
        fs.CLASS_WAITING,
    ),
    Case(
        "waiting: owner approval pending",
        {"mission": {"status": "awaiting_approval",
                      "final_approval_required": True}},
        fs.CLASS_WAITING,
    ),
    # -- semantic_failure ----------------------------------------------------
    Case(
        "semantic: NOT_SATISFIED validation verdict",
        {"delegation": {"state": "failed", "backend_state": "failed",
                         "outcome": "failed",
                         "validation_verdict": "NOT_SATISFIED"}},
        fs.CLASS_SEMANTIC,
        row_key="escalate_semantic",
        replan=True,
        note="D8 replan proposal allowed while attempts < max",
    ),
    Case(
        "semantic: clean_exit protocol violation",
        {"worker_exit": {"kind": "clean_exit", "code": 0}},
        fs.CLASS_SEMANTIC,
        replan=True,
        note="rc=0 while running = protocol violation (host vocabulary)",
    ),
    Case(
        "semantic: test-failure tokens",
        {"worker_exit": {"kind": "nonzero_exit", "code": 1},
         "last_failure_error": "2 tests failed, AssertionError in test_foo"},
        fs.CLASS_SEMANTIC,
    ),
    Case(
        "semantic: INVALID_CONTRACT verdict",
        {"delegation": {"state": "failed", "backend_state": "failed",
                         "outcome": "failed",
                         "validation_verdict": "INVALID_CONTRACT"}},
        fs.CLASS_SEMANTIC,
    ),
    Case(
        "semantic: replan bound exhausted → no replan proposal",
        {"delegation": {"state": "failed", "backend_state": "failed",
                         "outcome": "failed",
                         "validation_verdict": "NOT_SATISFIED"},
         "plan": {"replan_attempts_used": 1}},
        fs.CLASS_SEMANTIC,
        replan=False,
        note="D8: max 1 replan attempt by default",
    ),
    # -- authority ----------------------------------------------------------
    Case(
        "authority: quota/credential wall",
        {"worker_exit": {"kind": "nonzero_exit", "code": 1},
         "last_failure_error": "No usable credentials for provider; goodbye"},
    # expected below — authority
        fs.CLASS_AUTHORITY,
        row_key="park_authority",
    ),
    Case(
        "authority: 403 forbidden",
        {"worker_exit": {"nonzero": 0},  # placeholder replaced below
         },
        fs.CLASS_AUTHORITY,
    ),
    # -- capability ---------------------------------------------------------
    Case(
        "capability: negotiation not ok",
        {"capability": {"ok": False,
                         "reasons": ["profile_out_of_scope"]}},
        fs.CLASS_CAPABILITY,
        row_key="park_capability",
    ),
    # -- environment --------------------------------------------------------
    Case(
        "environment: stale lease token",
        {"last_failure_error": "reclaim skipped: stale lease detected"},
        fs.CLASS_ENVIRONMENT,
        row_key="recover_environment",
    ),
    # -- ambiguous ----------------------------------------------------------
    Case(
        "ambiguous: delegation reconciling",
        {"delegation": {"state": "reconciling", "backend_state": "ambiguous",
                         "outcome": "", "validation_verdict": ""}},
        fs.CLASS_AMBIGUOUS,
        row_key="observe_reconciling",
        note="never auto-redispatched (§7.4)",
    ),
    Case(
        "ambiguous: submission may have succeeded",
        {"delegation": {"state": "reconciling",
                         "backend_state": "accepted",
                         "outcome": "submission_may_have_succeeded",
                         "validation_verdict": ""}},
        fs.CLASS_AMBIGUOUS,
    ),
    # -- cancelled/terminal ---------------------------------------------------
    Case(
        "terminal: delegation cancelled",
        {"delegation": {"state": "cancelled", "backend_state": "cancelled",
                         "outcome": "cancelled", "validation_verdict": ""}},
        fs.CLASS_TERMINAL,
        row_key="terminal_no_action",
    ),
    Case(
        "terminal: mission paused by operator",
        {"mission": {"status": "paused", "final_approval_required": True}},
        fs.CLASS_TERMINAL,
    ),
    Case(
        "terminal: mission blocked by operator",
        {"mission": {"status": "blocked", "final_approval_required": True}},
        fs.CLASS_TERMINAL,
    ),
    # -- unknown / fail-closed -------------------------------------------------
    Case(
        "unknown: in-flight node without observation channel",
        {"plan": {"node_state": "running", "parent_done": False}},
        fs.CLASS_UNKNOWN,
        row_key="unknown_fail_closed",
        uncertainty="missing_observation:no_run_channel",
        need_attention=True,
    ),
    Case(
        "unknown: nonzero exit with no flavor",
        {"worker_exit": {"kind": "nonzero_exit", "code": 1}},
        fs.CLASS_UNKNOWN,
        uncertainty="missing_observation:no_failure_flavor",
        need_attention=True,
    ),
    Case(
        "unknown: bare runner failed outcome",
        {"runner": {"status": "failed", "outcome": "failed", "error": ""}},
        fs.CLASS_UNKNOWN,
        uncertainty="missing_observation:unflavored_runner_failure",
        need_attention=True,
    ),
    Case(
        "unknown: delegation failed unflavored",
        {"delegation": {"state": "failed", "backend_state": "failed",
                         "outcome": "failed", "validation_verdict": ""}},
        fs.CLASS_UNKNOWN,
        uncertainty="missing_observation:unflavored_delegation_failure",
        need_attention=True,
    ),
    Case(
        "unknown: node failed unflavored",
        {"plan": {"node_state": "failed", "parent_done": True}},
        fs.CLASS_UNKNOWN,
        uncertainty="missing_observation:unflavored_node_failure",
        need_attention=True,
    ),
    Case(
        "unknown: terminal success without validation",
        {"delegation": {"state": "succeeded", "backend_state": "succeeded",
                         "outcome": "succeeded", "validation_verdict": ""},
         "plan": {"all_children_terminal": True}},
        fs.CLASS_UNKNOWN,
        uncertainty="missing_observation:unverified_evidence",
        need_attention=True,
        note="§11.2 fail-closed evidence row",
    ),
    Case(
        "unknown: INCONCLUSIVE validation",
        {"delegation": {"state": "succeeded", "backend_state": "succeeded",
                         "outcome": "succeeded",
                         "validation_verdict": "INCONCLUSIVE"},
         "plan": {"all_children_terminal": True}},
        fs.CLASS_UNKNOWN,
        uncertainty="inconclusive_validation",
        need_attention=True,
    ),
    # -- non-failure progress rows ----------------------------------------------
    Case(
        "none_dispatchable: ready child, parent done",
        {"plan": {"node_state": "pending", "parent_done": True},
         "mission": {"status": "running", "final_approval_required": True}},
        fs.CLASS_NONE_DISPATCHABLE,
        row_key="dispatch_ready_child",
    ),
    Case(
        "none_awaiting_approval: all terminal + approval flag",
        {"delegation": {"state": "succeeded", "backend_state": "succeeded",
                         "outcome": "succeeded",
                         "validation_verdict": "SATISFIED"},
         "plan": {"all_children_terminal": True, "node_state": ""},
         "mission": {"status": "running", "final_approval_required": True}},
        fs.CLASS_NONE_APPROVAL,
        row_key="signal_awaiting_approval",
    ),
    Case(
        "none_completion: all terminal, verified, no approval",
        {"delegation": {"state": "succeeded", "backend_state": "succeeded",
                         "outcome": "succeeded",
                         "validation_verdict": "SATISFIED"},
         "plan": {"all_children_terminal": True, "node_state": ""},
         "mission": {"status": "running", "final_approval_required": False}},
        fs.CLASS_NONE_COMPLETION,
        row_key="request_completion",
    ),
    Case(
        "none: steady state",
        {"mission": {"status": "draft", "final_approval_required": True}},
        fs.CLASS_NONE,
        row_key="wait_recheck",
    ),
]

# Fix the two placeholder cases (kept literal above for readability).
for _c in MATRIX_CASES:
    if _c.label.startswith("authority: 403"):
        _c.observation = {
            "worker_exit": {"kind": "nonzero_exit", "code": 1},
            "last_failure_error": "HTTP 403 Forbidden: permission denied",
        }
    if _c.label.startswith("authority: quota"):
        _c.observation["worker_exit"] = {"kind": "nonzero_exit", "code": 1}


def run_taxonomy_matrix() -> None:
    print("== taxonomy coverage matrix ==")
    for case in MATRIX_CASES:
        env = fs._validate_envelope(case.observation)
        decision = fs.classify("msn-matrix", "node-x", env)
        label = case.label
        eq(decision["classification"], case.classification, f"{label}: class")
        if case.row_key is not None:
            eq(decision["row_key"], case.row_key, f"{label}: row")
        if case.need_attention is not None:
            eq(decision["need_attention"], case.need_attention,
               f"{label}: need_attention")
        if case.uncertainty is not None:
            eq(decision.get("classification_uncertainty"), case.uncertainty,
               f"{label}: uncertainty")
        if case.replan is not None:
            eq(bool(decision.get("replan_proposal")), case.replan,
               f"{label}: replan proposal present")
        eq(decision["would_execute"], False, f"{label}: would_execute False")


def run_class_action_semantics() -> None:
    print("== class → action semantics (§11.1) ==")
    for name, spec in fs.TAXONOMY.items():
        check(name in fs._CLASS_TO_ROW, f"{name} mapped to a matrix row")
        row = fs.MATRIX[fs._CLASS_TO_ROW[name]]
        if name == fs.CLASS_TRANSIENT:
            check(row["auto_retry"] is True, "transient row auto_retry True")
        else:
            check(row["auto_retry"] is False, f"{name} row auto_retry False")
    # Park/escalate classes must raise need_attention on their decisions.
    probes = {
        fs.CLASS_AUTHORITY: {"last_failure_error": "quota exceeded"},
        fs.CLASS_CAPABILITY: {"capability": {"ok": False, "reasons": ["x"]}},
        fs.CLASS_ENVIRONMENT: {"last_failure_error": "stale lease detected"},
        fs.CLASS_UNKNOWN: {"plan": {"node_state": "running"}},
    }
    for cls, obs in probes.items():
        d = fs.classify("m", "n", fs._validate_envelope(obs))
        eq(d["classification"], cls, f"probe {cls}: class")
        eq(d["need_attention"], True, f"probe {cls}: need_attention")
    # Every mapped row key exists in MATRIX and the mapping is total over the
    # taxonomy classes + progress classes.
    for cls, row_key in fs._CLASS_TO_ROW.items():
        check(row_key in fs.MATRIX, f"{cls} row_key {row_key} exists in MATRIX")
    # Rows reached via ladder overrides (not the static class map) must be
    # exercised through real classification probes — full reachability proof.
    override_probes = {
        "reclaim_dead_worker": fs._validate_envelope(
            {"plan": {"node_state": "running"},
             "worker_exit": {"kind": "unknown", "code": None}}),
        "breaker_exhausted": fs._validate_envelope(
            {"worker_exit": {"kind": "rate_limited", "code": 75},
             "breaker": {"consecutive_failures": 3, "limit": 3, "gave_up": False}}),
        "fail_closed_evidence": fs._validate_envelope(
            {"delegation": {"state": "succeeded", "backend_state": "succeeded",
                            "outcome": "succeeded", "validation_verdict": ""},
             "plan": {"all_children_terminal": True}}),
    }
    reached = {rk for rk in fs._CLASS_TO_ROW.values()}
    for expected_row, probe_env in override_probes.items():
        d = fs.classify("m", "n", probe_env)
        eq(d["row_key"], expected_row, f"override probe reaches {expected_row}")
        reached.add(d["row_key"])
    for row_key in fs.MATRIX:
        check(row_key in reached,
              f"matrix row {row_key} reachable from a class")


def run_determinism() -> None:
    print("== determinism ==")
    obs = {
        "delegation": {"state": "failed", "backend_state": "failed",
                        "outcome": "failed",
                        "validation_verdict": "NOT_SATISFIED"},
    }
    d1 = fs.finalize(fs.classify("msn-det", "node-a", fs._validate_envelope(obs)))
    d2 = fs.finalize(fs.classify("msn-det", "node-a", fs._validate_envelope(obs)))
    eq(d1["decision_sha256"], d2["decision_sha256"], "same input → same digest")
    # Key-order independence of the observation JSON.
    o2 = {k: obs[k] for k in reversed(list(obs))}
    d3 = fs.finalize(fs.classify("msn-det", "node-a", fs._validate_envelope(o2)))
    eq(d3["decision_sha256"], d1["decision_sha256"], "digest independent of key order")
    # A semantically different observation changes the digest (compare within
    # the same class so the no-flavor fail-closed gate cannot mask the delta).
    obs2 = {
        "delegation": {"state": "failed", "backend_state": "failed",
                        "outcome": "failed",
                        "validation_verdict": "NOT_SATISFIED"},
        "last_failure_error": "assertion error in test_foo",
    }
    d4 = fs.finalize(fs.classify("msn-det", "node-a", fs._validate_envelope(obs2)))
    eq(d4["classification"], fs.CLASS_SEMANTIC, "delta case stays semantic")
    check(d4["decision_sha256"] != d1["decision_sha256"], "different input → different digest")


def run_structural_rejects() -> None:
    print("== structural validation (caller errors) ==")
    raised = False
    try:
        fs._validate_envelope({"delegation": "queued"})
    except fs.ObservationError:
        raised = True
    check(raised, "non-dict delegation rejected")
    raised = False
    try:
        fs._validate_envelope({"unknown_key": 1})
    except fs.ObservationError:
        raised = True
    check(raised, "unknown top-level key rejected")
    # Malformed enum inside a valid structure → unknown (not a caller error).
    d = fs.classify("m", "n", fs._validate_envelope(
        {"delegation": {"state": "exploded", "backend_state": "", "outcome": "",
                         "validation_verdict": ""}}))
    eq(d["classification"], fs.CLASS_UNKNOWN, "invalid enum → unknown fail-closed")


def run_public_surfaces(tmp_root: Path) -> None:
    print("== public surfaces: policy gates + zero side effects ==")
    # Fresh mission store for the recording test.
    _make_mission(tmp_root)
    obs = json.dumps({
        "delegation": {"state": "failed", "backend_state": "failed",
                        "outcome": "failed",
                        "validation_verdict": "NOT_SATISFIED"},
    })
    # 1. Dry-run (default): read_only level, records nothing.
    out = json.loads(fs.hermes_failure_classify(
        "msn-fs", "node-a", obs, dry_run=True, hermes_root=tmp_root))
    check(out["success"] if "success" in out else True, "classify returns a decision")
    eq(out.get("classification"), fs.CLASS_SEMANTIC, "surface class semantic")
    eq(out.get("would_execute"), False, "surface would_execute False")
    rows = _controller_plan_rows(tmp_root)
    eq(len(rows), 0, "dry-run records nothing")
    # No other store was touched: no new missions; delegations store untouched.
    with mission._connect(mission._db_path(tmp_root), write=False) as db:
        n_missions = db.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
        has_delegations_table = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='delegations'"
        ).fetchone()[0]
    eq(n_missions, 1, "no extra missions created")
    eq(has_delegations_table, 0, "no delegations store created by this module")

    # 2. Recording requires workspace + direct + confirm.
    out2 = json.loads(fs.hermes_failure_classify(
        "msn-fs", "node-a", obs, confirm=False, dry_run=False, hermes_root=tmp_root))
    check(out2.get("success") is not True or out2.get("code") == "FAILURE_CLASSIFY_REJECTED"
          or "error" in out2, "record without confirm refused")
    rows = _controller_plan_rows(tmp_root)
    eq(len(rows), 0, "refused record wrote nothing")

    out3 = json.loads(fs.hermes_failure_classify(
        "msn-fs", "node-a", obs, confirm=True, dry_run=False, hermes_root=tmp_root))
    eq(out3.get("classification"), fs.CLASS_SEMANTIC, "recorded class semantic")
    eq(out3.get("persisted", {}).get("node_id"), "node-a", "persisted node id")
    rows = _controller_plan_rows(tmp_root)
    eq(len(rows), 1, "record wrote exactly one controller_plan row")
    eq(rows[0]["would_execute"], 0, "stored would_execute=0")
    eq(rows[0]["need_attention"], 0, "semantic row need_attention=0")
    eq(rows[0]["failure_class"], fs.CLASS_SEMANTIC, "stored failure_class")

    # 3. INV-9: raw error text + secret shapes never persisted.
    secret_obs = json.dumps({
        "worker_exit": {"kind": "nonzero_exit", "code": 1},
        "last_failure_error": "provider key sk-abcdefghijklmnopqrstuvwxyz123456 failed quota",
    })
    out4 = json.loads(fs.hermes_failure_classify(
        "msn-fs", "node-b", secret_obs, confirm=True, dry_run=False, hermes_root=tmp_root))
    eq(out4["classification"], fs.CLASS_AUTHORITY, "secret-shaped input still classified")
    with mission._connect(mission._db_path(tmp_root), write=False) as db:
        blob = " ".join(
            str(r[0]) for r in db.execute(
                "SELECT decision_json FROM controller_plan").fetchall())
    check("sk-abc" not in blob, "no raw secret persisted")
    check("provider key" not in blob, "no raw error text persisted")
    check("authority:quota" in out4["matched_tokens"], "matched token id retained")

    # 4. Read surfaces.
    tax = json.loads(fs.hermes_failure_taxonomy())
    eq(tax["success"], True, "taxonomy surface ok")
    eq(sorted(tax["classes"]), sorted(fs.FAILURE_CLASSES), "taxonomy lists 8 classes")
    eq(tax["replan_bound"]["max_attempts"], 1, "replan bound default 1")
    mat = json.loads(fs.hermes_recovery_matrix())
    eq(mat["success"], True, "matrix surface ok")
    eq(mat["would_execute"], False, "matrix would_execute False")
    eq(len(mat["rows"]), len(fs.MATRIX), "matrix surface lists all rows")
    mat1 = json.loads(fs.hermes_recovery_matrix("park_authority"))
    eq(list(mat1["rows"]), ["park_authority"], "matrix single-row fetch")
    plan = json.loads(fs.hermes_controller_plan_list("msn-fs", hermes_root=tmp_root))
    eq(plan["success"], True, "plan list surface ok")
    eq(plan["count"], 2, "plan list sees both recorded decisions")

    # 5. Policy disabled → refused.
    import os
    saved = os.environ.get(op.OPERATOR_ENABLED_ENV)
    os.environ.pop(op.OPERATOR_ENABLED_ENV, None)
    try:
        out5 = json.loads(fs.hermes_failure_classify(
            "msn-fs", "node-a", obs, hermes_root=tmp_root))
        check(out5.get("success") is not True, "operator disabled → refused")
    finally:
        if saved is not None:
            os.environ[op.OPERATOR_ENABLED_ENV] = saved


def run_inv9_envelope() -> None:
    print("== INV-9: token vocabularies and redaction ==")
    env = fs._validate_envelope({
        "last_failure_error": "quota exceeded for sk-abcdefghijklmnopqrst",
    })
    ev = fs._token_evidence(env)
    eq(ev["authority"], ["quota"], "quota matched to authority")
    blob = json.dumps(fs.classify("m", "n", env))
    check("sk-abc" not in blob, "secret redacted from decision blob")
    env2 = fs._validate_envelope({
        "last_failure_error": "rate limit exceeded (HTTP 429)",
    })
    eq(fs._token_evidence(env2)["transient"], ["429", "rate_limit"],
       "429 + rate_limit matched to transient")
    env3 = fs._validate_envelope({
        "last_failure_error": "Permission denied on path /vault/secrets",
    })
    eq(fs._token_evidence(env3)["authority"], ["permission_denied"],
       "permission_denied matched to authority")


def _make_mission(root: Path) -> None:
    spec = json.dumps({
        "schema": mission.MISSION_SPEC_SCHEMA,
        "mission_id": "msn-fs",
        "title": "Failure-semantics sandbox",
        "objective": "Exercise the failure classification matrix.",
        "owner_profile": "default",
        "acceptance_criteria": ["matrix green"],
        "context_refs": [],
        "skills": [],
        "final_approval_required": True,
    })
    out = json.loads(mission.hermes_mission_create(
        spec, confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _controller_plan_rows(root: Path) -> list:
    path = mission._db_path(root)
    if not path.is_file():
        return []
    with mission._connect(path, write=False) as db:
        try:
            return db.execute(
                "SELECT * FROM controller_plan ORDER BY created_at").fetchall()
        except sqlite3.Error:
            return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> int:
    run_taxonomy_matrix()
    run_class_action_semantics()
    run_determinism()
    run_structural_rejects()
    run_inv9_envelope()
    with tempfile.TemporaryDirectory(prefix="fs-matrix-") as td:
        root = Path(td) / "hermes"
        root.mkdir()
        op.set_audit_log_override(Path(td) / "audit.jsonl")
        os_env = __import__("os").environ
        saved = {k: os_env.get(k) for k in (
            op.OPERATOR_ENABLED_ENV, op.OPERATOR_LEVEL_ENV,
            op.OPERATOR_APPLY_MODE_ENV)}
        os_env[op.OPERATOR_ENABLED_ENV] = "1"
        os_env[op.OPERATOR_LEVEL_ENV] = "workspace"
        os_env[op.OPERATOR_APPLY_MODE_ENV] = "direct"
        try:
            run_public_surfaces(root)
        finally:
            for k, v in saved.items():
                if v is None:
                    os_env.pop(k, None)
                else:
                    os_env[k] = v
            op.set_audit_log_override(None)
    print(f"\n{len(passed)} passed, {len(failures)} failed")
    if failures:
        print("FAILED:", *failures, sep="\n  - ")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())


# ---------------------------------------------------------------------------
# Pytest entry (the standalone harness above remains the primary runner; this
# makes the whole matrix collectible when pytest is available).
# ---------------------------------------------------------------------------


def test_taxonomy_coverage_matrix() -> None:
    """Full matrix under pytest: 8 classes + unknown + progress rows."""
    assert run() == 0, "failure-semantics coverage matrix reported failures"
