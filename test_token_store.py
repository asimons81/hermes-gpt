"""Tests for v0.7 S5: durable encrypted token storage + OAuth promotion."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import oauth_auth as oa
import operator_oauth as op_oauth
import operator_policy as op
import token_store as ts


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def clean_token_env(monkeypatch):
    monkeypatch.delenv(ts.MASTER_KEY_ENV, raising=False)
    yield


def _oauth_state() -> oa.OAuthState:
    config = oa.OAuthConfig(
        issuer="https://auth.example.com",
        client_id="client-id",
        client_secret="client-secret-1234567890abcdefghijklmnopqrstuvwxyz-ABCDEF",
        redirect_uris=("https://auth.example.com/mcp",),
        scope="hermes",
    )
    return oa.OAuthState(config)


def test_ciphertext_on_disk_no_plaintext(hermes_root):
    state = _oauth_state()
    state.access_tokens["tok-1234567890abcdef"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": 10**12}
    state.persist_tokens(hermes_root)

    raw = (hermes_root / "secrets" / "hermes_gpt_tokens.json").read_text(encoding="utf-8")
    assert "tok-1234567890abcdef" not in raw
    envelope = json.loads(raw)
    assert envelope["version"] == 1
    assert envelope["kid"]
    assert envelope["ciphertext"]
    assert envelope["nonce"]


def test_envelope_file_mode_is_0600(hermes_root):
    state = _oauth_state()
    state.access_tokens["tok-1234567890abcdef"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": 10**12}
    state.persist_tokens(hermes_root)
    mode = os.stat(hermes_root / "secrets" / "hermes_gpt_tokens.json").st_mode & 0o777
    assert mode == 0o600


def test_restart_reload_roundtrip(hermes_root):
    state = _oauth_state()
    state.access_tokens["tok-abc"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.refresh_tokens["ref-xyz"] = {"client_id": "c", "scope": "hermes", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    # Fresh state (simulated restart) restores both stores.
    fresh = _oauth_state()
    summary = fresh.restore_tokens(hermes_root)
    assert summary["present"] is True
    assert summary["restored"] == 2
    assert "tok-abc" in fresh.access_tokens
    assert "ref-xyz" in fresh.refresh_tokens


def time_far() -> float:
    import time

    return time.time() + 3600 * 24 * 30


def test_corrupt_envelope_fails_closed(hermes_root):
    path = hermes_root / "secrets" / "hermes_gpt_tokens.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ts.TokenStoreError):
        ts.load_tokens(hermes_root)


def test_revoke_deletes_envelope_and_rotates_key(hermes_root):
    state = _oauth_state()
    state.access_tokens["tok-abc"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.persist_tokens(hermes_root)
    assert (hermes_root / "secrets" / "hermes_gpt_tokens.json").exists()

    result = ts.revoke_tokens(hermes_root, rotate_key=True)
    assert result["revoked"] is True
    assert result["key_rotated"] is True
    assert not (hermes_root / "secrets" / "hermes_gpt_tokens.json").exists()
    assert ts.load_tokens(hermes_root) == {}


def test_env_master_key_works(hermes_root, monkeypatch):
    monkeypatch.setenv(ts.MASTER_KEY_ENV, "test-master-key")
    state = _oauth_state()
    state.access_tokens["tok-abc"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    fresh = _oauth_state()
    assert fresh.restore_tokens(hermes_root)["restored"] == 1


def test_status_reveals_no_token_material(hermes_root):
    state = _oauth_state()
    state.access_tokens["tok-secret-value"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    out = json.loads(op_oauth.hermes_oauth_status(hermes_root=hermes_root))
    raw = json.dumps(out)
    assert "tok-secret-value" not in raw
    assert out["store"]["presence"] == "present"
    assert out["store"]["client_count"] >= 1
    assert out["success"] is True


def test_oauth_status_absent(hermes_root):
    out = json.loads(op_oauth.hermes_oauth_status(hermes_root=hermes_root))
    assert out["success"] is True
    assert out["store"]["presence"] == "absent"


def test_oauth_status_audits_changed_false(hermes_root, tmp_path):
    """Proof 3: the read-only status wrapper audits with changed=false and
    never mutates the token store."""
    log = tmp_path / "audit.jsonl"
    op.set_audit_log_override(log)
    try:
        out = json.loads(op_oauth.hermes_oauth_status(hermes_root=hermes_root))
        assert out["success"] is True
        records = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        status_records = [r for r in records if r["tool"] == "hermes_oauth_status"]
        assert status_records, "no audit record for hermes_oauth_status"
        assert all(r["changed"] is False for r in status_records)
        assert all(r["dry_run"] is True for r in status_records)
    finally:
        op.set_audit_log_override(None)


def test_revoke_requires_owner(hermes_root, monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    out = json.loads(op_oauth.hermes_oauth_revoke(confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is False
    assert out["code"] == "OAUTH_REVOKE_DENIED"


def test_revoke_owner_gated_success(hermes_root, monkeypatch):
    state = _oauth_state()
    state.access_tokens["tok-abc"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)

    out = json.loads(op_oauth.hermes_oauth_revoke(confirm=True, dry_run=False, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["changed"] is True
    assert not (hermes_root / "secrets" / "hermes_gpt_tokens.json").exists()


def test_revoke_dry_run_does_not_delete(hermes_root, monkeypatch):
    state = _oauth_state()
    state.access_tokens["tok-abc"] = {"client_id": "c", "scope": "hermes", "resource": "r", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "owner")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    monkeypatch.setenv(op.OWNER_ACTIVE_ENV, "1")
    monkeypatch.setenv(op.OWNER_ACK_ENV, op.OWNER_ACK_REQUIRED_VALUE)

    out = json.loads(op_oauth.hermes_oauth_revoke(dry_run=True, hermes_root=hermes_root))
    assert out["success"] is True
    assert out["dry_run"] is True
    assert (hermes_root / "secrets" / "hermes_gpt_tokens.json").exists()


def test_persist_hook_writes_after_exchange(hermes_root, monkeypatch):
    """Token issuance triggers the persistence hook (S5 server wiring)."""
    state = _oauth_state()
    state.access_tokens.clear()
    state.refresh_tokens.clear()
    monkeypatch.setattr(oa, "_persist_hook", lambda st, kind: st.persist_tokens(hermes_root))

    code = state.issue_authorization_code(
        client_id="client-id",
        redirect_uri="https://auth.example.com/mcp",
        scope="hermes offline_access",
        resource="https://auth.example.com/mcp",
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    )
    resp = state.exchange_authorization_code(
        code=code,
        client_id="client-id",
        redirect_uri="https://auth.example.com/mcp",
        code_verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    )
    assert resp["access_token"]
    envelope = hermes_root / "secrets" / "hermes_gpt_tokens.json"
    assert envelope.exists()
    raw = envelope.read_text(encoding="utf-8")
    assert resp["access_token"] not in raw


def test_restore_populates_after_restart(hermes_root):
    """oauth_state_from_env restores durable tokens after a restart (S5)."""
    state = _oauth_state()
    resource = state.config.resource
    state.access_tokens["tok-restart"] = {"client_id": "c", "scope": "hermes", "resource": resource, "expires_at": time_far()}
    state.refresh_tokens["ref-restart"] = {"client_id": "c", "scope": "hermes", "expires_at": time_far()}
    state.persist_tokens(hermes_root)

    # Simulate a fresh OAuthState restoring from the same root.
    fresh = _oauth_state()
    summary = fresh.restore_tokens(hermes_root)
    assert summary["restored"] == 2
    assert fresh.validate_access_token("tok-restart") is True
