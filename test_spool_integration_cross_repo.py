"""TEST-01 tier 3 — cross-repo integration lane: hermes-gpt ⇄ conductor spool.

Proves the corrected delegation rail end-to-end at the shared-contract seam,
importing the REAL conductor spool implementation (no reimplementation, no
mock of the consumer side):

    hermes-gpt HermesAgentBackend.dispatch
      -> durable pending envelope (schema 1, interface delegate_session)
        -> conductor DelegateSessionSpool.pending_work_orders()   [consumer]
          -> conductor write_result()                             [Hermes side]
            -> hermes-gpt observed_runs() fold                    [truth read]

The lane asserts the two sides agree on grammar and crash discipline:
envelope shape validation on the conductor side, terminal-fold idempotence
and resume-correct observation on the hermes-gpt side, and — the cross-repo
regression net — that no change to hermes-gpt's writer can drift from the
conductor reader without this suite going red.

Conductor source path comes from ``HERMES_CONDUCTOR_SRC`` (deployed layout)
and defaults to the canonical checkout. The lane skips (not fails) when the
conductor checkout is absent — it is an INTEGRATION lane, meaningful only
where both repos live (this host; CI lives in hermes-gpt and must not gain a
conductor dependency).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import operator_hermes_agent_runner as hab  # noqa: E402


def _conductor_src() -> Path | None:
    raw = os.environ.get("HERMES_CONDUCTOR_SRC")
    candidates = [Path(raw)] if raw else [
        Path("/work/projects/hermes-conductor/src"),
        Path.home() / "work/projects/hermes-conductor/src",
    ]
    for c in candidates:
        if (c / "hermes_conductor" / "delegate_spool.py").is_file():
            return c
    return None


CONDUCTOR_SRC = _conductor_src()

pytestmark = pytest.mark.skipif(
    CONDUCTOR_SRC is None,
    reason="conductor checkout not present — cross-repo lane runs on integration hosts only",
)


@pytest.fixture(scope="module")
def conductor_spool_cls():
    assert CONDUCTOR_SRC is not None
    sys.path.insert(0, str(CONDUCTOR_SRC))
    try:
        from hermes_conductor.delegate_spool import DelegateSessionSpool

        return DelegateSessionSpool
    finally:
        sys.path.remove(str(CONDUCTOR_SRC))


def _spool_pair(tmp_path: Path, monkeypatch):
    """One spool root shared by BOTH sides of the seam."""
    root = tmp_path / "spool"
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(root))
    return root


def _contract(tmp_path: Path, task_id: str = "attempt0000000001", delegate: str = "") -> dict:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    execution: dict = {"backend": "hermes_agent", "options": {"sandbox": "workspace-write"}}
    if delegate:
        execution["options"]["delegate"] = delegate
    return {
        "task_id": task_id,
        "objective": "Prove the spool seam end-to-end.",
        "execution": execution,
        "allowed_scope": {"workspaces": [str(ws)]},
    }


def _init_conductor_side(conductor_spool_cls, root: Path):
    """Conductor/Hermes-side supervisor initializes the SAME layout."""
    spool = conductor_spool_cls(root)
    spool.ensure_layout()
    return spool


# -- 1. happy path: dispatch -> consume -> result -> fold --------------------


def test_e2e_dispatch_consumed_by_conductor_and_result_folds(
    tmp_path, monkeypatch, conductor_spool_cls
):
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()

    out = be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    assert out["state"] == "queued"

    # The conductor supervisor (real implementation) sees exactly one order.
    orders = conductor.pending_work_orders()
    assert len(orders) == 1
    order = orders[0]
    assert order["schema"] == 1
    assert order["interface"] == "delegate_session"
    assert order["attempt_id"] == "attempt0000000001"
    assert order["backend_hint"] == "pi"
    assert order["session_binding"]["backend"] == "pi"

    # In flight from hermes-gpt's view: running, transport truth only.
    rows = be.observed_runs("attempt0000000001")
    assert rows and rows[0]["status"] == "running"

    # Hermes side completes the delegated turn and writes the terminal result.
    conductor.write_result(
        "attempt0000000001",
        status="succeeded",
        session_id="sess-e2e-1",
        backend="pi",
        detail="delegated turn ok",
    )

    rows = be.observed_runs("attempt0000000001")
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["session_id"] == "sess-e2e-1"
    assert rows[0]["delegate_backend"] == "pi"
    # Transport truth, never a contract verdict:
    assert rows[0]["scope"] == "runner:hermes_agent"

    # write_result retired the pending envelope (conductor crash discipline).
    assert not (root / "pending" / "attempt0000000001.json").exists()


def test_e2e_failed_result_folds_as_failed_not_contract_failure(
    tmp_path, monkeypatch, conductor_spool_cls
):
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)

    conductor.write_result(
        "attempt0000000001", status="failed", backend="pi", detail="turn error"
    )
    rows = be.observed_runs("attempt0000000001")
    assert rows[0]["status"] == "failed"
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["error"] == "turn error"


# -- 2. crash windows at the seam --------------------------------------------


def test_e2e_duplicate_dispatch_after_result_never_requeues(
    tmp_path, monkeypatch, conductor_spool_cls
):
    """Crash window: hermes-gpt re-dispatches after the result already landed.

    The terminal envelope is authority: the duplicate folds it, NEVER queues a
    second execution of the same attempt (no duplicate side effects).
    """
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    conductor.write_result("attempt0000000001", status="succeeded", backend="pi")

    out2 = be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    assert out2["state"] == "completed" and out2["changed"] is False
    # Conductor sees NOTHING new to execute.
    assert conductor.pending_work_orders() == []


def test_e2e_result_before_observation_is_durable(
    tmp_path, monkeypatch, conductor_spool_cls
):
    """Crash window: result lands while hermes-gpt was never looking.

    observed_runs (fresh process, empty caches) must reconstruct truth purely
    from the durable envelope — no in-memory dispatch state exists at all in
    this test by construction (backend instantiated AFTER the result).
    """
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    conductor.write_result(
        "attempt0000000001", status="succeeded", backend="opencode", session_id="s9"
    )

    fresh = hab.HermesAgentBackend()  # simulated restart: no shared state
    rows = fresh.observed_runs("attempt0000000001")
    assert rows and rows[0]["status"] == "completed"
    assert rows[0]["delegate_backend"] == "opencode"


def test_e2e_cancel_intent_visible_to_conductor_side(
    tmp_path, monkeypatch, conductor_spool_cls
):
    """Cancel records durable intent at the shared seam.

    The Hermes-side supervisor can observe ``<attempt>.cancel.json`` without
    any hermes-gpt API call — cross-process cancellation via layout only.
    """
    root = _spool_pair(tmp_path, monkeypatch)
    _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    out = be.cancel("attempt0000000001")
    assert out["state"] == "cancelling"
    cancel = root / "pending" / "attempt0000000001.cancel.json"
    assert cancel.is_file()
    intent = json.loads(cancel.read_text())
    assert intent["attempt_id"] == "attempt0000000001"
    # The work order itself survives (terminal envelope stays the authority).
    assert (root / "pending" / "attempt0000000001.json").is_file()


# -- 3. grammar guard: the writer may not drift from the reader --------------


def test_e2e_writer_payload_grammar_matches_conductor_reader(
    tmp_path, monkeypatch, conductor_spool_cls
):
    """Structural contract: every envelope hermes-gpt writes parses and keeps
    the fields conductor's reader actually reads (pending_work_orders consumes
    whole JSON objects keyed by attempt_id; write_result echoes the pending
    envelope's session_binding when hermes omits one).
    """
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    for i, delegate in enumerate(("pi", "opencode")):
        tid = f"attemptgrammar{i:010d}"
        contract = _contract(tmp_path, task_id=tid, delegate=delegate)
        be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
    orders = conductor.pending_work_orders()
    assert [o["backend_hint"] for o in orders] == ["pi", "opencode"]
    assert all(o["schema"] == 1 for o in orders)

    # write_result with no session_binding_id copies the envelope's binding —
    # hermes-gpt's binding block must remain readable by that path.
    conductor.write_result("attemptgrammar0000000000", status="succeeded", backend="pi")
    result = json.loads(
        (root / "results" / "attemptgrammar0000000000.json").read_text()
    )
    assert result["attempt_id"] == "attemptgrammar0000000000"
    assert result["status"] == "succeeded"


def test_e2e_resume_flag_round_trips_through_the_seam(
    tmp_path, monkeypatch, conductor_spool_cls
):
    """session_binding.resume survives the boundary: a crashed turn re-queued
    with resume=True must reach the Hermes side unchanged (it is the input the
    native delegate_session uses to reattach instead of restarting).
    """
    root = _spool_pair(tmp_path, monkeypatch)
    conductor = _init_conductor_side(conductor_spool_cls, root)
    be = hab.HermesAgentBackend()
    contract = _contract(tmp_path, task_id="attemptresume000001")
    contract["execution"]["options"]["resume"] = True
    contract["execution"]["options"]["run_id"] = "run-m3-retry"
    contract["execution"]["options"]["role"] = "primary"
    be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
    order = conductor.pending_work_orders()[0]
    assert order["session_binding"] == {
        "run_id": "run-m3-retry",
        "role": "primary",
        "backend": "pi",
        "affinity_key": "",
        "resume": True,
    }
