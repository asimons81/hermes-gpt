from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fabric_artifacts
import fabric_write_guard
import operator_fabric as fabric
import operator_fabric_router as router
import operator_fabric_view as view

NOW = datetime.now(timezone.utc)


def node(name: str = "node-a") -> fabric.FabricNode:
    return fabric.FabricNode(
        name=name,
        a2a_peer_name=f"peer-{name}",
        expected_identity=f"identity-{name}",
        coordinator_principal="coord-main",
        enabled=True,
        allowed_profiles=("default",),
        max_authorization="high_impact",
        allowed_remote_backends=("pi_rpc",),
        logical_workspaces=("repo",),
        required_features=("fabric-managed-peer-v1",),
    )


def facts(*, observed_at: datetime = NOW, active: int = 1) -> router.TargetFacts:
    return router.TargetFacts(
        observed_at=observed_at,
        max_age_seconds=300,
        os_names=frozenset({"linux"}),
        runtimes=frozenset({"python"}),
        runners=frozenset({"pi_rpc"}),
        providers=frozenset({"openai"}),
        models=frozenset({"gpt-5.6"}),
        tools=frozenset({"code"}),
        browser=False,
        vision=True,
        gpu_available=True,
        gpu_vendor="nvidia",
        gpu_memory_mb=24576,
        capacity=4,
        active=active,
        cost_bucket=1,
        locality_bucket=1,
    )


def all_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_strings(item)
    elif isinstance(value, str):
        yield value


def test_nodes_view_is_observational_and_marks_stale(monkeypatch):
    monkeypatch.setattr(view.fabric, "load_node_registry", lambda **_kwargs: {"node-a": node()})
    monkeypatch.setattr(
        view.router,
        "load_routing_policy",
        lambda **_kwargs: router.RoutingPolicy(
            targets={"node-a": facts(observed_at=NOW - timedelta(hours=2))}
        ),
    )
    result = view.nodes_view()
    assert result["success"] is True
    assert result["nodes"][0]["availability"] == "stale"
    assert result["nodes"][0]["freshness"] == "stale"
    assert result["nodes"][0]["capabilities"]["gpu"]["memory_mb"] == 24576
    joined = "\n".join(all_strings(result)).lower()
    assert "authorization: bearer" not in joined
    assert "http://" not in joined
    assert "https://" not in joined
    assert "/home/" not in joined


def test_attempts_view_does_not_create_missing_journal(tmp_path, monkeypatch):
    monkeypatch.setenv(fabric.COORDINATOR_DB_ENV, str(tmp_path / "coordinator.db"))
    result = view.attempts_view(hermes_root=tmp_path)
    assert result["success"] is True
    assert result["attempts"] == []
    assert not (tmp_path / "coordinator.db").exists()


def seed_attempt(root: Path, *, state: str = "BLOCKED", error_code: str = "FABRIC_PEER_UNAVAILABLE") -> tuple[Path, str]:
    db_path = root / "fabric" / "coordinator.db"
    fabric._init_coordinator_db(db_path)
    fabric_write_guard.migrate_coordinator(db_path)
    fabric_artifacts.migrate_coordinator(db_path)
    dispatch_id = "fdisp-1234567890abcdef"
    attempt_id = "fatt-1234567890abcdef"
    with fabric._connect(db_path) as db:
        db.execute(
            "INSERT INTO dispatches(dispatch_id,task_id,contract_sha256,node_name,evidence_policy_json,created_at) VALUES(?,?,?,?,?,?)",
            (dispatch_id, "task-1", "a" * 64, "node-a", '{"run_state":["managed_peer_structured"]}', NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO attempts(attempt_id,dispatch_id,envelope_sha256,node_name,peer_name,remote_backend,coordinator_principal,capability_sha256,peer_policy_sha256,state,remote_task_id,evidence_json,error_code,created_at,updated_at,write_epoch,retry_parent_attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                dispatch_id,
                "b" * 64,
                "node-a",
                "peer-node-a",
                "pi_rpc",
                "coord-main",
                "c" * 64,
                "d" * 64,
                state,
                "ftask-1234567890abcdef",
                json.dumps(
                    {
                        "terminal_state": "FAILED",
                        "created_at": NOW.isoformat(),
                        "policy_sha256": "d" * 64,
                        "coordinator_principal": "coord-main",
                        "observations": [
                            {
                                "kind": "run_state",
                                "provenance": "managed_peer_structured",
                                "state": "failed",
                                "outcome": "failed",
                                "started_at": NOW.isoformat(),
                                "ended_at": NOW.isoformat(),
                                "source": "runner:pi_rpc",
                                "error": "Bearer very-secret-token-value-that-must-not-render",
                            }
                        ],
                    }
                ),
                error_code,
                NOW.isoformat(),
                NOW.isoformat(),
                7,
                None,
            ),
        )
        db.execute(
            "INSERT INTO artifact_admissions(artifact_id,attempt_id,dispatch_id,logical_name,admission_path,size_bytes,sha256,media_type,active_content,admitted_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "fart-1234567890abcdef",
                attempt_id,
                dispatch_id,
                "reports/summary.html",
                "/home/tony/private/fabric/admitted/blob",
                123,
                "e" * 64,
                "text/html",
                1,
                NOW.isoformat(),
            ),
        )
    return db_path, attempt_id


def test_attempt_detail_redacts_private_paths_and_isolates_active_artifacts(tmp_path, monkeypatch):
    db_path, attempt_id = seed_attempt(tmp_path)
    monkeypatch.setenv(fabric.COORDINATOR_DB_ENV, str(db_path))
    monkeypatch.setattr(view.fabric, "load_node_registry", lambda **_kwargs: {"node-a": node()})
    result = view.attempt_detail(attempt_id, hermes_root=tmp_path)
    assert result["success"] is True
    attempt = result["attempt"]
    assert attempt["state"] == "BLOCKED"
    assert attempt["blocker"] == "FABRIC_PEER_UNAVAILABLE"
    assert attempt["authority"]["granted"] == "write_capable"
    assert attempt["authority"]["write_epoch"] == 7
    artifact = attempt["artifacts"][0]
    assert artifact["logical_name"] == "reports/summary.html"
    assert artifact["active_content"] is True
    assert artifact["render_policy"] == "isolated_metadata_only"
    assert "admission_path" not in artifact
    joined = "\n".join(all_strings(result))
    assert "/home/tony/private" not in joined
    assert "very-secret-token-value" not in joined
    assert "coord-main" not in joined


def test_routing_receipt_exposes_bounded_explanation(tmp_path):
    path = tmp_path / "fabric" / "routing-decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": router.ROUTING_DECISION_SCHEMA,
                "router": router.ROUTER_NAME,
                "task_id": "task-1",
                "original_contract_sha256": "1" * 64,
                "placed_contract_sha256": "a" * 64,
                "requirements": {"location": "remote", "gpu": True, "runners": ["pi_rpc"]},
                "selected": {"node": "node-a", "backend": "pi_rpc", "remote": True, "rank": [0, 0, 1]},
                "candidates": [
                    {
                        "node": "node-b",
                        "backend": "pi_rpc",
                        "remote": True,
                        "healthy": False,
                        "capability_fresh": False,
                        "authority_ceiling": "read_only",
                        "eligible": False,
                        "exclusions": [{"code": "CAPABILITY_STALE", "detail": "facts expired"}],
                        "rank": [1, 1, 9],
                    }
                ],
                "created_at": NOW.isoformat(),
            }
        )
        + "\n"
    )
    rows = view.routing_decisions_view(hermes_root=tmp_path)
    assert rows[0]["explanation_available"] is True
    assert rows[0]["requirements"]["gpu"] is True
    assert rows[0]["candidates"][0]["exclusions"][0]["code"] == "CAPABILITY_STALE"


def test_old_routing_receipt_degrades_without_inventing_explanation(tmp_path):
    path = tmp_path / "fabric" / "routing-decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": router.ROUTING_DECISION_SCHEMA,
                "router": router.ROUTER_NAME,
                "task_id": "task-old",
                "original_contract_sha256": "1" * 64,
                "placed_contract_sha256": "2" * 64,
                "selected": {"node": "node-a", "backend": "pi_rpc", "remote": True, "rank": [0]},
                "created_at": NOW.isoformat(),
            }
        )
        + "\n"
    )
    rows = view.routing_decisions_view(hermes_root=tmp_path)
    assert rows[0]["explanation_available"] is False
    assert rows[0]["requirements"] == {}
    assert rows[0]["candidates"] == []
