"""Tests for v0.7 S4: structured event history surface (operator_events)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import operator_events as ev


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "logs").mkdir(parents=True)
    (root / "swarm-workflows").mkdir(parents=True)
    (root / "codex-jobs").mkdir(parents=True)
    (root / "kanban" / "boards" / "default").mkdir(parents=True)
    (root / "profiles" / "default" / "cron").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def clean_events_env(monkeypatch):
    for name in (
        ev.EVENTS_ALLOWED_SOURCES_ENV,
        ev.EVENTS_MAX_AGE_DAYS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    ev._cache.clear()
    yield
    ev._cache.clear()


def _write_audit(root: Path, rows: list[dict]) -> None:
    with open(root / "logs" / "hermes_gpt_operator_audit.jsonl", "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _write_swarm(root: Path, workflow_id: str, status: str, ts: str) -> None:
    rec = {
        "schema": "hermes.swarm-workflow/v1",
        "workflow_id": workflow_id,
        "title": "wf",
        "status": status,
        "created_at": ts,
        "updated_at": ts,
        "stages": [
            {"id": "s1", "owner": "dev", "status": status, "started_at": ts, "ended_at": None},
        ],
    }
    (root / "swarm-workflows" / f"{workflow_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _write_codex(root: Path, job_id: str, status: str, ts: str) -> None:
    rec = {"schema": "codex-job", "job_id": job_id, "status": status, "created_at": ts, "updated_at": ts}
    (root / "codex-jobs" / f"{job_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _write_cron(root: Path, job_id: str, status: str, ts: str, error: str = "") -> None:
    db = root / "profiles" / "default" / "cron" / "executions.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS executions (job_id TEXT, status TEXT, started_at TEXT, error TEXT)"
        )
        conn.execute(
            "INSERT INTO executions (job_id, status, started_at, error) VALUES (?, ?, ?, ?)",
            (job_id, status, ts, error),
        )
        conn.commit()
    finally:
        conn.close()


def _write_kanban(root: Path, task_id: str, kind: str, ts: str, summary: str = "") -> None:
    db = root / "kanban" / "boards" / "default" / "kanban.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_events (task_id TEXT, kind TEXT, created_at TEXT, actor TEXT, summary TEXT)"
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at, actor, summary) VALUES (?, ?, ?, ?, ?)",
            (task_id, kind, ts, "dev", summary),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_all_sources(root: Path) -> None:
    _write_audit(root, [{"timestamp": "2026-08-15T10:00:00+00:00", "tool": "hermes_contract_validate", "profile": "dev", "success": True, "summary": "validate ok"}])
    _write_swarm(root, "sw-1", "done", "2026-08-15T11:00:00+00:00")
    _write_codex(root, "codex-1", "finished", "2026-08-15T12:00:00+00:00")
    _write_cron(root, "job-1", "completed", "2026-08-15T13:00:00+00:00")
    _write_kanban(root, "t_123", "claimed", "2026-08-15T14:00:00+00:00", "claimed by dev")


def test_query_all_sources_returns_ordered_timeline(hermes_root):
    _seed_all_sources(hermes_root)
    out = json.loads(ev.hermes_events_query(hermes_root=hermes_root))
    assert out["success"] is True
    assert out["count_total"] >= 5
    # Newest first ordering.
    timestamps = [t for t in (ev._parse_iso_ts(e["ts"]) for e in out["events"]) if t is not None]
    assert timestamps == sorted(timestamps, reverse=True)
    sources = {e["source"] for e in out["events"]}
    assert {"audit", "swarm", "codex", "cron", "kanban"} <= sources


def test_no_raw_body_across_all_sources(hermes_root):
    """No raw messages, prompts, transcripts, or secret bodies on the surface."""
    _seed_all_sources(hermes_root)
    # Add a nasty audit row with prompt text that must be summarized, not passed.
    _write_audit(
        hermes_root,
        [{"timestamp": "2026-08-15T15:00:00+00:00", "tool": "hermes_skill_create", "profile": "dev", "success": True, "summary": "created skill", "prompt": "SECRET PROMPT BODY with api key sk-1234567890abcdefghijklmnop", "content": "raw content body"}],
    )
    out = json.loads(ev.hermes_events_tail(limit=100, hermes_root=hermes_root))
    raw = json.dumps(out)
    for forbidden in (
        "SECRET PROMPT BODY",
        "raw content body",
        "sk-1234567890abcdefghijklmnop",
        "prompt_len",  # audit prompt_sha is not part of the event schema
    ):
        assert forbidden not in raw, f"leaked: {forbidden}"


def test_allowlist_unset_means_all(hermes_root):
    _seed_all_sources(hermes_root)
    out = json.loads(ev.hermes_events_query(hermes_root=hermes_root))
    assert out["success"] is True
    assert set(out["sources_allowed"]) == set(ev.EVENT_SOURCES)


def test_allowlist_list_restricts_sources(hermes_root, monkeypatch):
    _seed_all_sources(hermes_root)
    monkeypatch.setenv(ev.EVENTS_ALLOWED_SOURCES_ENV, "swarm,codex")
    out = json.loads(ev.hermes_events_query(hermes_root=hermes_root))
    sources = {e["source"] for e in out["events"]}
    assert sources <= {"swarm", "codex"}
    assert out["sources_allowed"] == ["codex", "swarm"]


def test_allowlist_empty_denies_all(hermes_root, monkeypatch):
    _seed_all_sources(hermes_root)
    monkeypatch.setenv(ev.EVENTS_ALLOWED_SOURCES_ENV, "")
    out = json.loads(ev.hermes_events_query(hermes_root=hermes_root))
    assert out["success"] is True
    assert out["events"] == []
    assert out["sources_allowed"] == []


def test_retention_window_filters_old_events(hermes_root, monkeypatch):
    monkeypatch.setenv(ev.EVENTS_MAX_AGE_DAYS_ENV, "1")
    _write_audit(hermes_root, [{"timestamp": "2020-01-01T00:00:00+00:00", "tool": "hermes_contract_validate", "profile": "dev", "success": True, "summary": "ancient"}])
    _write_audit(hermes_root, [{"timestamp": "2026-08-15T10:00:00+00:00", "tool": "hermes_contract_validate", "profile": "dev", "success": True, "summary": "fresh"}])
    out = json.loads(ev.hermes_events_query(source="audit", hermes_root=hermes_root))
    assert out["count_total"] == 1
    assert out["events"][0]["summary"] == "fresh"
    assert out["retention_max_age_days"] == 1


def test_query_by_subject_id_returns_ordered_timeline(hermes_root):
    _seed_all_sources(hermes_root)
    out = json.loads(ev.hermes_events_query(subject_id="sw-1", hermes_root=hermes_root))
    assert out["success"] is True
    assert all("sw-1" in str(e.get("subject_id") or "") for e in out["events"])
    assert out["events"][0]["source"] == "swarm"


def test_query_by_kind_filters(hermes_root):
    _seed_all_sources(hermes_root)
    out = json.loads(ev.hermes_events_query(kind="job_run", hermes_root=hermes_root))
    assert out["success"] is True
    assert all(e["kind"] == "job_run" for e in out["events"])
    assert all(e["source"] == "cron" for e in out["events"])


def test_limit_caps_and_truncation_flag(hermes_root):
    for i in range(10):
        _write_audit(hermes_root, [{"timestamp": f"2026-08-15T10:{i:02d}:00+00:00", "tool": "hermes_operator_status", "profile": "dev", "success": True, "summary": f"event {i}"}])
    out = json.loads(ev.hermes_events_query(source="audit", limit=3, hermes_root=hermes_root))
    assert out["count_returned"] == 3
    assert out["count_total"] == 10
    assert out["truncated"] is True


def test_events_surface_is_read_only(hermes_root):
    """The surface must never create or mutate stores."""
    _seed_all_sources(hermes_root)
    before = {
        "swarm": sorted(p.name for p in (hermes_root / "swarm-workflows").iterdir()),
        "codex": sorted(p.name for p in (hermes_root / "codex-jobs").iterdir()),
    }
    json.loads(ev.hermes_events_query(hermes_root=hermes_root))
    json.loads(ev.hermes_events_tail(hermes_root=hermes_root))
    assert sorted(p.name for p in (hermes_root / "swarm-workflows").iterdir()) == before["swarm"]
    assert sorted(p.name for p in (hermes_root / "codex-jobs").iterdir()) == before["codex"]
    # No new DBs/files created.
    assert not (hermes_root / "kanban" / "boards" / "default" / "kanban.db.bak").exists()


def test_events_tail_bounded(hermes_root):
    _seed_all_sources(hermes_root)
    out = json.loads(ev.hermes_events_tail(limit=2, hermes_root=hermes_root))
    assert out["count_returned"] <= 2
    assert out["truncated"] is True
