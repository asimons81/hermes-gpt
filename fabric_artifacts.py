"""Immutable G4-C peer snapshots and coordinator artifact admission."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import operator_fabric as base
import operator_policy as op

ARTIFACT_MANIFEST_SCHEMA = "hermes.fabric-artifact-manifest/v1"
ARTIFACT_CHUNK_SCHEMA = "hermes.fabric-artifact-chunk/v1"
FEATURE_ARTIFACT = "artifact-transfer-v1"
FEATURE_ARTIFACT_SNAPSHOT = "artifact-snapshot-v1"
ARTIFACT_FEATURES = frozenset({FEATURE_ARTIFACT, FEATURE_ARTIFACT_SNAPSHOT})

MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CHUNK_BYTES = 48 * 1024
_MAX_LOGICAL_NAME = 512
_WINDOWS_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_SECRET_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|\.ssh(?:/|$)|id_rsa(?:\.|$)|id_ed25519(?:\.|$)|"
    r"credentials?(?:[./]|$)|secrets?(?:[./]|$)|tokens?(?:[./]|$))",
    re.IGNORECASE,
)
_ACTIVE_MEDIA = frozenset(
    {
        "text/html",
        "image/svg+xml",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
    }
)


def migrate_peer(path: Path) -> None:
    base._init_peer_db(path)
    with base._connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              dispatch_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              logical_name TEXT NOT NULL,
              snapshot_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              media_type TEXT NOT NULL,
              active_content INTEGER NOT NULL,
              finalized_at TEXT NOT NULL,
              UNIQUE(attempt_id, logical_name)
            );
            CREATE INDEX IF NOT EXISTS peer_artifacts_attempt_idx ON artifacts(attempt_id);
            """
        )


def migrate_coordinator(path: Path) -> None:
    base._init_coordinator_db(path)
    with base._connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_requests (
              attempt_id TEXT PRIMARY KEY,
              dispatch_id TEXT NOT NULL,
              contract_sha256 TEXT NOT NULL,
              specs_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_admissions (
              artifact_id TEXT PRIMARY KEY,
              attempt_id TEXT NOT NULL,
              dispatch_id TEXT NOT NULL,
              logical_name TEXT NOT NULL,
              admission_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              media_type TEXT NOT NULL,
              active_content INTEGER NOT NULL,
              admitted_at TEXT NOT NULL,
              UNIQUE(attempt_id, logical_name)
            );
            CREATE INDEX IF NOT EXISTS coordinator_artifacts_attempt_idx
              ON artifact_admissions(attempt_id);
            """
        )


def logical_name(value: Any) -> str:
    name = base._bounded_string(value, field="artifact logical name", maximum=_MAX_LOGICAL_NAME)
    if "\\" in name or name.startswith("/") or _WINDOWS_ABS_RE.match(name):
        raise base.FabricError(
            "FABRIC_ARTIFACT_LOGICAL_NAME_INVALID",
            "remote artifact names must be relative POSIX logical paths",
        )
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise base.FabricError(
            "FABRIC_ARTIFACT_LOGICAL_NAME_INVALID",
            "remote artifact name is not a safe relative logical path",
        )
    normalized = path.as_posix()
    if _SECRET_RE.search(normalized):
        raise base.FabricError(
            "FABRIC_ARTIFACT_SECRET_PATH",
            "remote artifact logical name is denied by secret-path policy",
        )
    return normalized


def contract_specs(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("expected_artifacts") or []
    if not isinstance(raw, list) or len(raw) > MAX_ARTIFACTS:
        raise base.FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "expected_artifacts is not bounded")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise base.FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "expected artifact must be an object")
        name = logical_name(item.get("path"))
        if name in seen:
            raise base.FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact logical names must be unique")
        min_bytes = item.get("min_bytes", 0)
        if isinstance(min_bytes, bool) or not isinstance(min_bytes, int) or min_bytes < 0:
            raise base.FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact min_bytes is invalid")
        out.append(
            {
                "path": name,
                "must_exist": bool(item.get("must_exist", True)),
                "min_bytes": min_bytes,
            }
        )
        seen.add(name)
    return out


class PeerArtifactStore:
    def __init__(self, db_path: Path, root: Path) -> None:
        self.db_path = db_path
        self.root = root
        migrate_peer(db_path)

    @staticmethod
    def _manifest_item(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "logical_name": row["logical_name"],
            "size_bytes": int(row["size_bytes"]),
            "sha256": row["sha256"],
            "media_type": row["media_type"],
            "active_content": bool(row["active_content"]),
            "finalized_at": row["finalized_at"],
        }

    def _source(self, mapping: base.WorkspaceMapping, name: str) -> Path:
        name = logical_name(name)
        current = mapping.local_path
        for part in PurePosixPath(name).parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise base.FabricError(
                        "FABRIC_ARTIFACT_SYMLINK_REJECTED",
                        "artifact path contains a symlink",
                    )
            except OSError as exc:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_UNREADABLE",
                    "artifact path could not be inspected",
                ) from exc
        resolved = current.resolve(strict=True)
        if op.is_denied_path(resolved) or not op.path_under_allowed(resolved, [mapping.local_path]):
            raise base.FabricError(
                "FABRIC_ARTIFACT_PATH_REJECTED",
                "artifact path is outside the approved workspace",
            )
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise base.FabricError(
                "FABRIC_ARTIFACT_UNREADABLE",
                "artifact could not be inspected",
            ) from exc
        if not stat.S_ISREG(mode):
            raise base.FabricError(
                "FABRIC_ARTIFACT_SPECIAL_FILE_REJECTED",
                "artifact must be a regular file",
            )
        return resolved

    def snapshot(
        self,
        attempt: sqlite3.Row,
        mapping: base.WorkspaceMapping,
        name: str,
    ) -> dict[str, Any]:
        name = logical_name(name)
        with base._connect_readonly(self.db_path) as db:
            existing = db.execute(
                "SELECT * FROM artifacts WHERE attempt_id=? AND logical_name=?",
                (attempt["attempt_id"], name),
            ).fetchone()
        if existing is not None:
            return self._manifest_item(existing)

        source = self._source(mapping, name)
        artifact_id = "fart-" + hashlib.sha256(
            f"{attempt['attempt_id']}:{name}".encode()
        ).hexdigest()[:32]
        target_dir = self.root / attempt["attempt_id"]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_dir / f"{artifact_id}.blob"
        temp = target.with_suffix(".tmp")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(source, flags)
        digest = hashlib.sha256()
        total = 0
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise base.FabricError(
                    "FABRIC_ARTIFACT_SPECIAL_FILE_REJECTED",
                    "artifact must be a regular file",
                )
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise base.FabricError("FABRIC_ARTIFACT_TOO_LARGE", "artifact exceeds peer size policy")
            with os.fdopen(fd, "rb", closefd=False) as source_fh, temp.open("wb") as target_fh:
                while True:
                    chunk = source_fh.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARTIFACT_BYTES:
                        raise base.FabricError(
                            "FABRIC_ARTIFACT_TOO_LARGE",
                            "artifact exceeds peer size policy",
                        )
                    digest.update(chunk)
                    target_fh.write(chunk)
                target_fh.flush()
                os.fsync(target_fh.fileno())
            after = os.fstat(fd)
            identity_changed = (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or total != after.st_size
            )
            if identity_changed:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_UNSTABLE",
                    "artifact changed while being finalized",
                )
        finally:
            os.close(fd)
        temp.chmod(0o600)
        temp.replace(target)
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT INTO artifacts"
                "(artifact_id,attempt_id,dispatch_id,contract_sha256,logical_name,snapshot_path,size_bytes,sha256,media_type,active_content,finalized_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    attempt["attempt_id"],
                    attempt["dispatch_id"],
                    attempt["contract_sha256"],
                    name,
                    str(target),
                    total,
                    digest.hexdigest(),
                    media_type,
                    int(media_type in _ACTIVE_MEDIA),
                    base._now(),
                ),
            )
        with base._connect_readonly(self.db_path) as db:
            saved = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if saved is None:
            raise base.FabricError(
                "FABRIC_ARTIFACT_SNAPSHOT_FAILED",
                "artifact snapshot journal write failed",
            )
        return self._manifest_item(saved)

    def manifest(
        self,
        attempt: sqlite3.Row,
        mapping: base.WorkspaceMapping,
        specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if len(specs) > MAX_ARTIFACTS:
            raise base.FabricError("FABRIC_ARTIFACT_POLICY_INVALID", "artifact request is not bounded")
        items: list[dict[str, Any]] = []
        total = 0
        for spec in specs:
            try:
                item = self.snapshot(attempt, mapping, spec["path"])
            except FileNotFoundError:
                if spec["must_exist"]:
                    raise base.FabricError(
                        "FABRIC_ARTIFACT_MISSING",
                        "required artifact is missing",
                    ) from None
                continue
            total += int(item["size_bytes"])
            if total > MAX_TOTAL_ARTIFACT_BYTES:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_TOTAL_TOO_LARGE",
                    "artifact set exceeds bounded total",
                )
            items.append(item)
        manifest = {
            "schema": ARTIFACT_MANIFEST_SCHEMA,
            "version": 1,
            "dispatch_id": attempt["dispatch_id"],
            "attempt_id": attempt["attempt_id"],
            "contract_sha256": attempt["contract_sha256"],
            "node_name": attempt["node_name"],
            "artifacts": items,
            "total_bytes": total,
            "created_at": base._now(),
        }
        manifest["manifest_sha256"] = base.sha256_json(manifest)
        return manifest

    def chunk(
        self,
        *,
        dispatch_id: str,
        attempt_id: str,
        artifact_id: str,
        offset: int,
        maximum: int,
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= maximum <= MAX_CHUNK_BYTES:
            raise base.FabricError(
                "FABRIC_ARTIFACT_CHUNK_INVALID",
                "artifact chunk bounds are invalid",
            )
        with base._connect_readonly(self.db_path) as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND attempt_id=? AND dispatch_id=?",
                (artifact_id, attempt_id, dispatch_id),
            ).fetchone()
        if row is None:
            raise base.FabricError(
                "FABRIC_ARTIFACT_NOT_FOUND",
                "artifact snapshot is not present",
            )
        snapshot = Path(row["snapshot_path"])
        try:
            if snapshot.is_symlink():
                raise base.FabricError(
                    "FABRIC_ARTIFACT_SNAPSHOT_INVALID",
                    "artifact snapshot may not be a symlink",
                )
            snapshot_stat = snapshot.stat()
        except OSError as exc:
            raise base.FabricError(
                "FABRIC_ARTIFACT_SNAPSHOT_INVALID",
                "artifact snapshot cannot be inspected",
            ) from exc
        size = int(row["size_bytes"])
        if not stat.S_ISREG(snapshot_stat.st_mode) or snapshot_stat.st_size != size or offset > size:
            raise base.FabricError(
                "FABRIC_ARTIFACT_SNAPSHOT_INVALID",
                "artifact snapshot identity changed",
            )
        with snapshot.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read(maximum)
        next_offset = offset + len(raw)
        return {
            "schema": ARTIFACT_CHUNK_SCHEMA,
            "version": 1,
            "dispatch_id": dispatch_id,
            "attempt_id": attempt_id,
            "artifact_id": artifact_id,
            "offset": offset,
            "next_offset": next_offset,
            "size_bytes": size,
            "sha256": row["sha256"],
            "data_b64": base64.b64encode(raw).decode("ascii"),
            "eof": next_offset == size,
        }


class CoordinatorArtifactStore:
    def __init__(self, db_path: Path, root: Path) -> None:
        self.db_path = db_path
        self.root = root
        migrate_coordinator(db_path)

    def remember(
        self,
        *,
        attempt_id: str,
        dispatch_id: str,
        contract_sha256: str,
        specs: list[dict[str, Any]],
    ) -> None:
        if not specs:
            return
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO artifact_requests"
                "(attempt_id,dispatch_id,contract_sha256,specs_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    attempt_id,
                    dispatch_id,
                    contract_sha256,
                    base.canonical_json(specs),
                    base._now(),
                ),
            )

    def specs(self, attempt_id: str) -> list[dict[str, Any]] | None:
        with base._connect_readonly(self.db_path) as db:
            row = db.execute(
                "SELECT specs_json FROM artifact_requests WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        raw = base.strict_json_loads(row["specs_json"], maximum=32_000)
        if not isinstance(raw, list):
            raise base.FabricError(
                "FABRIC_ARTIFACT_POLICY_INVALID",
                "stored artifact request is invalid",
            )
        return raw

    @staticmethod
    def validate_manifest(
        manifest: Any,
        *,
        attempt: sqlite3.Row,
        dispatch: sqlite3.Row,
        node: base.FabricNode,
        specs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest = base._closed(
            manifest,
            required={
                "schema",
                "version",
                "dispatch_id",
                "attempt_id",
                "contract_sha256",
                "node_name",
                "artifacts",
                "total_bytes",
                "created_at",
                "manifest_sha256",
            },
            name="artifact manifest",
        )
        digest_value = {
            key: value for key, value in manifest.items() if key != "manifest_sha256"
        }
        if manifest["manifest_sha256"] != base.sha256_json(digest_value):
            raise base.FabricError(
                "FABRIC_ARTIFACT_MANIFEST_REJECTED",
                "artifact manifest digest is invalid",
            )
        if (
            manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA
            or manifest["version"] != 1
            or manifest["dispatch_id"] != attempt["dispatch_id"]
            or manifest["attempt_id"] != attempt["attempt_id"]
            or manifest["contract_sha256"] != dispatch["contract_sha256"]
            or manifest["node_name"] != node.name
        ):
            raise base.FabricError(
                "FABRIC_ARTIFACT_LINEAGE_MISMATCH",
                "artifact manifest lineage does not match attempt",
            )
        raw_items = manifest["artifacts"]
        if not isinstance(raw_items, list) or len(raw_items) > MAX_ARTIFACTS:
            raise base.FabricError(
                "FABRIC_ARTIFACT_MANIFEST_REJECTED",
                "artifact manifest is not bounded",
            )
        spec_map = {spec["path"]: spec for spec in specs}
        seen: set[str] = set()
        clean: list[dict[str, Any]] = []
        total = 0
        for raw in raw_items:
            item = base._closed(
                raw,
                required={
                    "artifact_id",
                    "logical_name",
                    "size_bytes",
                    "sha256",
                    "media_type",
                    "active_content",
                    "finalized_at",
                },
                name="artifact manifest item",
            )
            artifact_id = base._bounded_string(
                item["artifact_id"],
                field="artifact_id",
                pattern=base._ID_RE,
            )
            name = logical_name(item["logical_name"])
            if name not in spec_map or name in seen:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_MANIFEST_REJECTED",
                    "manifest contains unexpected or duplicate artifact",
                )
            size = item["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ARTIFACT_BYTES:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_TOO_LARGE",
                    "artifact size is invalid or exceeds bound",
                )
            digest = base._bounded_string(
                item["sha256"],
                field="artifact.sha256",
                pattern=base._SHA_RE,
            )
            media_type = base._bounded_string(
                item["media_type"],
                field="artifact.media_type",
                maximum=128,
            )
            if not isinstance(item["active_content"], bool):
                raise base.FabricError(
                    "FABRIC_ARTIFACT_MANIFEST_REJECTED",
                    "artifact active_content must be boolean",
                )
            total += size
            if total > MAX_TOTAL_ARTIFACT_BYTES:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_TOTAL_TOO_LARGE",
                    "artifact manifest exceeds total bound",
                )
            clean.append(
                {
                    "artifact_id": artifact_id,
                    "logical_name": name,
                    "size_bytes": size,
                    "sha256": digest,
                    "media_type": media_type,
                    "active_content": item["active_content"],
                    "finalized_at": str(item["finalized_at"]),
                }
            )
            seen.add(name)
        for spec in specs:
            if spec["must_exist"] and spec["path"] not in seen:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_MISSING",
                    "required artifact is absent from peer manifest",
                )
        if manifest["total_bytes"] != total:
            raise base.FabricError(
                "FABRIC_ARTIFACT_MANIFEST_REJECTED",
                "artifact manifest total is inconsistent",
            )
        return clean

    def pull(
        self,
        *,
        attempt: sqlite3.Row,
        node: base.FabricNode,
        item: dict[str, Any],
        rpc: Callable[[base.FabricNode, dict[str, Any], int], tuple[str | None, dict[str, Any]]],
        timeout: int,
    ) -> dict[str, Any]:
        target_dir = self.root / attempt["attempt_id"]
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = target_dir / f"{item['artifact_id']}.blob"
        temp = target.with_suffix(".tmp")
        digest = hashlib.sha256()
        offset = 0
        try:
            with temp.open("wb") as fh:
                while offset < item["size_bytes"]:
                    _, response = rpc(
                        node,
                        base._request(
                            "artifact_chunk",
                            node.coordinator_principal,
                            data={
                                "artifact_id": item["artifact_id"],
                                "offset": offset,
                                "max_bytes": min(
                                    MAX_CHUNK_BYTES,
                                    item["size_bytes"] - offset,
                                ),
                            },
                            dispatch_id=attempt["dispatch_id"],
                            attempt_id=attempt["attempt_id"],
                        ),
                        timeout,
                    )
                    response = base._validate_response(
                        response,
                        operation="artifact_chunk",
                    )
                    wrapper = base._closed(
                        response["data"],
                        required={"chunk"},
                        name="artifact chunk response",
                    )
                    chunk = base._closed(
                        wrapper["chunk"],
                        required={
                            "schema",
                            "version",
                            "dispatch_id",
                            "attempt_id",
                            "artifact_id",
                            "offset",
                            "next_offset",
                            "size_bytes",
                            "sha256",
                            "data_b64",
                            "eof",
                        },
                        name="artifact chunk",
                    )
                    if (
                        chunk["schema"] != ARTIFACT_CHUNK_SCHEMA
                        or chunk["version"] != 1
                        or chunk["dispatch_id"] != attempt["dispatch_id"]
                        or chunk["attempt_id"] != attempt["attempt_id"]
                        or chunk["artifact_id"] != item["artifact_id"]
                        or chunk["offset"] != offset
                        or chunk["size_bytes"] != item["size_bytes"]
                        or chunk["sha256"] != item["sha256"]
                    ):
                        raise base.FabricError(
                            "FABRIC_ARTIFACT_LINEAGE_MISMATCH",
                            "artifact chunk lineage mismatch",
                        )
                    try:
                        raw = base64.b64decode(chunk["data_b64"], validate=True)
                    except (ValueError, TypeError) as exc:
                        raise base.FabricError(
                            "FABRIC_ARTIFACT_CHUNK_INVALID",
                            "artifact chunk is not valid base64",
                        ) from exc
                    next_offset = chunk["next_offset"]
                    if (
                        not isinstance(next_offset, int)
                        or next_offset != offset + len(raw)
                        or next_offset > item["size_bytes"]
                        or (not raw and next_offset < item["size_bytes"])
                    ):
                        raise base.FabricError(
                            "FABRIC_ARTIFACT_CHUNK_INVALID",
                            "artifact chunk offset progression is invalid",
                        )
                    digest.update(raw)
                    fh.write(raw)
                    offset = next_offset
                    if bool(chunk["eof"]) != (offset == item["size_bytes"]):
                        raise base.FabricError(
                            "FABRIC_ARTIFACT_CHUNK_INVALID",
                            "artifact EOF marker is inconsistent",
                        )
                fh.flush()
                os.fsync(fh.fileno())
            if offset != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise base.FabricError(
                    "FABRIC_ARTIFACT_HASH_MISMATCH",
                    "artifact bytes failed coordinator verification",
                )
            temp.chmod(0o600)
            temp.replace(target)
        except Exception:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        with base._connect(self.db_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO artifact_admissions"
                "(artifact_id,attempt_id,dispatch_id,logical_name,admission_path,size_bytes,sha256,media_type,active_content,admitted_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item["artifact_id"],
                    attempt["attempt_id"],
                    attempt["dispatch_id"],
                    item["logical_name"],
                    str(target),
                    item["size_bytes"],
                    item["sha256"],
                    item["media_type"],
                    int(item["active_content"]),
                    base._now(),
                ),
            )
        return {
            "artifact_id": item["artifact_id"],
            "logical_name": item["logical_name"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
            "media_type": item["media_type"],
            "active_content": item["active_content"],
            "admitted": True,
        }
