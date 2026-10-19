"""Durable adoption lane for maintenance campaigns (AUTO-03).

AUTO-02 (``operator_campaign_controller``) reconciles ONE campaign per tick
from its manifest. Real operations run SEVERAL campaigns and must survive
restarts without re-adopting by hand, so this module adds the thin, additive
lane that owns exactly one durable artifact — the ordered registry of
campaign manifests under ``<data_root>/campaigns/lane.json`` — and nothing
else:

- ``register_campaign`` / ``unregister_campaign`` manage that registry.
  Registration validates the manifest via ONE dry-run controller tick and
  fails closed: a manifest that does not validate is never registered. The
  registry is idempotent by manifest path AND campaign_id, and refuses the
  same campaign_id from a different path (one campaign, one manifest, ever).
  Unregistration is containment only: the campaign's controller state
  (``campaigns/<id>/state.json``) and its missions/plans stay untouched.
- ``campaign_lane_run`` reads the registry FIRST (restart ⇒ read-then-adopt;
  the registry is the only lane-owned truth — mission/plan/run truth stays
  authoritative in the existing stores, per BOUNDARY.md "a caller, not a
  competing owner"), then makes one bounded pass: per registered campaign it
  calls the existing ``campaign_controller_tick`` verbatim (``apply`` mode)
  and persists per-campaign event cursor + last_tick to the lane state
  document (``campaigns/lane-state.json``). A campaign whose manifest is
  missing or fails validation fails THAT campaign only; the others are still
  ticked (mirrors the controller's no-monopoly doctrine).
- Between passes (``serve`` mode) the lane waits on the live-event bus using
  the ``operator_autopilot._wait_for_wakeup`` slice pattern across ALL
  registered campaigns' mission_ids. An event is a notification, never
  proof: it only ends the wait early and the next pass re-reads durable
  state exactly as it would after a timer wakeup. On an event every
  campaign's cursor jumps to the store's high-water mark instead of walking
  the backlog. A missing, delayed or erroring event store degrades to the
  timer and still honors the full wait — no stall, no busy spin.
- Bounds: ``max_ticks`` (passes), ``max_runtime`` (seconds) and ``mode=once``
  for systemd timer cadence (SCHED-02 "one trigger per family": the lane
  timer is THE single trigger). Every restart re-reads the registry and the
  controller re-derives every phase from truth, so a restart can never mint
  a duplicate mission.

There is no new scheduler, no dispatch path and no worker process here: the
lane is a CALLER of ``campaign_controller_tick`` and the live-event reads,
nothing more.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import operator_campaign_controller as ctrl
import operator_live_events as live_events
import operator_policy as op

SCHEMA_VERSION = "hermes.campaign-lane/v1"
REGISTRY_SCHEMA = "hermes.campaign-lane/v1"
LANE_STATE_SCHEMA = "hermes.campaign-lane-state/v1"

REGISTRY_FILENAME = "lane.json"
LANE_STATE_FILENAME = "lane-state.json"

# The registry is a bounded ordered lane, not an unbounded directory scan.
MAX_CAMPAIGNS = 32
MAX_MANIFEST_BYTES = ctrl.MAX_MANIFEST_BYTES

# Serve-mode idle poll (the timer backstop between event-driven wakeups).
TICK_SECONDS = 30.0
MIN_IDLE_POLL_SECONDS = 0.5
MAX_IDLE_POLL_SECONDS = 300.0
IDLE_POLL_ENV = "HERMES_GPT_CAMPAIGN_LANE_IDLE_SECONDS"

# Wakeup slices: an abort check or bounds re-evaluation is never delayed by
# more than one slice (mirrors operator_autopilot.WAIT_SLICE_SECONDS).
WAIT_SLICE_SECONDS = 1.0


class CampaignLaneError(ValueError):
    """Raised when the lane registry or a lane invocation is invalid."""


# ---------------------------------------------------------------------------
# Root/paths (same campaigns dir as the controller's state documents)
# ---------------------------------------------------------------------------

def _registry_path(hermes_root: Path | None) -> Path:
    return ctrl._campaigns_root(hermes_root) / REGISTRY_FILENAME


def _lane_state_path(hermes_root: Path | None) -> Path:
    return ctrl._campaigns_root(hermes_root) / LANE_STATE_FILENAME


def _now() -> str:
    return ctrl._now()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _safe_high_watermark(hermes_root: Path | None) -> int:
    try:
        return int(live_events.high_watermark(hermes_root=hermes_root))
    except (ValueError, TypeError, OSError, sqlite3.Error):
        return 0


# ---------------------------------------------------------------------------
# Durable registry (the ONLY lane-owned truth; fail closed on corruption)
# ---------------------------------------------------------------------------

def _load_registry(hermes_root: Path | None) -> list[dict[str, Any]]:
    path = _registry_path(hermes_root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignLaneError(f"lane registry is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != REGISTRY_SCHEMA:
        raise CampaignLaneError("lane registry schema mismatch")
    entries = raw.get("campaigns")
    if not isinstance(entries, list):
        raise CampaignLaneError("lane registry campaigns must be a list")
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CampaignLaneError("lane registry entry must be an object")
        campaign_id = entry.get("campaign_id")
        manifest_path = entry.get("manifest_path")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise CampaignLaneError("lane registry entry campaign_id is invalid")
        if not isinstance(manifest_path, str) or not manifest_path:
            raise CampaignLaneError(f"lane registry entry for {campaign_id!r} has no manifest_path")
        if campaign_id in seen_ids:
            raise CampaignLaneError(f"lane registry has duplicate campaign_id {campaign_id!r}")
        seen_ids.add(campaign_id)
    return entries


def _save_registry(entries: list[dict[str, Any]], hermes_root: Path | None) -> None:
    doc = {"schema": REGISTRY_SCHEMA, "campaigns": entries}
    _atomic_write(_registry_path(hermes_root), json.dumps(doc, sort_keys=True))


# ---------------------------------------------------------------------------
# Lane state (derived bookkeeping: cursors + last_tick; rebuilt if lost)
# ---------------------------------------------------------------------------

def _load_lane_state(hermes_root: Path | None) -> dict[str, Any]:
    path = _lane_state_path(hermes_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, dict) or raw.get("schema") != LANE_STATE_SCHEMA \
            or not isinstance(raw.get("campaigns"), dict):
        return {"schema": LANE_STATE_SCHEMA, "campaigns": {}}
    return raw


def _save_lane_state(state: dict[str, Any], hermes_root: Path | None) -> None:
    _atomic_write(_lane_state_path(hermes_root), json.dumps(state, sort_keys=True))


def _campaign_entry(state: dict[str, Any], campaign_id: str) -> dict[str, Any]:
    # NOTE: no event_cursor preset — the first pass snapshots it AFTER its
    # ticks (self-published events are stale notifications), via setdefault.
    entry = state["campaigns"].get(campaign_id)
    if not isinstance(entry, dict):
        entry = {"ticks": 0, "last_tick": None, "last_ok": None}
        state["campaigns"][campaign_id] = entry
    return entry


# ---------------------------------------------------------------------------
# Registry management
# ---------------------------------------------------------------------------

def register_campaign(
    manifest_path: str | os.PathLike[str],
    *,
    confirm: bool = False,
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Register one campaign manifest on the lane.

    Validates via ONE dry-run controller tick before registering and fails
    closed: a manifest that does not validate is never registered. Idempotent
    by manifest path AND campaign_id; refuses the same campaign_id from a
    different path. A manifest revised in place at a known path re-points the
    entry (same path, new campaign_id ⇒ replace, never a second entry).
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        if apply and not confirm:
            raise PermissionError("campaign lane register apply requires confirm=true")
        policy.require_mutation(not apply)
        effective_apply = apply and confirm and not policy.effective_dry_run(False)

        path = Path(manifest_path).expanduser().resolve()
        try:
            manifest_json = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CampaignLaneError(f"manifest unreadable at {path}: {exc}") from exc
        if len(manifest_json.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise CampaignLaneError("manifest exceeds the lane size bound")

        probe = json.loads(ctrl.campaign_controller_tick(manifest_json, hermes_root=hermes_root))
        if not probe.get("success"):
            raise CampaignLaneError(
                f"manifest failed its validation tick: {probe.get('error', 'unknown error')}")
        campaign_id = str(probe.get("campaign_id") or "")
        if not campaign_id:
            raise CampaignLaneError("validation tick did not report a campaign_id")

        entries = _load_registry(hermes_root)
        for entry in entries:
            same_id = entry["campaign_id"] == campaign_id
            same_path = entry["manifest_path"] == str(path)
            if same_id and same_path:
                op.audit_record(
                    tool="campaign_lane_register", level=policy.level, apply_mode=policy.apply_mode,
                    dry_run=not effective_apply, success=True, changed=False,
                    summary="campaign already registered (idempotent)",
                    extra={"campaign_id": campaign_id, "manifest_path": str(path)},
                )
                return json.dumps({
                    "success": True, "schema_version": SCHEMA_VERSION,
                    "tool": "campaign_lane_register", "campaign_id": campaign_id,
                    "manifest_path": str(path), "already_registered": True,
                    "dry_run": not effective_apply,
                })
            if same_id and not same_path:
                raise CampaignLaneError(
                    f"campaign_id {campaign_id!r} is already registered from a different "
                    f"manifest path: {entry['manifest_path']}")

        revised = [e for e in entries if e["manifest_path"] == str(path)]
        if not revised and len(entries) >= MAX_CAMPAIGNS:
            raise CampaignLaneError(f"lane registry exceeds {MAX_CAMPAIGNS} campaigns")

        entry = {"campaign_id": campaign_id, "manifest_path": str(path), "registered_at": _now()}
        if revised:
            entries[entries.index(revised[0])] = entry  # manifest revised in place
        else:
            entries.append(entry)
        if effective_apply:
            _save_registry(entries, hermes_root)
        op.audit_record(
            tool="campaign_lane_register", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not effective_apply, success=True, changed=True,
            summary="campaign registered on lane",
            extra={"campaign_id": campaign_id, "manifest_path": str(path)},
        )
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_register", "campaign_id": campaign_id,
            "manifest_path": str(path), "already_registered": False,
            "dry_run": not effective_apply, "campaigns": len(entries),
        })
    except (CampaignLaneError, ValueError, TypeError, PermissionError, LookupError,
            OSError, sqlite3.Error) as exc:
        op.audit_record(
            tool="campaign_lane_register", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not apply, success=False, changed=False,
            summary=f"campaign lane register rejected: {exc}",
        )
        return json.dumps({
            "success": False, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_register", "error": str(exc),
            "error_class": exc.__class__.__name__,
        })


def unregister_campaign(
    target: str,
    *,
    confirm: bool = False,
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Remove campaigns from the lane by campaign_id or manifest path.

    Containment only: the registry entry goes, the campaign's controller
    state (``campaigns/<id>/state.json``), missions and plans survive — the
    lane never mutates what it does not own.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        if apply and not confirm:
            raise PermissionError("campaign lane unregister apply requires confirm=true")
        policy.require_mutation(not apply)
        effective_apply = apply and confirm and not policy.effective_dry_run(False)

        resolved = Path(target).expanduser().resolve()
        entries = _load_registry(hermes_root)
        kept = [e for e in entries
                if e["campaign_id"] != target and e["manifest_path"] not in (target, str(resolved))]
        removed = [e for e in entries if e not in kept]
        if effective_apply and removed:
            _save_registry(kept, hermes_root)
            state = _load_lane_state(hermes_root)
            for entry in removed:
                state["campaigns"].pop(entry["campaign_id"], None)
            _save_lane_state(state, hermes_root)
        op.audit_record(
            tool="campaign_lane_unregister", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not effective_apply, success=True, changed=bool(removed),
            summary="campaign unregistered from lane",
            extra={"removed": [e["campaign_id"] for e in removed]},
        )
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_unregister", "removed": removed,
            "campaigns": len(kept), "dry_run": not effective_apply,
        })
    except (CampaignLaneError, ValueError, TypeError, PermissionError, LookupError,
            OSError, sqlite3.Error) as exc:
        op.audit_record(
            tool="campaign_lane_unregister", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not apply, success=False, changed=False,
            summary=f"campaign lane unregister rejected: {exc}",
        )
        return json.dumps({
            "success": False, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_unregister", "error": str(exc),
            "error_class": exc.__class__.__name__,
        })


# ---------------------------------------------------------------------------
# One bounded pass: tick every registered campaign, isolation per campaign
# ---------------------------------------------------------------------------

def _manifest_mission_ids(manifest_json: str) -> list[str]:
    """Mission ids declared by a manifest (wake targets exist pre-mint)."""
    try:
        raw = json.loads(manifest_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        return []
    ids: list[str] = []
    for seg in raw["segments"]:
        if not (isinstance(seg, dict) and isinstance(seg.get("campaign_json"), str)):
            continue
        try:
            doc = json.loads(seg["campaign_json"])
        except (ValueError, TypeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("mission_id"), str):
            ids.append(doc["mission_id"])
    return ids


def _one_pass(hermes_root: Path | None, *, apply: bool) -> dict[str, Any]:
    entries = _load_registry(hermes_root)
    state = _load_lane_state(hermes_root)
    views: list[dict[str, Any]] = []
    manifests: dict[str, str] = {}
    for entry in entries:
        campaign_id = entry["campaign_id"]
        tick: dict[str, Any]
        try:
            manifest_json = Path(entry["manifest_path"]).read_text(encoding="utf-8")
            manifests[campaign_id] = manifest_json
            tick = json.loads(ctrl.campaign_controller_tick(
                manifest_json, confirm=apply, apply=apply, hermes_root=hermes_root))
        except Exception as exc:  # one campaign never blocks the others
            tick = {"success": False, "error": f"{exc.__class__.__name__}: {exc}"}
        ok = bool(tick.get("success"))
        actions = tick.get("actions") or []
        by_action: dict[str, int] = {}
        for act in actions:
            if isinstance(act, dict):
                by_action[act.get("action", "?")] = by_action.get(act.get("action", "?"), 0) + 1
        views.append({
            "campaign_id": campaign_id,
            "manifest_path": entry["manifest_path"],
            "ok": ok,
            "error": None if ok else str(tick.get("error", "tick failed")),
            "changed": bool(tick.get("changed")),
            "finished": bool(tick.get("finished")),
            "actions": by_action,
        })
        if apply:
            book = _campaign_entry(state, campaign_id)
            book["ticks"] = int(book.get("ticks", 0)) + 1
            book["last_tick"] = _now()
            book["last_ok"] = ok
    if apply:
        # Cursor init snapshot AFTER the pass: events published by the pass
        # itself are stale notifications and must not self-wake the lane.
        watermark = _safe_high_watermark(hermes_root)
        for entry in entries:
            book = _campaign_entry(state, entry["campaign_id"])
            book.setdefault("event_cursor", watermark)
        state["campaigns"] = {e["campaign_id"]: state["campaigns"][e["campaign_id"]]
                              for e in entries if e["campaign_id"] in state["campaigns"]}
        _save_lane_state(state, hermes_root)
    return {
        "campaigns": views,
        "ok": all(v["ok"] for v in views) if views else True,
        "changed": any(v["changed"] for v in views),
        "manifests": manifests,
    }


# ---------------------------------------------------------------------------
# Event-cursor wakeup (the operator_autopilot._wait_for_wakeup slice pattern,
# fanned out across ALL registered campaigns' mission_ids)
# ---------------------------------------------------------------------------

def _idle_poll_seconds() -> float:
    """Serve-mode backstop interval: env override, clamped; garbage means default."""
    raw = os.environ.get(IDLE_POLL_ENV, "").strip()
    try:
        value = float(raw) if raw else TICK_SECONDS
    except ValueError:
        return TICK_SECONDS
    if math.isnan(value):
        return TICK_SECONDS
    return max(MIN_IDLE_POLL_SECONDS, min(MAX_IDLE_POLL_SECONDS, value))


def _wait_for_lane_wakeup(
    watch: list[dict[str, Any]],
    wait_seconds: float,
    hermes_root: Path | None,
    abort_check: Any = None,
) -> tuple[int, str]:
    """Block up to ``wait_seconds`` for a live event about any lane mission.

    ``watch`` entries carry ``cursor`` and ``mission_ids``. Returns
    ``(new_cursor, reason)`` with reason ``event``, ``timer`` or ``abort``.
    Per ``docs/live-events.md`` an event is a notification, never proof:
    nothing in it is read or acted on — it only ends the wait early and the
    next pass re-reads durable state exactly as after a timer wakeup. On an
    event the cursor jumps to the store's high-water mark instead of walking
    the backlog (events are published after the authoritative commit, so
    state at or below that mark is already visible). A missing, delayed or
    erroring event store degrades to the timer and still honors the full
    wait: it can neither stall work nor turn the loop into a busy spin. The
    wait is taken in slices of at most ``WAIT_SLICE_SECONDS`` so an
    ``abort_check`` is never delayed by more than one slice.
    """
    floor = max([int(w.get("cursor") or 0) for w in watch], default=0)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return floor, "timer"
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        started = time.monotonic()
        high = 0
        woke = False
        for w in watch:
            cursor = int(w.get("cursor") or 0)
            for mission_id in w.get("mission_ids") or []:
                try:
                    payload = json.loads(live_events.hermes_live_events_since(
                        cursor, mission_id=mission_id, limit=1, wait_ms=0,
                        hermes_root=hermes_root,
                    ))
                except (ValueError, TypeError, OSError, sqlite3.Error):
                    payload = {}
                if isinstance(payload, dict) and payload.get("success") is True \
                        and int(payload.get("count") or 0) > 0:
                    woke = True
                    high = max(high, int(payload.get("next_cursor") or 0),
                               int(payload.get("high_watermark") or 0))
        if woke:
            return max(floor, high), "event"
        pause = slice_seconds - (time.monotonic() - started)
        if pause > 0:
            time.sleep(pause)  # an erroring store returns instantly: never spin
        if abort_check is not None and abort_check():
            return floor, "abort"


def _watch_set(hermes_root: Path | None) -> list[dict[str, Any]]:
    """Wake targets: per registered campaign its cursor + manifest mission ids."""
    entries = _load_registry(hermes_root)
    state = _load_lane_state(hermes_root)
    watch: list[dict[str, Any]] = []
    for entry in entries:
        try:
            manifest_json = Path(entry["manifest_path"]).read_text(encoding="utf-8")
        except OSError:
            manifest_json = ""
        cursor = int(state["campaigns"].get(entry["campaign_id"], {}).get("event_cursor") or 0)
        watch.append({"campaign_id": entry["campaign_id"], "cursor": cursor,
                      "mission_ids": _manifest_mission_ids(manifest_json)})
    return watch


def _advance_cursors(hermes_root: Path | None, cursor: int) -> None:
    """On an event wakeup every campaign's cursor jumps to the high-water mark."""
    entries = _load_registry(hermes_root)
    state = _load_lane_state(hermes_root)
    for entry in entries:
        book = _campaign_entry(state, entry["campaign_id"])
        book["event_cursor"] = max(int(book.get("event_cursor", 0)), int(cursor))
    _save_lane_state(state, hermes_root)


# ---------------------------------------------------------------------------
# The lane run (once | serve)
# ---------------------------------------------------------------------------

def campaign_lane_run(
    *,
    mode: str = "once",
    max_ticks: int | None = None,
    max_runtime: float | None = None,
    idle_poll: float | None = None,
    confirm: bool = False,
    apply: bool = False,
    hermes_root: Path | None = None,
) -> str:
    """Run the lane. ``once`` makes exactly one pass; ``serve`` loops.

    The registry is read FIRST on every pass (restart ⇒ read-then-adopt) and
    is the only lane-owned truth. Each pass ticks every registered campaign
    through the existing ``campaign_controller_tick`` (apply requires
    ``confirm`` and direct apply mode, exactly like the controller) and
    persists per-campaign event cursor + last_tick. ``serve`` waits between
    passes on the event-cursor long poll with the timer as backstop.
    ``max_ticks`` bounds passes (each pass = one tick per campaign);
    ``max_runtime`` bounds total seconds. At least one pass always runs.
    """
    policy = op.OperatorPolicy()
    try:
        policy.require_level("workspace")
        if mode not in ("once", "serve"):
            raise CampaignLaneError(f"mode must be 'once' or 'serve', got {mode!r}")
        if max_ticks is not None and int(max_ticks) < 1:
            raise CampaignLaneError("max_ticks must be >= 1")
        if max_runtime is not None and float(max_runtime) <= 0:
            raise CampaignLaneError("max_runtime must be > 0")
        if apply and not confirm:
            raise PermissionError("campaign lane run apply requires confirm=true")
        policy.require_mutation(not apply)
        effective_apply = apply and confirm and not policy.effective_dry_run(False)

        poll = _idle_poll_seconds() if idle_poll is None else float(idle_poll)
        poll = max(MIN_IDLE_POLL_SECONDS, min(MAX_IDLE_POLL_SECONDS, poll))
        hard_deadline = time.monotonic() + float(max_runtime) if max_runtime is not None else None

        ticks = 0
        wakeups: dict[str, int] = {"event": 0, "timer": 0, "abort": 0}
        last_pass: dict[str, Any] | None = None
        while True:
            last_pass = _one_pass(hermes_root, apply=effective_apply)
            ticks += 1
            if mode == "once":
                break
            if max_ticks is not None and ticks >= int(max_ticks):
                break
            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                break
            budget = poll
            if hard_deadline is not None:
                budget = min(budget, hard_deadline - time.monotonic())
            if budget <= 0:
                continue
            _, reason = _wait_for_lane_wakeup(
                _watch_set(hermes_root), budget, hermes_root,
            )
            wakeups[reason] = wakeups.get(reason, 0) + 1
            if reason == "event":
                _advance_cursors(hermes_root, _safe_high_watermark(hermes_root))

        op.audit_record(
            tool="campaign_lane_run", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not effective_apply, success=True,
            changed=bool(last_pass and last_pass["changed"]),
            summary=f"campaign lane run ({mode})",
            extra={"ticks": ticks, "wakeups": wakeups},
        )
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_run", "mode": mode,
            "dry_run": not effective_apply, "ticks": ticks,
            "campaigns": last_pass["campaigns"] if last_pass else [],
            "all_ok": bool(last_pass and last_pass["ok"]),
            "changed": bool(last_pass and last_pass["changed"]),
            "wakeups": wakeups,
        })
    except (CampaignLaneError, ValueError, TypeError, PermissionError, LookupError,
            OSError, sqlite3.Error) as exc:
        op.audit_record(
            tool="campaign_lane_run", level=policy.level, apply_mode=policy.apply_mode,
            dry_run=not apply, success=False, changed=False,
            summary=f"campaign lane run rejected: {exc}",
        )
        return json.dumps({
            "success": False, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_run", "error": str(exc),
            "error_class": exc.__class__.__name__,
        })


def campaign_lane_status(hermes_root: Path | None = None) -> str:
    """Read-only lane view: registry order plus per-campaign lane bookkeeping."""
    policy = op.OperatorPolicy()
    try:
        policy.require_level("read_only")
        entries = _load_registry(hermes_root)
        state = _load_lane_state(hermes_root)
        campaigns = [
            {
                "campaign_id": e["campaign_id"],
                "manifest_path": e["manifest_path"],
                "registered_at": e.get("registered_at"),
                "lane": state["campaigns"].get(e["campaign_id"]),
            }
            for e in entries
        ]
        return json.dumps({
            "success": True, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_status", "campaigns": campaigns,
        })
    except (CampaignLaneError, ValueError, TypeError, PermissionError, LookupError,
            OSError, sqlite3.Error) as exc:
        return json.dumps({
            "success": False, "schema_version": SCHEMA_VERSION,
            "tool": "campaign_lane_status", "error": str(exc),
            "error_class": exc.__class__.__name__,
        })


# ---------------------------------------------------------------------------
# CLI (systemd timer cadence: --once is THE single trigger per family)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="operator_campaign_lane",
        description="Durable adoption lane for maintenance campaigns (AUTO-03).",
    )
    # Subparsers do not inherit top-level options, so --hermes-root is
    # attached to every surface through this parent (positional order free).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hermes-root", default=None,
                        help="hermes data root (default: HERMES_HOME / ~/.hermes)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_register = sub.add_parser("register", parents=[common],
                                help="register a campaign manifest on the lane")
    p_register.add_argument("manifest_path")
    p_register.add_argument("--apply", action="store_true")
    p_register.add_argument("--confirm", action="store_true")

    p_unregister = sub.add_parser("unregister", parents=[common],
                                  help="remove campaigns (containment only)")
    p_unregister.add_argument("target", help="campaign_id or manifest path")
    p_unregister.add_argument("--apply", action="store_true")
    p_unregister.add_argument("--confirm", action="store_true")

    p_run = sub.add_parser("run", parents=[common], help="run the lane")
    mode = p_run.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one pass (timer cadence; default)")
    mode.add_argument("--serve", action="store_true", help="loop with event-cursor wakeups")
    p_run.add_argument("--max-ticks", type=int, default=None)
    p_run.add_argument("--max-runtime", type=float, default=None)
    p_run.add_argument("--idle-poll", type=float, default=None)
    p_run.add_argument("--apply", action="store_true")
    p_run.add_argument("--confirm", action="store_true")

    sub.add_parser("status", parents=[common], help="read-only lane view")

    args = parser.parse_args(argv)
    root = Path(args.hermes_root).expanduser() if args.hermes_root else None
    if args.cmd == "register":
        out = register_campaign(args.manifest_path, confirm=args.confirm,
                                apply=args.apply, hermes_root=root)
    elif args.cmd == "unregister":
        out = unregister_campaign(args.target, confirm=args.confirm,
                                  apply=args.apply, hermes_root=root)
    elif args.cmd == "run":
        run_mode = "serve" if args.serve else "once"
        out = campaign_lane_run(mode=run_mode, max_ticks=args.max_ticks,
                                max_runtime=args.max_runtime, idle_poll=args.idle_poll,
                                confirm=args.confirm, apply=args.apply, hermes_root=root)
    else:
        out = campaign_lane_status(hermes_root=root)
    print(out)
    return 0 if json.loads(out).get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
