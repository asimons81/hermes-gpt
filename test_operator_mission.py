"""Tests for the Mission Control read-only surface (v0.6 M0).

Mirrors the existing ``test_operator_*.py`` style. All tests run against a temp
``hermes_root`` fixture — never production data. ``Path.home`` is patched to
the temp root so codex/action-items/vault default locations resolve inside the
fixture and the real home directory is never read.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import operator_policy as op
import operator_fleet as op_fleet
import operator_mission as mission

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, *, sessions: int = 2, delegations: list[dict] | None = None) -> None:
    """Create a minimal state.db with sessions / async_delegations / usage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " id TEXT PRIMARY KEY, source TEXT, model TEXT, profile_name TEXT,"
            " started_at TEXT, ended_at TEXT, last_activity_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS async_delegations ("
            " delegation_id TEXT PRIMARY KEY, origin_session TEXT, parent_session_id TEXT,"
            " state TEXT, dispatched_at TEXT, completed_at TEXT, task_json TEXT, result_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage ("
            " id INTEGER PRIMARY KEY, session_id TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, estimated_cost_usd REAL, cost_status TEXT)"
        )
        now = datetime.now(timezone.utc).isoformat()
        for i in range(sessions):
            conn.execute(
                "INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?,?)",
                (f"ses-{i}", "user", "model-x", "default",
                 (datetime.now(timezone.utc) - timedelta(hours=i * 2)).isoformat(),
                 None, (datetime.now(timezone.utc) - timedelta(hours=i * 2)).isoformat()),
            )
        conn.execute(
            "INSERT OR IGNORE INTO session_model_usage VALUES (1, 'ses-0', 100, 50, 0.05, 'known')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO session_model_usage VALUES (2, 'ses-1', 200, 100, 0.10, 'unknown')"
        )
        for d in delegations or []:
            conn.execute(
                "INSERT OR IGNORE INTO async_delegations VALUES (?,?,?,?,?,?,?,?)",
                (
                    d["delegation_id"],
                    d.get("origin_session"),
                    d.get("parent_session_id"),
                    d.get("state", "completed"),
                    d.get("dispatched_at"),
                    d.get("completed_at"),
                    d.get("task_json", "RAW TASK BODY SHOULD NEVER SURFACE"),
                    d.get("result_json", "RAW RESULT BODY SHOULD NEVER SURFACE"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_cron(home: Path, *, jobs: list[dict] | None = None, executions: list[tuple] | None = None) -> None:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs = jobs or []
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    (cron_dir / "ticker_heartbeat").write_text("tick", encoding="utf-8")
    conn = sqlite3.connect(cron_dir / "executions.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS executions ("
            " id INTEGER PRIMARY KEY, job_id TEXT, source TEXT, pid INTEGER,"
            " status TEXT, started_at TEXT, finished_at TEXT, error TEXT)"
        )
        for row in executions or []:
            conn.execute(
                "INSERT INTO executions (job_id, source, status, started_at, error) VALUES (?,?,?,?,?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _make_kanban_board(boards_dir: Path, slug: str, runs: list[dict]) -> None:
    board_dir = boards_dir / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(board_dir / "kanban.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_runs ("
            " id INTEGER PRIMARY KEY, task_id TEXT, assignee TEXT, status TEXT,"
            " outcome TEXT, error TEXT, body TEXT, metadata TEXT, started_at TEXT, ended_at TEXT)"
        )
        for r in runs:
            conn.execute(
                "INSERT INTO task_runs (task_id, assignee, status, outcome, error, body, metadata, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    r["task_id"],
                    r.get("assignee"),
                    r.get("status", "done"),
                    r.get("outcome"),
                    r.get("error"),
                    r.get("body", "RAW BODY SHOULD NEVER SURFACE"),
                    r.get("metadata", "RAW METADATA SHOULD NEVER SURFACE"),
                    r.get("started_at"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_vault(hermes_root: Path, *, present: bool = True) -> None:
    vdir = hermes_root / "hermes-vault-data"
    if not present:
        return
    vdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(vdir / "vault.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS credentials ("
            " id INTEGER PRIMARY KEY, service TEXT, alias TEXT, status TEXT, raw_secret TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS leases ("
            " id INTEGER PRIMARY KEY, service TEXT, expires_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS access_requests ("
            " id INTEGER PRIMARY KEY, status TEXT, secret TEXT, secret_sha256 TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS access_logs ("
            " id INTEGER PRIMARY KEY, decision TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO credentials (service, alias, status, raw_secret) VALUES (?,?,?,?)",
            [
                ("openai", None, "active", "sk-RAW-SECRET-SHOULD-NEVER-SURFACE"),
                ("github", None, "invalid", "ghp_RAW-SECRET-SHOULD-NEVER-SURFACE"),
            ],
        )
        conn.execute("INSERT INTO leases (service, expires_at) VALUES (?,?)", ("openai", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
        conn.execute("INSERT INTO access_requests (status, secret, created_at) VALUES ('pending', 'raw-request-secret', ?)", (datetime.now(timezone.utc).isoformat(),))
        conn.execute("INSERT INTO access_logs (decision) VALUES ('allow')")
        conn.execute("INSERT INTO access_logs (decision) VALUES ('deny')")
        conn.commit()
    finally:
        conn.close()


def _make_codex_job(hermes_root: Path, job_id: str = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4") -> None:
    codex_dir = hermes_root / "codex-jobs"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": "completed",
                "model": "gpt-4o",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "return_code": 0,
                "prompt": "RAW CODEX PROMPT SHOULD NEVER SURFACE",
            }
        ),
        encoding="utf-8",
    )
    (codex_dir / f"{job_id}.jsonl").write_text('{"type":"event","message":"RAW TRANSCRIPT SHOULD NEVER SURFACE"}\n', encoding="utf-8")


def _make_interrupted_turn(hermes_root: Path) -> None:
    d = hermes_root / "desktop"
    d.mkdir(parents=True, exist_ok=True)
    (d / "interrupted_turns.json").write_text(
        json.dumps(
            {
                "20260812_231621_5bb0e3": {
                    "attempts": 0,
                    "prompt": "RAW INTERRUPTED PROMPT SHOULD NEVER SURFACE",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )


def _make_errors_log(hermes_root: Path) -> None:
    logs = hermes_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "errors.log").write_text(
        "AuthenticationError: 401 PAID_MODEL_AUTH_REQUIRED\nAnotherError: transient\n",
        encoding="utf-8",
    )


def _audit_override(tmp_path: Path) -> Path:
    audit_path = tmp_path / "audit.jsonl"
    op.set_audit_log_override(audit_path)
    return audit_path


@pytest.fixture
def hermes_root(tmp_path: Path, monkeypatch) -> Path:
    """Build a hermetic Hermes root with realistic read-only sources."""
    # Patch Path.home to the fixture so codex / action-items / vault default
    # locations never touch the real home directory.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Never shell out to the real `hermes a2a registry list` in tests.
    monkeypatch.setattr(
        op_fleet,
        "hermes_fleet_list",
        lambda **kw: json.dumps({"success": True, "count": 1, "agents": [{"name": "rza", "has_token": True}]}),
    )
    _audit_override(tmp_path)
    mission._cache_clear()
    root = tmp_path / "hermes"
    root.mkdir(parents=True, exist_ok=True)

    # Default profile lives at the root (state.db, cron, gateway_state).
    (root / "config.yaml").write_text("model: test-model\nprovider: test-provider\n", encoding="utf-8")
    _make_state_db(
        root / "state.db",
        sessions=2,
        delegations=[
            {
                "delegation_id": "del-1",
                "origin_session": "ses-0",
                "state": "completed",
                "dispatched_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
                "completed_at": (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),
            },
            {
                "delegation_id": "del-2",
                "origin_session": "ses-1",
                "state": "running",
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            },
        ],
    )
    _make_cron(
        root,
        jobs=[
            {
                "id": "job-1",
                "name": "daily-briefing",
                "schedule_display": "0 9 * * *",
                "enabled": True,
                "last_status": "ok",
                "next_run_at": datetime.now(timezone.utc).isoformat(),
                "prompt": "RAW CRON PROMPT SHOULD NEVER SURFACE",
                "deliver": "telegram",
            }
        ],
        executions=[("job-1", "cron", "failed", datetime.now(timezone.utc).isoformat(), "cron exploded")],
    )
    (root / "gateway_state.json").write_text(
        json.dumps({"pid": 999999, "gateway_state": "running", "served_profiles": ["default", "dev"]}),
        encoding="utf-8",
    )

    # Named profile with its own state.db + cron.
    dev = root / "profiles" / "dev"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "config.yaml").write_text("model: dev-model\nprovider: dev-provider\n", encoding="utf-8")
    _make_state_db(
        dev / "state.db",
        sessions=1,
        delegations=[
            {"delegation_id": "del-3", "origin_session": "dev-ses", "state": "error",
             "dispatched_at": datetime.now(timezone.utc).isoformat()},
        ],
    )
    _make_cron(dev, jobs=[], executions=[])

    # Two kanban boards (G3 cross-board).
    boards = root / "kanban" / "boards"
    _make_kanban_board(
        boards, "board-a",
        [{"task_id": "t-1", "assignee": "hermes-dev", "status": "done", "outcome": "completed"}],
    )
    _make_kanban_board(
        boards, "board-b",
        [{"task_id": "t-2", "assignee": "legal", "status": "blocked", "outcome": "blocked", "error": "needs input"}],
    )

    _make_vault(root)
    _make_codex_job(root)
    _make_interrupted_turn(root)
    _make_errors_log(root)

    return root


def _run(tool: str, root: Path, **kwargs) -> dict:
    """Call a mission tool and parse its JSON envelope."""
    fn = getattr(mission, tool)
    out = fn(hermes_root=root, **kwargs)
    assert isinstance(out, str), f"{tool} should return a JSON string"
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. Envelope / contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, surface",
    [
        ("hermes_mission_health_tool", "health"),
        ("hermes_mission_cron_tool", "cron"),
        ("hermes_mission_fleet_tool", "fleet"),
        ("hermes_mission_audit_tool", "audit"),
        ("hermes_mission_profiles_tool", "profiles"),
        ("hermes_mission_delegations_tool", "delegations"),
        ("hermes_mission_failures_tool", "failures"),
        ("hermes_mission_approvals_tool", "approvals"),
        ("hermes_mission_codex_tool", "codex"),
        ("hermes_mission_vault_tool", "vault"),
        ("hermes_mission_usage_tool", "usage"),
    ],
)
def test_envelope_contract(hermes_root, tool, surface):
    out = _run(tool, hermes_root)
    assert out["schema_version"] == mission.SCHEMA_VERSION
    assert out["success"] is True
    assert out["surface"] == surface
    assert "generated_at" in out
    assert "trace_id" in out
    assert "counts" in out
    assert isinstance(out["data"], dict)
    # Bounded: envelope must fit well under the surface hard cap.
    size = len(json.dumps(out))
    assert size < mission.SURFACE_HARD_CAP_BYTES


def test_overview_contract(hermes_root):
    out = json.loads(mission.hermes_mission_overview_tool(hermes_root=hermes_root))
    assert out["schema_version"] == mission.SCHEMA_VERSION
    assert out["surface"] == "overview"
    assert out["tool"] == "hermes_mission_overview"
    # All required sections present.
    for section in ("fleet_health", "profiles", "fleet_agents", "codex", "cron",
                    "delegations", "failures", "pending_approvals", "vault", "usage"):
        assert section in out, f"overview missing section {section}"
    assert isinstance(out["surfaces_unavailable"], list)
    size = len(json.dumps(out))
    assert size < mission.OVERVIEW_HARD_CAP_BYTES


# ---------------------------------------------------------------------------
# 2. Redaction / no raw bodies
# ---------------------------------------------------------------------------


def test_no_raw_prompts_anywhere(hermes_root):
    """Prompts appear as prompt_len + prompt_sha256 only, never raw text."""
    cron = _run("hermes_mission_cron_tool", hermes_root)
    raw = json.dumps(cron)
    assert "RAW CRON PROMPT SHOULD NEVER SURFACE" not in raw
    assert "prompt_len" in raw
    assert "prompt_sha256" in raw

    approvals = _run("hermes_mission_approvals_tool", hermes_root)
    raw = json.dumps(approvals)
    assert "RAW INTERRUPTED PROMPT SHOULD NEVER SURFACE" not in raw


def test_no_async_delegation_bodies(hermes_root):
    out = _run("hermes_mission_delegations_tool", hermes_root)
    raw = json.dumps(out)
    assert "RAW TASK BODY SHOULD NEVER SURFACE" not in raw
    assert "RAW RESULT BODY SHOULD NEVER SURFACE" not in raw
    for d in out["data"]["delegations"]:
        assert "task_json" not in d
        assert "result_json" not in d
        assert "event_json" not in d


def test_kanban_body_and_metadata_stripped(hermes_root):
    out = _run("hermes_mission_delegations_tool", hermes_root)
    raw = json.dumps(out)
    assert "RAW BODY SHOULD NEVER SURFACE" not in raw
    assert "RAW METADATA SHOULD NEVER SURFACE" not in raw
    runs = out["data"]["kanban_runs"]
    assert len(runs) >= 2
    # Board slug normalized in the view model (G3).
    boards = {r["board"] for r in runs}
    assert "board-a" in boards and "board-b" in boards
    for r in runs:
        assert "body" not in r
        assert "metadata" not in r


def test_codex_transcripts_absent(hermes_root):
    out = _run("hermes_mission_codex_tool", hermes_root)
    raw = json.dumps(out)
    assert "RAW CODEX PROMPT SHOULD NEVER SURFACE" not in raw
    assert "RAW TRANSCRIPT SHOULD NEVER SURFACE" not in raw
    assert out["data"]["operator_store_present"] is True
    assert len(out["data"]["operator_jobs"]) == 1
    assert out["data"]["operator_jobs"][0]["status"] == "completed"


def test_vault_never_exposes_secrets(hermes_root):
    out = _run("hermes_mission_vault_tool", hermes_root)
    raw = json.dumps(out)
    assert "sk-RAW-SECRET-SHOULD-NEVER-SURFACE" not in raw
    assert "ghp_RAW-SECRET-SHOULD-NEVER-SURFACE" not in raw
    assert "raw_secret" not in raw
    assert "raw-request-secret" not in raw
    assert out["data"]["pending_requests"] == 1


# ---------------------------------------------------------------------------
# 3. Conditional availability (D9)
# ---------------------------------------------------------------------------


def test_codex_conditional_available_false_when_absent(hermes_root, tmp_path):
    # Remove the codex store so the surface reports unavailable + reason.
    import shutil

    shutil.rmtree(hermes_root / "codex-jobs", ignore_errors=True)
    out = _run("hermes_mission_codex_tool", hermes_root)
    assert out["available"] is False
    assert out["unavailable_reason"] is not None


def test_vault_conditional_available_false_when_absent(hermes_root):
    import shutil

    shutil.rmtree(hermes_root / "hermes-vault-data", ignore_errors=True)
    out = _run("hermes_mission_vault_tool", hermes_root)
    assert out["available"] is False
    assert out["unavailable_reason"] is not None


def test_fleet_authority_not_configured_is_state_not_error(hermes_root):
    out = _run("hermes_mission_fleet_tool", hermes_root)
    assert out["success"] is True
    assert out["data"]["authority"] in ("configured", "not_configured")
    # On the fixture there is no authority manifest -> not_configured.
    assert out["data"]["authority"] == "not_configured"


# ---------------------------------------------------------------------------
# 4. Read-only guarantee
# ---------------------------------------------------------------------------


def test_all_sqlite_opens_are_mode_ro(hermes_root, monkeypatch):
    """Every SQLite open uses file:...?mode=ro; never a write URI."""
    uris: list[str] = []
    real_connect = sqlite3.connect

    def capturing_connect(database, *args, **kwargs):
        if isinstance(database, str) and database.startswith("file:"):
            uris.append(database)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr("operator_mission.sqlite3.connect", capturing_connect)
    # Exercise surfaces that open SQLite.
    _run("hermes_mission_profiles_tool", hermes_root)
    _run("hermes_mission_delegations_tool", hermes_root)
    _run("hermes_mission_cron_tool", hermes_root)
    _run("hermes_mission_vault_tool", hermes_root)
    _run("hermes_mission_usage_tool", hermes_root)

    assert uris, "expected at least one SQLite open"
    for uri in uris:
        assert "?mode=ro" in uri, f"non-read-only SQLite URI: {uri}"
        assert "mode=rw" not in uri


def test_surface_does_not_modify_state(hermes_root):
    """Running mission surfaces must not create or mutate any source file."""
    before = {
        p.relative_to(hermes_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in hermes_root.rglob("*")
        if p.is_file()
    }
    for tool in (
        "hermes_mission_profiles_tool", "hermes_mission_cron_tool",
        "hermes_mission_fleet_tool", "hermes_mission_delegations_tool",
        "hermes_mission_failures_tool", "hermes_mission_approvals_tool",
        "hermes_mission_vault_tool", "hermes_mission_usage_tool",
        "hermes_mission_codex_tool",
    ):
        _run(tool, hermes_root)
    after = {
        p.relative_to(hermes_root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in hermes_root.rglob("*")
        if p.is_file()
    }
    # The audit log is written under tmp_path, not hermes_root, so the source
    # tree must be byte-identical.
    assert after == before


# ---------------------------------------------------------------------------
# 5. Cross-board aggregation (G3) + 6. per-profile (G7)
# ---------------------------------------------------------------------------


def test_cross_board_and_per_profile_aggregation(hermes_root):
    out = _run("hermes_mission_delegations_tool", hermes_root)
    # Delegations joined across default + dev profile.
    assert len(out["data"]["delegations"]) == 3  # del-1, del-2, del-3
    by_state = out["counts"]
    assert by_state["total"] == 3
    assert by_state["completed"] == 1  # del-1
    # Kanban runs from two boards, ids unique.
    runs = out["data"]["kanban_runs"]
    task_ids = [r["task_id"] for r in runs]
    assert "t-1" in task_ids and "t-2" in task_ids
    assert len(set(task_ids)) == len(task_ids)


def test_usage_aggregates_across_profiles(hermes_root):
    out = _run("hermes_mission_usage_tool", hermes_root)
    assert out["data"]["sessions_24h"] == 3  # 2 default + 1 dev
    assert set(out["data"]["by_profile"].keys()) == {"default", "dev"}
    # Each profile contributes 300 in / 150 out from the fixture usage rows.
    assert out["data"]["tokens_24h"]["input"] == 600
    assert out["data"]["tokens_24h"]["output"] == 300
    assert out["data"]["estimated_cost_24h_usd"] > 0


# ---------------------------------------------------------------------------
# 7. Overview composition
# ---------------------------------------------------------------------------


def test_overview_sections_present_and_bounded(hermes_root):
    out = json.loads(mission.hermes_mission_overview_tool(hermes_root=hermes_root))
    assert out["profiles"]["available"] is True
    assert out["cron"]["available"] is True
    assert out["delegations"]["available"] is True
    assert out["vault"]["available"] is True
    size = len(json.dumps(out))
    assert size < mission.OVERVIEW_HARD_CAP_BYTES


def test_overview_truncates_large_lists(hermes_root):
    # Add a board with many runs to force a large overview list.
    boards = hermes_root / "kanban" / "boards"
    _make_kanban_board(
        boards, "board-big",
        [{"task_id": f"big-{i}", "assignee": "x", "status": "done"} for i in range(500)],
    )
    out = json.loads(mission.hermes_mission_overview_tool(hermes_root=hermes_root))
    # Per design §6.2, truncation markers are per-section (in `delegations`),
    # not top-level: a large kanban_runs list is trimmed with truncated/count_total.
    delegations = out.get("delegations", {})
    assert delegations.get("truncated") is True or "count_total" in delegations


# ---------------------------------------------------------------------------
# 8. Cache
# ---------------------------------------------------------------------------


def test_cache_served_from_cache_and_force_refresh(hermes_root):
    first = json.loads(mission.hermes_mission_profiles_tool(hermes_root=hermes_root))
    assert first.get("served_from_cache") is None
    second = json.loads(mission.hermes_mission_profiles_tool(hermes_root=hermes_root))
    assert second.get("served_from_cache") is True
    assert "age_ms" in second
    forced = json.loads(mission.hermes_mission_profiles_tool(hermes_root=hermes_root, force_refresh=True))
    assert forced.get("served_from_cache") is None


# ---------------------------------------------------------------------------
# 9. Allowlist (deny-by-default)
# ---------------------------------------------------------------------------


def test_allowlist_denies_unlisted_surfaces(hermes_root, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "health")
    out = _run("hermes_mission_cron_tool", hermes_root)
    assert out["success"] is False
    assert out["code"] == "AUTHZ_DENIED"
    assert out["available"] is False

    # Allowed surface still works.
    ok = _run("hermes_mission_health_tool", hermes_root)
    assert ok["success"] is True
    assert ok["available"] is True


def test_allowlist_empty_denies_all(hermes_root, monkeypatch):
    monkeypatch.setenv("HERMES_GPT_MISSION_ALLOWED_SURFACES", "")
    out = _run("hermes_mission_profiles_tool", hermes_root)
    assert out["code"] == "AUTHZ_DENIED"


def test_allowlist_default_allows_all(hermes_root):
    out = _run("hermes_mission_cron_tool", hermes_root)
    assert out["success"] is True


# ---------------------------------------------------------------------------
# 10. Audit
# ---------------------------------------------------------------------------


def test_each_mission_call_is_audited(hermes_root):
    audit_path = op.audit_log_path()
    _run("hermes_mission_cron_tool", hermes_root)
    _run("hermes_mission_profiles_tool", hermes_root)
    _run("hermes_mission_vault_tool", hermes_root)

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    tools = [r["tool"] for r in records]
    assert "hermes_mission_cron" in tools
    assert "hermes_mission_profiles" in tools
    assert "hermes_mission_vault" in tools
    for rec in records:
        assert rec["dry_run"] is True
        assert rec["changed"] is False


# ---------------------------------------------------------------------------
# Error / failure surface bounds
# ---------------------------------------------------------------------------


def test_failures_bounded_and_sanitized(hermes_root):
    out = _run("hermes_mission_failures_tool", hermes_root)
    assert out["counts"]["recent_error_count"] >= 1
    for err in out["data"]["recent_errors"]:
        assert "message" in err
    assert out["data"]["by_source"]["errors.log"] >= 1


def test_free_text_pii_strip_removes_contact_handles_and_name_patterns():
    raw = "Name: Jane Doe email jane.doe@example.com phone +1 (555) 123-4567 @janedoe"
    safe = mission._sanitize_error(raw)
    assert "Jane Doe" not in safe
    assert "jane.doe@example.com" not in safe
    assert "555" not in safe
    assert "@janedoe" not in safe
    assert "[redacted-email]" in safe
    assert "[redacted-phone]" in safe
    assert "[redacted-username]" in safe
    assert "[redacted-name]" in safe
