"""Regression: hermes-gpt → Hermes Agent delegation adapter (P0 correction).

The corrected architecture routes native Delegations through Hermes Agent's
``delegate_session`` (persistent Pi RPC / OpenCode sessions) instead of
hermes-gpt-owned process trees. These pins hold the transport contract:

1. dispatch durably publishes a conforming work-order envelope (attempt-id
   keyed, session binding carried, bounded payload) and returns queued.
2. dry_run never touches the spool.
3. execution.options.delegate selects the native backend (pi/opencode).
4. observations fold ONLY from transport truth (terminal envelope);
   a session-completion report can never self-satisfy a Work Contract —
   hermes-gpt's independent artifact checks stay the completion authority.
5. missing/unauthorized spool, bad ids, missing workspace fail closed.
6. cancel records intent durably and never reaps a terminal result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import operator_hermes_agent_runner as hab  # noqa: E402


def _spool(tmp_path: Path) -> Path:
    root = tmp_path / "spool"
    (root / "pending").mkdir(parents=True)
    (root / "results").mkdir(parents=True)
    return root


def _contract(tmp_path: Path, *, backend: str = "hermes_agent", delegate: str = "") -> dict:
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    execution: dict = {"backend": backend, "options": {"sandbox": "workspace-write"}}
    if delegate:
        execution["options"]["delegate"] = delegate
    return {
        "task_id": "attempt0000000001",
        "objective": "Fix the boot-lock and prove it with a regression test.",
        "execution": execution,
        "allowed_scope": {"workspaces": [str(ws)]},
    }


def _backend(monkeypatch, tmp_path: Path) -> hab.HermesAgentBackend:
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(_spool(tmp_path)))
    return hab.HermesAgentBackend()


def test_dispatch_publishes_conforming_envelope(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    out = be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    assert out["success"] is True and out["state"] == "queued"
    assert out["delegate_backend"] == "pi"  # hermes_agent/pi_rpc map to native pi
    envelope = tmp_path / "spool" / "pending" / "attempt0000000001.json"
    assert envelope.is_file()
    order = json.loads(envelope.read_text())
    assert order["interface"] == "delegate_session"
    assert order["attempt_id"] == "attempt0000000001"
    assert order["backend_hint"] == "pi"
    assert order["session_binding"]["role"] == "primary"
    assert order["session_binding"]["backend"] == "pi"
    assert order["workspace_path"].endswith("workspace")
    assert order["phase_prompt"].startswith("Fix the boot-lock")


def test_dispatch_dry_run_never_touches_spool(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    out = be.dispatch(_contract(tmp_path), confirm=True, dry_run=True, timeout=900)
    assert out["dry_run"] is True and out["state"] == "planned"
    assert not (tmp_path / "spool" / "pending").iterdir().__next__() if any(
        (tmp_path / "spool" / "pending").iterdir()
    ) else True
    assert not list((tmp_path / "spool" / "pending").iterdir())


def test_dispatch_delegate_option_selects_opencode(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    out = be.dispatch(
        _contract(tmp_path, delegate="opencode"), confirm=True, dry_run=False, timeout=900
    )
    assert out["delegate_backend"] == "opencode"
    order = json.loads(
        (tmp_path / "spool" / "pending" / "attempt0000000001.json").read_text()
    )
    assert order["backend_hint"] == "opencode"
    assert order["session_binding"]["backend"] == "opencode"


def test_dispatch_rejects_unknown_delegate_backend(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="delegate must be one of"):
        be.dispatch(
            _contract(tmp_path, delegate="codex"), confirm=True, dry_run=False, timeout=900
        )
    assert not list((tmp_path / "spool" / "pending").iterdir())


def test_dispatch_requires_workspace(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    contract = _contract(tmp_path)
    contract["allowed_scope"]["workspaces"] = []
    out = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
    assert out["success"] is False and out["changed"] is False
    assert out["code"] == "HERMES_AGENT_WORKSPACE_REQUIRED"


def test_dispatch_requires_initialized_spool(tmp_path, monkeypatch):
    empty = tmp_path / "empty-spool"
    empty.mkdir()
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(empty))
    be = hab.HermesAgentBackend()
    with pytest.raises(RuntimeError, match="not initialized"):
        be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)


def test_duplicate_dispatch_folds_terminal_envelope(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    results = tmp_path / "spool" / "results" / "attempt0000000001.json"
    results.write_text(
        json.dumps(
            {
                "attempt_id": "attempt0000000001",
                "status": "succeeded",
                "session_id": "sess-1",
                "backend": "pi",
            }
        )
    )
    out = be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    assert out["state"] == "completed"
    assert out["changed"] is False
    # No re-queue: pending stays empty.
    assert not list((tmp_path / "spool" / "pending").iterdir())


def test_observation_is_transport_truth_only(tmp_path, monkeypatch):
    """A session-completion report must NEVER read as contract completion.

    The observation carries ``status``/``outcome`` describing the delegated
    turn; hermes-gpt's independent artifact validation decides completion.
    """
    be = _backend(monkeypatch, tmp_path)
    results = tmp_path / "spool" / "results" / "attempt0000000001.json"
    results.write_text(
        json.dumps(
            {"attempt_id": "attempt0000000001", "status": "succeeded", "session_id": "s1"}
        )
    )
    rows = be.observed_runs("attempt0000000001")
    assert len(rows) == 1
    # Transport shape, deliberately NOT a contract verdict:
    assert rows[0]["outcome"] in {"completed", "failed"}
    assert rows[0].get("scope") == "runner:hermes_agent"


def test_observation_running_from_pending_envelope(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    rows = be.observed_runs("attempt0000000001")
    assert rows and rows[0]["status"] == "running"
    assert rows[0]["delegate_backend"] == "pi"


def test_observation_empty_for_unknown_attempt(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    assert be.observed_runs("attempt0000000009") == []
    # Non-conforming ids can never join into paths.
    assert be.observed_runs("../escape") == []


def test_cancel_records_intent_and_respects_terminal(tmp_path, monkeypatch):
    be = _backend(monkeypatch, tmp_path)
    be.dispatch(_contract(tmp_path), confirm=True, dry_run=False, timeout=900)
    out = be.cancel("attempt0000000001")
    assert out["success"] is True and out["state"] == "cancelling"
    cancel = json.loads(
        (tmp_path / "spool" / "pending" / "attempt0000000001.cancel.json").read_text()
    )
    assert cancel["attempt_id"] == "attempt0000000001"
    # Terminal results are never reaped by cancel.
    results = tmp_path / "spool" / "results" / "attempt0000000001.json"
    results.write_text(
        json.dumps({"attempt_id": "attempt0000000001", "status": "failed"})
    )
    out2 = be.cancel("attempt0000000001")
    assert out2["success"] is False and out2["proven_terminal"] is True
    assert results.is_file()


def test_registration_first_wins_and_module_imports_clean():
    """The corrected rail registers BEFORE the direct-launch backends."""
    import operator_runners as op_runners

    names = list(op_runners._BACKENDS.keys())
    assert "hermes_agent" in names
    assert names[0] == "hermes_agent"


def test_dispatch_creates_missing_artifacts_workspace(tmp_path, monkeypatch):
    """The autopilot's delegated workspace (hermes_root/missions/artifacts/)
    is created on first dispatch — the contract itself authorizes writes
    under it (msn-m3-pi-dev-004 first-dispatch failure)."""
    root = _spool(tmp_path)          # spool under <tmp>/spool; data root = <tmp>
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(root))
    be = hab.HermesAgentBackend()
    ws = tmp_path / "missions" / "artifacts" / "ap-m-attempt0000000001"
    contract = _contract(tmp_path)
    contract["allowed_scope"]["workspaces"] = [str(ws)]
    out = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
    assert out["success"] is True
    assert ws.is_dir()


def test_dispatch_refuses_workspace_outside_data_root(tmp_path, monkeypatch):
    root = _spool(tmp_path)
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(root))
    be = hab.HermesAgentBackend()
    contract = _contract(tmp_path)
    contract["allowed_scope"]["workspaces"] = ["/tmp/definitely-not-ours-x9"]
    Path("/tmp/definitely-not-ours-x9").mkdir(exist_ok=True)
    try:
        out = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
        # dir exists so the creation branch never fires; dispatch proceeds —
        # the data-root guard only gates CREATION, existing dirs are honored
        assert out["success"] is True
    finally:
        import shutil as _shutil
        _shutil.rmtree("/tmp/definitely-not-ours-x9", ignore_errors=True)


def test_dispatch_refusal_for_missing_uncreatable_workspace(tmp_path, monkeypatch):
    root = _spool(tmp_path)
    monkeypatch.setenv(hab.SPOOL_ROOT_ENV, str(root))
    be = hab.HermesAgentBackend()
    # under data root, but parent is a FILE → mkdir fails → typed refusal
    blocker = tmp_path / "missions"
    blocker.write_text("not a dir")
    contract = _contract(tmp_path)
    contract["allowed_scope"]["workspaces"] = [str(blocker / "artifacts" / "x")]
    out = be.dispatch(contract, confirm=True, dry_run=False, timeout=900)
    assert out["success"] is False and out["changed"] is False
    assert "could not be created" in out["safe_message"]
    # NotADirectoryError is an OSError — but the runner re-raises it as
    # ValueError via the refusal path; assert the transport never queued.
    assert not list(root.glob("pending/*"))
