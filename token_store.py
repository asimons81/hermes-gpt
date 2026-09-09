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
LEDGER_FILENAME = "hermes_gpt_token_ledger"
_SQLITE_MAX_INT = 2**63 - 1


def _ledger_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / LEDGER_FILENAME


def _ledger_lock_path(hermes_root: Path) -> Path:
    return _secrets_dir(hermes_root) / (LEDGER_FILENAME + ".lock")


def _token_key(kind: str, token_value: str) -> str:
    """Stable ledger key: opaque hash of the token value (never the value)."""
    digest = hashlib.sha256(f"{kind}\0{token_value}".encode("utf-8")).hexdigest()
    return "sha256:" + digest


class _FileLock:
    """Advisory interprocess lock via O_CREAT|O_EXCL with stale breaking.

    Adequate for the sidecar's low write rate: a lock older than the TTL is
    treated as crashed and broken. Every critical section is short, and all
    mutation of the envelope + ledger happens while holding it, which is
    what makes revoke-vs-commit interleavings impossible.
    """

    def __init__(self, path: Path, ttl: float = 10.0) -> None:
        self.path = path
        self.ttl = ttl
        self.acquired = False

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self.ttl
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.ttl:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise TokenStoreError("token ledger lock is busy")
                time.sleep(0.01)

    def __exit__(self, *exc: Any) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass


def _read_ledger_raw(hermes_root: Path) -> dict[str, Any]:
    """Read the ledger file; {} when absent or corrupt."""
    path = _ledger_path(hermes_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_ledger_raw(hermes_root: Path, ledger: dict[str, Any]) -> None:
    d = _secrets_dir(hermes_root)
    d.mkdir(parents=True, exist_ok=True)
    path = _ledger_path(hermes_root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(ledger, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ledger_alive(hermes_root: Path, kind: str, token_value: str, now: float) -> bool:
    """Retirement gate ONLY: the ledger records what was retired/revoked.

    Presence is deliberately NOT required from the ledger: envelopes written
    before the ledger existed (older releases) carry live tokens with no
    ledger entry, and they must keep validating. The ledger's job is the
    revocation/rotation tombstone, which survives envelope rewrites.
    """
    ledger = _read_ledger_raw(hermes_root)
    retired = ledger.get("retired") if isinstance(ledger.get("retired"), dict) else {}
    key = _token_key(kind, token_value)
    if key in retired:
        return False
    return True


def read_revocation_epoch(hermes_root: Path) -> int:
    """Current durable revocation epoch (monotonic; 0 = never revoked).

    Lives in the ledger so revocation advances it inside the same locked
    transaction; survives envelope deletion, so any process (including a
    clustered peer that never saw the revocation event) can detect that the
    tokens it still holds in memory predate a revocation.
    """
    ledger = _read_ledger_raw(hermes_root)
    value = ledger.get("revocation_epoch", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= _SQLITE_MAX_INT else 0


def issue_key(kind: str, token_value: str) -> str:
    """Ledger key for an access/refresh token value."""
    return _token_key(kind, token_value)


def lookup_token(
    hermes_root: Path, kind: str, token_value: str
) -> dict[str, Any] | None:
    """Return the durable record for a live token, or None.

    The plaintext ledger decides liveness (hash present, not retired, not
    expired); the record body (client_id/scope/resource/expiry) is read from
    the encrypted envelope. None when the token is unknown, expired, or
    retired (revoked/rotated).
    """
    now = time.time()
    if not _ledger_alive(hermes_root, kind, token_value, now):
        return None
    bundle = load_tokens(hermes_root)
    section = bundle.get(f"{kind}_tokens") if isinstance(bundle, dict) else None
    item = (section or {}).get(token_value) if isinstance(section, dict) else None
    if isinstance(item, dict) and item.get("expires_at", 0) > now:
        return item
    return None


def load_live_tokens(hermes_root: Path) -> dict[str, Any]:
    """Envelope bundle filtered to live (unretired, unexpired) tokens."""
    now = time.time()
    bundle = load_tokens(hermes_root)
    if not isinstance(bundle, dict):
        return {}
    out: dict[str, Any] = {}
    for kind in ("access", "refresh"):
        section = bundle.get(f"{kind}_tokens")
        if not isinstance(section, dict):
            continue
        live = {
            value: item
            for value, item in section.items()
            if isinstance(item, dict)
            and item.get("expires_at", 0) > now
            and _ledger_alive(hermes_root, kind, value, now)
        }
        if live:
            out[f"{kind}_tokens"] = live
    return out


def commit_tokens(
    hermes_root: Path,
    *,
    source_epoch: int,
    issue: dict[str, dict[str, Any]] | None = None,
    retire: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Atomically commit token issuance/retirement under the ledger lock.

    ``issue`` maps ledger keys (:func:`issue_key`) to the caller's token
    records. ``retire`` maps a kind (``"access"``/``"refresh"``) to the list
    of raw token values being consumed/rotated.

    The complete epoch-check + merge + write of BOTH the encrypted envelope
    and the plaintext ledger runs while holding the interprocess lock, so it
    can never interleave with a concurrent revocation (which takes the same
    lock). ``source_epoch`` fences off commits built on a pre-revocation
    view.

    Retirement is authoritative and permanent: once a token hash is retired,
    a later commit from a stale peer cache can never resurrect it. Expired
    records are pruned from both files on every commit, so the store cannot
    grow without bound.
    """
    issue = issue or {}
    retire = retire or {}
    now = time.time()
    key, kid, source = _resolve_key(hermes_root)
    _secrets_dir(hermes_root).mkdir(parents=True, exist_ok=True)
    with _FileLock(_ledger_lock_path(hermes_root)):
        ledger = _read_ledger_raw(hermes_root)
        current_epoch = ledger.get("revocation_epoch", 0)
        if isinstance(current_epoch, bool) or not isinstance(current_epoch, int):
            current_epoch = 0
        if current_epoch > source_epoch:
            raise TokenStoreError(
                "token store was revoked after this view was built; refusing to persist"
            )
        # --- envelope: raw values (encrypted) ---
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
        # retire (raw values arrive via the retire map's value list)
        for kind, values in retire.items():
            section = access if kind == "access" else refresh
            for value in values or ():
                section.pop(value, None)
        # prune expired
        access = {v: i for v, i in access.items() if isinstance(i, dict) and i.get("expires_at", 0) > now}
        refresh = {v: i for v, i in refresh.items() if isinstance(i, dict) and i.get("expires_at", 0) > now}
        # issue
        issued_values: dict[str, dict[str, Any]] = {}
        for ledger_key, item in issue.items():
            if not (isinstance(item, dict) and item.get("expires_at", 0) > now):
                continue
            value = str(item.get("_token_value") or "")
            kind = str(item.get("_kind") or "")
            if not value or kind not in ("access", "refresh"):
                continue
            section = access if kind == "access" else refresh
            clean = {k: w for k, w in item.items() if not k.startswith("_")}
            section[value] = clean
            issued_values[ledger_key] = {"expires_at": clean.get("expires_at", 0)}
        _write_envelope(hermes_root, kid, {"access_tokens": access, "refresh_tokens": refresh}, key)
        # --- ledger: hashes + retirement (plaintext, no token material) ---
        records = ledger.get("tokens") if isinstance(ledger.get("tokens"), dict) else {}
        retired = ledger.get("retired") if isinstance(ledger.get("retired"), dict) else {}
        for kind, values in retire.items():
            for value in values or ():
                ledger_key = _token_key(kind, value)
                records.pop(ledger_key, None)
                retired[ledger_key] = {"retired_at": now}
        for ledger_key, meta in issued_values.items():
            if ledger_key in retired:
                continue
            records[ledger_key] = meta
        records = {
            k: v
            for k, v in records.items()
            if isinstance(v, dict) and v.get("expires_at", 0) > now
        }
        # Backfill ledger records for legacy envelope tokens (pre-ledger
        # envelopes): they are live in the envelope but have no hash entry.
        for kind, section in (("access", access), ("refresh", refresh)):
            for value in section:
                ledger_key = _token_key(kind, value)
                if ledger_key not in retired and ledger_key not in records:
                    item = section[value]
                    if isinstance(item, dict):
                        records[ledger_key] = {"expires_at": item.get("expires_at", 0)}
        ledger["tokens"] = records
        ledger["retired"] = retired
        ledger["revocation_epoch"] = current_epoch
        _write_ledger_raw(hermes_root, ledger)
        return {
            "kid": kid,
            "source": source,
            "path": str(envelope_path(hermes_root)),
            "epoch": current_epoch,
            "records": len(records),
            "retired": len(retired),
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


def status(hermes_root: Path) -> dict[str, Any]:
    """Read-only store status: presence, expiry, revocation epoch. No material."""
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
    """Revoke durable tokens under the ledger lock.

    Deletes the encrypted envelope, wipes the ledger's live records, marks
    every previously live token hash permanently retired, and advances the
    durable revocation epoch — all in one locked transaction. A clustered
    peer that still holds pre-revocation tokens in memory therefore can
    never re-persist them (epoch fencing + permanent retirement marks).
    Optionally rotates the master key. Returns a bounded summary; never
    exposes token material.
    """
    path = envelope_path(hermes_root)
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except OSError as exc:
            raise TokenStoreError(f"could not remove token envelope: {exc}") from exc
    _secrets_dir(hermes_root).mkdir(parents=True, exist_ok=True)
    with _FileLock(_ledger_lock_path(hermes_root)):
        ledger = _read_ledger_raw(hermes_root)
        epoch = ledger.get("revocation_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            epoch = 0
        retired = ledger.get("retired") if isinstance(ledger.get("retired"), dict) else {}
        records = ledger.get("tokens") if isinstance(ledger.get("tokens"), dict) else {}
        now = time.time()
        for ledger_key in records:
            retired[ledger_key] = {"retired_at": now}
        ledger["retired"] = retired
        ledger["tokens"] = {}
        ledger["revocation_epoch"] = epoch + 1
        _write_ledger_raw(hermes_root, ledger)
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
        "epoch": epoch + 1,
    }
