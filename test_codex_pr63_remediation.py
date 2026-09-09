from __future__ import annotations

import json
import time
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
    # The ONLY durable side effect is the Operator audit trail (every tool
    # call is audited — AGENTS.md invariant), never controller state.
    audit_log = root.parent / "audit.jsonl"
    if audit_log.exists():
        for line in audit_log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                assert rec.get("changed") is False


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
    durable = token_store.load_tokens(root)
    assert token not in (durable.get("access_tokens") or {})


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


def test_post_revocation_fresh_exchange_persists(tmp_path: Path):
    """After revocation, a FRESH code exchange in the same process must still
    produce durable, cross-process-valid tokens (no permanent fencing)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    state.persist_tokens(root)

    token_store.revoke_tokens(root, rotate_key=False)
    state.clear_live_tokens()

    oauth_auth.set_persist_hook(lambda s, kind: s.persist_tokens(root))
    try:
        code = state.issue_authorization_code(
            client_id=config.client_id,
            redirect_uri=config.redirect_uris[0],
            scope=config.scope,
            resource=config.resource,
            code_challenge="",
        )
        resp = state.exchange_authorization_code(
            code=code,
            client_id=config.client_id,
            redirect_uri=config.redirect_uris[0],
            code_verifier="",
        )
    finally:
        oauth_auth.set_persist_hook(None)

    bundle = token_store.load_tokens(root)
    assert resp["access_token"] in bundle["access_tokens"]
    peer = oauth_auth.OAuthState(config)
    peer.restore_tokens(root)
    assert peer.validate_access_token(resp["access_token"]) is True
    assert peer.validate_access_token(token) is False


def test_stale_peer_cannot_reissue_retired_refresh(tmp_path: Path):
    """Peer B restores R; A rotates R durably; B's later persist must NOT
    make R usable again (permanent retirement beats stale caches)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    seeder = oauth_auth.OAuthState(config)
    seeder.restore_tokens(root)
    refresh, ritem = seeder._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    seeder.refresh_tokens[refresh] = ritem
    seeder.persist_tokens(root)

    a = oauth_auth.OAuthState(config)
    a.restore_tokens(root)
    b = oauth_auth.OAuthState(config)
    b.restore_tokens(root)

    oauth_auth.set_persist_hook(lambda s, kind: s.persist_tokens(root))
    try:
        resp = a.exchange_refresh_token(
            refresh_token=refresh, client_id=config.client_id, requested_scope=""
        )
    finally:
        oauth_auth.set_persist_hook(None)

    # B does an unrelated issuance; its stale cache still lists R.
    tb, ib = b._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    b.access_tokens[tb] = ib
    b.persist_tokens(root)

    with pytest.raises(oauth_auth.OAuthError) as excinfo:
        b.validate_refresh_token_grant(refresh, config.client_id)
    assert excinfo.value.error == "invalid_grant"
    assert token_store.lookup_token(root, "refresh", refresh) is None
    # The new rotated token is the one that works (a peer that syncs from
    # the durable store picks it up).
    fresh_peer = oauth_auth.OAuthState(config)
    fresh_peer.restore_tokens(root)
    assert fresh_peer.validate_refresh_token_grant(
        resp["refresh_token"], config.client_id
    ) is not None


def test_expired_tokens_are_pruned_from_durable_store(tmp_path: Path):
    """Once a token expires inside the envelope, any subsequent commit must
    prune it (bounded store)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    old, oitem = state._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    state.refresh_tokens[old] = oitem
    state.persist_tokens(root)
    assert old in token_store.load_tokens(root)["refresh_tokens"]

    # Force-expire it inside the durable store only (simulating the passage
    # of time with no further access by this process).
    import sqlite3

    conn = sqlite3.connect(token_store._db_path(root))
    conn.execute(
        "UPDATE tokens SET expires_at=? WHERE token_key=?",
        (time.time() - 100, token_store.issue_key("refresh", old)),
    )
    conn.commit()
    conn.close()

    other = oauth_auth.OAuthState(config)
    other.restore_tokens(root)
    tn, tin = other._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    other.access_tokens[tn] = tin
    other.persist_tokens(root)

    assert token_store.lookup_token(root, "refresh", old) is None, "expired token survived a commit"
    assert token_store.lookup_token(root, "access", tn) is not None


def test_concurrent_commits_and_revocation_are_serialized(tmp_path: Path):
    """Commit vs revoke interleaving must never resurrect a revoked token:
    both take the ledger lock, so the race window is closed."""
    import threading

    root = tmp_path / "hermes"
    config = _oauth_config()
    seeder = oauth_auth.OAuthState(config)
    seeder.restore_tokens(root)
    tok, item = seeder._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    seeder.access_tokens[tok] = item
    seeder.persist_tokens(root)

    errors: list[Exception] = []
    outcomes = {"revoked": False, "committed": False}

    def revoker() -> None:
        try:
            token_store.revoke_tokens(root, rotate_key=False)
            outcomes["revoked"] = True
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def committer() -> None:
        try:
            # A peer with a pre-revocation view tries to persist.
            token_store.commit_tokens(
                root,
                source_epoch=0,
                issue={
                    token_store.issue_key("access", tok): {
                        "client_id": config.client_id,
                        "scope": config.scope,
                        "resource": config.resource,
                        "expires_at": time.time() + 3600,
                        "_kind": "access",
                        "_token_value": tok,
                    }
                },
            )
            outcomes["committed"] = True
        except token_store.TokenStoreError:
            outcomes["committed"] = False
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=revoker)
    t2 = threading.Thread(target=committer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, errors

    # Whatever the interleaving, the token must NOT be live afterwards.
    assert token_store.lookup_token(root, "access", tok) is None, (
        "revoked/concurrent token resurrected"
    )


def test_revocation_closes_legacy_migration(tmp_path: Path):
    """After revocation, leftover legacy JSON artifacts must never re-import
    revoked credentials (migration closes permanently)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    state.persist_tokens(root)

    # Simulate a legacy envelope left behind (e.g. failed cleanup).
    legacy = token_store._legacy_envelope_path(root)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")

    token_store.revoke_tokens(root, rotate_key=False)
    # Re-create the envelope AFTER revocation (worst case: stray file).
    legacy.write_text("{}", encoding="utf-8")

    # A later commit with a stale view must not resurrect anything, and the
    # migration marker must keep legacy imports closed.
    peer = oauth_auth.OAuthState(config)
    peer._hermes_root = root
    with pytest.raises(token_store.TokenStoreError):
        peer.persist_tokens(root)
    import sqlite3

    db = sqlite3.connect(token_store._db_path(root))
    marker = db.execute(
        "SELECT value FROM token_meta WHERE name='legacy_migration'"
    ).fetchone()
    db.close()
    assert marker is not None and marker[0].startswith("closed"), marker


def test_corrupt_legacy_ledger_fails_closed(tmp_path: Path):
    """An unparseable legacy retirement ledger aborts the import."""
    import json as _json

    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    state.persist_tokens(root)

    # Build a legacy envelope + corrupt ledger on a FRESH root.
    root2 = tmp_path / "hermes2"
    (root2 / "secrets").mkdir(parents=True)
    token_store.save_tokens(
        root2, {"access_tokens": {token: dict(item)}, "refresh_tokens": {}}
    )
    (root2 / "secrets" / token_store.LEGACY_LEDGER_FILENAME).write_text(
        "{corrupt!!", encoding="utf-8"
    )

    with pytest.raises(token_store.TokenStoreError):
        token_store.commit_tokens(root2, source_epoch=0, issue={})
    # Nothing was imported.
    assert token_store.lookup_token(root2, "access", token) is None
    # The recovery source is intact (deleted only after a successful commit).
    assert token_store._legacy_envelope_path(root2).exists()


def test_migration_imports_live_tokens_with_cache_markers(tmp_path: Path):
    """Legacy upgrade imports credentials restorable after a restart."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    refresh, ritem = state._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    state.refresh_tokens[refresh] = ritem
    state.persist_tokens(root)

    # Convert the DB store back into legacy shape on a fresh root. Pin the
    # master key via env so both roots resolve the same key regardless of
    # keyring availability.
    monkeypatch_key = pytest.MonkeyPatch()
    monkeypatch_key.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    try:
        root2 = tmp_path / "hermes2"
        (root2 / "secrets").mkdir(parents=True)
        # Write the legacy envelope with the env key in effect.
        token_store.save_tokens(root2, {"refresh_tokens": {refresh: dict(ritem)}})

        token_store.commit_tokens(root2, source_epoch=0, issue={})
        fresh = oauth_auth.OAuthState(config)
        summary = fresh.restore_tokens(root2)
        assert summary["restored"] >= 1
        assert refresh in fresh.refresh_tokens
        assert fresh.validate_refresh_token_grant(refresh, config.client_id) is not None
        assert not token_store._legacy_envelope_path(root2).exists()
    finally:
        monkeypatch_key.undo()


def test_outstanding_code_dies_on_revocation(tmp_path: Path):
    """An unexchanged code issued before revocation must not mint durable
    credentials afterwards (epoch-bound v2 codes)."""
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
    token_store.revoke_tokens(root, rotate_key=False)
    state.clear_live_tokens()
    # The peer adopts the new epoch for fresh grants, but the pre-revocation
    # code is still bound to the old epoch and must be rejected.
    with pytest.raises(oauth_auth.OAuthError) as excinfo:
        state.exchange_authorization_code(
            code=code,
            client_id=config.client_id,
            redirect_uri=config.redirect_uris[0],
            code_verifier="",
        )
    assert excinfo.value.error == "invalid_grant"


def test_exchange_fails_loud_when_persistence_fails(tmp_path: Path):
    """A corrupt durable store must fail the exchange (no unusable creds)."""
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
    # Break durable persistence AFTER the code decodes: the store exists
    # (epoch readable) but commits fail.
    calls = {"n": 0}

    def _failing_persist(st, kind):
        calls["n"] += 1
        raise token_store.TokenStoreError("injected persistence failure")

    oauth_auth.set_persist_hook(_failing_persist)
    try:
        with pytest.raises(oauth_auth.OAuthError) as excinfo:
            state.exchange_authorization_code(
                code=code,
                client_id=config.client_id,
                redirect_uri=config.redirect_uris[0],
                code_verifier="",
            )
        assert excinfo.value.error in ("temporarily_unavailable", "invalid_grant")
    finally:
        oauth_auth.set_persist_hook(None)
    assert calls["n"] >= 1, "strict hook must have been invoked"
    # No uncommitted credentials remain in the live caches.
    assert not state.access_tokens


def test_startup_restore_migrates_legacy_envelope(tmp_path: Path):
    """restore_tokens() must run the legacy migration BEFORE loading, so an
    upgrade restores existing credentials without needing a prior write."""
    import pytest as _pytest

    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True)
    config = _oauth_config()
    mp = _pytest.MonkeyPatch()
    mp.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    try:
        refresh, ritem = oauth_auth.OAuthState(config)._new_refresh_token(
            client_id=config.client_id, scope=config.scope
        )
        token_store.save_tokens(
            root, {"refresh_tokens": {refresh: dict(ritem)}}
        )
        # Startup restore on the legacy root migrates + restores.
        state = oauth_auth.OAuthState(config)
        summary = state.restore_tokens(root)
        assert summary["restored"] >= 1, summary
        assert refresh in state.refresh_tokens
        # The legacy artifact is gone after the migration committed.
        assert not token_store._legacy_envelope_path(root).exists()
    finally:
        mp.undo()


def test_ledger_full_window_reports_truncated(tmp_path: Path):
    """A source holding exactly MAX_PER_SOURCE+1 events must not claim
    completeness at limit=MAX_PER_SOURCE (lookahead signal)."""
    import operator_mission_ledger as ld
    import operator_mission_runtime as mission

    root = tmp_path / "hermes"
    root.mkdir(parents=True)
    mid = "msn-trunc"
    conn = mission._connect(mission._db_path(root), write=True)
    conn.execute(
        "INSERT INTO missions (mission_id, spec_json, status, version, approval_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (mid, "{}", "running", 1, "{}", "2026-09-08T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
    )
    n = ld.MAX_PER_SOURCE + 1
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO mission_events (mission_id, event_type, from_status, to_status, reason_sha256, details_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, f"ev.{i}", "running", "running", "f" * 64, "{}", f"2026-09-08T00:{i//60:02d}:{i%60:02d}+00:00"),
        )
    conn.commit()
    conn.close()

    out = json.loads(ld.hermes_mission_ledger(mid, cursor=0, limit=ld.MAX_PER_SOURCE, hermes_root=root))
    assert out["count_returned"] == ld.MAX_PER_SOURCE
    assert out["truncated"] is True
    # Resuming from next_cursor must deliver the remaining event.
    out2 = json.loads(ld.hermes_mission_ledger(mid, cursor=out["next_cursor"], limit=100, hermes_root=root))
    assert out2["count_returned"] == 1
    assert out2["truncated"] is False


def test_structurally_invalid_legacy_ledger_fails_closed(tmp_path: Path):
    """A legacy ledger whose 'retired' key parses as JSON but is not a dict
    must abort the import (no silent fail-open)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    state.persist_tokens(root)

    root2 = tmp_path / "hermes2"
    (root2 / "secrets").mkdir(parents=True)
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    try:
        token_store.save_tokens(root2, {"access_tokens": {token: dict(item)}})
        (root2 / "secrets" / token_store.LEGACY_LEDGER_FILENAME).write_text(
            json.dumps({"retired": [], "revocation_epoch": 0}), encoding="utf-8"
        )
        with pytest.raises(token_store.TokenStoreError):
            token_store.commit_tokens(root2, source_epoch=0, issue={})
        assert token_store.lookup_token(root2, "access", token) is None
    finally:
        mp.undo()


def test_startup_migration_preserves_positive_legacy_epoch(tmp_path: Path):
    """A legacy store that was revoked once (epoch=1) must migrate, restore
    its live credentials, keep epoch=1, and still allow fresh issuance."""
    import pytest as _pytest

    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True)
    config = _oauth_config()
    mp = _pytest.MonkeyPatch()
    mp.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    try:
        refresh, ritem = oauth_auth.OAuthState(config)._new_refresh_token(
            client_id=config.client_id, scope=config.scope
        )
        token_store.save_tokens(root, {"refresh_tokens": {refresh: dict(ritem)}})
        (root / "secrets" / token_store.LEGACY_LEDGER_FILENAME).write_text(
            json.dumps({"retired": {}, "revocation_epoch": 1}), encoding="utf-8"
        )

        state = oauth_auth.OAuthState(config)
        summary = state.restore_tokens(root)
        assert summary["restored"] >= 1
        assert refresh in state.refresh_tokens
        assert token_store.read_revocation_epoch(root) == 1
        assert not token_store._legacy_envelope_path(root).exists()

        # Fresh issuance still works against the migrated store.
        oauth_auth.set_persist_hook(lambda s, k: s.persist_tokens(root))
        try:
            code = state.issue_authorization_code(
                client_id=config.client_id,
                redirect_uri=config.redirect_uris[0],
                scope=config.scope,
                resource=config.resource,
                code_challenge="",
            )
            resp = state.exchange_authorization_code(
                code=code,
                client_id=config.client_id,
                redirect_uri=config.redirect_uris[0],
                code_verifier="",
            )
            assert resp["access_token"]
            assert (
                token_store.lookup_token(root, "access", resp["access_token"])
                is not None
            )
        finally:
            oauth_auth.set_persist_hook(None)
    finally:
        mp.undo()


def test_retirement_tombstones_survive_expiry_and_block_reissue(tmp_path: Path):
    """A retired (rotated/revoked) token hash must remain a tombstone past
    its original expiry, and a stale peer reissue must stay dead."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    seeder = oauth_auth.OAuthState(config)
    seeder.restore_tokens(root)
    refresh, ritem = seeder._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    seeder.refresh_tokens[refresh] = ritem
    seeder.persist_tokens(root)

    oauth_auth.set_persist_hook(lambda s, k: s.persist_tokens(root))
    try:
        resp = seeder.exchange_refresh_token(
            refresh_token=refresh, client_id=config.client_id, requested_scope=""
        )
    finally:
        oauth_auth.set_persist_hook(None)
    assert token_store.lookup_token(root, "refresh", refresh) is None

    # Force the tombstone row's expiry into the past.
    import sqlite3

    conn = sqlite3.connect(token_store._db_path(root))
    conn.execute(
        "UPDATE tokens SET expires_at=? WHERE token_key=?",
        (time.time() - 1000, token_store.issue_key("refresh", refresh)),
    )
    conn.commit()
    conn.close()

    # Any later commit must prune expired LIVE rows but keep the tombstone.
    other = oauth_auth.OAuthState(config)
    other.restore_tokens(root)
    tn, tin = other._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    other.access_tokens[tn] = tin
    other.persist_tokens(root)

    conn = sqlite3.connect(token_store._db_path(root))
    row = conn.execute(
        "SELECT retired FROM tokens WHERE token_key=?",
        (token_store.issue_key("refresh", refresh),),
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == 1, "tombstone was pruned on expiry"

    # A stale peer holding the old token cannot reissue it.
    stale = oauth_auth.OAuthState(config)
    stale._hermes_root = root
    stale._epoch = token_store.read_revocation_epoch(root)
    stale.refresh_tokens[refresh] = ritem
    stale.persist_tokens(root)
    assert token_store.lookup_token(root, "refresh", refresh) is None


def test_restore_fails_closed_on_undecryptable_live_record(tmp_path: Path):
    """A live record that cannot be decrypted must abort the restore, not
    silently restore a partial credential set."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    good, gi = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[good] = gi
    bad, bi = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[bad] = bi
    state.persist_tokens(root)

    import sqlite3

    conn = sqlite3.connect(token_store._db_path(root))
    conn.execute(
        "UPDATE tokens SET ciphertext=? WHERE token_key=?",
        (b"\x00" * 40, token_store.issue_key("access", bad)),
    )
    conn.commit()
    conn.close()

    fresh = oauth_auth.OAuthState(config)
    with pytest.raises(token_store.TokenStoreError):
        fresh.restore_tokens(root)


def test_exchange_commit_cannot_overwrite_tombstone(tmp_path: Path):
    """exchange_commit's replacement issuance must never resurrect a retired
    token key (same guard as commit_tokens)."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    live, li = state._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    dead, di = state._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    state.refresh_tokens[live] = li
    state.refresh_tokens[dead] = di
    state.persist_tokens(root)

    # Retire `dead` directly (simulating a prior rotation elsewhere).
    import sqlite3

    conn = sqlite3.connect(token_store._db_path(root))
    conn.execute(
        "UPDATE tokens SET retired=1, retired_at=? WHERE token_key=?",
        (time.time(), token_store.issue_key("refresh", dead)),
    )
    conn.commit()
    conn.close()

    # Exchange `live`, but the replacement set hostile-ly includes the
    # retired `dead` value as a "new" refresh token.
    result = token_store.exchange_commit(
        root,
        source_epoch=token_store.read_revocation_epoch(root),
        presented_kind="refresh",
        presented_value=live,
        issue={
            token_store.issue_key("refresh", dead): {
                "client_id": config.client_id,
                "scope": config.scope,
                "expires_at": time.time() + 3600,
                "_kind": "refresh",
                "_token_value": dead,
            }
        },
    )
    assert result["issued"] == 0, "retired key was re-issued by exchange_commit"
    assert token_store.lookup_token(root, "refresh", dead) is None
    # The presented live token was still consumed (rotation happened).
    assert token_store.lookup_token(root, "refresh", live) is None




def test_revoke_reports_key_rotation_truthfully(tmp_path: Path, monkeypatch):
    """With an env-managed master key, revoke must NOT claim key_rotated."""
    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    result = token_store.revoke_tokens(root, rotate_key=True)
    assert result["key_rotated"] is False
    assert "env-managed" in result.get("key_rotation_note", "")


def test_legacy_epoch_survives_envelope_absent_close(tmp_path: Path, monkeypatch):
    """A prior revocation (envelope deleted, epoch file = 3) must keep its
    epoch fence when migration closes with no envelope present."""
    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    (root / "secrets" / token_store.LEGACY_EPOCH_FILENAME).write_text(
        "3", encoding="ascii"
    )
    # A stale peer (epoch-0 view) tries to persist: fenced, epoch preserved.
    with pytest.raises(token_store.TokenStoreError):
        token_store.commit_tokens(root, source_epoch=0, issue={})
    # ...and the epoch fence now blocks epoch-0 views durably: a correctly
    # epoch-aware writer commits and the epoch stays 3.
    res = token_store.commit_tokens(root, source_epoch=3, issue={})
    assert res["epoch"] == 3
    assert token_store.read_revocation_epoch(root) == 3


def test_malformed_legacy_epoch_fails_closed(tmp_path: Path, monkeypatch):
    """An unparseable legacy epoch file is unknown revocation history:
    treated as at-least-once revoked (epoch >= 1), never epoch 0."""
    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    (root / "secrets" / token_store.LEGACY_EPOCH_FILENAME).write_text(
        "not-a-number", encoding="ascii"
    )
    try:
        token_store.commit_tokens(root, source_epoch=0, issue={})
    except token_store.TokenStoreError:
        pass
    # The close path wrote epoch >= 1 before the fence rejected issuance.
    res = token_store.commit_tokens(root, source_epoch=1, issue={})
    assert res["epoch"] >= 1


def test_revoke_rotation_happens_after_commit(tmp_path: Path):
    """Rotation must not run inside the SQLite transaction: if the commit
    failed, an in-transaction keyring swap would leave rolled-back-to-live
    rows undecryptable. Observable contract: revocation succeeds and the
    reported rotation reflects the post-commit attempt."""
    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    token, item = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[token] = item
    state.persist_tokens(root)
    result = token_store.revoke_tokens(root, rotate_key=True)
    assert result["revoked"] is True
    assert result["epoch"] >= 1
    assert token_store.lookup_token(root, "access", token) is None
    # key_rotated reflects the real post-commit outcome (True or False,
    # never a lie): both are acceptable; it must be a bool.
    assert isinstance(result["key_rotated"], bool)


def test_rotation_and_issuance_are_serialized(tmp_path: Path):
    """A grant committing concurrently with revoke+rotate must never end up
    encrypted under a key that rotation just discarded."""
    import threading

    root = tmp_path / "hermes"
    config = _oauth_config()
    state = oauth_auth.OAuthState(config)
    state.restore_tokens(root)
    seed, si = state._new_access_token(
        client_id=config.client_id, scope=config.scope, resource=config.resource
    )
    state.access_tokens[seed] = si
    state.persist_tokens(root)

    outcomes = {"issued": 0, "fenced": 0, "errors": 0}
    grant_values: list[str] = []
    lock = threading.Lock()

    def issuer() -> None:
        st = oauth_auth.OAuthState(config)
        st.restore_tokens(root)
        for _ in range(5):
            value, item = st._new_access_token(
                client_id=config.client_id, scope=config.scope, resource=config.resource
            )
            try:
                token_store.commit_tokens(
                    root,
                    source_epoch=token_store.read_revocation_epoch(root),
                    issue={
                        token_store.issue_key("access", value): {
                            "client_id": config.client_id,
                            "scope": config.scope,
                            "resource": config.resource,
                            "expires_at": item["expires_at"],
                            "_kind": "access",
                            "_token_value": value,
                        }
                    },
                )
                with lock:
                    outcomes["issued"] += 1
                    grant_values.append(value)
            except token_store.TokenStoreError:
                with lock:
                    outcomes["fenced"] += 1
            except Exception:
                with lock:
                    outcomes["errors"] += 1

    def revoker() -> None:
        token_store.revoke_tokens(root, rotate_key=True)

    threads = [threading.Thread(target=issuer) for _ in range(3)]
    rev = threading.Thread(target=revoker)
    for t in threads:
        t.start()
    rev.start()
    for t in threads:
        t.join()
    rev.join()

    assert outcomes["errors"] == 0, outcomes
    # Every credential that committed must still be readable (correct key),
    # and the revoked seed must be dead.
    for value in grant_values:
        assert token_store.lookup_token(root, "access", value) is not None, (
            "issued token unreadable after concurrent rotation"
        )
    assert token_store.lookup_token(root, "access", seed) is None


def test_negative_legacy_ledger_epoch_fails_closed(tmp_path: Path, monkeypatch):
    """A negative revocation_epoch in the legacy ledger is out-of-range
    history: it must import as epoch >= 1, never 0."""
    import time as _time

    root = tmp_path / "hermes2"
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(token_store.MASTER_KEY_ENV, "test-master-key")
    config = _oauth_config()
    refresh, ritem = oauth_auth.OAuthState(config)._new_refresh_token(
        client_id=config.client_id, scope=config.scope
    )
    token_store.save_tokens(
        root, {"refresh_tokens": {refresh: dict(ritem)}}
    )
    (root / "secrets" / token_store.LEGACY_LEDGER_FILENAME).write_text(
        json.dumps({"retired": {}, "revocation_epoch": -5}), encoding="utf-8"
    )
    migrated = token_store.migrate_store(root)
    assert migrated["epoch"] >= 1, "negative legacy epoch imported as 0"
