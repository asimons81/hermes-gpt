from __future__ import annotations

import re
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one literal match, found {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one regex match, found {count}: {pattern[:80]}")
    p.write_text(updated, encoding="utf-8")


# ---------------------------------------------------------------------------
# P1: OAuth revocation must invalidate clustered/stateless bearer validation.
# Keep clustered discovery, but make the durable store authoritative whenever
# server startup has bound OAuthState to a Hermes data root.
# ---------------------------------------------------------------------------
replace_once(
    "oauth_auth.py",
    "        self.refresh_tokens: dict[str, dict[str, Any]] = {}\n",
    "        self.refresh_tokens: dict[str, dict[str, Any]] = {}\n"
    "        # Bound by restore_tokens()/persist_tokens() in server mode. When set,\n"
    "        # the durable store is authoritative for bearer validity so revocation\n"
    "        # cannot be bypassed by the clustered signed-token fallback.\n"
    "        self._hermes_root: Path | None = None\n",
)

regex_once(
    "oauth_auth.py",
    r"    def validate_access_token\(self, token_value: str\) -> bool:\n.*?\n    # ------------------------------------------------------------------\n    # Durable token persistence",
    '''    def _durable_access_token_valid(self, token_value: str) -> bool:\n        """Validate bearer presence against the authoritative durable envelope.\n\n        A clustered peer may not have the token in process memory, so a cache\n        miss is resolved by reading the shared durable store. Conversely, once\n        revocation removes that envelope, an already-cached token is rejected\n        immediately instead of being resurrected solely from its MAC.\n        """\n        if self._hermes_root is None:\n            item = self.access_tokens.get(token_value)\n            return bool(\n                item\n                and item.get("expires_at", 0) > time.time()\n                and item.get("resource") == self.config.resource\n            )\n        try:\n            import token_store\n\n            bundle = token_store.load_tokens(self._hermes_root)\n        except Exception:\n            self.access_tokens.pop(token_value, None)\n            return False\n        item = (bundle.get("access_tokens") or {}).get(token_value) if isinstance(bundle, dict) else None\n        if not (\n            isinstance(item, dict)\n            and item.get("expires_at", 0) > time.time()\n            and item.get("resource") == self.config.resource\n            and item.get("client_id") == self.config.client_id\n        ):\n            self.access_tokens.pop(token_value, None)\n            return False\n        self.access_tokens[token_value] = item\n        return True\n\n    def validate_access_token(self, token_value: str) -> bool:\n        if not token_value:\n            return False\n        self.cleanup()\n\n        # In server mode the encrypted durable envelope is the revocation\n        # authority for both legacy opaque and v1 signed tokens. Signed tokens\n        # still require a valid MAC, but a MAC alone is never enough.\n        if self._hermes_root is not None:\n            if token_value.startswith(ACCESS_TOKEN_PREFIX) and self._decode_signed_access_token(token_value) is None:\n                return False\n            return self._durable_access_token_valid(token_value)\n\n        # Standalone/in-memory OAuthState instances have no durable authority.\n        # Preserve their local validation behavior for tests and embedded use.\n        item = self.access_tokens.get(token_value)\n        if (\n            item\n            and item.get("expires_at", 0) > time.time()\n            and item.get("resource") == self.config.resource\n        ):\n            return True\n        return self._decode_signed_access_token(token_value) is not None\n\n    # ------------------------------------------------------------------\n    # Durable token persistence''',
)

replace_once(
    "oauth_auth.py",
    "        if not hermes_root:\n            hermes_root = Path.home() / \".hermes\"\n        return token_store.save_tokens(hermes_root, bundle)\n",
    "        if not hermes_root:\n            hermes_root = Path.home() / \".hermes\"\n        self._hermes_root = Path(hermes_root)\n        return token_store.save_tokens(hermes_root, bundle)\n",
)
replace_once(
    "oauth_auth.py",
    "        if not hermes_root:\n            hermes_root = Path.home() / \".hermes\"\n        bundle = token_store.load_tokens(hermes_root)\n",
    "        if not hermes_root:\n            hermes_root = Path.home() / \".hermes\"\n        self._hermes_root = Path(hermes_root)\n        bundle = token_store.load_tokens(hermes_root)\n",
)


# ---------------------------------------------------------------------------
# P1/P2: controller mutation gates + frontier-node delegation binding.
# ---------------------------------------------------------------------------
regex_once(
    "operator_controller.py",
    r"def _latest_delegation\(\n.*?\n\ndef _runner_observation\(",
    '''def _latest_delegation(\n    hermes_root: Path | None, mission_id: str, contract_sha256: str = ""\n) -> dict[str, Any] | None:\n    """Read authoritative delegation state, optionally bound to a contract.\n\n    When a frontier node supplies a contract hash, never substitute an\n    unrelated mission-level latest delegation: parallel nodes must be observed\n    against their own durable lineage.\n    """\n    dbp = deleg._db_path(hermes_root)\n    if not dbp.is_file():\n        return None\n    try:\n        with deleg._connect(dbp, write=False) as db:\n            if contract_sha256:\n                row = db.execute(\n                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "\n                    "FROM delegations WHERE mission_id=? AND contract_sha256=? "\n                    "ORDER BY updated_at DESC LIMIT 1",\n                    (mission_id, contract_sha256),\n                ).fetchone()\n            else:\n                row = db.execute(\n                    "SELECT delegation_id,task_id,contract_sha256,state,backend_state,outcome,validation_verdict "\n                    "FROM delegations WHERE mission_id=? ORDER BY updated_at DESC LIMIT 1",\n                    (mission_id,),\n                ).fetchone()\n            if not row:\n                return None\n            return dict(row)\n    except (sqlite3.Error, FileNotFoundError):\n        return None\n\n\ndef _runner_observation(''',
)

replace_once(
    "operator_controller.py",
    "    deleg_state: dict[str, Any] | None = None\n    if node_id and frontier.get(\"contract_sha256\"):\n        # Prefer a delegation whose task maps to this node; fall back to latest.\n        latest = _latest_delegation(hermes_root, mission_id)\n        deleg_state = latest\n",
    "    deleg_state: dict[str, Any] | None = None\n    if node_id and frontier.get(\"contract_sha256\"):\n        deleg_state = _latest_delegation(\n            hermes_root, mission_id, str(frontier[\"contract_sha256\"])\n        )\n",
)

regex_once(
    "operator_controller.py",
    r"def hermes_controller_reconcile\(\n.*?\n\ndef hermes_controller_status\(",
    '''def hermes_controller_reconcile(\n    mission_id: str,\n    trigger_kind: str = TRIGGER_MANUAL,\n    *,\n    dry_run: bool = True,\n    hermes_root: Path | None = None,\n) -> str:\n    """Preview or run one supervised shadow reconciliation pass.\n\n    ``dry_run=True`` is genuinely non-persisting: it validates the request and\n    returns the action that would be observed without touching controller\n    plans, telemetry, leases, heartbeat files, or attention queues. A real\n    reconciliation persists controller metadata and therefore requires the\n    normal ``workspace`` + ``direct`` mutation gates.\n    """\n    policy = op.OperatorPolicy()\n    try:\n        policy.require_level("read_only")\n        if not MISSION_ID_RE.fullmatch(mission_id or ""):\n            raise ValueError("mission_id is invalid")\n        if trigger_kind not in TRIGGERS:\n            raise ValueError(f"trigger_kind must be one of {TRIGGERS}")\n\n        effective_dry_run = policy.effective_dry_run(dry_run)\n        if effective_dry_run:\n            result = {\n                "success": True,\n                "schema_version": SCHEMA_VERSION,\n                "mode": CONTROLLER_MODE,\n                "mission_id": mission_id,\n                "trigger_kind": trigger_kind,\n                "dry_run": True,\n                "changed": False,\n                "would_reconcile": True,\n                "would_execute": False,\n                "note": "non-persisting preview; no controller state was written",\n            }\n            _audit(\n                "hermes_controller_reconcile",\n                policy,\n                dry_run=True,\n                success=True,\n                changed=False,\n                mission_id=mission_id,\n                extra={"trigger_kind": trigger_kind, "preview": True},\n            )\n            return json.dumps(result, ensure_ascii=False, indent=2)\n\n        policy.require_level("workspace")\n        policy.require_mutation(False)\n        result = reconcile_pass(\n            mission_id,\n            trigger_kind,\n            host=NullHostAdapter(),\n            hermes_root=hermes_root,\n            interval=DEFAULT_INTERVAL_SECONDS,\n        )\n        result["dry_run"] = False\n        result["changed"] = True\n        _audit(\n            "hermes_controller_reconcile",\n            policy,\n            dry_run=False,\n            success=not any(k in result for k in ("error",)),\n            changed=True,\n            mission_id=mission_id,\n            node_id=result.get("node_id", ""),\n            extra={\n                "classification": result.get("classification", ""),\n                "row_key": result.get("row_key", ""),\n                "pass_result": result.get("pass_result", ""),\n                "lease_acquired": bool(result.get("lease_acquired")),\n            },\n        )\n        return json.dumps(result, ensure_ascii=False, indent=2)\n    except (\n        ValueError,\n        TypeError,\n        PermissionError,\n        LookupError,\n        OSError,\n        sqlite3.Error,\n    ) as exc:\n        _audit(\n            "hermes_controller_reconcile",\n            policy,\n            dry_run=policy.effective_dry_run(dry_run),\n            success=False,\n            changed=False,\n            mission_id=mission_id,\n        )\n        return _error(\n            exc,\n            "CONTROLLER_RECONCILE_REJECTED",\n            "Check the mission id, trigger kind, and Operator mutation policy.",\n        )\n\n\ndef hermes_controller_status(''',
)

regex_once(
    "operator_controller.py",
    r"def hermes_controller_trigger\(\n.*\Z",
    '''def hermes_controller_trigger(\n    mission_id: str,\n    trigger_kind: str,\n    ref: str = "",\n    hermes_root: Path | None = None,\n) -> str:\n    """Enqueue a T1–T5 request (persistent controller mutation)."""\n    policy = op.OperatorPolicy()\n    try:\n        policy.require_level("workspace")\n        policy.require_mutation(False)\n        result = trigger(mission_id, trigger_kind, ref, hermes_root=hermes_root)\n        _audit(\n            "hermes_controller_trigger",\n            policy,\n            dry_run=False,\n            success=True,\n            changed=True,\n            mission_id=mission_id,\n            extra={"trigger_kind": trigger_kind, "seq": result.get("seq", 0)},\n        )\n        return json.dumps(result, ensure_ascii=False, indent=2)\n    except (\n        ValueError,\n        TypeError,\n        PermissionError,\n        LookupError,\n        OSError,\n        sqlite3.Error,\n    ) as exc:\n        _audit(\n            "hermes_controller_trigger",\n            policy,\n            dry_run=False,\n            success=False,\n            changed=False,\n            mission_id=mission_id,\n        )\n        return _error(\n            exc, "CONTROLLER_TRIGGER_REJECTED", "Check the mission id, trigger kind, and Operator mutation policy."\n        )\n''',
)


# ---------------------------------------------------------------------------
# P2: failed plan parents must never make a pending child ready.
# ---------------------------------------------------------------------------
replace_once(
    "operator_mission_plan.py",
    "def hermes_plan_review(mission_id: str, hermes_root: Path | None = None) -> str:\n",
    '''def _ready_node_ids(nodes: list[dict[str, Any]]) -> list[str]:\n    """Return pending nodes whose complete parent set succeeded."""\n    by_id = {n["node_id"]: n for n in nodes}\n    return [\n        n["node_id"]\n        for n in nodes\n        if n["state"] == "pending"\n        and all(\n            parent_id in by_id and by_id[parent_id]["state"] == "completed"\n            for parent_id in n.get("parents", [])\n        )\n    ]\n\n\ndef hermes_plan_review(mission_id: str, hermes_root: Path | None = None) -> str:\n''',
)
replace_once(
    "operator_mission_plan.py",
    "            # Ready = no parent is non-terminal.\n            value[\"ready_nodes\"] = [\n                n[\"node_id\"]\n                for n in nodes\n                if n[\"state\"] == \"pending\"\n                and all(p[\"state\"] in TERMINAL_NODE_STATES for p in nodes if p[\"node_id\"] in n[\"parents\"])\n            ]\n",
    "            # Ready means every declared parent completed successfully.\n            value[\"ready_nodes\"] = _ready_node_ids(nodes)\n",
)


# ---------------------------------------------------------------------------
# P2: stable resumable mission-ledger cursors + pagination through visible rows.
# Cursor tokens are opaque vector watermarks over authoritative source-local
# append sequences. This stays read-only and needs no second evidence store.
# ---------------------------------------------------------------------------
replace_once("operator_mission_ledger.py", "import hashlib\nimport json\n", "import base64\nimport hashlib\nimport json\n")
replace_once(
    "operator_mission_ledger.py",
    "_SOURCE_RANK = {\"mission\": 0, \"delegation\": 1, \"audit\": 2, \"kanban\": 3}\n",
    "_SOURCE_RANK = {\"mission\": 0, \"delegation\": 1, \"audit\": 2, \"kanban\": 3}\n_CURSOR_PREFIX = \"ld1.\"\n_MAX_CURSOR_TOKEN = 8192\n",
)
replace_once(
    "operator_mission_ledger.py",
    '                        "source": "mission",\n                        "source_seq": int(row["seq"]),\n',
    '                        "source": "mission",\n                        "cursor_key": "mission",\n                        "source_seq": int(row["seq"]),\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                            "source": "delegation",\n                            "source_seq": int(row["seq"]),\n',
    '                            "source": "delegation",\n                            "cursor_key": "delegation",\n                            "source_seq": int(row["seq"]),\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                        "source": "audit",\n                        "source_seq": n,\n',
    '                        "source": "audit",\n                        "cursor_key": "audit",\n                        "source_seq": n + 1,\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                        "event_id": f"audit:{rec.get(\'timestamp\') or \'\'}:{n}",\n',
    '                        "event_id": f"audit:{rec.get(\'timestamp\') or \'\'}:{n + 1}",\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                        f"SELECT task_id, kind, created_at, actor, summary FROM task_events "\n                        f"WHERE task_id IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",\n',
    '                        f"SELECT rowid AS source_rowid, task_id, kind, created_at, actor, summary FROM task_events "\n                        f"WHERE task_id IN ({placeholders}) ORDER BY rowid ASC LIMIT ?",\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                                "source": "kanban",\n                                "source_seq": len(events),\n',
    '                                "source": "kanban",\n                                "cursor_key": f"kanban:{slug}",\n                                "source_seq": int(row["source_rowid"]),\n',
)
replace_once(
    "operator_mission_ledger.py",
    '                                "event_id": f"kanban:{slug}:{task_id}:{ts}:{len(events)}",\n',
    '                                "event_id": f"kanban:{slug}:{int(row[\"source_rowid\"])}",\n',
)

regex_once(
    "operator_mission_ledger.py",
    r"# ---------------------------------------------------------------------------\n# Merge \(deterministic cursor stream\)\n# ---------------------------------------------------------------------------\n\n\ndef _merge\(events: list\[dict\[str, Any\]\]\) -> list\[dict\[str, Any\]\]:\n.*?\n\n# ---------------------------------------------------------------------------\n# Audit \+ envelope \+ public tool",
    '''# ---------------------------------------------------------------------------\n# Merge (stable vector cursor stream)\n# ---------------------------------------------------------------------------\n\n\ndef _encode_cursor(watermarks: dict[str, int]) -> str:\n    payload = json.dumps(\n        {"v": 1, "w": dict(sorted(watermarks.items()))},\n        separators=(",", ":"),\n        sort_keys=True,\n    ).encode("utf-8")\n    return _CURSOR_PREFIX + base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")\n\n\ndef _decode_cursor(value: Any) -> dict[str, int]:\n    if value in (None, "", 0, "0"):\n        return {}\n    if isinstance(value, int):\n        raise ValueError("legacy numeric ledger cursors are not resumable; restart from cursor=0")\n    token = str(value).strip()\n    if len(token) > _MAX_CURSOR_TOKEN or not token.startswith(_CURSOR_PREFIX):\n        raise ValueError("ledger cursor is invalid")\n    encoded = token[len(_CURSOR_PREFIX):]\n    try:\n        padding = "=" * (-len(encoded) % 4)\n        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)\n        payload = json.loads(raw.decode("utf-8"))\n    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:\n        raise ValueError("ledger cursor is invalid") from exc\n    watermarks = payload.get("w") if isinstance(payload, dict) and payload.get("v") == 1 else None\n    if not isinstance(watermarks, dict) or len(watermarks) > 256:\n        raise ValueError("ledger cursor is invalid")\n    out: dict[str, int] = {}\n    for key, seq in watermarks.items():\n        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(seq, int) or seq < 0:\n            raise ValueError("ledger cursor is invalid")\n        out[key] = seq\n    return out\n\n\ndef _event_cursor_key(event: dict[str, Any]) -> str:\n    return str(event.get("cursor_key") or event.get("source") or "")\n\n\ndef _merge(events: list[dict[str, Any]]) -> list[dict[str, Any]]:\n    """Merge append-only source streams without reordering within a source.\n\n    Each authoritative source keeps its own stable monotonic sequence. The\n    merge chooses the oldest timestamp only among each source's current head,\n    so a late event with an older timestamp can never move behind a watermark\n    that was already returned to a client.\n    """\n    groups: dict[str, list[dict[str, Any]]] = {}\n    for event in events:\n        groups.setdefault(_event_cursor_key(event), []).append(event)\n    for group in groups.values():\n        group.sort(key=lambda e: int(e.get("source_seq", 0)))\n\n    positions = {key: 0 for key in groups}\n    ordered: list[dict[str, Any]] = []\n    while True:\n        candidates: list[tuple[tuple[float, int, str, int], str, dict[str, Any]]] = []\n        for cursor_key, group in groups.items():\n            pos = positions[cursor_key]\n            if pos >= len(group):\n                continue\n            event = group[pos]\n            sort_key = (\n                _parse_iso_ts(event.get("ts")) or 0.0,\n                _SOURCE_RANK.get(event.get("source", ""), 9),\n                cursor_key,\n                int(event.get("source_seq", 0)),\n            )\n            candidates.append((sort_key, cursor_key, event))\n        if not candidates:\n            break\n        _key, cursor_key, event = min(candidates, key=lambda item: item[0])\n        ordered.append(event)\n        positions[cursor_key] += 1\n    return ordered\n\n\n# ---------------------------------------------------------------------------\n# Audit + envelope + public tool''',
)

regex_once(
    "operator_mission_ledger.py",
    r"def _envelope\(\n.*?\n\ndef _mission_status\(",
    '''def _envelope(\n    *,\n    tool: str,\n    mission_id: str,\n    events: list[dict[str, Any]],\n    limit: int,\n    sources: list[str],\n    warnings: list[str],\n    trace_id: str,\n    mission_status: str,\n    cursor_state: dict[str, int] | None = None,\n) -> dict[str, Any]:\n    truncated = len(events) > limit\n    visible = events[:limit]\n    watermarks = dict(cursor_state or {})\n    for event in visible:\n        cursor_key = _event_cursor_key(event)\n        watermarks[cursor_key] = max(\n            int(watermarks.get(cursor_key, 0)), int(event.get("source_seq", 0))\n        )\n        event["cursor"] = _encode_cursor(watermarks)\n    next_cursor = _encode_cursor(watermarks)\n    return {\n        "success": True,\n        "schema_version": SCHEMA_VERSION,\n        "ledger_schema": LEDGER_SCHEMA,\n        "tool": tool,\n        "surface": "mission_ledger",\n        "mission_id": mission_id,\n        "mission_status": mission_status,\n        "trace_id": trace_id,\n        "generated_at": datetime.now(timezone.utc).isoformat(),\n        "count_returned": len(visible),\n        "count_total": len(events),\n        "truncated": truncated,\n        "max_cursor": next_cursor,\n        "next_cursor": next_cursor,\n        "sources_queried": sources,\n        "sources_allowed": sorted(_allowed_sources()),\n        "warnings": warnings,\n        "events": visible,\n    }\n\n\ndef _mission_status(''',
)

replace_once(
    "operator_mission_ledger.py",
    "    cursor: int = 0,\n",
    "    cursor: int | str = 0,\n",
)
replace_once(
    "operator_mission_ledger.py",
    "    try:\n        cursor = max(0, int(cursor))\n    except (TypeError, ValueError):\n        cursor = 0\n",
    '''    try:\n        cursor_state = {} if replay else _decode_cursor(cursor)\n    except (TypeError, ValueError) as exc:\n        warnings.append(str(exc))\n        return json.dumps(\n            _envelope(\n                tool=tool,\n                mission_id=mission_id,\n                events=[],\n                limit=limit,\n                sources=[],\n                warnings=warnings,\n                trace_id=tid,\n                mission_status=_mission_status(root, mission_id),\n                cursor_state={},\n            ),\n            ensure_ascii=False,\n            indent=2,\n        )\n''',
)
replace_once(
    "operator_mission_ledger.py",
    "    merged = _merge(all_events)\n    if not replay:\n        merged = [e for e in merged if int(e.get(\"cursor\", 0)) > cursor]\n",
    '''    merged = _merge(all_events)\n    if not replay:\n        merged = [\n            event\n            for event in merged\n            if int(event.get("source_seq", 0))\n            > int(cursor_state.get(_event_cursor_key(event), 0))\n        ]\n''',
)
replace_once(
    "operator_mission_ledger.py",
    "            mission_status=status,\n        ),\n",
    "            mission_status=status,\n            cursor_state=cursor_state,\n        ),\n",
)


# Update legacy ledger tests to exercise the stable cursor contract.
p = Path("test_operator_mission_ledger.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    '''    # Cursors are monotonically increasing across the whole stream.\n    cursors = [e["cursor"] for e in out["events"]]\n    assert cursors == sorted(cursors)\n    assert cursors == list(range(1, 9))\n''',
    '''    # Event cursors are opaque stable watermark tokens.\n    cursors = [e["cursor"] for e in out["events"]]\n    assert all(isinstance(c, str) and c.startswith("ld1.") for c in cursors)\n    assert len(set(cursors)) == len(cursors)\n''',
)
text = text.replace(
    '''def test_cursor_resume_is_append_only(hermes_root: Path):\n    root, mid = _full_seed(hermes_root)\n    out = json.loads(ld.hermes_mission_ledger(mid, cursor=4, hermes_root=root))\n    assert out["success"] is True\n    # Events strictly after cursor 4.\n    assert all(e["cursor"] > 4 for e in out["events"])\n    assert out["count_total"] == 4  # 8 total, 4 after cursor 4\n    assert out["events"][0]["cursor"] == 5\n''',
    '''def test_cursor_resume_is_append_only(hermes_root: Path):\n    root, mid = _full_seed(hermes_root)\n    first = json.loads(ld.hermes_mission_ledger(mid, limit=4, hermes_root=root))\n    assert first["success"] is True\n    assert first["count_returned"] == 4\n    assert first["truncated"] is True\n    second = json.loads(\n        ld.hermes_mission_ledger(mid, cursor=first["next_cursor"], hermes_root=root)\n    )\n    assert second["success"] is True\n    assert second["count_total"] == 4\n    first_ids = {e["event_id"] for e in first["events"]}\n    second_ids = {e["event_id"] for e in second["events"]}\n    assert first_ids.isdisjoint(second_ids)\n    assert len(first_ids | second_ids) == 8\n''',
)
text = text.replace(
    "    ld.hermes_mission_ledger(mid, cursor=1, hermes_root=root)\n",
    "    ld.hermes_mission_ledger(mid, cursor=0, hermes_root=root)\n",
)
if "def test_late_older_timestamp_event_is_not_skipped" not in text:
    text += '''\n\ndef test_late_older_timestamp_event_is_not_skipped(hermes_root: Path):\n    root, mid = _full_seed(hermes_root)\n    first = json.loads(ld.hermes_mission_ledger(mid, hermes_root=root))\n    cursor = first["next_cursor"]\n\n    db = mission._db_path(root)\n    conn = mission._connect(db, write=True)\n    try:\n        conn.execute(\n            "INSERT INTO mission_events (mission_id, event_type, from_status, to_status, reason_sha256, details_json, created_at) "\n            "VALUES (?,?,?,?,?,?,?)",\n            (mid, "late_ingest", "running", "running", "f" * 64, "{}", "2026-08-15T09:00:00+00:00"),\n        )\n        conn.commit()\n    finally:\n        conn.close()\n\n    resumed = json.loads(ld.hermes_mission_ledger(mid, cursor=cursor, hermes_root=root))\n    assert resumed["count_total"] == 1\n    assert resumed["events"][0]["kind"] == "late_ingest"\n'''
p.write_text(text, encoding="utf-8")


# Focused regression coverage for the remaining findings.
Path("test_codex_pr63_remediation.py").write_text(r'''from __future__ import annotations

import json
import sqlite3
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


def test_controller_reconcile_dry_run_is_non_persisting(monkeypatch, tmp_path: Path):
    _policy(monkeypatch, "read_only", "dry_run")
    called = False

    def fake_reconcile(*args, **kwargs):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(controller, "reconcile_pass", fake_reconcile)
    out = json.loads(controller.hermes_controller_reconcile("msn-codex", hermes_root=tmp_path))
    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["changed"] is False
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
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    runtime = text.split("dependencies = [", 1)[1].split("]", 1)[0].lower()
    assert "pyyaml" in runtime
''', encoding="utf-8")

# Remove the temporary marker once real remediation exists.
Path("docs/remediation/pr63-codex-findings.md").unlink(missing_ok=True)

print("PR #63 Codex remediation patches applied")
