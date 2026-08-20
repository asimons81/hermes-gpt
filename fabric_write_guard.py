"""Durable G4-C write ownership and whole-tree execution-unit helpers."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import operator_fabric as base

FEATURE_WRITE_OWNERSHIP = "write-ownership-v1"
FEATURE_EXECUTION_UNIT = "execution-unit-v1"
FEATURE_WRITE_EPOCH = "write-epoch-v1"
WRITE_FEATURES = frozenset(
    {FEATURE_WRITE_OWNERSHIP, FEATURE_EXECUTION_UNIT, FEATURE_WRITE_EPOCH}
)
WRITE_AUTH = frozenset({"reversible_write", "high_impact"})


def is_write(value: dict[str, Any]) -> bool:
    authorization = value.get("authorization") or {}
    return str(authorization.get("class") or "") in WRITE_AUTH


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(db: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_peer(path: Path) -> None:
    base._init_peer_db(path)
    with base._connect(path) as db:
        _add_column(db, "attempts", "write_epoch", "INTEGER")
        _add_column(db, "attempts", "execution_unit_kind", "TEXT")
        _add_column(db, "attempts", "execution_unit_id", "TEXT")
        _add_column(db, "attempts", "retry_parent_attempt_id", "TEXT")
        _add_column(db, "write_claims", "epoch", "INTEGER NOT NULL DEFAULT 0")
        _add_column(db, "write_claims", "execution_unit_kind", "TEXT")
        _add_column(db, "write_claims", "execution_unit_id", "TEXT")
        _add_column(db, "write_claims", "release_proof", "TEXT")


def migrate_coordinator(path: Path) -> None:
    base._init_coordinator_db(path)
    with base._connect(path) as db:
        _add_column(db, "attempts", "write_epoch", "INTEGER")
        _add_column(db, "attempts", "retry_parent_attempt_id", "TEXT")


class SystemdUserUnitManager:
    """Use a Linux user-systemd service/cgroup as the verified writer unit."""

    def __init__(self) -> None:
        self.systemd_run = shutil.which("systemd-run")
        self.systemctl = shutil.which("systemctl")
        self._cached_at = 0.0
        self._cached_available = False

    def unit_name(self, attempt_id: str) -> str:
        digest = hashlib.sha256(attempt_id.encode()).hexdigest()[:24]
        return f"hermes-fabric-{digest}.service"

    def available(self) -> bool:
        now = time.monotonic()
        if now - self._cached_at < 15:
            return self._cached_available
        self._cached_at = now
        if not sys.platform.startswith("linux") or not self.systemd_run or not self.systemctl:
            self._cached_available = False
            return False
        try:
            result = subprocess.run(
                [self.systemctl, "--user", "show-environment"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._cached_available = False
        else:
            self._cached_available = result.returncode == 0
        return self._cached_available

    def launch(
        self,
        unit: str,
        task_id: str,
        workspace: Path,
        jobs_root: Path,
        worker_script: Path,
    ) -> dict[str, Any]:
        if not self.available():
            return {"accepted": False, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_UNAVAILABLE"}
        argv = [
            str(self.systemd_run),
            "--user",
            f"--unit={unit}",
            "--collect",
            "--quiet",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=5s",
            f"--working-directory={workspace}",
            sys.executable,
            str(worker_script),
            "--worker",
            task_id,
            "--root",
            str(jobs_root),
        ]
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"accepted": False, "ambiguous": True, "code": "FABRIC_EXECUTION_UNIT_START_AMBIGUOUS"}
        except OSError:
            return {"accepted": False, "ambiguous": False, "code": "FABRIC_EXECUTION_UNIT_START_FAILED"}
        return {
            "accepted": result.returncode == 0,
            "ambiguous": False,
            "code": (
                "FABRIC_EXECUTION_UNIT_STARTED"
                if result.returncode == 0
                else "FABRIC_EXECUTION_UNIT_START_FAILED"
            ),
        }

    def inspect(self, unit: str) -> dict[str, Any]:
        if not self.available():
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        try:
            result = subprocess.run(
                [
                    str(self.systemctl),
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=Result",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        props: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value
        load_state = props.get("LoadState", "")
        active_state = props.get("ActiveState", "")
        if result.returncode != 0 and load_state != "not-found":
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        active = active_state in {"active", "activating", "deactivating", "reloading"}
        quiescent = load_state == "not-found" or active_state in {"inactive", "failed", "dead"}
        return {
            "known": True,
            "active": active,
            "quiescent": quiescent,
            "state": active_state or ("not-found" if load_state == "not-found" else "unknown"),
            "result": props.get("Result", ""),
        }

    def stop(self, unit: str) -> dict[str, Any]:
        if not self.available():
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        try:
            subprocess.run(
                [str(self.systemctl), "--user", "stop", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"known": False, "active": False, "quiescent": False, "state": "unknown"}
        for _ in range(10):
            status = self.inspect(unit)
            if status.get("quiescent"):
                return status
            time.sleep(0.1)
        return self.inspect(unit)


class WriteClaims:
    """Non-expiring conflict-domain ownership with monotonic epochs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        migrate_peer(db_path)

    def acquire(
        self,
        db: sqlite3.Connection,
        *,
        conflict_domain: str,
        attempt_id: str,
        unit_id: str,
    ) -> int:
        claim = db.execute(
            "SELECT * FROM write_claims WHERE conflict_domain=?",
            (conflict_domain,),
        ).fetchone()
        if claim is not None and claim["state"] == "ACTIVE":
            if claim["attempt_id"] == attempt_id:
                return int(claim["epoch"] or 0)
            raise base.FabricError(
                "FABRIC_WRITE_OWNERSHIP_BLOCKED",
                "peer write conflict domain already has an active claim",
            )
        epoch = int(claim["epoch"] or 0) + 1 if claim is not None else 1
        db.execute(
            "INSERT OR REPLACE INTO write_claims"
            "(conflict_domain,attempt_id,state,acquired_at,released_at,epoch,execution_unit_kind,execution_unit_id,release_proof)"
            " VALUES(?,?,?,?,NULL,?,?,?,NULL)",
            (
                conflict_domain,
                attempt_id,
                "ACTIVE",
                base._now(),
                epoch,
                "systemd-user-unit",
                unit_id,
            ),
        )
        return epoch

    def state(self, attempt: sqlite3.Row) -> str:
        if attempt["write_epoch"] is None:
            return "NONE"
        with base._connect_readonly(self.db_path) as db:
            claim = db.execute(
                "SELECT state,attempt_id,epoch FROM write_claims WHERE conflict_domain=?",
                (attempt["conflict_domain"],),
            ).fetchone()
        if (
            claim is None
            or claim["attempt_id"] != attempt["attempt_id"]
            or int(claim["epoch"] or 0) != int(attempt["write_epoch"])
        ):
            return "SUPERSEDED"
        return str(claim["state"])

    def release(self, attempt: sqlite3.Row, *, proof: str) -> bool:
        epoch = attempt["write_epoch"]
        if epoch is None:
            return False
        with base._connect(self.db_path) as db:
            cursor = db.execute(
                "UPDATE write_claims SET state='RELEASED',released_at=?,release_proof=?"
                " WHERE conflict_domain=? AND attempt_id=? AND epoch=? AND state='ACTIVE'",
                (
                    base._now(),
                    proof[:128],
                    attempt["conflict_domain"],
                    attempt["attempt_id"],
                    int(epoch),
                ),
            )
        return bool(cursor.rowcount)
