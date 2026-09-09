"""Tests for the per-mission merged replayable ledger read model (vNext slice-1)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_delegations as delegations
import operator_mission_ledger as ld
import operator_mission_runtime as mission


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "logs").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def clean_ledger_env(monkeypatch):
    monkeypatch.delenv(ld.LEDGER_SOURCES_ENV, raising=False)
    yield


def _seed_mission(root: Path, mid: str) -> None:
    db = mission._db_path(root)
    conn = mission._connect(db, write=True)
    try:
        conn.execute(
            "INSERT INTO missions (mission_id, spec_json, status, version, approval_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                mid,
                "{}",
                "running",
                1,
                "{}",
                "2026-08-15T10:00:00+00:00",
                "2026-08-15T10:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, from_status, to_status, reason_sha256, details_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, "created", "", "draft", "a" * 64, "{}", "2026-08-15T10:00:05+00:00"),
        )
        conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, from_status, to_status, reason_sha256, details_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                mid,
                "transitioned",
                "draft",
                "running",
                "b" * 64,
                "{}",
                "2026-08-15T11:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_delegation(root: Path, mid: str, delegation_id: str, task_id: str) -> None:
    db = delegations._db_path(root)
    conn = delegations._connect(db, write=True)
    delegations._init(conn)
    try:
        conn.execute(
            "INSERT INTO delegations (delegation_id, schema, mission_id, task_id, contract_sha256, backend, state, "
            "created_at, dispatched_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                delegation_id,
                "hermes.delegation/v1",
                mid,
                task_id,
                "c" * 64,
                "codex",
                "dispatched",
                "2026-08-15T10:30:00+00:00",
                "2026-08-15T10:30:00+00:00",
                "2026-08-15T10:30:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO delegation_events (delegation_id, event_type, from_state, to_state, backend_state, observed_sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                delegation_id,
                "dispatched",
                "",
                "running",
                "running",
                "d" * 64,
                "2026-08-15T10:30:05+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO delegation_events (delegation_id, event_type, from_state, to_state, backend_state, observed_sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                delegation_id,
                "succeeded",
                "running",
                "succeeded",
                "ok",
                "e" * 64,
                "2026-08-15T12:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_audit(root: Path, mid: str) -> None:
    recs = [
        {
            "timestamp": "2026-08-15T10:20:00+00:00",
            "tool": "hermes_mission_reconcile",
            "mission_id": mid,
            "level": "read_only",
            "apply_mode": "direct",
            "dry_run": True,
            "success": True,
            "changed": False,
            "summary": "reconcile observed",
            "profile": "default",
        },
        {
            "timestamp": "2026-08-15T10:21:00+00:00",
            "tool": "hermes_mission_reconcile",
            "mission_id": mid,
            "level": "read_only",
            "apply_mode": "direct",
            "dry_run": True,
            "success": True,
            "changed": False,
            "summary": "reconcile ok",
            "profile": "default",
        },
        {
            "timestamp": "2026-08-15T10:22:00+00:00",
            "tool": "hermes_mission_get",
            "mission_id": "other-mission",
            "level": "read_only",
            "apply_mode": "direct",
            "dry_run": True,
            "success": True,
            "changed": False,
            "summary": "should not appear",
            "profile": "default",
        },
    ]
    with open(
        root / "logs" / "hermes_gpt_operator_audit.jsonl", "a", encoding="utf-8"
    ) as fh:
        fh.writelines(json.dumps(rec, sort_keys=True) + "\n" for rec in recs)


def _seed_kanban(root: Path, task_id: str) -> None:
    db = root / "kanban" / "boards" / "default" / "kanban.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_events (task_id TEXT, kind TEXT, created_at TEXT, actor TEXT, summary TEXT)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at, actor, summary) VALUES (?,?,?,?,?)",
            (task_id, "claimed", "2026-08-15T10:45:00+00:00", "dev", "claimed by dev"),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at, actor, summary) VALUES (?,?,?,?,?)",
            (
                task_id,
                "spawned",
                "2026-08-15T10:45:30+00:00",
                "dispatcher",
                "spawned worker",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _full_seed(root: Path, mid: str = "msn-1") -> tuple[Path, str]:
    _seed_mission(root, mid)
    _seed_delegation(root, mid, "dlg-1", "task-1")
    _seed_audit(root, mid)
    _seed_kanban(root, "task-1")
    return root, mid


def test_replay_reproduces_event_history(hermes_root: Path):
    root, mid = _full_seed(hermes_root)
    out = json.loads(ld.hermes_mission_ledger_replay(mid, hermes_root=root))
    assert out["success"] is True
    assert out["mission_id"] == mid
    assert out["mission_status"] == "running"
    # 2 mission + 2 delegation + 2 audit + 2 kanban events.
    assert out["count_total"] == 8
    sources = {e["source"] for e in out["events"]}
    assert sources == {"mission", "delegation", "audit", "kanban"}
    # Cursors are monotonically increasing across the whole stream.
    cursors = [e["cursor"] for e in out["events"]]
    assert cursors == sorted(cursors)
    assert cursors == list(range(1, 9))
    # Audit rows for other-mission are excluded.
    assert all(
        e["source"] != "audit" or "other-mission" not in str(e.get("refs"))
        for e in out["events"]
    )


def test_cursor_resume_is_append_only(hermes_root: Path):
    root, mid = _full_seed(hermes_root)
    out = json.loads(ld.hermes_mission_ledger(mid, cursor=4, hermes_root=root))
    assert out["success"] is True
    # Events strictly after cursor 4.
    assert all(e["cursor"] > 4 for e in out["events"])
    assert out["count_total"] == 4  # 8 total, 4 after cursor 4
    assert out["events"][0]["cursor"] == 5


def test_ledger_inv9_no_raw_bodies(hermes_root: Path):
    root, mid = _full_seed(hermes_root)
    # Inject a raw prompt/memory body into an audit record for the mission,
    # mirroring how audit_record stores sensitive content (as length+sha256,
    # not raw). The ledger must never surface the raw body.
    with open(
        root / "logs" / "hermes_gpt_operator_audit.jsonl", "a", encoding="utf-8"
    ) as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": "2026-08-15T10:23:00+00:00",
                    "tool": "hermes_owner_run_command",
                    "mission_id": mid,
                    "success": True,
                    "changed": False,
                    "summary": "secret action",
                    "prompt": "cat ~/.ssh/id_ed25519",
                    "prompt_len": 25,
                    "prompt_sha256": "9" * 64,
                    "content": "memory-body-xyz",
                    "content_len": 15,
                    "content_sha256": "7" * 64,
                },
                sort_keys=True,
            )
            + "\n"
        )
    out = json.loads(ld.hermes_mission_ledger_replay(mid, hermes_root=root))
    blob = json.dumps(out)
    assert "cat ~/.ssh/id_ed25519" not in blob
    assert "memory-body-xyz" not in blob
    # Prompt content only appears as a content address.
    for e in out["events"]:
        if e["source"] == "audit" and e.get("summary") == "secret action":
            assert e["provenance_sha256"]


def _db_rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_ledger_read_only_no_mutation(hermes_root: Path):
    root, mid = _full_seed(hermes_root)
    m_rows = _db_rows(mission._db_path(root), "missions")
    me_rows = _db_rows(mission._db_path(root), "mission_events")
    d_rows = _db_rows(delegations._db_path(root), "delegations")
    de_rows = _db_rows(delegations._db_path(root), "delegation_events")
    ld.hermes_mission_ledger_replay(mid, hermes_root=root)
    ld.hermes_mission_ledger(mid, cursor=1, hermes_root=root)
    # No data rows are added, removed, or modified by the read paths.
    assert _db_rows(mission._db_path(root), "missions") == m_rows
    assert _db_rows(mission._db_path(root), "mission_events") == me_rows
    assert _db_rows(delegations._db_path(root), "delegations") == d_rows
    assert _db_rows(delegations._db_path(root), "delegation_events") == de_rows


def test_ledger_allowlist_filters_sources(hermes_root: Path, monkeypatch):
    root, mid = _full_seed(hermes_root)
    monkeypatch.setenv(ld.LEDGER_SOURCES_ENV, "mission,delegation")
    out = json.loads(ld.hermes_mission_ledger_replay(mid, hermes_root=root))
    assert out["count_total"] == 4
    assert {e["source"] for e in out["events"]} == {"mission", "delegation"}
