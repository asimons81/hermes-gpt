"""Tests for the deterministic placement filter-and-score module (vNext slice-1).

Covers: pure-core determinism (same inputs -> same output), every hard filter,
every soft score + ordering, ``no_capable_target`` classification + stubbed
escalation, approval-node classification, INV-9 containment, and the read-only /
dry-run no-side-effect guarantee at the public tool surface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import operator_capability_manifest as cm
import operator_mission_plan as plan
import operator_mission_runtime as mission
import operator_placement as pl
import operator_policy as op

# ---------------------------------------------------------------------------
# Synthetic manifest targets (pure-core tests; no filesystem)
# ---------------------------------------------------------------------------


def _target(
    entity_id: str,
    *,
    kind: str = "profile",
    name: str = "",
    enabled: bool = True,
    reachable: bool = True,
    identity_configured: bool = True,
    authorization_ceiling: str = "",
    allowed_profiles: list[str] | None = None,
    features: list[str] | None = None,
    workspaces: list[str] | None = None,
    backends: list[str] | None = None,
    skills: list[str] | None = None,
    model: str = "",
    provider: str = "",
    host_role: str = "",
    allow_public_actions: bool = False,
) -> dict:
    return {
        "entity_id": entity_id,
        "kind": kind,
        "name": name or entity_id,
        "enabled": enabled,
        "reachable": reachable,
        "identity_configured": identity_configured,
        "authorization_ceiling": authorization_ceiling,
        "allowed_profiles": allowed_profiles or [],
        "features": features or [],
        "workspaces": workspaces or [],
        "backends": backends or [],
        "skills": skills or [],
        "model": model,
        "provider": provider,
        "host_role": host_role,
        "allow_public_actions": allow_public_actions,
    }


def _req(**overrides) -> dict:
    base = {
        "profile": "dev",
        "skills": [],
        "authorization_class": "reversible_write",
        "features": [],
        "workspace": "",
        "backends": [],
        "kind": "single",
        "owner": "default",
        "budget": {"est_minutes": 0, "est_tokens": 0},
    }
    base.update(overrides)
    return base


def _safe_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _content_files(root: Path) -> set[Path]:
    """Return content files under ``root``, excluding transient SQLite WAL files."""
    return {
        p
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


def _placement_rows(root: Path) -> list:
    path = mission._db_path(root)
    if not path.is_file():
        return []
    with mission._connect(path, write=False) as db:
        try:
            return db.execute("SELECT * FROM placement_decisions").fetchall()
        except sqlite3.Error:
            return []


# ---------------------------------------------------------------------------
# Determinism (AC: same inputs -> same output)
# ---------------------------------------------------------------------------


def test_determinism_same_inputs_same_output():
    targets = [
        _target(
            "profile:dev",
            kind="profile",
            name="dev",
            authorization_ceiling="",
            allowed_profiles=["dev"],
            skills=["code-review", "compound-engineering"],
            model="claude",
            provider="anthropic",
        ),
        _target(
            "fabric:node-a",
            kind="fabric_node",
            name="node-a",
            authorization_ceiling="reversible_write",
            allowed_profiles=["dev"],
            features=["fabric-execute"],
            workspaces=["ws-default"],
        ),
    ]
    req = _req(skills=["code-review", "compound-engineering"])
    ctx = {
        "concurrency": {"dev": {"in_progress": 1, "max_in_progress": 4}},
        "health": {"dev": {"success_rate": 0.9, "samples": 20}},
        "budget": {"remaining_tokens": 1_000_000, "quota_tokens": 2_000_000},
        "priority": 5,
    }
    d1 = pl.build_decision("msn-aaa", "node-a", req, targets, ctx)
    d2 = pl.build_decision("msn-aaa", "node-a", req, targets, ctx)
    # The decision content (candidates + scores + optouts + digest) is
    # deterministic; only the generated_at timestamp is metadata.
    assert _safe_json(d1["score_breakdown"]) == _safe_json(d2["score_breakdown"])
    assert _safe_json(d1["candidate_set"]) == _safe_json(d2["candidate_set"])
    assert _safe_json(d1["filter_optouts"]) == _safe_json(d2["filter_optouts"])
    assert d1["top_candidate"] == d2["top_candidate"]
    assert d1["decision_sha256"] == d2["decision_sha256"]
    assert d1["classification"] == d2["classification"]


def test_determinism_ordering_stable_under_permutation():
    # Ties (identical scores) must still resolve deterministically by entity_id.
    a = _target(
        "profile:zz",
        kind="profile",
        name="zz",
        authorization_ceiling="reversible_write",
        allowed_profiles=["dev"],
        skills=["code-review"],
        model="m",
        provider="p",
    )
    b = _target(
        "profile:aa",
        kind="profile",
        name="aa",
        authorization_ceiling="reversible_write",
        allowed_profiles=["dev"],
        skills=["code-review"],
        model="m",
        provider="p",
    )
    order1 = [
        c["entity_id"]
        for c in pl.score_targets(_req(skills=["code-review"]), [a, b])["candidate_set"]
    ]
    order2 = [
        c["entity_id"]
        for c in pl.score_targets(_req(skills=["code-review"]), [b, a])["candidate_set"]
    ]
    assert order1 == order2 == ["profile:aa", "profile:zz"]


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------


def test_filter_disabled_unreachable_identity():
    req = _req()
    disabled = _target("fabric:off", kind="fabric_node", enabled=False, reachable=False)
    unreachable = _target(
        "fabric:rr", kind="fabric_node", enabled=True, reachable=False
    )
    no_identity = _target(
        "fabric:ii",
        kind="fabric_node",
        enabled=True,
        reachable=True,
        identity_configured=False,
    )
    v = pl.score_targets(req, [disabled, unreachable, no_identity])
    codes = v["filter_optouts"]
    assert "disabled" in codes["fabric:off"]
    assert "unreachable" in codes["fabric:rr"]
    assert "identity_unconfigured" in codes["fabric:ii"]
    assert v["classification"] == pl.CLASS_NO_TARGET


def test_filter_required_features_missing():
    req = _req(features=["fabric-execute"])
    good = _target(
        "fabric:g",
        kind="fabric_node",
        features=["fabric-execute"],
        allowed_profiles=["dev"],
    )
    bad = _target("fabric:b", kind="fabric_node", features=[], allowed_profiles=["dev"])
    v = pl.score_targets(req, [good, bad])
    assert "required_features_missing" in v["filter_optouts"]["fabric:b"]
    assert "fabric:g" in {c["entity_id"] for c in v["candidate_set"]}


def test_filter_auth_ceiling_exceeded():
    req = _req(authorization_class="high_impact")
    low = _target(
        "fabric:low",
        kind="fabric_node",
        authorization_ceiling="read_only",
        allowed_profiles=["dev"],
    )
    ok = _target(
        "fabric:ok",
        kind="fabric_node",
        authorization_ceiling="high_impact",
        allowed_profiles=["dev"],
    )
    v = pl.score_targets(req, [low, ok])
    assert "auth_ceiling_exceeded" in v["filter_optouts"]["fabric:low"]
    assert "fabric:ok" in {c["entity_id"] for c in v["candidate_set"]}


def test_filter_profile_out_of_scope():
    req = _req(profile="dev")
    other = _target("fabric:o", kind="fabric_node", allowed_profiles=["qa"])
    own = _target("profile:dev", kind="profile", name="dev", allowed_profiles=["dev"])
    v = pl.score_targets(req, [other, own])
    assert "profile_out_of_scope" in v["filter_optouts"]["fabric:o"]
    assert "profile:dev" in {c["entity_id"] for c in v["candidate_set"]}


def test_filter_workspace_out_of_scope():
    req = _req(workspace="ws-default")
    wrong = _target(
        "fabric:w",
        kind="fabric_node",
        workspaces=["ws-other"],
        allowed_profiles=["dev"],
    )
    right = _target(
        "fabric:x",
        kind="fabric_node",
        workspaces=["ws-default"],
        allowed_profiles=["dev"],
    )
    v = pl.score_targets(req, [wrong, right])
    assert "workspace_out_of_scope" in v["filter_optouts"]["fabric:w"]
    assert "fabric:x" in {c["entity_id"] for c in v["candidate_set"]}


def test_filter_concurrency_full():
    req = _req()
    full = _target(
        "profile:busy", kind="profile", name="busy", allowed_profiles=["dev"]
    )
    free = _target("profile:dev", kind="profile", name="dev", allowed_profiles=["dev"])
    ctx = {
        "concurrency": {
            "busy": {"in_progress": 3, "max_in_progress": 3},
            "dev": {"in_progress": 0, "max_in_progress": 4},
        }
    }
    v = pl.score_targets(req, [full, free], ctx)
    assert "concurrency_full" in v["filter_optouts"]["profile:busy"]
    assert "profile:dev" in {c["entity_id"] for c in v["candidate_set"]}


def test_filter_forbidden_action_public():
    req = _req()
    public = _target(
        "fleet:p",
        kind="fleet_peer",
        name="p",
        allow_public_actions=True,
        allowed_profiles=["dev"],
        identity_configured=True,
    )
    private = _target(
        "fleet:q",
        kind="fleet_peer",
        name="q",
        allow_public_actions=False,
        allowed_profiles=["dev"],
        identity_configured=True,
    )
    ctx = {"forbidden_actions": ["public"]}
    v = pl.score_targets(req, [public, private], ctx)
    assert "forbidden_action" in v["filter_optouts"]["fleet:p"]
    assert "fleet:q" in {c["entity_id"] for c in v["candidate_set"]}


# ---------------------------------------------------------------------------
# Soft scores + ordering
# ---------------------------------------------------------------------------


def test_authorization_match_prefers_just_enough():
    req = _req(authorization_class="read_only")
    just = _target(
        "fabric:just",
        kind="fabric_node",
        authorization_ceiling="read_only",
        allowed_profiles=["dev"],
    )
    over = _target(
        "fabric:over",
        kind="fabric_node",
        authorization_ceiling="high_impact",
        allowed_profiles=["dev"],
    )
    v = pl.score_targets(req, [just, over])
    sb = v["score_breakdown"]
    assert sb["fabric:just"]["scores"]["authorization_match"] == 1.0
    assert sb["fabric:over"]["scores"]["authorization_match"] < 1.0
    # Just-enough ranks first.
    assert v["candidate_set"][0]["entity_id"] == "fabric:just"


def test_affinity_skill_owner_ranks_first():
    req = _req(skills=["code-review"])
    owner = _target(
        "profile:dev",
        kind="profile",
        name="dev",
        allowed_profiles=["dev"],
        skills=["code-review"],
        model="m",
        provider="p",
    )
    other = _target(
        "profile:qa",
        kind="profile",
        name="qa",
        allowed_profiles=["dev"],
        skills=[],
        model="m",
        provider="p",
    )
    v = pl.score_targets(req, [owner, other])
    assert v["candidate_set"][0]["entity_id"] == "profile:dev"
    assert v["score_breakdown"]["profile:dev"]["scores"]["affinity"] == 1.0
    assert v["score_breakdown"]["profile:qa"]["scores"]["affinity"] == 0.0


def test_load_headroom_prefers_idle():
    req = _req()
    busy = _target("profile:b", kind="profile", name="b", allowed_profiles=["dev"])
    idle = _target("profile:d", kind="profile", name="d", allowed_profiles=["dev"])
    ctx = {
        "concurrency": {
            "b": {"in_progress": 3, "max_in_progress": 4},
            "d": {"in_progress": 0, "max_in_progress": 4},
        }
    }
    v = pl.score_targets(req, [busy, idle], ctx)
    assert v["score_breakdown"]["profile:b"]["scores"]["load_headroom"] < 1.0
    assert v["score_breakdown"]["profile:d"]["scores"]["load_headroom"] == 1.0
    assert v["candidate_set"][0]["entity_id"] == "profile:d"


def test_health_and_cost_priority_neutral_defaults():
    req = _req(skills=["x"], budget={"est_minutes": 0, "est_tokens": 1_000})
    t = _target(
        "profile:dev",
        kind="profile",
        name="dev",
        allowed_profiles=["dev"],
        skills=["x"],
        model="m",
        provider="p",
    )
    v = pl.score_targets(req, [t])
    sb = v["score_breakdown"]["profile:dev"]["scores"]
    # No health / no budget context -> neutral + no cost pressure.
    assert sb["health"] == 0.5
    assert sb["cost_priority"] == 1.0
    assert "health_unavailable" in v["score_breakdown"]["profile:dev"]["caveats"]


def test_weighted_total_and_top_selection():
    req = _req(skills=["code-review"])
    good = _target(
        "profile:dev",
        kind="profile",
        name="dev",
        allowed_profiles=["dev"],
        skills=["code-review"],
        model="claude",
        provider="anthropic",
    )
    ctx = {
        "concurrency": {"dev": {"in_progress": 0, "max_in_progress": 2}},
        "health": {"dev": {"success_rate": 1.0, "samples": 10}},
    }
    v = pl.score_targets(req, [good], ctx)
    top = v["candidate_set"][0]
    assert top["entity_id"] == "profile:dev"
    expected = round(
        pl.WEIGHTS["capability_fit"] * 1.0
        + pl.WEIGHTS["authorization_match"] * 0.5
        + pl.WEIGHTS["affinity"] * 1.0
        + pl.WEIGHTS["load_headroom"] * 1.0
        + pl.WEIGHTS["health"] * 1.0
        + pl.WEIGHTS["cost_priority"] * 1.0,
        6,
    )
    assert top["total"] == expected


# ---------------------------------------------------------------------------
# no_capable_target + escalation stub
# ---------------------------------------------------------------------------


def test_no_capable_target_classification_and_escalate_stub():
    # No target passes the hard filters.
    req = _req(profile="nobody")
    targets = [
        _target("profile:dev", kind="profile", name="dev", allowed_profiles=["dev"]),
        _target("fabric:a", kind="fabric_node", allowed_profiles=["dev"]),
    ]
    d = pl.build_decision("msn-aaa", "node-a", req, targets)
    assert d["classification"] == pl.CLASS_NO_TARGET
    assert d["assigned_agent"] == ""
    assert d["top_candidate"] is None
    assert d["would_assign"] is False
    assert d["escalate"]["would_raise_signal"] == "capability"
    assert d["escalate"]["path"] == "stubbed"


def test_approval_node_classified_human_approval():
    req = _req(
        kind="approval",
        owner="owner",
        profile="owner",
        authorization_class="high_impact",
    )
    targets = [
        _target("profile:dev", kind="profile", name="dev", allowed_profiles=["dev"])
    ]
    d = pl.build_decision("msn-aaa", "node-approve", req, targets)
    assert d["classification"] == pl.CLASS_HUMAN
    assert d["assigned_agent"] == "owner"
    assert d["would_assign"] is False


def test_assigned_agent_auto_distinct_from_owner():
    req = _req(profile="dev", owner="default")
    targets = [
        _target("profile:dev", kind="profile", name="dev", allowed_profiles=["dev"])
    ]
    d = pl.build_decision("msn-aaa", "node-a", req, targets)
    assert d["classification"] == pl.CLASS_ASSIGNED
    assert d["assigned_agent"] == "auto"
    assert d["owner"] == "default"
    assert d["assigned_agent"] != d["owner"]


# ---------------------------------------------------------------------------
# INV-9 containment
# ---------------------------------------------------------------------------


def test_inv9_no_secret_or_raw_body_in_decision():
    req = _req(profile="dev", skills=["code-review"])
    targets = [
        _target(
            "profile:dev",
            kind="profile",
            name="dev",
            allowed_profiles=["dev"],
            skills=["code-review"],
            model="m",
            provider="p",
        )
    ]
    d = pl.build_decision("msn-aaa", "node-a", req, targets)
    blob = json.dumps(d)
    for bad in ("sk-", "bearer ", "api_key", "prompt", "transcript", "raw_body"):
        if bad == "prompt":
            assert "prompt" not in blob
        else:
            assert bad not in blob.lower()


def test_inv9_requirement_redacts_secret_like_values():
    # A secret-like value must be stripped (sanitized), never leak.
    bad = _req(profile="dev", owner="Bearer sk-abcdefghijklmnopqrstuvwxyz123456")
    stance = pl._stance(bad)
    blob = json.dumps(stance)
    # The raw secret token must not appear in the stanced requirement.
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob
    # A non-secret requirement canonicalizes cleanly.
    assert pl.validate_requirement(_req(profile="dev")) is True


# ---------------------------------------------------------------------------
# Public tool surface (requires operator enabled + manifest/plan seeding)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "config").mkdir(parents=True)
    (root / "profiles" / "dev" / "skills" / "compound-engineering").mkdir(parents=True)
    (root / "profiles" / "dev" / "skills" / "code-review").mkdir(parents=True)

    # Fabric node registry.
    (root / "config" / "fabric-nodes.json").write_text(
        json.dumps(
            {
                "schema": "hermes.fabric-node-registry/v1",
                "version": 1,
                "nodes": [
                    {
                        "name": "node-a",
                        "a2a_peer_name": "rza",
                        "expected_identity": "id-a",
                        "coordinator_principal": "coord@example",
                        "enabled": True,
                        "allowed_profiles": ["dev"],
                        "max_authorization": "reversible_write",
                        "allowed_remote_backends": ["codex"],
                        "logical_workspaces": ["ws-default"],
                        "required_features": ["fabric-execute"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    # Fleet authority manifest.
    (root / "config" / "fleet-authority.json").write_text(
        json.dumps(
            {
                "version": 1,
                "peers": [
                    {
                        "name": "rza",
                        "expected_host_role": "worker",
                        "expected_card_identity": "tony-machine",
                        "allowed_profiles": ["dev"],
                        "max_authorization": "reversible_write",
                        "allow_public_actions": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # Profile config + skills.
    (root / "config.yaml").write_text(
        "model: gpt-5\nprovider: openai\n", encoding="utf-8"
    )
    (root / "profiles" / "dev" / "config.yaml").write_text(
        "model: claude-sonnet\nprovider: anthropic\n", encoding="utf-8"
    )
    for skill in ("compound-engineering", "code-review"):
        (root / "profiles" / "dev" / "skills" / skill / "SKILL.md").write_text(
            "# skill\n", encoding="utf-8"
        )

    # missions.db with a mission + plan + node (so placement_score can read it).
    path = mission._db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with plan._connect(path, write=True) as db:
        plan._begin_write(db)
        db.execute(
            "INSERT INTO missions(mission_id, spec_json, status, version, approval_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "msn-xyz1",
                json.dumps({"objective": "o", "priority": 5, "owner_profile": "dev"}),
                "running",
                1,
                "{}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        plan_json = json.dumps(
            {
                "schema": plan.PLAN_SCHEMA,
                "mission_id": "msn-xyz1",
                "version": 1,
                "decomposition": "test",
                "objective_sha256": "0" * 64,
                "objective_len": 1,
                "nodes": [
                    {
                        "schema": plan.NODE_SCHEMA,
                        "node_id": "node-a",
                        "kind": "single",
                        "owner": "dev",
                        "parents": [],
                        "objective_sha256": "0" * 64,
                        "objective_len": 1,
                        "contract_ref": "contract:node-a",
                        "contract_sha256": "0" * 64,
                        "capability_req": {
                            "profile": "dev",
                            "skills": ["code-review"],
                            "authorization_class": "reversible_write",
                        },
                        "budget": {"est_minutes": 30, "est_tokens": 100_000},
                        "expected_artifacts": ["evidence.json"],
                    },
                ],
            }
        )
        db.execute(
            "INSERT INTO mission_plans(mission_id, plan_json, version, status, plan_sha256, decomposition, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "msn-xyz1",
                plan_json,
                1,
                "approved",
                "0" * 64,
                "test",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.execute(
            "INSERT INTO plan_nodes(mission_id, node_id, contract_sha256, capability_req, budget, deps, state, lease_lock, lease_expires, epoch, failure_kind, retries, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "msn-xyz1",
                "node-a",
                "0" * 64,
                json.dumps(
                    {
                        "profile": "dev",
                        "skills": ["code-review"],
                        "authorization_class": "reversible_write",
                    }
                ),
                json.dumps({"est_minutes": 30, "est_tokens": 100_000}),
                "[]",
                "pending",
                "",
                "",
                0,
                "",
                0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()
    return root


def _enable(monkeypatch, level: str) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, level)
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "dry_run")


def test_placement_candidates_read_only_and_valid(seeded_root: Path, monkeypatch):
    _enable(monkeypatch, "read_only")
    before = _content_files(seeded_root)
    out = json.loads(
        pl.hermes_placement_candidates(
            "dev", skills="code-review", hermes_root=seeded_root
        )
    )
    after = _content_files(seeded_root)
    assert before == after
    assert out["success"] is True
    assert out["count_total"] >= 1
    # The dev profile and node-a fabric node should survive the profile-scope filter.
    names = {c["name"] for c in out["candidate_set"]}
    assert "dev" in names or "node-a" in names
    # No placement decision was recorded.
    assert _placement_rows(seeded_root) == []


def test_placement_score_dry_run_no_write_and_auditable(seeded_root: Path, monkeypatch):
    _enable(monkeypatch, "workspace")
    before = _content_files(seeded_root)
    out = json.loads(
        pl.hermes_placement_score("msn-xyz1", "node-a", hermes_root=seeded_root)
    )
    after = _content_files(seeded_root)
    assert before == after
    # Dry-run records nothing durable.
    assert _placement_rows(seeded_root) == []
    assert out["classification"] == pl.CLASS_ASSIGNED
    assert out["assigned_agent"] == "auto"
    assert out["would_assign"] is False
    assert out["dry_run"] is True
    assert out["decision_sha256"]
    assert (
        "score_breakdown" in out and "candidate_set" in out and "filter_optouts" in out
    )
    # score_breakdown + candidate_set + filter_optouts are populated per D7.
    assert out["candidate_set"]


def test_placement_get_returns_not_found_before_record(seeded_root: Path, monkeypatch):
    _enable(monkeypatch, "read_only")
    out = json.loads(
        pl.hermes_placement_get("msn-xyz1", "node-a", hermes_root=seeded_root)
    )
    assert out["found"] is False


def test_placement_list_empty_before_record(seeded_root: Path, monkeypatch):
    _enable(monkeypatch, "read_only")
    out = json.loads(pl.hermes_placement_list("msn-xyz1", hermes_root=seeded_root))
    assert out["count"] == 0


def test_manifest_targets_respect_allowlist(seeded_root: Path, monkeypatch):
    _enable(monkeypatch, "read_only")
    monkeypatch.setenv(cm.CAPABILITY_SOURCES_ENV, "profile")
    targets = pl.load_manifest_targets(seeded_root)
    assert targets and all(t["kind"] == "profile" for t in targets)


def test_validate_requirement_flag():
    assert pl.validate_requirement(_req()) is True
    assert pl.validate_requirement({"profile": "dev"}) is True
    assert (
        pl.validate_requirement({"skills": [], "authorization_class": "bogus"}) is False
    )
