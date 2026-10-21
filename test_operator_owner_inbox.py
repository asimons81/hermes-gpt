"""Tests for the read-only Owner inbox (operator_owner_inbox).

Covers: empty root, each live source (Mission awaiting approval, Autopilot
waiting for Owner, controller attention spool), fail-visible source errors,
limit validation, bounded summaries, and the pure-read guarantee (the Mission
root tree is byte-for-byte and mtime-for-mtime unchanged by ``build_inbox``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import operator_autopilot as autopilot
import operator_controller as controller
import operator_delegations as delegations
import operator_mission_runtime as mission
import operator_owner_inbox as inbox
import operator_policy as op
import operator_review as op_review
import operator_runners as runners

MID = "msn-inbox1"
AUTO_MID = "msn-inboxauto"


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    hermes = tmp_path / "hermes"
    hermes.mkdir()
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.delenv(op.OWNER_ACTIVE_ENV, raising=False)
    monkeypatch.delenv(op.OWNER_ACK_ENV, raising=False)
    monkeypatch.delenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", raising=False)
    yield hermes
    op.set_audit_log_override(None)


def _create_mission(root: Path, mid: str = MID) -> None:
    spec = json.dumps({
        "schema": mission.MISSION_SPEC_SCHEMA, "mission_id": mid, "title": "Inbox test",
        "objective": "Exercise the owner inbox.", "owner_profile": "default",
        "acceptance_criteria": ["listed"], "context_refs": [], "skills": [],
        "final_approval_required": True,
    })
    out = json.loads(mission.hermes_mission_create(spec, confirm=True, dry_run=False, hermes_root=root))
    assert out["success"] is True, out


def _set_mission_status(root: Path, mid: str, status: str) -> None:
    db = sqlite3.connect(root / "missions" / "missions.db")
    try:
        db.execute("UPDATE missions SET status=? WHERE mission_id=?", (status, mid))
        db.commit()
    finally:
        db.close()


def _spool(root: Path, *, mission_id: str = MID, tier_reasons=("placement:no_capable_target",),
           row_key: str = "park_capability", title_extra: str = "") -> None:
    envelope = controller.build_attention_envelope(
        mission_id=mission_id, node_id="n1", classification="capability", row_key=row_key,
        proposed_action="escalate capability", reasons=list(tier_reasons), uncertainty="",
        tier_reasons=list(tier_reasons), pass_seq=1,
    )
    envelope["title"] = envelope["title"] + title_extra
    controller.spool_attention_envelope(envelope, hermes_root=root)


def _waiting_run(root: Path, mid: str = AUTO_MID) -> None:
    autopilot._write_run(mid, root, state="waiting_for_owner", attempt=1, max_concurrency=2)  # noqa: SLF001


def _snapshot(root: Path) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()  # stable keys on Windows too
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        out[rel] = (path.is_dir(), st.st_size, st.st_mtime_ns, digest)
    return out


def test_empty_root_has_no_items_and_is_complete(root):
    result = inbox.build_inbox(hermes_root=root)
    assert result["items"] == []
    assert result["count"] == 0 and result["truncated"] is False
    assert result["sources"] == {
        "missions": "empty", "autopilot": "empty", "controller_attention": "empty",
        "work_contract_review": "empty", "acks": "empty",
    }
    assert result["complete"] is True
    assert result["read_only"] is True
    assert result["skipped_sources"] == []
    assert result["hidden_acknowledged"] == 0


def test_awaiting_approval_mission_is_listed_with_approve_suggestion(root):
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["missions"] == "ok"
    (item,) = [i for i in result["items"] if i["kind"] == "mission_awaiting_approval"]
    assert item["mission_id"] == MID
    assert item["suggested_tool"] == "hermes_mission_approve"
    assert item["requires"] == "owner"
    assert item["since"]
    assert set(item) == {
        "item_id", "kind", "mission_id", "summary", "since", "suggested_tool", "requires", "acknowledged",
    }
    assert item["acknowledged"] is False


def test_running_mission_is_not_listed(root):
    _create_mission(root)
    result = inbox.build_inbox(hermes_root=root)
    assert result["items"] == []
    assert result["sources"]["missions"] == "empty"


def test_waiting_for_owner_autopilot_run_is_listed(root):
    _waiting_run(root)
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["autopilot"] == "ok"
    (item,) = result["items"]
    assert item["kind"] == "autopilot_waiting_owner"
    assert item["mission_id"] == AUTO_MID
    assert item["requires"] == "owner"
    assert "waiting_for_owner" in item["summary"]


def test_controller_spool_envelope_is_listed(root):
    _spool(root)
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["controller_attention"] == "ok"
    (item,) = result["items"]
    assert item["kind"] == "no_capable_target"
    assert item["requires"] == "workspace"
    assert item["suggested_tool"] == "hermes_controller_status"
    assert item["mission_id"] == MID


def test_budget_and_red_spool_envelopes_are_classified(root):
    _spool(root, tier_reasons=("budget:breaker_signal",), row_key="park_budget")
    _spool(root, mission_id="msn-inboxred", tier_reasons=("evidence:stale",), row_key="park_authority")
    result = inbox.build_inbox(hermes_root=root)
    kinds = {i["kind"]: i for i in result["items"]}
    assert set(kinds) == {"budget_breaker", "red_health"}
    assert kinds["budget_breaker"]["suggested_tool"] == "hermes_budget_check"
    assert kinds["red_health"]["requires"] == "owner"


def test_items_are_newest_first_and_capped(root):
    _spool(root, mission_id="msn-inboxa", row_key="r1")
    _spool(root, mission_id="msn-inboxb", row_key="r2")
    _spool(root, mission_id="msn-inboxc", row_key="r3")
    full = inbox.build_inbox(hermes_root=root, limit=50)
    stamps = [i["since"] for i in full["items"]]
    assert stamps == sorted(stamps, reverse=True)
    capped = inbox.build_inbox(hermes_root=root, limit=2)
    assert capped["count"] == 2 and capped["truncated"] is True and len(capped["items"]) == 2
    assert capped["items"] == full["items"][:2]


def test_corrupted_mission_store_is_unavailable_and_incomplete(root):
    _waiting_run(root)
    (root / "missions").mkdir(parents=True, exist_ok=True)
    (root / "missions" / "missions.db").write_bytes(b"this is not a sqlite database" * 50)
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["missions"] == "unavailable"
    assert result["complete"] is False
    # Other sources are still reported truthfully, not hidden by the failure.
    assert result["sources"]["autopilot"] == "ok"
    assert [i["kind"] for i in result["items"]] == ["autopilot_waiting_owner"]


def test_corrupted_autopilot_record_is_unavailable_not_silently_empty(root):
    base = root / "autopilot"
    base.mkdir(parents=True)
    (base / f"{AUTO_MID}.json").write_text("{not json", encoding="utf-8")
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["autopilot"] == "unavailable"
    assert result["complete"] is False
    assert result["items"] == []


def test_malformed_spool_line_is_unavailable(root):
    _spool(root)
    path = controller._attention_spool_path(root)  # noqa: SLF001
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken line\n")
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["controller_attention"] == "unavailable"
    assert result["complete"] is False


@pytest.mark.parametrize("bad", [0, -1, 201, True, "5", 1.5])
def test_limit_is_validated(root, bad):
    with pytest.raises(ValueError):
        inbox.build_inbox(hermes_root=root, limit=bad)


def test_limit_bounds_are_inclusive(root):
    assert inbox.build_inbox(hermes_root=root, limit=1)["limit"] == 1
    assert inbox.build_inbox(hermes_root=root, limit=200)["limit"] == 200


def test_string_wrapper_returns_json_and_reports_invalid_limit(root):
    ok = json.loads(inbox.hermes_owner_inbox(limit=10, hermes_root=root))
    assert ok["success"] is True and ok["tool"] == "hermes_owner_inbox" and ok["read_only"] is True
    bad = json.loads(inbox.hermes_owner_inbox(limit=0, hermes_root=root))
    assert bad["success"] is False


def test_summaries_are_redacted_and_bounded(root):
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    _spool(root, title_extra=" token=" + secret + " " + "x" * 2000)
    (item,) = inbox.build_inbox(hermes_root=root)["items"]
    assert secret not in item["summary"]
    assert len(item["summary"]) <= 300


def _db_rows(root: Path) -> list[tuple]:
    db = sqlite3.connect(f"file:{root / 'missions' / 'missions.db'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT mission_id, status, version, updated_at FROM missions ORDER BY mission_id").fetchall()
    finally:
        db.close()


def test_build_inbox_writes_nothing(root):
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    _waiting_run(root)
    _spool(root)
    before = _snapshot(root)
    rows_before = _db_rows(root)
    assert before, "fixture must populate the root"
    result = inbox.build_inbox(hermes_root=root)
    assert result["count"] >= 3
    # The mission read is the existing read-only path; opening a WAL database
    # read-only may create SQLite's -shm/-wal sidecars. Those are not durable
    # records, so compare everything else exactly and the mission rows by value.
    after = _snapshot(root)
    def durable(snap: dict[str, tuple]) -> dict[str, tuple]:
        return {k: v for k, v in snap.items() if not v[0] and not k.endswith(("-shm", "-wal"))}

    assert durable(after) == durable(before)
    assert _db_rows(root) == rows_before
    # A repeat read leaves durable files and mission rows untouched too.
    inbox.build_inbox(hermes_root=root)
    assert durable(_snapshot(root)) == durable(before)
    assert _db_rows(root) == rows_before


def test_wrapper_never_approves_or_advances(root):
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    inbox.hermes_owner_inbox(hermes_root=root)
    db = sqlite3.connect(root / "missions" / "missions.db")
    try:
        (status,) = db.execute("SELECT status FROM missions WHERE mission_id=?", (MID,)).fetchone()
    finally:
        db.close()
    assert status == "awaiting_approval"


# ---------------------------------------------------------------------------
# Work Contract review source
# ---------------------------------------------------------------------------

REVIEW_TASK = "inbox-review-task"
REVIEW_DLG = "dlg-inbox-review"
CONTRACT_OBJECTIVE = "INBOX_OBJECTIVE_MUST_NOT_APPEAR"


def _review_contract(workspace: Path, *, task_id: str, required: bool) -> dict:
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": task_id,
        "assigned_agent": "coder",
        "assigned_profile": "default",
        "objective": CONTRACT_OBJECTIVE,
        "allowed_scope": {"workspaces": [str(workspace)], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {
            "required": required,
            "reviewer": "reviewer-two" if required else "",
            "evidence": "",
            "approval_required": False,
        },
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": required,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        # The parser requires approved_by and approval_reference. approved_by is the
        # assignee profile, so the validator's human-approval evidence path (approver
        # distinct from the assignee) cannot satisfy the review gate in these fixtures.
        "authorization": {"class": "reversible_write", "approved": True, "approved_by": "default", "approval_reference": "inbox-test"},
        "execution": {"backend": "pi_rpc", "options": {}},
    }


def _review_delegation(
    root: Path, tmp_path: Path, monkeypatch, *, required: bool = True,
    task_id: str = REVIEW_TASK, dlg: str = REVIEW_DLG,
) -> dict:
    """Dispatch a contract, let the backend report success, then reconcile it durably."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(workspace))
    _create_mission(root)
    monkeypatch.setattr(
        delegations.contract_mod,
        "hermes_contract_dispatch",
        lambda *a, **k: json.dumps({"success": True, "changed": True, "backend": "pi_rpc", "state": "queued"}),
    )
    created = json.loads(delegations.hermes_delegation_dispatch(
        json.dumps(_review_contract(workspace, task_id=task_id, required=required)),
        mission_id=MID, delegation_id=dlg, confirm=True, dry_run=False, hermes_root=root,
    ))
    assert created["success"] is True, created
    meta_path, _, _ = runners._job_paths(task_id, root)  # noqa: SLF001
    runners._atomic_json(meta_path, {  # noqa: SLF001
        "schema_version": runners.SCHEMA_VERSION,
        "task_id": task_id,
        "backend": "pi_rpc",
        "state": "completed",
        "outcome": "completed",
        "created_at": "2026-08-22T00:00:00+00:00",
        "started_at": "2026-08-22T00:00:01+00:00",
        "ended_at": "2026-08-22T00:00:02+00:00",
        "error": "",
    })
    reconciled = json.loads(delegations.hermes_delegation_reconcile(dlg, apply=True, hermes_root=root))
    assert reconciled["success"] is True, reconciled
    return reconciled["delegation"]


def _accept_review(root: Path, monkeypatch, contract_sha256: str, task_id: str = REVIEW_TASK) -> None:
    """Write a SATISFIED review acceptance by a reviewer distinct from the assignee (Owner gate)."""
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)
    out = json.loads(op_review.hermes_review_accept(
        contract_sha256, task_id, "default", "reviewer-two", "SATISFIED",
        evidence_refs=["artifact:inbox-test"], dry_run=False, confirm=True, hermes_root=root,
    ))
    assert out["success"] is True, out


def _kinds(result: dict) -> list[str]:
    return [item["kind"] for item in result["items"]]


def test_review_pending_item_is_listed_until_acceptance(root, tmp_path, monkeypatch):
    delegation = _review_delegation(root, tmp_path, monkeypatch)
    assert delegation["state"] == "reconciling"
    assert delegation["validation"]["verdict"] == "NOT_SATISFIED"
    before = _snapshot(root)
    result = inbox.build_inbox(hermes_root=root)
    assert _durable(_snapshot(root)) == _durable(before)  # the review source is a pure read
    assert result["sources"]["work_contract_review"] == "ok"
    assert result["skipped_sources"] == []
    (item,) = [i for i in result["items"] if i["kind"] == "contract_review_pending"]
    assert item["suggested_tool"] == "hermes_review_accept"
    assert item["requires"] == "review"
    assert item["mission_id"] == MID
    assert delegation["contract_sha256"][:12] in item["summary"]
    assert REVIEW_TASK in item["summary"]
    assert CONTRACT_OBJECTIVE not in json.dumps(result)
    assert len(item["summary"]) <= 240

    _accept_review(root, monkeypatch, delegation["contract_sha256"])
    cleared = inbox.build_inbox(hermes_root=root)
    assert "contract_review_pending" not in _kinds(cleared)
    assert cleared["sources"]["work_contract_review"] == "empty"


def test_review_not_required_is_not_listed(root, tmp_path, monkeypatch):
    delegation = _review_delegation(root, tmp_path, monkeypatch, required=False, task_id="inbox-no-review", dlg="dlg-inbox-noreview")
    assert delegation["state"] == "succeeded"
    result = inbox.build_inbox(hermes_root=root)
    assert "contract_review_pending" not in _kinds(result)
    assert result["sources"]["work_contract_review"] == "empty"


def test_corrupt_delegation_store_makes_review_source_unavailable(root):
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    (root / "delegations").mkdir(parents=True, exist_ok=True)
    (root / "delegations" / "delegations.db").write_bytes(b"this is not a sqlite database" * 50)
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["work_contract_review"] == "unavailable"
    assert result["complete"] is False
    assert result["sources"]["missions"] == "ok"
    assert _kinds(result) == ["mission_awaiting_approval"]


# ---------------------------------------------------------------------------
# Stable item ids
# ---------------------------------------------------------------------------


def test_item_ids_are_stable_across_reads_and_distinct(root):
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    _waiting_run(root)
    _spool(root)
    first = inbox.build_inbox(hermes_root=root)
    second = inbox.build_inbox(hermes_root=root)
    ids = [i["item_id"] for i in first["items"]]
    assert ids == [i["item_id"] for i in second["items"]]
    assert len(ids) == 3 and len(set(ids)) == 3
    for item_id in ids:
        assert len(item_id) == 24 and set(item_id) <= set("0123456789abcdef")
    (mission_item,) = [i for i in first["items"] if i["kind"] == "mission_awaiting_approval"]
    expected = hashlib.sha256(f"mission_awaiting_approval|{MID}|awaiting_approval".encode()).hexdigest()[:24]
    assert mission_item["item_id"] == expected
    # Timestamps are not part of the id: a changed updated_at keeps the same id.
    db = sqlite3.connect(root / "missions" / "missions.db")
    try:
        db.execute("UPDATE missions SET updated_at=? WHERE mission_id=?", ("2030-01-01T00:00:00+00:00", MID))
        db.commit()
    finally:
        db.close()
    later = inbox.build_inbox(hermes_root=root)
    assert mission_item["item_id"] in [i["item_id"] for i in later["items"]]


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------

ACK_FILE = inbox.ACK_STORE_RELATIVE


def _durable(snap: dict[str, tuple], *, exclude: tuple[str, ...] = ()) -> dict[str, tuple]:
    """Durable files only: no directories, SQLite sidecars, or excluded paths."""
    return {
        k: v for k, v in snap.items()
        if not v[0] and not k.endswith(("-shm", "-wal")) and k not in exclude
    }


def _awaiting_mission(root: Path) -> str:
    _create_mission(root)
    _set_mission_status(root, MID, "awaiting_approval")
    (item,) = [i for i in inbox.build_inbox(hermes_root=root)["items"] if i["kind"] == "mission_awaiting_approval"]
    return item["item_id"]


def _ack(item_ids, *, dry_run: bool, root: Path) -> dict:
    return json.loads(inbox.hermes_owner_inbox_ack(item_ids, dry_run=dry_run, hermes_root=root))


def test_ack_dry_run_writes_nothing(root):
    item_id = _awaiting_mission(root)
    before = _snapshot(root)
    out = _ack([item_id], dry_run=True, root=root)
    assert out["success"] is True and out["dry_run"] is True and out["changed"] is False
    assert out["planned"] == [item_id] and out["already_acknowledged"] == [] and out["unknown"] == []
    assert not (root / ACK_FILE).exists()
    assert _durable(_snapshot(root)) == _durable(before)


def test_ack_refused_below_workspace_level(root, monkeypatch):
    item_id = _awaiting_mission(root)
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    out = _ack([item_id], dry_run=False, root=root)
    assert out["success"] is False
    assert "OWNER_INBOX_ACK_DENIED" in json.dumps(out)
    assert not (root / ACK_FILE).exists()


def test_ack_without_direct_apply_mode_is_a_dry_run_plan(root, monkeypatch):
    # Same convention as hermes_mission_create: a non-direct apply mode turns a
    # dry_run=False request into a plan, so nothing is written.
    item_id = _awaiting_mission(root)
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")
    out = _ack([item_id], dry_run=False, root=root)
    assert out["success"] is True and out["dry_run"] is True and out["changed"] is False
    assert out["planned"] == [item_id]
    assert not (root / ACK_FILE).exists()


def test_real_ack_hides_item_and_include_flag_shows_it(root):
    item_id = _awaiting_mission(root)
    out = _ack([item_id], dry_run=False, root=root)
    assert out["success"] is True and out["changed"] is True and out["acknowledged"] == [item_id]
    lines = (root / ACK_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {"item_id", "acked_at", "by"}
    assert record["item_id"] == item_id and record["by"] == "owner_inbox_ack"

    hidden = inbox.build_inbox(hermes_root=root)
    assert hidden["items"] == [] and hidden["count"] == 0
    assert hidden["hidden_acknowledged"] == 1
    assert hidden["sources"]["acks"] == "ok" and hidden["complete"] is True

    shown = inbox.build_inbox(hermes_root=root, include_acknowledged=True)
    (item,) = shown["items"]
    assert item["item_id"] == item_id and item["acknowledged"] is True
    assert shown["hidden_acknowledged"] == 0

    assert json.loads(inbox.hermes_owner_inbox(hermes_root=root))["count"] == 0
    wrapped = json.loads(inbox.hermes_owner_inbox(include_acknowledged=True, hermes_root=root))
    assert wrapped["success"] is True and wrapped["count"] == 1
    assert inbox.acknowledged_ids(root) == {item_id}


def test_ack_is_idempotent(root):
    item_id = _awaiting_mission(root)
    assert _ack([item_id], dry_run=False, root=root)["changed"] is True
    again = _ack([item_id], dry_run=False, root=root)
    assert again["success"] is True and again["changed"] is False
    assert again["acknowledged"] == [] and again["already_acknowledged"] == [item_id]
    assert len((root / ACK_FILE).read_text(encoding="utf-8").splitlines()) == 1


def test_unknown_ids_are_reported_and_write_nothing(root):
    _awaiting_mission(root)
    unknown = "f" * 24
    out = _ack([unknown], dry_run=False, root=root)
    assert out["success"] is True and out["changed"] is False
    assert out["unknown"] == [unknown] and out["acknowledged"] == []
    assert not (root / ACK_FILE).exists()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([], id="empty"),
        pytest.param(["g" * 24], id="non-hex"),
        pytest.param(["A" * 24], id="uppercase"),
        pytest.param(["a" * 23], id="short"),
        pytest.param([123], id="non-string"),
        pytest.param(["0" * 24] * 51, id="too-many"),
        pytest.param("0" * 24, id="not-a-list"),
    ],
)
def test_malformed_ids_are_rejected(root, bad):
    _awaiting_mission(root)
    out = _ack(bad, dry_run=False, root=root)
    assert out["success"] is False
    assert "INVALID_OWNER_INBOX_ACK" in json.dumps(out)
    assert not (root / ACK_FILE).exists()


def test_include_acknowledged_must_be_a_bool(root):
    with pytest.raises(ValueError):
        inbox.build_inbox(hermes_root=root, include_acknowledged="yes")
    bad = json.loads(inbox.hermes_owner_inbox(include_acknowledged="yes", hermes_root=root))
    assert bad["success"] is False


def test_corrupt_ack_store_is_unavailable_and_refuses_writes(root):
    item_id = _awaiting_mission(root)
    (root / ACK_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / ACK_FILE).write_bytes(b"{not json\n")
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["acks"] == "unavailable"
    assert result["complete"] is False
    assert [i["item_id"] for i in result["items"]] == [item_id]
    assert result["items"][0]["acknowledged"] is False
    with pytest.raises(ValueError):
        inbox.acknowledged_ids(root)
    before = (root / ACK_FILE).read_bytes()
    out = _ack([item_id], dry_run=False, root=root)
    assert out["success"] is False and "OWNER_INBOX_ACK_STORE_UNAVAILABLE" in json.dumps(out)
    assert (root / ACK_FILE).read_bytes() == before


def test_ack_never_mutates_mission_plan_or_contract_stores(root, tmp_path, monkeypatch):
    delegation = _review_delegation(root, tmp_path, monkeypatch)
    _set_mission_status(root, MID, "awaiting_approval")
    result = inbox.build_inbox(hermes_root=root)
    ids = [i["item_id"] for i in result["items"]]
    assert len(ids) == 2  # the mission item and the review item
    before = _snapshot(root)
    rows_before = _db_rows(root)
    mission_before = json.loads(mission.hermes_mission_get(MID, hermes_root=root))

    out = _ack(ids, dry_run=False, root=root)
    assert out["success"] is True and sorted(out["acknowledged"]) == sorted(ids)

    assert _durable(_snapshot(root), exclude=(ACK_FILE,)) == _durable(before, exclude=(ACK_FILE,))
    assert _db_rows(root) == rows_before
    mission_after = json.loads(mission.hermes_mission_get(MID, hermes_root=root))
    assert mission_after["status"] == "awaiting_approval" == mission_before["status"]
    assert mission_after == mission_before
    still = json.loads(delegations.hermes_delegation_get(delegation["delegation_id"], hermes_root=root))
    assert still["delegation"]["state"] == "reconciling"
    assert inbox.build_inbox(hermes_root=root)["items"] == []


def _unrelated_acceptances(root: Path, count: int) -> None:
    for index in range(count):
        op_review._write_acceptance(root, {  # noqa: SLF001 - test fixture writes the store directly
            "schema": op_review.REVIEW_SCHEMA,
            "record_id": f"rev-{index:016x}",
            "contract_sha256": f"{index + 1:064x}",
            "task_id": f"t_unrelated_{index}",
            "assignee": "worker-a",
            "reviewer": "reviewer-a",
            "verdict": "SATISFIED",
            "evidence_refs": [],
            "created_at": "2026-10-01T00:00:00+00:00",
        })


def test_review_item_clears_after_250_unrelated_acceptances(root, tmp_path, monkeypatch):
    delegation = _review_delegation(root, tmp_path, monkeypatch)
    _unrelated_acceptances(root, 250)
    assert "contract_review_pending" in _kinds(inbox.build_inbox(hermes_root=root))
    _accept_review(root, monkeypatch, delegation["contract_sha256"])
    cleared = inbox.build_inbox(hermes_root=root)
    assert "contract_review_pending" not in _kinds(cleared)
    assert cleared["sources"]["work_contract_review"] == "empty"
    assert cleared["complete"] is True


def test_review_source_reads_a_store_past_the_old_5000_line_bound(root, tmp_path, monkeypatch):
    delegation = _review_delegation(root, tmp_path, monkeypatch)
    _unrelated_acceptances(root, 5200)
    result = inbox.build_inbox(hermes_root=root)
    assert result["sources"]["work_contract_review"] == "ok"
    assert result["complete"] is True
    assert "contract_review_pending" in _kinds(result)
    _accept_review(root, monkeypatch, delegation["contract_sha256"])
    assert "contract_review_pending" not in _kinds(inbox.build_inbox(hermes_root=root))
