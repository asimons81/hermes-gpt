"""Mission-scoped spend envelope (budget_accounts + budget_check) for Hermes GPT v0.9.

Implements architecture proposal §17 item 4 (mirrors §6.2 / D3 / §10.6 INV-8,
INV-9): a mission-scoped spend envelope plus a read-only ``budget_check`` surface.

**Phase 2 — dry-run.** This slice only:
  - creates / reads a ``budget_accounts`` row per Mission (``quota`` spend limit,
    ``spend`` accumulator, ``policy`` JSON);
  - records spend increments (``budget_record``) and appends a ``budget_events``
    row;
  - evaluates the envelope (``budget_check``) and reports, per D3, what the
    on-crossing behavior *would* be.

It **never** enforces-pause: the hard-block wiring (pause the Mission + raise a
human ``budget_breaker`` signal) is Phase 4/5. The D3 on-crossing semantics are
designed here and carried in the policy behind a ``hard_block_enabled`` flag that
defaults to **off**; even when on, this slice only *reports* ``would_pause`` and
never mutates the Mission lifecycle. See ``docs/design/v0.9-budget-hardblock.md``
for the full hard-block path.

Invariants enforced here:
- **INV-8 (spend envelope).** ``spend`` is a bounded non-negative number; the
  envelope is ``spend < quota -> within``, ``spend >= quota -> crossing``. The
  controller obeys ``min(ceiling, budget_envelope)``; within-envelope spend is
  not gated (D3).
- **INV-9 (data containment).** ``budget_accounts`` / ``budget_events`` hold only
  bounded metadata, refs and SHA-256 hashes — no raw prompt, transcript, memory
  body, credential, or secret-path content. A redaction check rejects secret-like
  policy input, and ``ref`` is validated against the ref alphabet; ``reason`` is
  bounded and its SHA-256 is recorded.

Read surfaces require ``read_only``; envelope set / spend record require
``workspace`` + ``direct`` (dry-run-first). Every call is audited.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import operator_mission_runtime as mission
import operator_policy as op

SCHEMA_VERSION = "0.9-budget.1"
POLICY_SCHEMA = "hermes.budget-policy/v1"
ACCOUNT_SCHEMA = "hermes.budget-account/v1"
EVENT_SCHEMA = "hermes.budget-event/v1"

MISSION_ID_RE = mission.MISSION_ID_RE
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
UNIT_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SIGNAL_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")

# Supported spend units (bounded, deterministic).
UNITS = ("tokens", "minutes", "usd")
DEFAULT_UNIT = "tokens"

# Authorization classes (matched to the Plan node vocabulary).
AUTH_CLASSES = ("read_only", "reversible_write", "high_impact")
DEFAULT_AUTH_CLASS = "reversible_write"

# Bounded numeric limits.
MAX_QUOTA = 1_000_000_000_000.0  # 1e12
MAX_SPEND = 1_000_000_000_000.0  # 1e12
MAX_AMOUNT = 1_000_000_000_000.0  # 1e12
MAX_REF = 256
MAX_REASON = 200
MAX_SIGNAL = 64

# Envelope statuses.
STATUS_WITHIN = "within"
STATUS_CROSSING = "crossing"
STATUS_INVALID = "invalid"


# ---------------------------------------------------------------------------
# Time / root / DB helpers (reuse the Mission runtime store)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(hermes_root: Path | None) -> Path:
    return mission._db_path(hermes_root)


def _init_budget_tables(db: sqlite3.Connection) -> None:
    """Addive budget tables. IF NOT EXISTS, so coexists with Mission runtime."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS budget_accounts (
            mission_id TEXT PRIMARY KEY,
            quota REAL NOT NULL,
            spend REAL NOT NULL DEFAULT 0,
            unit TEXT NOT NULL DEFAULT 'tokens',
            policy_json TEXT NOT NULL DEFAULT '{}',
            account_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS budget_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            amount REAL NOT NULL,
            spend_after REAL NOT NULL,
            quota REAL NOT NULL,
            status TEXT NOT NULL,
            hard_block INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL DEFAULT 'spend_record',
            ref TEXT NOT NULL DEFAULT '',
            reason_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_budget_accounts_mission ON budget_accounts(mission_id);
        CREATE INDEX IF NOT EXISTS idx_budget_events_mission ON budget_events(mission_id, seq);
        """
    )
    db.commit()


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if write:
        db = mission._connect(path, write=True)
        _init_budget_tables(db)
        return db
    return mission._connect(path, write=False)


def _begin_write(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")


def _audit(
    tool: str,
    policy: op.OperatorPolicy,
    *,
    dry_run: bool,
    success: bool,
    changed: bool,
    mission_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        op.audit_record(
            tool=tool,
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=dry_run,
            success=success,
            changed=changed,
            summary=f"{tool} mission={mission_id}",
            extra={"mission_id": mission_id, **(extra or {})},
        )
    except (OSError, TypeError, ValueError):
        return


def _error(exc: Exception, code: str, action: str) -> str:
    return json.dumps(
        op.error_from_exception(
            exc, layer="operator", code=code, suggested_action=action
        )
    )


# ---------------------------------------------------------------------------
# Bounded numeric / policy helpers
# ---------------------------------------------------------------------------


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


def _clean_num(value: Any, *, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or value is None:
        raise TypeError(f"{field} must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise TypeError(f"{field} must be a number") from None
    if not math.isfinite(num):
        raise ValueError(f"{field} must be finite")
    if num < minimum or num > maximum:
        raise ValueError(f"{field} out of range ({minimum}..{maximum})")
    return num


def _clean_policy(raw: Any) -> dict[str, Any]:
    """Validate + canonicalize a budget policy document (INV-9 checked)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise TypeError("policy must be a JSON object")
    schema = str(raw.get("schema", POLICY_SCHEMA)).strip()
    if schema != POLICY_SCHEMA:
        raise ValueError(f"budget policy schema must be {POLICY_SCHEMA!r}")

    unit = str(raw.get("unit", DEFAULT_UNIT)).strip().lower()
    if unit not in UNITS:
        raise ValueError(f"budget policy unit must be one of {list(UNITS)}")

    hard_block = raw.get("hard_block_enabled", False)
    if not isinstance(hard_block, bool):
        raise TypeError("budget policy hard_block_enabled must be boolean")
    if hard_block:
        # Phase 2: the hard-stop semantics are designed (D3) but wired in Phase
        # 4/5. Tempting to reject enabling, but the card says "implemented behind
        # a flag, default off" — so we allow it on but never enforce here.
        pass

    pause_on_cross = raw.get("pause_on_cross", True)
    if not isinstance(pause_on_cross, bool):
        raise TypeError("budget policy pause_on_cross must be boolean")

    signal = str(raw.get("breaker_signal", "budget_breaker")).strip().lower()
    if not SIGNAL_RE.fullmatch(signal):
        raise ValueError(
            f"budget policy breaker_signal must match {SIGNAL_RE.pattern!r}"
        )

    auth_class = str(raw.get("authorization_class", DEFAULT_AUTH_CLASS)).strip().lower()
    if auth_class not in AUTH_CLASSES:
        raise ValueError(
            f"budget policy authorization_class must be one of {list(AUTH_CLASSES)}"
        )

    canonical = {
        "schema": POLICY_SCHEMA,
        "unit": unit,
        "hard_block_enabled": bool(hard_block),
        "pause_on_cross": bool(pause_on_cross),
        "breaker_signal": signal,
        "authorization_class": auth_class,
    }
    # INV-9: never allow a secret-like policy into a durable store.
    if op.redact_output(json.dumps(canonical)) != json.dumps(canonical):
        raise PermissionError("budget policy contains secret-like durable values")
    return canonical


def _account_sha256(quota: float, policy: dict[str, Any]) -> str:
    skeleton = {"quota": quota, "policy": policy}
    enc = json.dumps(
        skeleton, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(enc.encode("utf-8")).hexdigest()


def _envelope_status(spend: float, quota: float, unit: str) -> dict[str, Any]:
    """Evaluate spend vs quota. INV-8: within is un-gated, crossing is the edge."""
    if quota <= 0 or not math.isfinite(spend) or not math.isfinite(quota):
        return {
            "unit": unit,
            "spend": spend,
            "quota": quota,
            "status": STATUS_INVALID,
            "within_envelope": False,
            "crosses_envelope": False,
            "consumed": 0.0,
        }
    consumed = spend / quota
    crosses = spend >= quota
    return {
        "unit": unit,
        "spend": spend,
        "quota": quota,
        "status": STATUS_CROSSING if crosses else STATUS_WITHIN,
        "within_envelope": not crosses,
        "crosses_envelope": crosses,
        "consumed": round(consumed, 6),
        "utilization_percent": round(consumed * 100.0, 4),
        "remaining": round(max(0.0, quota - spend), 6),
    }


def _would_block(env: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """D3 on-crossing behavior: designed now; enforcement is Phase 4/5."""
    crossing = env.get("crosses_envelope", False)
    hb = bool(policy["hard_block_enabled"])
    pause = bool(policy["pause_on_cross"])
    return {
        "hard_block_enabled": hb,
        "pause_on_cross": pause,
        "breaker_signal": policy["breaker_signal"],
        # Would the designed D3 hard-stop fire? (Enforcement NOT executed here.)
        "would_pause": crossing and hb and pause,
        "crosses_envelope": crossing,
        # Phase 2 is always dry-run w.r.t. the Mission lifecycle.
        "enforced_now": False,
        "enforcement_phase": "phase_4_5",
    }


def _account_view(row: sqlite3.Row) -> dict[str, Any]:
    policy = json.loads(row["policy_json"])
    spend = float(row["spend"])
    quota = float(row["quota"])
    unit = row["unit"]
    return {
        "schema": ACCOUNT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "mission_id": row["mission_id"],
        "spend": spend,
        "quota": quota,
        "unit": unit,
        "policy": policy,
        "account_sha256": row["account_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "envelope": _envelope_status(spend, quota, unit),
        "block": _would_block(_envelope_status(spend, quota, unit), policy),
    }


def _get_account_row(db: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
    if not MISSION_ID_RE.fullmatch(mission_id):
        raise ValueError("mission_id is invalid")
    row = db.execute(
        "SELECT * FROM budget_accounts WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"budget account {mission_id!r} not found")
    return row


def _account_table_exists(db: sqlite3.Connection) -> bool:
    """Return whether the budget_accounts table exists (pre-first-set reads)."""
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_accounts'"
    ).fetchone()
    return row is not None


def _read_account(db: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    row = _get_account_row(db, mission_id)
    return _account_view(row)


# ---------------------------------------------------------------------------
# Budget envelope CRUD
# ---------------------------------------------------------------------------


def hermes_budget_set(
    mission_id: str,
    quota: float,
    policy_json: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Create (or update) the mission-scoped spend envelope (budget_accounts).

    ``quota`` is the spend limit; ``policy_json`` carries unit, hard-block flag
    (default off), on-crossing behavior, and breaker signal. This writes only the
    budget store; it never pauses a Mission and never mutates its lifecycle.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        quota_num = _clean_num(
            quota, field="quota", minimum=0.000001, maximum=MAX_QUOTA
        )
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct budget set requires confirm=true")

        parsed_policy = _clean_policy(
            json.loads(policy_json) if policy_json.strip() else {}
        )
        acct_sha = _account_sha256(quota_num, parsed_policy)

        if effective_dry:
            # Read current (if any) to report the delta; never write.
            view = None
            path = _db_path(hermes_root)
            if path.is_file():
                with _connect(path, write=False) as db:
                    try:
                        if _account_table_exists(db):
                            view = _read_account(db, mission_id)
                    except LookupError:
                        view = None
                    except sqlite3.OperationalError:
                        view = None
            reported = {"version": 0, **({"prior": view} if view else {})}
            _audit(
                "hermes_budget_set",
                policy,
                dry_run=True,
                success=True,
                changed=False,
                mission_id=mission_id,
                extra={"quota": quota_num, "unit": parsed_policy["unit"]},
            )
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "tool": "hermes_budget_set",
                    "mission_id": mission_id,
                    "quota": quota_num,
                    "unit": parsed_policy["unit"],
                    "policy": parsed_policy,
                    "account_sha256": acct_sha,
                    "changed": False,
                    "dry_run": True,
                    **reported,
                }
            )

        path = _db_path(hermes_root)
        with _connect(path, write=True) as db:
            _begin_write(db)
            mission._get_row(db, mission_id)  # verify mission exists
            now = _now()
            existing = db.execute(
                "SELECT quota, unit, policy_json FROM budget_accounts WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE budget_accounts SET quota=?, unit=?, policy_json=?, account_sha256=?, updated_at=? WHERE mission_id=?",
                    (
                        quota_num,
                        parsed_policy["unit"],
                        json.dumps(parsed_policy, sort_keys=True),
                        acct_sha,
                        now,
                        mission_id,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO budget_accounts(mission_id,quota,spend,unit,policy_json,account_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        mission_id,
                        quota_num,
                        0.0,
                        parsed_policy["unit"],
                        json.dumps(parsed_policy, sort_keys=True),
                        acct_sha,
                        now,
                        now,
                    ),
                )
            db.commit()
        _audit(
            "hermes_budget_set",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={
                "quota": quota_num,
                "unit": parsed_policy["unit"],
                "updated_existing": bool(existing),
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_budget_set",
                "mission_id": mission_id,
                "quota": quota_num,
                "unit": parsed_policy["unit"],
                "policy": parsed_policy,
                "account_sha256": acct_sha,
                "changed": True,
                "dry_run": False,
                "updated_existing": bool(existing),
            }
        )
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        _audit(
            "hermes_budget_set",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "BUDGET_SET_REJECTED",
            "Check mission id, quota, policy schema, and Operator workspace/direct policy.",
        )


def hermes_budget_get(mission_id: str, hermes_root: Path | None = None) -> str:
    """Read the mission spend envelope (read-only)."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            if not _account_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
            try:
                view = _read_account(db, mission_id)
            except LookupError:
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
        view["success"] = True
        view["tool"] = "hermes_budget_get"
        view["found"] = True
        _audit(
            "hermes_budget_get",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
        )
        return json.dumps(view)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "BUDGET_READ_FAILED", "Check mission id and Operator read access."
        )


def hermes_budget_check(mission_id: str, hermes_root: Path | None = None) -> str:
    """The ``budget_check`` surface: evaluate the envelope against spend.

    Read-only w.r.t. the store. Reports whether spend is within or crossing the
    envelope and the would-be D3 behavior (pause + ``budget_breaker`` signal),
    with the note that enforcement is Phase 4/5 and is never executed here.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        path = _db_path(hermes_root)
        if not path.is_file():
            return json.dumps(
                {
                    "success": True,
                    "schema_version": SCHEMA_VERSION,
                    "mission_id": mission_id,
                    "found": False,
                }
            )
        with _connect(path, write=False) as db:
            if not _account_table_exists(db):
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
            try:
                view = _read_account(db, mission_id)
            except LookupError:
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "mission_id": mission_id,
                        "found": False,
                    }
                )
        env = view["envelope"]
        view["tool"] = "hermes_budget_check"
        view["found"] = True
        view["envelope_status"] = env["status"]
        view["crosses_envelope"] = env["crosses_envelope"]
        view["block"] = _would_block(env, view["policy"])
        view["success"] = True
        _audit(
            "hermes_budget_check",
            policy,
            dry_run=True,
            success=True,
            changed=False,
            mission_id=mission_id,
            extra={"status": env["status"], "crosses": env["crosses_envelope"]},
        )
        return json.dumps(view)
    except FileNotFoundError:
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "mission_id": mission_id,
                "found": False,
            }
        )
    except (ValueError, PermissionError, OSError, sqlite3.Error) as exc:
        return _error(
            exc, "BUDGET_CHECK_FAILED", "Check mission id and Operator read access."
        )


def hermes_budget_record(
    mission_id: str,
    amount: float,
    ref: str = "",
    reason: str = "",
    *,
    confirm: bool = False,
    dry_run: bool = True,
    hermes_root: Path | None = None,
) -> str:
    """Record a spend increment against a mission envelope (dry-run enforcement).

    Adds ``amount`` to the account ``spend`` and appends a ``budget_events`` row.
    Per D3, when the envelope crosses and ``hard_block_enabled``, the designed
    action is pause + ``budget_breaker`` signal — but Phase 2 is dry-run and
    **never executes pause or raises a signal**; it only reports ``would_pause``.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        policy.require_mutation(dry_run)
        if not MISSION_ID_RE.fullmatch(mission_id):
            raise ValueError("mission_id is invalid")
        amount_num = _clean_num(amount, field="amount", minimum=0.0, maximum=MAX_AMOUNT)
        ref_clean = _clean_text(ref, field="ref", maximum=MAX_REF) if ref else ""
        if ref_clean and not REF_RE.fullmatch(ref_clean):
            raise ValueError("budget record ref contains unsupported characters")
        reason_clean = (
            _clean_text(reason, field="reason", maximum=MAX_REASON) if reason else ""
        )
        reason_sha = (
            hashlib.sha256(reason_clean.encode("utf-8")).hexdigest()
            if reason_clean
            else ""
        )
        effective_dry = policy.effective_dry_run(dry_run)
        if not effective_dry and not confirm:
            raise PermissionError("direct budget record requires confirm=true")

        path = _db_path(hermes_root)
        if not path.is_file():
            raise LookupError(
                "missions database not found; no budget account available"
            )

        with _connect(path, write=not effective_dry) as db:
            if not effective_dry:
                _begin_write(db)
            account = _get_account_row(db, mission_id)  # raises if not found
            spend_before = float(account["spend"])
            quota = float(account["quota"])
            unit = account["unit"]
            policy_obj = json.loads(account["policy_json"])
            spend_after = spend_before + amount_num
            if spend_after < 0 or spend_after > MAX_SPEND:
                raise ValueError("spend after record is out of range")
            env_after = _envelope_status(spend_after, quota, unit)
            block = _would_block(env_after, policy_obj)

            if effective_dry:
                db.rollback()
                _audit(
                    "hermes_budget_record",
                    policy,
                    dry_run=True,
                    success=True,
                    changed=False,
                    mission_id=mission_id,
                    extra={
                        "amount": amount_num,
                        "would_cross": env_after["crosses_envelope"],
                    },
                )
                return json.dumps(
                    {
                        "success": True,
                        "schema_version": SCHEMA_VERSION,
                        "tool": "hermes_budget_record",
                        "mission_id": mission_id,
                        "amount": amount_num,
                        "spend_before": spend_before,
                        "spend_after": spend_after,
                        "quota": quota,
                        "status": env_after["status"],
                        "crosses_envelope": env_after["crosses_envelope"],
                        "block": block,
                        "changed": False,
                        "dry_run": True,
                        "enforcement": "not_executed_phase_2",
                    }
                )

            now = _now()
            db.execute(
                "UPDATE budget_accounts SET spend=?, updated_at=? WHERE mission_id=?",
                (spend_after, now, mission_id),
            )
            db.execute(
                "INSERT INTO budget_events(mission_id,amount,spend_after,quota,status,hard_block,event_type,ref,reason_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    mission_id,
                    amount_num,
                    spend_after,
                    quota,
                    env_after["status"],
                    int(policy_obj["hard_block_enabled"]),
                    "spend_record",
                    ref_clean,
                    reason_sha,
                    now,
                ),
            )
            db.commit()
        _audit(
            "hermes_budget_record",
            policy,
            dry_run=False,
            success=True,
            changed=True,
            mission_id=mission_id,
            extra={
                "amount": amount_num,
                "spend_after": spend_after,
                "crosses": env_after["crosses_envelope"],
                "would_pause": block["would_pause"],
            },
        )
        return json.dumps(
            {
                "success": True,
                "schema_version": SCHEMA_VERSION,
                "tool": "hermes_budget_record",
                "mission_id": mission_id,
                "amount": amount_num,
                "spend_before": spend_before,
                "spend_after": spend_after,
                "quota": quota,
                "status": env_after["status"],
                "crosses_envelope": env_after["crosses_envelope"],
                "block": block,
                "changed": True,
                "dry_run": False,
                "enforcement": "not_executed_phase_2",
                "reason_sha256": reason_sha,
            }
        )
    except (
        ValueError,
        TypeError,
        PermissionError,
        LookupError,
        OSError,
        sqlite3.Error,
    ) as exc:
        _audit(
            "hermes_budget_record",
            policy,
            dry_run=dry_run,
            success=False,
            changed=False,
            mission_id=mission_id,
        )
        return _error(
            exc,
            "BUDGET_RECORD_REJECTED",
            "Check mission id, amount, ref, and Operator workspace/direct policy.",
        )


# ---------------------------------------------------------------------------
# Invariant + status predicates (exposed for tests / controllers)
# ---------------------------------------------------------------------------


def validate_envelope(policy: dict[str, Any], spend: float, quota: float) -> bool:
    """Return whether a (policy, spend, quota) tuple is a valid INV-8 envelope."""
    try:
        canonical = _clean_policy(policy)
        quota_num = _clean_num(
            quota, field="quota", minimum=0.000001, maximum=MAX_QUOTA
        )
        spend_num = _clean_num(spend, field="spend", minimum=0.0, maximum=MAX_SPEND)
    except (ValueError, TypeError):
        return False
    return quota_num > 0 and spend_num >= 0 and canonical["unit"] in UNITS


def within_envelope(policy: dict[str, Any], spend: float, quota: float) -> bool:
    """INV-8 predicate: True when spend is strictly inside the envelope."""
    if not validate_envelope(policy, spend, quota):
        return False
    return float(spend) < float(quota)
