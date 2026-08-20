from __future__ import annotations

from datetime import datetime, timedelta, timezone

import operator_fabric as fabric
import operator_fabric_router as router
import operator_runners as runners
import pytest


NOW = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)


def target_facts(
    *,
    observed_at: datetime = NOW,
    os_names=("linux",),
    runtimes=("python",),
    runner_names=("pi_rpc",),
    providers=("openai",),
    models=("gpt-5.6",),
    tools=("code",),
    browser=False,
    vision=False,
    gpu=False,
    gpu_memory_mb=0,
    capacity=4,
    active=0,
    cost_bucket=1,
    locality_bucket=1,
):
    return router.TargetFacts(
        observed_at=observed_at,
        max_age_seconds=300,
        os_names=frozenset(os_names),
        runtimes=frozenset(runtimes),
        runners=frozenset(runner_names),
        providers=frozenset(providers),
        models=frozenset(models),
        tools=frozenset(tools),
        browser=browser,
        vision=vision,
        gpu_available=gpu,
        gpu_vendor="nvidia" if gpu else "",
        gpu_memory_mb=gpu_memory_mb,
        capacity=capacity,
        active=active,
        cost_bucket=cost_bucket,
        locality_bucket=locality_bucket,
    )


def node(name="node-a", max_authorization="high_impact", backends=("pi_rpc",)):
    return fabric.FabricNode(
        name=name,
        a2a_peer_name=f"peer-{name}",
        expected_identity=f"identity-{name}",
        coordinator_principal="coord-main",
        enabled=True,
        allowed_profiles=("default",),
        max_authorization=max_authorization,
        allowed_remote_backends=tuple(backends),
        logical_workspaces=("repo",),
        required_features=(),
    )


def contract(tmp_path, *, auth="read_only", requirements=None, preferences=None, runner_options=None):
    return {
        "schema": "hermes.work-contract/v1",
        "task_id": "task-auto-1",
        "assigned_agent": "auto",
        "assigned_profile": "default",
        "objective": "Perform bounded work",
        "allowed_scope": {"workspaces": [str(tmp_path)], "profiles": ["default"]},
        "forbidden_actions": [],
        "expected_artifacts": [],
        "tests": [],
        "review_requirements": {
            "required": False,
            "reviewer": "",
            "evidence": "",
            "approval_required": False,
        },
        "completion_criteria": {
            "run_state": {"terminal": True, "outcome_ok": ["completed"]},
            "artifacts_present": False,
            "tests_pass": False,
            "review_satisfied": False,
            "no_forbidden_actions": True,
        },
        "inputs": [],
        "constraints": [],
        "authorization": {"class": auth, "approved": auth == "high_impact"},
        "execution": {
            "backend": "auto",
            "options": {
                "requirements": requirements or {},
                "preferences": preferences or {},
                "logical_workspace": "repo",
                "runner_options": runner_options or {},
            },
        },
    }


def make_router(
    *,
    facts=None,
    nodes=None,
    local=("codex",),
    remote_probe=None,
    local_auth="high_impact",
):
    facts = facts or {}
    nodes = nodes or {}
    return router.AutoRouter(
        registry_loader=lambda: nodes,
        routing_policy_loader=lambda: router.RoutingPolicy(targets=facts),
        remote_probe=remote_probe or (lambda _node, _timeout: {"healthy": True, "latency_ms": 25.0}),
        local_backends=lambda: list(local),
        local_posture=lambda _dry: {"ready": True, "max_authorization": local_auth},
        now=lambda: NOW,
    )


def candidate(decision, node_name, backend):
    return next(item for item in decision["candidates"] if item["node"] == node_name and item["backend"] == backend)


def exclusion_codes(item):
    return {entry["code"] for entry in item["exclusions"]}


def test_hard_constraints_run_before_gpu_preference(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    facts = {
        "local": target_facts(runner_names=("codex",), gpu=False, cost_bucket=0, locality_bucket=0),
        "node-a": target_facts(gpu=True, gpu_memory_mb=24576, cost_bucket=9, locality_bucket=9),
    }
    r = make_router(facts=facts, nodes={"node-a": node(max_authorization="read_only")})
    value = contract(
        tmp_path,
        auth="reversible_write",
        preferences={"prefer_local": False, "prefer_gpu": True},
    )
    decision = r.route(value)
    remote = candidate(decision, "node-a", "pi_rpc")
    assert "AUTHORITY_INSUFFICIENT" in exclusion_codes(remote)
    assert decision["selected"]["node"] == "local"
    assert decision["selected"]["backend"] == "codex"


def test_fresh_gpu_remote_can_win_specialized_hardware_rank(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    facts = {
        "local": target_facts(runner_names=("codex",), gpu=False, cost_bucket=0, locality_bucket=0),
        "node-a": target_facts(gpu=True, gpu_memory_mb=24576, cost_bucket=9, locality_bucket=0),
    }
    r = make_router(facts=facts, nodes={"node-a": node()})
    decision = r.route(
        contract(tmp_path, preferences={"prefer_local": False, "prefer_gpu": True})
    )
    assert decision["selected"]["node"] == "node-a"
    assert decision["selected"]["backend"] == "pi_rpc"


def test_stale_remote_manifest_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    stale = target_facts(observed_at=NOW - timedelta(hours=2))
    r = make_router(facts={"node-a": stale}, nodes={"node-a": node()}, local=())
    decision = r.route(contract(tmp_path))
    item = candidate(decision, "node-a", "pi_rpc")
    assert item["eligible"] is False
    assert "CAPABILITY_STALE" in exclusion_codes(item)
    assert decision["selected"] is None


def test_unhealthy_peer_is_never_ranked_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)

    def broken(_node, _timeout):
        raise fabric.FabricError("FABRIC_PEER_UNAVAILABLE", "offline")

    r = make_router(
        facts={"node-a": target_facts()},
        nodes={"node-a": node()},
        local=(),
        remote_probe=broken,
    )
    decision = r.route(contract(tmp_path))
    item = candidate(decision, "node-a", "pi_rpc")
    assert "FABRIC_PEER_UNAVAILABLE" in exclusion_codes(item)
    assert decision["selected"] is None


def test_missing_gpu_tool_runtime_are_hard_exclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    facts = {"node-a": target_facts(runtimes=("python",), tools=("code",), gpu=False)}
    r = make_router(facts=facts, nodes={"node-a": node()}, local=())
    decision = r.route(
        contract(
            tmp_path,
            requirements={
                "runtimes": ["cuda"],
                "tools": ["browser"],
                "gpu": True,
                "min_gpu_memory_mb": 8192,
            },
        )
    )
    codes = exclusion_codes(candidate(decision, "node-a", "pi_rpc"))
    assert {
        "CAPABILITY_RUNTIME_MISMATCH",
        "CAPABILITY_TOOL_MISMATCH",
        "CAPABILITY_GPU_MISMATCH",
        "CAPABILITY_GPU_MEMORY_MISMATCH",
    } <= codes


def test_no_candidate_returns_explainable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    r = make_router(local=(), nodes={})
    decision = r.route(contract(tmp_path, requirements={"gpu": True}))
    assert decision["selected"] is None
    assert decision["candidates"] == []


def test_ties_are_stable_by_node_then_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    facts = {
        "node-a": target_facts(runner_names=("codex", "pi_rpc"), cost_bucket=1),
        "node-b": target_facts(runner_names=("codex", "pi_rpc"), cost_bucket=1),
    }
    nodes = {
        "node-b": node("node-b", backends=("pi_rpc", "codex")),
        "node-a": node("node-a", backends=("pi_rpc", "codex")),
    }
    r = make_router(facts=facts, nodes=nodes, local=())
    decision = r.route(contract(tmp_path, preferences={"prefer_local": False}))
    assert decision["selected"]["node"] == "node-a"
    assert decision["selected"]["backend"] == "codex"


def test_nonfinite_latency_is_only_worst_soft_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    r = make_router(
        facts={"node-a": target_facts()},
        nodes={"node-a": node()},
        local=(),
        remote_probe=lambda _node, _timeout: {"healthy": True, "latency_ms": float("nan")},
    )
    decision = r.route(contract(tmp_path))
    item = candidate(decision, "node-a", "pi_rpc")
    assert item["eligible"] is True
    assert item["rank"][3] == 9
    assert decision["selected"]["node"] == "node-a"


def test_absolute_path_smuggling_in_auto_runner_options_is_rejected(tmp_path):
    r = make_router(local=())
    with pytest.raises(router.RoutingError) as exc:
        r.route(contract(tmp_path, runner_options={"workdir": "/tmp/peer-path"}))
    assert exc.value.code == "FABRIC_ROUTING_CALLER_PATH_REJECTED"


def test_explicit_backend_selection_remains_unchanged():
    value = {"execution": {"backend": "pi_rpc", "options": {}}}
    assert runners.selected_backend(value) == "pi_rpc"


def test_remote_placement_preserves_authorization_and_uses_fabric(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    r = make_router(
        facts={"node-a": target_facts(gpu=True, gpu_memory_mb=24576)},
        nodes={"node-a": node()},
        local=(),
    )
    value = contract(tmp_path, auth="reversible_write", runner_options={"sandbox": "workspace-write"})
    decision = r.route(value)
    placed = r.placed_contract(value, decision)
    assert placed["authorization"] == value["authorization"]
    assert placed["assigned_agent"] == "node-a"
    assert placed["execution"]["backend"] == "fabric"
    assert placed["execution"]["options"]["remote_backend"] == "pi_rpc"
    assert placed["execution"]["options"]["remote_options"] == {"sandbox": "workspace-write"}


def test_auto_backend_dispatches_only_the_selected_concrete_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(runners, "_runner_allowed", lambda _name: True)
    r = make_router(
        facts={"local": target_facts(runner_names=("codex",))},
        nodes={},
        local=("codex",),
    )
    captured = []

    def dispatch_fn(placed, **kwargs):
        captured.append((placed, kwargs))
        return {"success": True, "changed": True, "backend": placed["execution"]["backend"]}

    backend = router.AutoBackend(router_factory=lambda **_kwargs: r, dispatch_fn=dispatch_fn)
    result = backend.dispatch(
        contract(tmp_path),
        confirm=True,
        dry_run=False,
        timeout=10,
        hermes_root=tmp_path,
        fabric_router=r,
    )
    assert result["success"] is True
    assert result["backend"] == "auto"
    assert result["selected_backend"] == "codex"
    assert captured[0][0]["execution"] == {"backend": "codex", "options": {}}
    assert captured[0][0]["authorization"] == contract(tmp_path)["authorization"]
    journal = tmp_path / "fabric" / "routing-decisions.jsonl"
    assert journal.is_file()
    assert "objective" not in journal.read_text(encoding="utf-8")


def test_auto_requires_unambiguous_assigned_agent(tmp_path):
    value = contract(tmp_path)
    value["assigned_agent"] = "node-a"
    with pytest.raises(router.RoutingError) as exc:
        make_router().route(value)
    assert exc.value.code == "FABRIC_ROUTING_INVALID"


def test_routing_policy_loader_rejects_unknown_fields(tmp_path):
    path = tmp_path / "routing.json"
    path.write_text(
        '{"schema":"hermes.fabric-routing-policy/v1","version":1,"targets":{"local":{"observed_at":"2026-08-20T15:00:00Z","surprise":true}}}',
        encoding="utf-8",
    )
    with pytest.raises(router.RoutingError) as exc:
        router.load_routing_policy(path)
    assert exc.value.code == "FABRIC_ROUTING_CONFIG_INVALID"
