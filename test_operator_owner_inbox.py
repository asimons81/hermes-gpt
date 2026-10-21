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
import operator_mission_runtime as mission
import operator_owner_inbox as inbox
import operator_policy as op

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
        rel = str(path.relative_to(root))
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        out[rel] = (path.is_dir(), st.st_size, st.st_mtime_ns, digest)
    return out


def test_empty_root_has_no_items_and_is_complete(root):
    result = inbox.build_inbox(hermes_root=root)
    assert result["items"] == []
    assert result["count"] == 0 and result["truncated"] is False
    assert result["sources"] == {"missions": "empty", "autopilot": "empty", "controller_attention": "empty"}
    assert result["complete"] is True
    assert result["read_only"] is True
    assert "work_contract_review" in result["skipped_sources"]


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
    assert set(item) == {"kind", "mission_id", "summary", "since", "suggested_tool", "requires"}


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
