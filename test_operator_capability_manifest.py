"""Tests for the derived capability-manifest read model (vNext slice-1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_capability_manifest as cm


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    (root / "config").mkdir(parents=True)
    (root / "profiles" / "dev" / "skills" / "compound-engineering").mkdir(parents=True)
    (root / "profiles" / "dev" / "skills" / "code-review").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def clean_cm_env(monkeypatch):
    monkeypatch.delenv(cm.CAPABILITY_SOURCES_ENV, raising=False)
    monkeypatch.delenv(cm.CACHE_TTL_ENV, raising=False)
    cm._placement_cache_invalidate()
    yield
    cm._placement_cache_invalidate()


@pytest.fixture
def seeded(hermes_root: Path) -> Path:
    root = hermes_root

    # Fabric node registry (schema + version 1 + nodes).
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
                        "allowed_profiles": ["gza"],
                        "max_authorization": "reversible_write",
                        "allowed_remote_backends": ["codex"],
                        "logical_workspaces": ["ws-default"],
                        "required_features": ["fabric-execute"],
                    },
                    {
                        "name": "node-b",
                        "a2a_peer_name": "rza",
                        "expected_identity": "id-b",
                        "coordinator_principal": "coord@example",
                        "enabled": False,
                        "allowed_profiles": ["gza"],
                        "max_authorization": "read_only",
                        "allowed_remote_backends": [],
                        "logical_workspaces": [],
                        "required_features": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # Fleet authority manifest (version + peers).
    (root / "config" / "fleet-authority.json").write_text(
        json.dumps(
            {
                "version": 1,
                "peers": [
                    {
                        "name": "rza",
                        "expected_host_role": "worker",
                        "expected_card_identity": "tony-machine",
                        "allowed_profiles": ["gza"],
                        "max_authorization": "read_only",
                        "allow_public_actions": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Default profile config (model/provider).
    (root / "config.yaml").write_text(
        "model: gpt-5\nprovider: openai\n", encoding="utf-8"
    )

    # Named profile: config + skills.
    (root / "profiles" / "dev" / "config.yaml").write_text(
        "model: claude-sonnet\nprovider: anthropic\n", encoding="utf-8"
    )
    for skill in ("compound-engineering", "code-review"):
        (root / "profiles" / "dev" / "skills" / skill / "SKILL.md").write_text(
            "# skill\n", encoding="utf-8"
        )
    return root


def test_manifest_folds_all_source_kinds(seeded: Path):
    out = json.loads(cm.hermes_capability_manifest(hermes_root=seeded, limit=500))
    assert out["success"] is True
    assert out["surface"] == "capability_manifest"
    kinds = {e["entity_kind"] for e in out["entities"]}
    assert {"fabric_node", "fleet_peer", "profile", "provider"} <= kinds
    # Fabric entity carries capability metadata + content-address.
    fabric = [e for e in out["entities"] if e["entity_kind"] == "fabric_node"]
    assert any(
        e["name"] == "node-a"
        and e["enabled"]
        and e["authorization_ceiling"] == "reversible_write"
        for e in fabric
    )
    # Each entity carries an INV-9 content address.
    for e in out["entities"]:
        assert e["capability_sha256"]
    # Provider dimension derived from profile configs.
    providers = {e["name"] for e in out["entities"] if e["entity_kind"] == "provider"}
    assert {"openai", "anthropic"} <= providers


def test_manifest_placement_cache_ttl_snapshot(seeded: Path):
    out = json.loads(
        cm.hermes_capability_manifest(hermes_root=seeded, include_cache=True)
    )
    cache = out["placement_cache"]
    assert cache["built_at"]
    assert cache["expires_at"]
    assert cache["ttl_seconds"] >= cm.CACHE_TTL_FLOOR
    # Disabled fabric nodes are excluded from the placeable candidate set.
    names = {c["name"] for c in cache["candidates"]}
    assert "node-a" in names
    assert "node-b" not in names
    assert cache["candidate_count"] == len(cache["candidates"])


def test_manifest_allowlist_filters_sources(seeded: Path, monkeypatch):
    monkeypatch.setenv(cm.CAPABILITY_SOURCES_ENV, "fabric")
    out = json.loads(cm.hermes_capability_manifest(hermes_root=seeded, limit=500))
    kinds = {e["entity_kind"] for e in out["entities"]}
    assert kinds == {"fabric_node"}
    assert out["sources_allowed"] == ["fabric"]


def test_manifest_inv9_no_secret_or_raw_body(seeded: Path):
    # Emit a profile config carrying a secret value; it must not surface.
    (seeded / "profiles" / "dev" / "config.yaml").write_text(
        "model: x\nprovider: y\napi_key: sk-supersecret1234567890abcdef\n",
        encoding="utf-8",
    )
    out = json.loads(cm.hermes_capability_manifest(hermes_root=seeded, limit=500))
    blob = json.dumps(out)
    assert "sk-supersecret1234567890abcdef" not in blob
    assert "api_key" not in blob or blob.count("api_key") == 0
    # No raw prompt/transcript/memory field exists on the surface.
    assert "prompt" not in blob
    assert "transcript" not in blob
    assert "raw_body" not in blob


def test_manifest_bounded_and_audited(seeded: Path):
    out = json.loads(cm.hermes_capability_manifest(hermes_root=seeded, limit=2))
    # limit caps the returned entities, count_total reflects the full set.
    assert out["count_returned"] <= 2
    assert out["count_total"] >= 2
    assert out["truncated"] is True


def test_manifest_is_read_only_no_mutation(seeded: Path):
    # Snapshot the set of files under the root before and after the read.
    before = {p for p in seeded.rglob("*") if p.is_file()}
    cm.hermes_capability_manifest(hermes_root=seeded, include_cache=True)
    after = {p for p in seeded.rglob("*") if p.is_file()}
    assert before == after
