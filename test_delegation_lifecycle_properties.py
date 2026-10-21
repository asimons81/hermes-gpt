"""TEST-01 property extension — delegation idempotency & resume (tier 3).

Hypothesis properties over the delegation transport lifecycle (the EXEC-03
crash-window surface at the spool seam):

- **P5 dispatch idempotence**: re-dispatching the same contract is
  byte-stable on the wire (same pending envelope content) and, once a
  terminal result exists, folds instead of re-queueing — at most ONE
  side-effecting execution per attempt id.
- **P6 observation totality**: observed_runs is total over envelope states
  (absent -> [], pending -> running, malformed/unrelated results -> []) and
  never fabricates a status from a non-terminal or foreign envelope.
- **P7 binding stability**: the session binding hermes-gpt derives from
  contract options is a pure function (stable across dispatches) — the
  identity Hermes Agent reattaches to after a crash.

Isolation: each generated example gets a FRESH spool root + workspace under
its own mkdtemp (no function-scoped pytest fixtures — hypothesis does not
reset them between inputs; the per-example context manager is the reset).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hypothesis import given, settings, strategies as st  # noqa: E402

import operator_hermes_agent_runner as hab  # noqa: E402

ATTEMPT_IDS = st.from_regex(r"[a-z][a-z0-9]{7,15}", fullmatch=True).map(
    lambda s: f"attempt{s}"
)
DELEGATES = st.sampled_from(["pi", "opencode"])
ROLES = st.sampled_from(["primary", "review", "repair"])


@contextmanager
def _spool_env():
    """Fresh isolated spool root + workspace for exactly one example."""
    base = Path(tempfile.mkdtemp(prefix="nrm-prop-"))
    try:
        root = base / "spool"
        (root / "pending").mkdir(parents=True)
        (root / "results").mkdir(parents=True)
        workspace = base / "workspace"
        workspace.mkdir()
        with patch.dict(os.environ, {hab.SPOOL_ROOT_ENV: str(root)}):
            yield hab.HermesAgentBackend(), root, workspace, base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _contract(workspace: Path, task_id, delegate, resume, run_id, role, context=""):
    options = {
        "sandbox": "workspace-write",
        "delegate": delegate,
        "resume": resume,
        "run_id": run_id,
        "role": role,
    }
    if context:
        options["context"] = context
    return {
        "task_id": task_id,
        "objective": "Property-tier delegation lifecycle probe.",
        "execution": {"backend": "hermes_agent", "options": options},
        "allowed_scope": {"workspaces": [str(workspace)]},
    }


# -- P5: dispatch idempotence ------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    task_id=ATTEMPT_IDS,
    delegate=DELEGATES,
    resume=st.booleans(),
    run_id=st.text(alphabet="run-", min_size=4, max_size=12),
    role=ROLES,
    context=st.text(alphabet="ctx ", min_size=0, max_size=20),
)
def test_p5_redispatch_is_byte_identical_until_terminal(
    task_id, delegate, resume, run_id, role, context
):
    with _spool_env() as (be, root, workspace, _base):
        contract = _contract(workspace, task_id, delegate, resume, run_id, role, context)

        out1 = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        first = (root / "pending" / f"{task_id}.json").read_bytes()
        mtime1 = (root / "pending" / f"{task_id}.json").stat().st_mtime_ns

        out2 = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        second = (root / "pending" / f"{task_id}.json").read_bytes()

        assert out1["state"] == "queued" and out2["state"] == "queued"
        # Same attempt, same order: the wire bytes never change (first-publish
        # authority — mirrors conductor's send() idempotence).
        assert first == second
        # And the envelope was not even rewritten (mtime preserved) in this window.
        assert (root / "pending" / f"{task_id}.json").stat().st_mtime_ns == mtime1
        # Exactly ONE envelope exists for the attempt — no sibling, no dupe.
        assert len(list((root / "pending").glob(f"{task_id}.json"))) == 1

        # Terminal result present: the next dispatch folds, never re-queues.
        (root / "results" / f"{task_id}.json").write_text(
            json.dumps({"attempt_id": task_id, "status": "succeeded", "backend": delegate})
        )
        out3 = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        assert out3["changed"] is False
        assert out3["state"] == "completed"
        assert (root / "pending" / f"{task_id}.json").read_bytes() == first


@settings(max_examples=50, deadline=None)
@given(task_id=ATTEMPT_IDS, delegate=DELEGATES, detail=st.text(max_size=40))
def test_p5_terminal_fold_never_reexecutes(task_id, delegate, detail):
    with _spool_env() as (be, root, workspace, _base):
        contract = _contract(workspace, task_id, delegate, False, f"run-{task_id}", "primary")
        be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        (root / "results" / f"{task_id}.json").write_text(
            json.dumps(
                {"attempt_id": task_id, "status": "failed", "backend": delegate, "detail": detail}
            )
        )
        for _ in range(3):
            out = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
            assert out["changed"] is False
            assert out["state"] == "failed"
        # pending envelope untouched by any fold — exactly one, original bytes.
        assert len(list((root / "pending").glob(f"{task_id}.json"))) == 1


# -- P6: observation totality -------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(task_id=ATTEMPT_IDS, delegate=DELEGATES)
def test_p6_observation_partition_and_fail_closed(task_id, delegate):
    with _spool_env() as (be, root, workspace, _base):
        # Absent everything -> no knowledge.
        assert be.observed_runs(task_id) == []

        # Foreign attempt-id inside the result envelope -> NEVER folds (fail
        # closed on identity mismatch).
        (root / "results" / f"{task_id}.json").write_text(
            json.dumps({"attempt_id": "somebodyelse", "status": "succeeded"})
        )
        assert be.observed_runs(task_id) == []

        # Non-terminal statuses -> no observation (never fabricate completion).
        for status in ("running", "queued", "cancelled", "", "SUCCEEDED"):
            (root / "results" / f"{task_id}.json").write_text(
                json.dumps({"attempt_id": task_id, "status": status})
            )
            assert be.observed_runs(task_id) == []

        # Malformed JSON -> no observation, no crash.
        (root / "results" / f"{task_id}.json").write_text("{not json")
        assert be.observed_runs(task_id) == []

        # Corrupt terminal envelope + re-dispatch -> fail CLOSED: the
        # transport is untrustworthy for this attempt; dispatch refuses to
        # fold or re-queue rather than crash (AttributeError) or blindly
        # double-execute (the defect this property pinned).
        with pytest.raises(RuntimeError, match="non-conforming"):
            be.dispatch(
                _contract(workspace, task_id, delegate, False, task_id, "primary"),
                confirm=True, dry_run=False, timeout=900,
            )

        # Clean slate: pending envelope alone -> running (dispatch known,
        # outcome unknown). A DIFFERENT attempt exercises the healthy path.
        tid2 = task_id + "x"
        be.dispatch(_contract(workspace, tid2, delegate, False, tid2, "primary"),
                    confirm=True, dry_run=False, timeout=900)
        rows = be.observed_runs(tid2)
        assert len(rows) == 1 and rows[0]["status"] == "running"


# -- P7: binding stability (resume identity) ----------------------------------


@settings(max_examples=50, deadline=None)
@given(
    task_id=ATTEMPT_IDS,
    delegate=DELEGATES,
    resume=st.booleans(),
    run_id=st.text(alphabet="run-", min_size=4, max_size=12),
    role=ROLES,
)
def test_p7_session_binding_pure_function_of_contract(task_id, delegate, resume, run_id, role):
    with _spool_env() as (be, root, workspace, _base):
        contract = _contract(workspace, task_id, delegate, resume, run_id, role)
        be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        binding1 = json.loads(
            (root / "pending" / f"{task_id}.json").read_text()
        )["session_binding"]

    with _spool_env() as (be2, root2, workspace2, _base2):
        # A fresh backend + fresh spool (simulated restart) derives the
        # identical binding from the same contract — the resume path
        # reattaches to the same identity.
        be2.dispatch(_contract(workspace2, task_id, delegate, resume, run_id, role),
                     confirm=True, dry_run=False, timeout=900)
        binding2 = json.loads(
            (root2 / "pending" / f"{task_id}.json").read_text()
        )["session_binding"]

    assert binding1 == binding2
    assert binding1["backend"] == delegate
    assert binding1["resume"] is resume
    assert binding1["run_id"] == run_id
    assert binding1["role"] == role
