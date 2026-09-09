from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import pytest

import oauth_auth
import operator_controller as controller
import operator_delegations as deleg
import operator_mission_plan as plan
import operator_policy as op
import token_store


def _oauth_config() -> oauth_auth.OAuthConfig:
    return oauth_auth.OAuthConfig(
        issuer="https://example.test",
        client_id="codex-remediation-client",
        client_secret="x" * 48,
        redirect_uris=("https://example.test/callback",),
    )


def test_signed_access_token_rejected_after_durable_revocation(tmp_path: Path):
    root = tmp_path / "hermes"
    config = _oauth_config()
    issuer = oauth_auth.OAuthState(config)
    issuer.restore_tokens(root)
    token, item = issuer._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    issuer.access_tokens[token] = item
    issuer.persist_tokens(root)

    peer = oauth_auth.OAuthState(config)
    peer.restore_tokens(root)
    assert peer.validate_access_token(token) is True

    token_store.revoke_tokens(root, rotate_key=False)
    assert peer.validate_access_token(token) is False


def _policy(monkeypatch, level: str, apply_mode: str = "direct") -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, level)
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, apply_mode)


def test_controller_reconcile_read_only_cannot_persist(monkeypatch, tmp_path: Path):
    _policy(monkeypatch, "read_only")
    called = False

    def fake_reconcile(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(controller, "reconcile_pass", fake_reconcile)
    out = json.loads(
        controller.hermes_controller_reconcile(
            "msn-codex", dry_run=False, hermes_root=tmp_path
        )
    )
    assert out["success"] is False
    assert called is False


def test_controller_trigger_read_only_cannot_enqueue(monkeypatch, tmp_path: Path):
    _policy(monkeypatch, "read_only")
    called = False

    def fake_trigger(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True, "seq": 1}

    monkeypatch.setattr(controller, "trigger", fake_trigger)
    out = json.loads(
        controller.hermes_controller_trigger(
            "msn-codex", controller.TRIGGER_MANUAL, hermes_root=tmp_path
        )
    )
    assert out["success"] is False
    assert called is False


def test_plan_ready_set_requires_successful_parent_completion():
    nodes = [
        {"node_id": "parent", "state": "failed", "parents": []},
        {"node_id": "child", "state": "pending", "parents": ["parent"]},
        {"node_id": "orphan", "state": "pending", "parents": ["missing"]},
    ]
    assert plan._ready_node_ids(nodes) == []
    nodes[0]["state"] = "completed"
    assert plan._ready_node_ids(nodes) == ["child"]


def test_frontier_delegation_lookup_binds_contract(tmp_path: Path):
    root = tmp_path / "hermes"
    dbp = deleg._db_path(root)
    with deleg._connect(dbp, write=True) as db:
        deleg._init(db)
        rows = [
            ("dlg-a", "task-a", "a" * 64, "2026-09-08T01:00:00+00:00"),
            ("dlg-b", "task-b", "b" * 64, "2026-09-08T02:00:00+00:00"),
        ]
        for did, task, contract_sha, updated in rows:
            db.execute(
                "INSERT INTO delegations (delegation_id,schema,mission_id,task_id,contract_sha256,backend,state,created_at,dispatched_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    deleg.DELEGATION_SCHEMA,
                    "msn-parallel",
                    task,
                    contract_sha,
                    "codex",
                    "running",
                    updated,
                    updated,
                    updated,
                ),
            )
        db.commit()

    observed = controller._latest_delegation(root, "msn-parallel", "a" * 64)
    assert observed is not None
    assert observed["delegation_id"] == "dlg-a"
    assert observed["contract_sha256"] == "a" * 64


def test_pyyaml_is_a_runtime_dependency():
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime = [str(dep).lower() for dep in data["project"]["dependencies"]]
    assert any(dep == "pyyaml" or dep.startswith("pyyaml") for dep in runtime)


# ---------------------------------------------------------------------------
# Independent-review regressions (follow-up findings on the remediation itself)
# ---------------------------------------------------------------------------


def test_refresh_grant_rejected_after_durable_revocation(tmp_path: Path):
    """A refresh token held in memory must not outlive durable revocation."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    issuer = oauth_auth.OAuthState(config)
    issuer.restore_tokens(root)  # binds the durable root (server mode)
    refresh, item = issuer._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    issuer.refresh_tokens[refresh] = item
    issuer.persist_tokens(root)

    peer = oauth_auth.OAuthState(config)
    peer.restore_tokens(root)
    assert peer.validate_refresh_token_grant(refresh, config.client_id)["client_id"] == config.client_id

    token_store.revoke_tokens(root, rotate_key=False)
    with pytest.raises(oauth_auth.OAuthError) as excinfo:
        peer.validate_refresh_token_grant(refresh, config.client_id)
    assert excinfo.value.error == "invalid_grant"
    # The stale in-memory copy is dropped, not just rejected once.
    assert refresh not in peer.refresh_tokens


def test_revocation_hook_clears_live_state_and_prevents_resurrection(tmp_path: Path):
    """After revoke, the live caches are dropped AND any later persist is
    refused by revocation-epoch fencing (never re-writes old tokens)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    refresh, ritem = state._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    state.refresh_tokens[refresh] = ritem
    state.persist_tokens(root)

    token_store.revoke_tokens(root, rotate_key=False)
    # server.py wires the hook to clear_live_tokens; simulate that wiring:
    oauth_auth.set_revocation_hook(state.clear_live_tokens)
    try:
        oauth_auth.run_revocation_hook()
        assert token not in state.access_tokens
        assert refresh not in state.refresh_tokens
        # A later persist (e.g. triggered by a fresh issuance hook) is refused
        # outright by epoch fencing — the envelope stays empty, never
        # repopulated with pre-revocation tokens.
        with pytest.raises(token_store.TokenStoreError):
            state.persist_tokens(root)
        bundle = token_store.load_tokens(root)
        assert token not in (bundle.get("access_tokens") or {})
        assert refresh not in (bundle.get("refresh_tokens") or {})
    finally:
        oauth_auth.set_revocation_hook(None)


def test_controller_reconcile_dry_run_apply_mode_is_rejected(monkeypatch, tmp_path: Path):
    """The PERSISTING pass (dry_run=False) must require direct apply mode."""
    _policy(monkeypatch, "workspace", apply_mode="dry_run")
    called = False

    def fake_reconcile(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(controller, "reconcile_pass", fake_reconcile)
    out = json.loads(
        controller.hermes_controller_reconcile(
            "msn-codex", dry_run=False, hermes_root=tmp_path
        )
    )
    assert out["success"] is False
    assert called is False
    assert "direct" in json.dumps(out)


def test_controller_reconcile_preview_writes_nothing(monkeypatch, tmp_path: Path):
    """dry_run=True is a truthful preview: full envelope, zero durable writes."""
    import sqlite3

    import operator_mission_runtime as mission

    _policy(monkeypatch, "read_only", apply_mode="dry_run")
    monkeypatch.setattr(
        controller, "reconcile_pass",
        lambda *a, **k: pytest.fail("dry_run=True must not invoke the persisting pass"),
    )
    root = tmp_path / "hermes"
    mid = "msn-preview"
    conn = mission._connect(mission._db_path(root), write=True)
    conn.execute(
        "INSERT INTO missions (mission_id, spec_json, status, version, approval_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (mid, "{}", "running", 1, "{}", "2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    plan_conn = controller.plan._connect(controller.plan._db_path(root), write=True)
    plan_conn.close()

    out = json.loads(
        controller.hermes_controller_reconcile(mid, dry_run=True, hermes_root=root)
    )
    assert out.get("preview") is True
    assert out["dry_run"] is True
    assert out["changed"] is False
    assert out["would_execute"] is False
    assert "classification" in out and "row_key" in out
    # No durable controller state was created by the preview.
    assert not (root / "missions" / "controller_heartbeat.json").exists()
    db = sqlite3.connect(mission._db_path(root))
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "controller_telemetry" not in tables, "preview must not create controller tables"
    db.close()


def test_controller_trigger_dry_run_apply_mode_is_rejected(monkeypatch, tmp_path: Path):
    _policy(monkeypatch, "workspace", apply_mode="dry_run")
    called = False

    def fake_trigger(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True, "seq": 1}

    monkeypatch.setattr(controller, "trigger", fake_trigger)
    out = json.loads(
        controller.hermes_controller_trigger(
            "msn-codex", controller.TRIGGER_MANUAL, hermes_root=tmp_path
        )
    )
    assert out["success"] is False
    assert called is False


def test_controller_gates_pass_in_direct_mode(monkeypatch, tmp_path: Path):
    """The tightened gates must still admit the legitimate direct-mode path."""
    _policy(monkeypatch, "workspace", apply_mode="direct")
    monkeypatch.setattr(
        controller, "reconcile_pass", lambda *a, **k: {"success": True}
    )
    out = json.loads(
        controller.hermes_controller_reconcile(
            "msn-codex", dry_run=False, hermes_root=tmp_path
        )
    )
    assert out["success"] is True
    monkeypatch.setattr(controller, "trigger", lambda *a, **k: {"success": True, "seq": 1})
    out2 = json.loads(
        controller.hermes_controller_trigger(
            "msn-codex", controller.TRIGGER_MANUAL, hermes_root=tmp_path
        )
    )
    assert out2["success"] is True


# ---------------------------------------------------------------------------
# Round-2 Codex review regressions (clustered OAuth + hostile cursors)
# ---------------------------------------------------------------------------


def test_peer_cannot_resurrect_revoked_tokens_after_revocation(tmp_path: Path):
    """A clustered peer holding pre-revocation tokens must not be able to
    re-persist them over a durable revocation (epoch fencing)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    issuer = oauth_auth.OAuthState(config)
    issuer.restore_tokens(root)
    token, item = issuer._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    issuer.access_tokens[token] = item
    issuer.persist_tokens(root)
    assert token in token_store.load_tokens(root)["access_tokens"]

    peer = oauth_auth.OAuthState(config)
    peer.restore_tokens(root)  # peer view built BEFORE revocation

    token_store.revoke_tokens(root, rotate_key=False)
    with pytest.raises(token_store.TokenStoreError):
        peer.persist_tokens(root)
    # Nothing was resurrected durably.
    assert token_store.load_tokens(root) == {}


def test_revocation_rotates_authorization_code_key(tmp_path: Path):
    """clear_live_tokens must retain used-code replay state (so an
    already-exchanged code cannot be replayed) and rotate the signing key
    (so outstanding unexchanged codes die too)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    code = state.issue_authorization_code(
        client_id=config.client_id,
        redirect_uri=config.redirect_uris[0],
        scope=config.scope,
        resource=config.resource,
        code_challenge="",
    )
    first = state.exchange_authorization_code(
        code=code,
        client_id=config.client_id,
        redirect_uri=config.redirect_uris[0],
        code_verifier="",
    )
    assert first["access_token"]

    token_store.revoke_tokens(root, rotate_key=False)
    state.clear_live_tokens()
    # (a) the already-exchanged code cannot be replayed
    with pytest.raises(oauth_auth.OAuthError) as replay_exc:
        state.exchange_authorization_code(
            code=code,
            client_id=config.client_id,
            redirect_uri=config.redirect_uris[0],
            code_verifier="",
        )
    assert replay_exc.value.error == "invalid_grant"
    # (b) a fresh code minted before revocation is dead after key rotation
    old_key = state._authorization_code_key
    state.clear_live_tokens()
    assert state._authorization_code_key != old_key


def test_persist_merges_instead_of_replacing_peer_tokens(tmp_path: Path):
    """Peer B's issuance must not evict peer A's still-valid durable token."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    a = oauth_auth.OAuthState(config)
    a.restore_tokens(root)
    b = oauth_auth.OAuthState(config)
    b.restore_tokens(root)

    ta, ia = a._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    a.access_tokens[ta] = ia
    a.persist_tokens(root)

    tb, ib = b._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    b.access_tokens[tb] = ib
    b.persist_tokens(root)

    bundle = token_store.load_tokens(root)
    assert ta in bundle["access_tokens"], "peer A's token was dropped by B's persist"
    assert tb in bundle["access_tokens"]


def test_rotated_refresh_token_is_durably_retired(tmp_path: Path):
    """Refresh rotation must remove the consumed token from the shared
    envelope, or it stays replayable after a restart."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    # server.py installs this hook; simulate the production wiring so the
    # exchange's persist (and rotation retirement) actually lands durably.
    oauth_auth.set_persist_hook(lambda s, kind: s.persist_tokens(root))
    try:
        refresh, ritem = state._new_refresh_token(
            client_id=config.client_id, scope=config.scope
        )
        state.refresh_tokens[refresh] = ritem
        state.persist_tokens(root)

        resp = state.exchange_refresh_token(
            refresh_token=refresh,
            client_id=config.client_id,
            requested_scope="",
        )
        assert resp["refresh_token"] != refresh
        bundle = token_store.load_tokens(root)
        assert refresh not in bundle["refresh_tokens"], "consumed refresh token stayed durable"
        assert resp["refresh_token"] in bundle["refresh_tokens"]
    finally:
        oauth_auth.set_persist_hook(None)


def test_hostile_ledger_cursors_fail_closed(tmp_path: Path):
    """Malformed cursor tokens must produce the invalid-cursor envelope,
    never an uncaught OverflowError/RecursionError."""
    import base64

    import operator_mission_ledger as ld
    import operator_mission_runtime as mission

    def cursor_for(obj) -> str:
        return "ld1." + base64.urlsafe_b64encode(
            json.dumps(obj).encode()
        ).rstrip(b"=").decode()

    root = tmp_path / "hermes"
    root.mkdir(parents=True)
    mid = "msn-cur"
    conn = mission._connect(mission._db_path(root), write=True)
    conn.execute(
        "INSERT INTO missions (mission_id, spec_json, status, version, approval_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (mid, "{}", "running", 1, "{}", "2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # 2**63: passes JSON validation, would overflow SQLite INTEGER binding.
    big = cursor_for({"v": 1, "w": {"mission": 2**63}})
    with pytest.raises(ValueError):
        ld._decode_cursor(big)
    out = json.loads(ld.hermes_mission_ledger(mid, cursor=big, hermes_root=root))
    assert out["success"] is True
    assert any("invalid" in w for w in out["warnings"])
    assert out["count_returned"] == 0

    # boolean seq (isinstance(True, int) is True — must be rejected explicitly)
    with pytest.raises(ValueError):
        ld._decode_cursor(cursor_for({"v": 1, "w": {"mission": True}}))

    # deep nesting within the token byte budget
    deep = "ld1." + base64.urlsafe_b64encode(
        b'{"v":1,"w":{"mission":' + b"[" * 2000 + b"]" * 2000 + b"}}"
    ).rstrip(b"=").decode()
    with pytest.raises(ValueError):
        ld._decode_cursor(deep)
    out2 = json.loads(ld.hermes_mission_ledger(mid, cursor=deep, hermes_root=root))
    assert out2["success"] is True
    assert any("invalid" in w for w in out2["warnings"])
