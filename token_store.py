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
import json
import os
import secrets
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


REVOCATION_EPOCH_FILENAME = "hermes_gpt_token_epoch"
_SQLITE_MAX_INT = 2**63 - 1


def _epoch_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / REVOCATION_EPOCH_FILENAME


def read_revocation_epoch(hermes_root: Path) -> int:
    """Current durable revocation epoch (monotonic; 0 = never revoked).

    The epoch file survives envelope deletion, so any process — including a
    clustered peer that never saw the revocation event — can detect that the
    tokens it still holds in memory predate a revocation and must not be
    re-persisted.
    """
    path = _epoch_path(hermes_root)
    if not path.exists():
        return 0
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
    if not 0 <= value <= _SQLITE_MAX_INT:
        return 0
    return value


def _bump_revocation_epoch(hermes_root: Path) -> int:
    """Atomically advance the revocation epoch and return the new value."""
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = _epoch_path(hermes_root)
    next_epoch = read_revocation_epoch(hermes_root) + 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(next_epoch), encoding="ascii")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return next_epoch


def merge_persist_tokens(
    hermes_root: Path,
    access_updates: dict[str, Any],
    refresh_updates: dict[str, Any],
    *,
    removed_refresh: tuple[str, ...] = (),
    observed_epoch: int,
    source_epoch: int,
) -> dict[str, Any]:
    """Merge-update the shared durable envelope under revocation-epoch fencing.

    Clustered peers each hold only their own issued tokens in memory, so a
    plain save would drop every other peer's valid tokens (and, after a
    revocation, could resurrect pre-revocation tokens from a stale cache).
    This loads the authoritative envelope, applies only the caller's
    additions/removals, and re-writes it atomically.

    Epoch fencing: the caller records the epoch its in-memory view was built
    under (``source_epoch``) and the epoch it observed at decision time
    (``observed_epoch``). If a revocation happened in between (or the store
    was revoked at any point before this write: current epoch >
    ``source_epoch``), the merge refuses so no pre-revocation token can be
    written back. New tokens minted *after* the caller's last epoch check are
    fenced by ``observed_epoch``: they are only written when the store's
    current epoch still equals it.

    Removals (rotated/consumed refresh tokens) are applied against the
    envelope regardless of the caller's cache, so rotation stays durable even
    when the writer's view is otherwise stale.
    """
    key, kid, source = _resolve_key(hermes_root)
    current_epoch = read_revocation_epoch(hermes_root)
    if current_epoch > observed_epoch or current_epoch > source_epoch:
        raise TokenStoreError(
            "token envelope was revoked after this view was built; refusing to persist"
        )
    try:
        bundle = load_tokens(hermes_root)
    except TokenStoreError:
        bundle = {}
    if not isinstance(bundle, dict):
        bundle = {}
    access = bundle.get("access_tokens")
    refresh = bundle.get("refresh_tokens")
    access = dict(access) if isinstance(access, dict) else {}
    refresh = dict(refresh) if isinstance(refresh, dict) else {}
    now = time.time()
    for value, item in (access_updates or {}).items():
        if isinstance(item, dict) and item.get("expires_at", 0) > now:
            access[value] = item
    for value, item in (refresh_updates or {}).items():
        if isinstance(item, dict) and item.get("expires_at", 0) > now:
            refresh[value] = item
    for value in removed_refresh:
        refresh.pop(value, None)
    merged = {"access_tokens": access, "refresh_tokens": refresh}
    _write_envelope(hermes_root, kid, merged, key)
    return {
        "kid": kid,
        "source": source,
        "path": str(envelope_path(hermes_root)),
        "epoch": current_epoch,
        "merged": True,
    }


def load_tokens(hermes_root: Path) -> dict[str, Any]:
    """Load + decrypt the token bundle. Raises TokenStoreError on problems."""
    envelope = load_envelope(hermes_root)
    if envelope is None:
        return {}
    plaintext = decrypt_envelope(envelope, hermes_root)
    if not isinstance(plaintext, dict):
        raise TokenStoreError("token envelope plaintext is not an object")
    return plaintext


def revoke_tokens(hermes_root: Path, *, rotate_key: bool = True) -> dict[str, Any]:
    """Revoke durable tokens: delete the envelope (optionally rotate key).

    Also advances the durable revocation epoch so clustered peers that still
    hold pre-revocation tokens in memory can never re-persist them over the
    revocation. Returns a bounded summary; never exposes token material.
    """
    path = envelope_path(hermes_root)
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except OSError as exc:
            raise TokenStoreError(f"could not remove token envelope: {exc}") from exc
    epoch = _bump_revocation_epoch(hermes_root)
    rotated = False
    if rotate_key:
        try:
            key_file_path(hermes_root).unlink(missing_ok=True)
            _resolve_key(hermes_root)  # regenerates
            rotated = True
        except Exception:
            rotated = False
    return {
        "revoked": existed,
        "envelope_removed": existed,
        "key_rotated": rotated,
        "epoch": epoch,
    }


def status(hermes_root: Path) -> dict[str, Any]:
    """Read-only store status: presence, expiry, revocation time. No material."""
    envelope = load_envelope(hermes_root)
    if envelope is None:
        return {
            "available": False,
            "presence": "absent",
            "expires_at": None,
            "revoked_at": None,
            "kid": "",
        }
    try:
        tokens = decrypt_envelope(envelope, hermes_root)
    except TokenStoreError:
        return {
            "available": True,
            "presence": "corrupt",
            "expires_at": None,
            "revoked_at": None,
            "kid": envelope.get("kid", ""),
        }
    expiries = [
        v.get("expires_at")
        for v in tokens.values()
        if isinstance(v, dict) and v.get("expires_at")
    ]
    expires_at = max(expiries) if expiries else None  # type: ignore[type-var]
    return {
        "available": True,
        "presence": "present",
        "expires_at": expires_at,
        "revoked_at": None,
        "kid": envelope.get("kid", ""),
        "client_count": len(tokens),
    }
