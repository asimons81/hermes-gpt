from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

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
