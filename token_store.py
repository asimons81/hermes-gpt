"""Durable encrypted token storage for hermes-gpt v0.7 (Flight Deck, S5).

Implements ADR-001: OAuth credentials survive restarts via an encrypted
envelope at ``<hermes_data>/secrets/hermes_gpt_tokens.json`` (0600),
AES-256-GCM, with key management precedence OS keyring (``keyring`` lib,
optional) → key file (``<hermes_data>/secrets/hermes_gpt_token_key``, 0600) →
env ``HERMES_GPT_TOKEN_MASTER_KEY`` (CI/test only, weakest).

Rotation via ``kid``; revocation deletes the envelope (optionally rotates the
key). No token material ever appears in audit records or MCP responses — the
public surface exposes presence/expiry only.

Token store is NOT an MCP mutation surface: only ``oauth_auth`` calls it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = 1
ENVELOPE_FILENAME = "hermes_gpt_tokens.json"
KEY_FILENAME = "hermes_gpt_token_key"
SECRETS_DIR = "secrets"
MASTER_KEY_ENV = "HERMES_GPT_TOKEN_MASTER_KEY"
SERVICE_NAME = "hermes-gpt"
USERNAME = "oauth-tokens"


class TokenStoreError(RuntimeError):
    pass


def _secrets_dir(hermes_root: Path) -> Path:
    return hermes_root / SECRETS_DIR


def envelope_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / ENVELOPE_FILENAME


def key_file_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / KEY_FILENAME


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _key_from_env() -> bytes | None:
    raw = os.environ.get(MASTER_KEY_ENV)
    if not raw:
        return None
    # Derive a 32-byte key from any env material (documented weakest path).
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).digest()


def _key_from_keyring() -> bytes | None:
    try:
        import keyring  # optional dependency

        raw = keyring.get_password(SERVICE_NAME, USERNAME)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return _unb64(raw)
    except Exception:
        return None


def _store_key_in_keyring(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, USERNAME, _b64(key))
        return True
    except Exception:
        return False


def _key_from_file(hermes_root: Path) -> bytes | None:
    path = key_file_path(hermes_root)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) == 32:
        return raw
    try:
        return _unb64(raw.decode("ascii").strip())
    except Exception:
        return None


def _write_key_file(hermes_root: Path, key: bytes) -> None:
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = key_file_path(hermes_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _resolve_key(hermes_root: Path) -> tuple[bytes, str, str]:
    """Return (key, kid, source). Key precedence env → keyring → key file."""
    env_key = _key_from_env()
    if env_key is not None:
        return env_key, "env", "env"
    keyring_key = _key_from_keyring()
    if keyring_key is not None:
        return keyring_key, "keyring", "keyring"
    file_key = _key_from_file(hermes_root)
    if file_key is not None:
        return file_key, "keyfile", "keyfile"
    # First use: generate a key, prefer keyring, else key file (0600).
    generated = secrets.token_bytes(32)
    if _store_key_in_keyring(generated):
        return generated, "keyring", "keyring"
    _write_key_file(hermes_root, generated)
    return generated, "keyfile", "keyfile"


def load_envelope(hermes_root: Path) -> dict[str, Any] | None:
    """Read the envelope file if present. Returns None when absent."""
    path = envelope_path(hermes_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TokenStoreError("token envelope is corrupt or unreadable")
    if data.get("version") != ENVELOPE_VERSION:
        raise TokenStoreError("unsupported token envelope version")
    for field in ("kid", "ciphertext", "nonce"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise TokenStoreError(f"token envelope missing {field!r}")
    return data


def decrypt_envelope(envelope: dict[str, Any], hermes_root: Path) -> dict[str, Any]:
    """Decrypt an envelope to its plaintext token bundle."""
    key, _, _ = _resolve_key(hermes_root)
    try:
        nonce = _unb64(envelope["nonce"])
        ciphertext = _unb64(envelope["ciphertext"])
        if len(nonce) != 12:
            raise TokenStoreError("token envelope nonce must be 12 bytes")
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))
    except TokenStoreError:
        raise
    except Exception as exc:
        raise TokenStoreError(f"could not decrypt token envelope: {exc.__class__.__name__}") from exc


def _write_envelope(hermes_root: Path, kid: str, plaintext: dict[str, Any], key: bytes) -> None:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        json.dumps(plaintext, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        None,
    )
    envelope = {
        "version": ENVELOPE_VERSION,
        "kid": kid,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ciphertext": _b64(ciphertext),
        "nonce": _b64(nonce),
    }
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = envelope_path(hermes_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_tokens(hermes_root: Path, tokens: dict[str, Any]) -> dict[str, Any]:
    """Encrypt and persist a token bundle. Returns {kid, source, path}."""
    key, kid, source = _resolve_key(hermes_root)
    _write_envelope(hermes_root, kid, tokens, key)
    return {"kid": kid, "source": source, "path": str(envelope_path(hermes_root))}


DB_FILENAME = "hermes_gpt_tokens.db"
LEGACY_ENVELOPE_FILENAME = "hermes_gpt_tokens.json"
LEGACY_EPOCH_FILENAME = "hermes_gpt_token_epoch"
LEGACY_LEDGER_FILENAME = "hermes_gpt_token_ledger"
_SQLITE_MAX_INT = 2**63 - 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    expires_at REAL NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0,
    retired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_expiry ON tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_kind ON tokens(kind, retired);
"""


def _token_key(kind: str, token_value: str) -> str:
    """Stable row key: opaque hash of the token value (never the value)."""
    digest = hashlib.sha256(f"{kind}\0{token_value}".encode("utf-8")).hexdigest()
    return "sha256:" + digest


def issue_key(kind: str, token_value: str) -> str:
    """Row key for an access/refresh token value."""
    return _token_key(kind, token_value)


def _db_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / DB_FILENAME


def _legacy_envelope_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / LEGACY_ENVELOPE_FILENAME


def _connect(hermes_root: Path) -> sqlite3.Connection:
    """Open the token DB read-write; initializes the schema. Fails closed.

    Raises TokenStoreError when the database is unreadable/corrupt — callers
    treat tokens as invalid rather than falling back to permissive behavior.
    """
    path = _db_path(hermes_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=15.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        db.executescript(_SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return db
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unavailable: {exc}") from exc


def _encrypt_record(key: bytes, record: dict[str, Any]) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(
        nonce,
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        None,
    )
    return nonce, ct


def _decrypt_record(key: bytes, nonce: bytes, ciphertext: bytes) -> dict[str, Any]:
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise TokenStoreError("token record plaintext is not an object")
    return data


def _migrate_legacy_locked(db: sqlite3.Connection, hermes_root: Path, key: bytes, kid: str, now: float) -> int:
    """One-time import of the legacy JSON envelope (and hash ledger) into the DB.

    Runs inside the caller's write transaction. Rules:

    - A durable ``legacy_migration`` marker closes migration permanently once
      set; revocation sets it too, so leftover legacy files can never
      re-import revoked credentials (fail closed).
    - A corrupt/unparseable legacy ledger is a hard error: the transaction
      aborts rather than importing credentials whose retirement history
      cannot be established.
    - Legacy artifacts are NOT deleted inside this transaction; cleanup
      happens only after the enclosing transaction commits (the caller
      schedules it), so a rollback never loses the recovery source.
    - Imported records carry the internal markers needed to reconstruct
      caches (``_kind``/``_token_value``) exactly like fresh records.
    """
    marker = db.execute(
        "SELECT value FROM token_meta WHERE name='legacy_migration'"
    ).fetchone()
    if marker is not None:
        return 0  # already migrated (or closed by revocation)
    env_path = _legacy_envelope_path(hermes_root)
    ledger_path = _secrets_dir(hermes_root) / LEGACY_LEDGER_FILENAME
    epoch_path = _secrets_dir(hermes_root) / LEGACY_EPOCH_FILENAME
    if not env_path.exists():
        # No envelope anywhere: close migration so later stray files cannot
        # be imported after revocation has happened.
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','closed:empty')"
        )
        return 0
    try:
        envelope = load_envelope(hermes_root)
        bundle = decrypt_envelope(envelope, hermes_root) if envelope else {}
    except TokenStoreError:
        # Corrupt/undecryptable legacy store: close migration, keep files.
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','closed:corrupt')"
        )
        return 0
    if not isinstance(bundle, dict):
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','closed:corrupt')"
        )
        return 0
    legacy_ledger: dict[str, Any] = {}
    ledger_corrupt = False
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                legacy_ledger = data
            else:
                ledger_corrupt = True
        except (OSError, ValueError):
            ledger_corrupt = True
    if ledger_corrupt:
        # Fail closed: retirement history cannot be established.
        raise TokenStoreError("legacy retirement ledger is corrupt; refusing to import")
    retired_keys = legacy_ledger.get("retired")
    if retired_keys is None:
        retired = {}
    elif not isinstance(retired_keys, dict):
        # Parsed but structurally invalid: retirement history cannot be
        # established. Fail closed instead of importing everything live.
        raise TokenStoreError("legacy retirement ledger is malformed; refusing to import")
    else:
        retired = retired_keys
    legacy_epoch_raw = legacy_ledger.get("revocation_epoch")
    if legacy_epoch_raw is not None and (
        isinstance(legacy_epoch_raw, bool) or not isinstance(legacy_epoch_raw, int)
    ):
        raise TokenStoreError("legacy retirement ledger is malformed; refusing to import")
    migrated = 0
    for kind in ("access", "refresh"):
        section = bundle.get(f"{kind}_tokens")
        if not isinstance(section, dict):
            continue
        for value, item in section.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            row_key = _token_key(kind, value)
            if row_key in retired:
                # Preserve the tombstone so rotated/revoked stay dead.
                db.execute(
                    "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,1,?)",
                    (row_key, kind, b"", b"", item.get("expires_at", 0), now),
                )
                continue
            record = dict(item)
            record["_kind"] = kind
            record["_token_value"] = value
            nonce, ct = _encrypt_record(key, record)
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item.get("expires_at", 0)),
            )
            migrated += 1
    # Preserve the legacy revocation epoch.
    legacy_epoch = 0
    if isinstance(legacy_ledger.get("revocation_epoch"), int) and not isinstance(
        legacy_ledger.get("revocation_epoch"), bool
    ):
        legacy_epoch = int(legacy_ledger["revocation_epoch"])
    elif epoch_path.exists():
        try:
            legacy_epoch = int(epoch_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            legacy_epoch = 1  # unknown history -> treat as revoked once
    have_epoch = db.execute(
        "SELECT 1 FROM token_meta WHERE name='revocation_epoch'"
    ).fetchone()
    if not have_epoch:
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(max(0, legacy_epoch)),),
        )
    db.execute(
        "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','done')"
    )
    return migrated


def _cleanup_legacy_artifacts(hermes_root: Path) -> None:
    """Remove legacy artifacts AFTER the enclosing transaction committed.

    Safe to retry: each unlink is missing_ok. If cleanup fails the worst case
    is leftover files that the closed migration marker ignores.
    """
    try:
        _legacy_envelope_path(hermes_root).unlink(missing_ok=True)
        (_secrets_dir(hermes_root) / LEGACY_LEDGER_FILENAME).unlink(missing_ok=True)
        (_secrets_dir(hermes_root) / LEGACY_EPOCH_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def read_revocation_epoch(hermes_root: Path) -> int:
    """Current durable revocation epoch (monotonic; 0 = never revoked).

    Fails closed on a corrupt database by raising TokenStoreError.
    """
    if not _db_path(hermes_root).exists():
        return 0
    try:
        db = _connect(hermes_root)
    except TokenStoreError:
        raise
    try:
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except (sqlite3.Error, ValueError) as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()


def lookup_token(
    hermes_root: Path, kind: str, token_value: str
) -> dict[str, Any] | None:
    """Return the durable record for a live token, or None.

    None when the token is unknown, expired, or retired (revoked/rotated).
    Raises TokenStoreError when the store is unreadable — callers fail
    closed instead of treating corruption as permission.
    """
    row_key = _token_key(kind, token_value)
    db = _connect(hermes_root)
    try:
        row = db.execute(
            "SELECT nonce,ciphertext,expires_at,retired FROM tokens WHERE token_key=?",
            (row_key,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()
    if not row or row["retired"] or row["expires_at"] <= time.time():
        return None
    try:
        key, _, _ = _resolve_key_parts(hermes_root)
        return _decrypt_record(key, row["nonce"], row["ciphertext"])
    except Exception as exc:
        raise TokenStoreError("token record could not be decrypted") from exc


def load_live_tokens(hermes_root: Path) -> dict[str, Any]:
    """All live records, grouped envelope-style ({access_tokens, refresh_tokens}).

    Raises TokenStoreError on corruption (fail closed).
    """
    now = time.time()
    db = _connect(hermes_root)
    try:
        rows = db.execute(
            "SELECT token_key,kind,nonce,ciphertext,expires_at FROM tokens WHERE retired=0 AND expires_at>?",
            (now,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise TokenStoreError(f"token database unreadable: {exc}") from exc
    finally:
        db.close()
    key, _, _ = _resolve_key_parts(hermes_root)
    out: dict[str, Any] = {"access_tokens": {}, "refresh_tokens": {}}
    for row in rows:
        try:
            record = _decrypt_record(key, row["nonce"], row["ciphertext"])
        except Exception as exc:
            # A live (unretired, unexpired) record that cannot be decrypted
            # means a corrupt or key-mismatched store. Fail closed: a partial
            # restore would silently drop credentials (and their revocation
            # semantics) instead of surfacing the damage.
            raise TokenStoreError(
                "token database contains an undecryptable live record"
            ) from exc
        section = out.get(f"{row['kind']}_tokens")
        if isinstance(section, dict):
            value = record.get("_token_value")
            if isinstance(value, str) and value:
                clean = {k: v for k, v in record.items() if not k.startswith("_")}
                section[value] = clean
    return out


def _resolve_key_parts(hermes_root: Path) -> tuple[bytes, str, str]:
    key, kid, source = _resolve_key(hermes_root)
    return key, kid, source


def commit_tokens(
    hermes_root: Path,
    *,
    source_epoch: int,
    issue: dict[str, dict[str, Any]] | None = None,
    retire: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Atomically commit token issuance/retirement in one SQLite transaction.

    ``issue`` maps row keys (:func:`issue_key`) to records carrying internal
    ``_kind``/``_token_value`` markers. ``retire`` maps a kind to raw token
    values being consumed/rotated. The epoch check, retirement, issuance,
    expiry pruning, and legacy migration all commit (or roll back) together;
    SQLite's write lock serializes this against every other mutation,
    including revocation, so no interleaving can resurrect retired tokens.
    """
    issue = issue or {}
    retire = retire or {}
    now = time.time()
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        current_epoch = int(row["value"]) if row else 0
        if current_epoch > source_epoch:
            db.execute("ROLLBACK")
            raise TokenStoreError(
                "token store was revoked after this view was built; refusing to persist"
            )
        retired_count = 0
        for kind, values in retire.items():
            for value in values or ():
                row_key = _token_key(kind, value)
                cur = db.execute(
                    "UPDATE tokens SET retired=1, retired_at=? WHERE token_key=? AND retired=0",
                    (now, row_key),
                )
                retired_count += cur.rowcount if cur.rowcount > 0 else 0
        issued = 0
        for row_key, item in issue.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            kind = str(item.get("_kind") or "")
            value = str(item.get("_token_value") or "")
            if kind not in ("access", "refresh") or not value:
                continue
            # Retirement is permanent: a tombstoned key can never be
            # re-issued, even by a stale peer cache that still lists it.
            tomb = db.execute(
                "SELECT 1 FROM tokens WHERE token_key=? AND retired=1", (row_key,)
            ).fetchone()
            if tomb:
                continue
            nonce, ct = _encrypt_record(key, dict(item))
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item["expires_at"]),
            )
            issued += 1
        # Prune expired LIVE credentials only. Retired rows are tombstones:
        # they must outlive their original expiry so a later stale reissue of
        # the same token value can never succeed after the row is gone.
        db.execute("DELETE FROM tokens WHERE expires_at<=? AND retired=0", (now,))
        live = db.execute("SELECT COUNT(*) AS c FROM tokens WHERE retired=0").fetchone()
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(current_epoch),),
        )
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": current_epoch,
            "records": live["c"] if live else 0,
            "retired": retired_count,
            "issued": issued,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token commit failed: {exc}") from exc
    finally:
        db.close()


def migrate_store(hermes_root: Path) -> dict[str, Any]:
    """Run the legacy -> SQLite migration as its own transaction.

    Migration is NOT credential issuance: it must faithfully import
    whatever revocation epoch the legacy artifacts carry, so the epoch
    fence used for grants does not apply here (a positive legacy epoch
    must commit, not roll back). Safe to call at startup and idempotent:
    the durable 'legacy_migration' marker closes it after the first run.
    """
    now = time.time()
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        epoch = int(meta["value"]) if meta else 0
        live = db.execute(
            "SELECT COUNT(*) AS c FROM tokens WHERE retired=0"
        ).fetchone()
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": epoch,
            "records": live["c"] if live else 0,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token store migration failed: {exc}") from exc
    finally:
        db.close()


def exchange_commit(
    hermes_root: Path,
    *,
    source_epoch: int,
    presented_kind: str,
    presented_value: str,
    issue: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Atomically CONSUME one presented token and publish replacements.

    The liveness check and the retirement of the presented token happen in
    the same transaction as the issuance of its replacements, so two peers
    racing to exchange the same refresh token cannot both succeed: the first
    commit retires it, the second sees the tombstone and is rejected whole
    (no replacement credentials are published).
    """
    issue = issue or {}
    now = time.time()
    presented_key = _token_key(presented_kind, presented_value)
    db = _connect(hermes_root)
    try:
        db.execute("BEGIN IMMEDIATE")
        key, kid, source = _resolve_key_parts(hermes_root)
        _migrate_legacy_locked(db, hermes_root, key, kid, now)
        row = db.execute(
            "SELECT nonce,ciphertext,expires_at,retired FROM tokens WHERE token_key=?",
            (presented_key,),
        ).fetchone()
        if not row or row["retired"] or row["expires_at"] <= now:
            db.execute("ROLLBACK")
            raise TokenStoreError("presented token is not live")
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        current_epoch = int(meta["value"]) if meta else 0
        if current_epoch > source_epoch:
            db.execute("ROLLBACK")
            raise TokenStoreError(
                "token store was revoked after this view was built; refusing to persist"
            )
        presented_record = _decrypt_record(key, row["nonce"], row["ciphertext"])
        db.execute(
            "UPDATE tokens SET retired=1, retired_at=? WHERE token_key=?",
            (now, presented_key),
        )
        issued = 0
        for row_key, item in issue.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            kind = str(item.get("_kind") or "")
            value = str(item.get("_token_value") or "")
            if kind not in ("access", "refresh") or not value:
                continue
            nonce, ct = _encrypt_record(key, dict(item))
            db.execute(
                "INSERT OR REPLACE INTO tokens(token_key,kind,nonce,ciphertext,expires_at,retired,retired_at) VALUES(?,?,?,?,?,0,NULL)",
                (row_key, kind, nonce, ct, item["expires_at"]),
            )
            issued += 1
        db.execute("DELETE FROM tokens WHERE expires_at<=? AND retired=0", (now,))
        db.execute("COMMIT")
        _cleanup_legacy_artifacts(hermes_root)
        return {
            "kid": kid,
            "source": source,
            "epoch": current_epoch,
            "issued": issued,
            "presented": presented_record,
        }
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token exchange commit failed: {exc}") from exc
    finally:
        db.close()


def load_tokens(hermes_root: Path) -> dict[str, Any]:
    """Load the live token bundle (SQLite store first, legacy envelope fallback).

    Raises TokenStoreError on unreadable stores — callers fail closed.
    """
    if _db_path(hermes_root).exists():
        return load_live_tokens(hermes_root)
    envelope = load_envelope(hermes_root)
    if envelope is None:
        return {}
    plaintext = decrypt_envelope(envelope, hermes_root)
    if not isinstance(plaintext, dict):
        raise TokenStoreError("token envelope plaintext is not an object")
    return plaintext


def status(hermes_root: Path) -> dict[str, Any]:
    """Read-only store status: presence, expiry, revocation epoch. No material.

    Understands both the current SQLite store and a not-yet-migrated legacy
    envelope, so the browser account-status derivation keeps working across
    the upgrade.
    """
    if not _db_path(hermes_root).exists():
        envelope = load_envelope(hermes_root) if _legacy_envelope_path(hermes_root).exists() else None
        if envelope is None:
            return {
                "available": False,
                "presence": "absent",
                "expires_at": None,
                "revocation_epoch": 0,
                "kid": "",
            }
        try:
            bundle = decrypt_envelope(envelope, hermes_root)
        except TokenStoreError:
            return {
                "available": True,
                "presence": "corrupt",
                "expires_at": None,
                "revocation_epoch": 0,
                "kid": envelope.get("kid", ""),
            }
        flat = _legacy_flat_records(bundle)
        expiries = [v.get("expires_at") for v in flat if v.get("expires_at")]
        return {
            "available": True,
            "presence": "present",
            "expires_at": max(expiries) if expiries else None,
            "revocation_epoch": 0,
            "kid": envelope.get("kid", ""),
            "client_count": len([v for v in flat if v.get("expires_at", 0) > time.time()]),
        }
    try:
        db = _connect(hermes_root)
        rows = db.execute(
            "SELECT expires_at,retired FROM tokens"
        ).fetchall()
        meta = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        db.close()
    except (sqlite3.Error, TokenStoreError) as exc:
        return {
            "available": True,
            "presence": "corrupt",
            "expires_at": None,
            "revocation_epoch": None,
            "kid": "",
            "error": f"{exc}"[:120],
        }
    now = time.time()
    live = [r for r in rows if not r["retired"] and r["expires_at"] > now]
    expiries = [r["expires_at"] for r in rows]
    return {
        "available": True,
        "presence": "present",
        "expires_at": max(expiries) if expiries else None,
        "revocation_epoch": int(meta["value"]) if meta else 0,
        "kid": "",
        "client_count": len(live),
    }


def _legacy_flat_records(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    sections = [v for v in bundle.values() if isinstance(v, dict)]
    for section in sections:
        flat.extend(i for i in section.values() if isinstance(i, dict))
    for item in bundle.values():
        if isinstance(item, dict) and "expires_at" in item and item not in flat:
            flat.append(item)
    return flat
    envelope = load_envelope(hermes_root)
    if envelope is None:
        return {
            "available": False,
            "presence": "absent",
            "expires_at": None,
            "revocation_epoch": read_revocation_epoch(hermes_root),
            "kid": "",
        }
    try:
        bundle = load_tokens(hermes_root)
    except TokenStoreError:
        return {
            "available": True,
            "presence": "corrupt",
            "expires_at": None,
            "revocation_epoch": read_revocation_epoch(hermes_root),
            "kid": envelope.get("kid", ""),
        }
    flat: list[dict[str, Any]] = []
    if isinstance(bundle, dict):
        # Sectioned shape (current writer) and flat legacy shape both count.
        # Expiry reflects ALL entries (an expired max is how the UI derives
        # the 'expired' state); liveness only gates the count.
        sections = [v for v in bundle.values() if isinstance(v, dict)]
        for section in sections:
            for item in section.values():
                if isinstance(item, dict):
                    flat.append(item)
        for item in bundle.values():
            if isinstance(item, dict) and "expires_at" in item and item not in flat:
                flat.append(item)
    now = time.time()
    live = [i for i in flat if i.get("expires_at", 0) > now]
    expiries = [v.get("expires_at") for v in flat if v.get("expires_at")]
    expires_at = max(expiries) if expiries else None  # type: ignore[type-var]
    return {
        "available": True,
        "presence": "present",
        "expires_at": expires_at,
        "revocation_epoch": read_revocation_epoch(hermes_root),
        "kid": envelope.get("kid", ""),
        "client_count": len(live),
    }


def revoke_tokens(hermes_root: Path, *, rotate_key: bool = True) -> dict[str, Any]:
    """Revoke durable tokens in one SQLite transaction.

    Marks every live token retired, advances the durable revocation epoch,
    and (when requested) rotates the master key INSIDE the same write
    transaction, so no commit can slip between the epoch bump and the key
    swap and leave an undecryptable envelope behind. Also removes legacy
    artifacts. Returns a bounded summary; never exposes token material.
    """
    now = time.time()
    envelope_existed = _legacy_envelope_path(hermes_root).exists()
    db = _connect(hermes_root)
    epoch = 0
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM token_meta WHERE name='revocation_epoch'"
        ).fetchone()
        epoch = int(row["value"]) if row else 0
        db.execute("UPDATE tokens SET retired=1, retired_at=? WHERE retired=0", (now,))
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('revocation_epoch',?)",
            (str(epoch + 1),),
        )
        # Close legacy migration permanently: leftover JSON artifacts must
        # never re-import credentials this revocation just killed.
        db.execute(
            "INSERT OR REPLACE INTO token_meta(name,value) VALUES('legacy_migration','closed:revoked')"
        )
        if rotate_key:
            # Rotate while still holding the write lock: any concurrent
            # commit either completed before us (its tokens are now retired)
            # or waits and then sees the bumped epoch and is refused.
            try:
                key_file_path(hermes_root).unlink(missing_ok=True)
                _resolve_key(hermes_root)  # regenerates
            except Exception:
                pass
        db.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise TokenStoreError(f"token revocation failed: {exc}") from exc
    finally:
        db.close()
    # Legacy artifacts are obsolete once revoked (migration is closed inside
    # the transaction; deletion after commit is best-effort and retryable).
    _cleanup_legacy_artifacts(hermes_root)
    return {
        "revoked": True,
        "envelope_removed": envelope_existed,
        "key_rotated": bool(rotate_key),
        "epoch": epoch + 1,
    }
