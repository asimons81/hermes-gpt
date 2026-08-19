"""Tests for the bounded A2A-backed Hermes GPT fleet control surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import operator_policy as op
import operator_fleet as fleet


HERMES = "/test/hermes"
REGISTRY = {
    "agents": [
        {"name": "rza", "url": "http://rza.example:8765", "hasToken": True},
        {"name": "nous-girl", "url": "http://nous.example:8765", "hasToken": True},
    ]
}


def runner_with(responses: dict[tuple[str, ...], tuple[int, str, str]], calls: list[list[str]]):
    def runner(argv: list[str], *, timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        return responses.get(tuple(argv), (1, "", "unexpected argv"))

    return runner


def test_fleet_runner_uses_declared_remote_output_bound(monkeypatch):
    seen = {}

    def fake_run(argv, *, timeout, max_output_chars):
        seen.update(argv=argv, timeout=timeout, max_output_chars=max_output_chars)
        return 0, "ok", ""

    monkeypatch.setattr(op, "run_argv", fake_run)
    assert fleet._run(["hermes", "a2a", "task"], timeout=15, runner=None) == (0, "ok", "")
    assert seen["max_output_chars"] == fleet._MAX_REMOTE_BYTES


def enable_read_only(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


def authority_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "fleet-authority.json"
    path.write_text(json.dumps({
        "version": 1,
        "peers": [
            {
                "name": "rza",
                "expected_host_role": "wu_tang_host",
                "expected_card_identity": "Wu-Tang RZA Router",
                "allowed_profiles": ["default", "gza"],
                "max_authorization": "high_impact",
                "allow_public_actions": True,
            },
            {
                "name": "nous-girl",
                "expected_host_role": "compute_specialist",
                "expected_card_identity": "Nous Girl GPU Compute",
                "allowed_profiles": ["default"],
                "max_authorization": "reversible_write",
                "allow_public_actions": False,
            },
        ],
    }), encoding="utf-8")
    return path


def test_authority_manifest_accepts_human_readable_card_identities(tmp_path):
    peers = fleet._load_authority(authority_manifest(tmp_path))
    assert peers["rza"].expected_card_identity == "Wu-Tang RZA Router"
    assert peers["nous-girl"].expected_card_identity == "Nous Girl GPU Compute"


def test_authority_manifest_accepts_gaming_4090_peer(tmp_path):
    """gaming-4090 is a supported fleet peer (2-node fleet, corrected 2026-08-15)."""
    path = tmp_path / "fleet-authority.json"
    path.write_text(json.dumps({
        "version": 1,
        "peers": [
            {
                "name": "gaming-4090",
                "expected_host_role": "gpu-compute-subordinate",
                "expected_card_identity": "TONY-GAMING-TOP",
                "allowed_profiles": ["default"],
                "max_authorization": "read_only",
                "allow_public_actions": False,
            },
        ],
    }), encoding="utf-8")
    peers = fleet._load_authority(path)
    assert peers["gaming-4090"].expected_card_identity == "TONY-GAMING-TOP"
    assert peers["gaming-4090"].allowed_profiles == ("default",)
    # profile ceiling enforced: unknown profile for this peer is rejected
    bad = json.loads(path.read_text())
    bad["peers"][0]["allowed_profiles"] = ["default", "rza"]
    path.write_text(json.dumps(bad), encoding="utf-8")
    import pytest
    with pytest.raises(PermissionError):
        fleet._load_authority(path)


def enable_workspace(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")


def work_order(**overrides):
    value = {
        "agent": "rza", "task_id": "wo-123", "target_profile": "gza",
        "objective": "Review the release", "workspace": "/srv/project",
        "inputs": ["README.md"],
        "constraints": ["Do not publish", "Do not expose credentials", "Do not edit credential store policy"],
        "acceptance_checks": ["pytest passes"], "deliverables": ["review.md"],
        "authorization": {"class": "read_only", "approved": False},
    }
    value.update(overrides)
    return value


def test_fleet_list_requires_operator_mode_and_never_returns_registry_urls(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(fleet.hermes_fleet_list(runner=runner, hermes_bin=HERMES))

    assert out == {
        "success": True,
        "count": 2,
        "agents": [
            {"name": "rza", "has_token": True},
            {"name": "nous-girl", "has_token": True},
        ],
    }
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_status_uses_fixed_doctor_argv_and_safe_summary(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "doctor", "rza", "--timeout", "9", "--json"): (
                0,
                json.dumps({"ok": True, "status": "compatible", "name": "RZA", "capabilities": {"message_send": True}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_status(agent="rza", timeout=9, runner=runner, hermes_bin=HERMES))

    assert out["success"] is True
    assert out["agent"] == "rza"
    assert out["status"] == "compatible"
    assert out["capability_count"] == 1
    assert out["warnings_count"] == 0
    assert out["errors_count"] == 0
    assert calls[-1] == [HERMES, "a2a", "doctor", "rza", "--timeout", "9", "--json"]


def test_fleet_dispatch_dry_run_never_sends_message(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message="review the deployment", dry_run=True, confirm=True,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out["success"] is True
    assert out["dry_run"] is True
    assert out["plan"] == {"agent": "rza", "message_len": 21}
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_dispatch_requires_confirm_before_remote_execution(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message="review the deployment", dry_run=False, confirm=False,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out["success"] is False
    assert out["code"] == "CONFIRMATION_REQUIRED"
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_dispatch_sends_to_registered_agent_and_redacts_prompt_from_output(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    message = "secret-looking but legitimate task payload"
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "send", "--json", "rza", "--", message): (
                0,
                json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_SUBMITTED"}, "history": [{"parts": [{"text": message}]}]}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(
        fleet.hermes_fleet_dispatch(
            agent="rza", message=message, dry_run=False, confirm=True,
            runner=runner, hermes_bin=HERMES,
        )
    )

    assert out == {
        "success": True,
        "changed": True,
        "agent": "rza",
        "task_id": "task-123",
        "state": "TASK_STATE_SUBMITTED",
    }
    assert message not in json.dumps(out)
    assert calls[-1] == [HERMES, "a2a", "send", "--json", "rza", "--", message]


def test_official_send_timeout_recovers_peer_task_id_by_context(monkeypatch):
    monkeypatch.setattr(fleet, "_fetch_card", lambda *args, **kwargs: {})
    calls = []

    def fake_post(url, body, headers, timeout):
        calls.append(body)
        if body["method"] == "SendMessage":
            raise TimeoutError("peer reply timed out")
        assert body["method"] == "ListTasks"
        context_id = body["params"]["contextId"]
        assert context_id == calls[0]["params"]["message"]["contextId"]
        return {"result": {"tasks": [{"id": "task-peer123"}]}}

    monkeypatch.setattr(fleet, "_http_post_json", fake_post)
    peer = {"url": "http://rza.example:8765", "auth": {}, "timeout": 30}

    with pytest.raises(fleet.FleetDispatchTimeout) as excinfo:
        fleet._send_message("rza", peer, "long-running task", 30)

    assert excinfo.value.task_id == "task-peer123"
    assert [call["method"] for call in calls] == ["SendMessage", "ListTasks"]


def test_fleet_dispatch_timeout_returns_pollable_task_id(monkeypatch):
    enable_workspace(monkeypatch)
    monkeypatch.setattr(fleet, "_registered_agent", lambda *args, **kwargs: ("rza", None))
    monkeypatch.setattr(fleet, "_a2a_peers_with_resolved_tokens", lambda: {
        "rza": {"url": "http://rza.example:8765", "auth": {}},
    })
    monkeypatch.setattr(
        fleet,
        "_send_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(fleet.FleetDispatchTimeout("task-timeout123")),
    )

    out = json.loads(fleet.hermes_fleet_dispatch(
        agent="rza", message="long-running task", dry_run=False, confirm=True,
    ))

    assert out["success"] is False
    assert out["code"] == "FLEET_DISPATCH_TIMEOUT"
    assert out["task_id"] == "task-timeout123"
    assert out["submission_may_have_succeeded"] is True
    assert "hermes_fleet_task" in out["suggested_action"]


def test_work_order_timeout_returns_remote_pollable_task_id(monkeypatch, tmp_path):
    enable_workspace(monkeypatch)
    monkeypatch.setattr(fleet, "_registered_agent", lambda *args, **kwargs: ("rza", None))
    monkeypatch.setattr(fleet, "_verify_live_peer", lambda *args, **kwargs: None)
    monkeypatch.setattr(fleet, "_a2a_peers_with_resolved_tokens", lambda: {
        "rza": {"url": "http://rza.example:8765", "auth": {}},
    })
    monkeypatch.setattr(
        fleet,
        "_send_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(fleet.FleetDispatchTimeout("task-timeout456")),
    )

    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(), dry_run=False, confirm=True,
        authority_manifest=authority_manifest(tmp_path),
    ))

    assert out["success"] is False
    assert out["code"] == "FLEET_DISPATCH_TIMEOUT"
    assert out["task_id"] == "task-timeout456"
    assert out["submission_may_have_succeeded"] is True


def test_fleet_task_rejects_unknown_agent_before_any_remote_command(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")},
        calls,
    )

    out = json.loads(fleet.hermes_fleet_task(agent="not-a-peer", task_id="task-123", runner=runner, hermes_bin=HERMES))

    assert out["success"] is False
    assert out["code"] == "UNKNOWN_AGENT"
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def test_fleet_task_reads_the_a2a_cli_wrapped_task_shape(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "task", "--agent", "rza", "--json", "--", "task-123"): (
                0,
                json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "2026-07-26T20:00:00+00:00"}, "artifacts": [{}, {}]}}),
                "",
            ),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_task(agent="rza", task_id="task-123", runner=runner, hermes_bin=HERMES))

    assert out == {
        "success": True,
        "agent": "rza",
        "task_id": "task-123",
        "state": "TASK_STATE_COMPLETED",
        "timestamp": "2026-07-26T20:00:00+00:00",
        "artifact_count": 2,
    }


def test_server_registers_the_bounded_fleet_control_tools():
    import asyncio
    import server

    names = {tool.name for tool in asyncio.run(server.build_server().list_tools())}

    assert {
        "hermes_fleet_list",
        "hermes_fleet_status",
        "hermes_fleet_dispatch",
        "hermes_fleet_task",
    } <= names


def test_fleet_read_tools_are_denied_when_operator_mode_is_disabled(monkeypatch):
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_list(runner=runner, hermes_bin=HERMES))

    assert out["code"] == "FLEET_POLICY_DENIED"
    assert calls == []


def test_fleet_dispatch_validates_policy_before_registry_lookup(monkeypatch):
    monkeypatch.delenv(op.OPERATOR_ENABLED_ENV, raising=False)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_dispatch(agent="rza", message="safe task", runner=runner, hermes_bin=HERMES))

    assert out["code"] == "FLEET_POLICY_DENIED"
    assert calls == []


def test_fleet_dispatch_dash_leading_message_is_a_positional_argument(monkeypatch):
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_APPLY_MODE_ENV, "direct")
    message = "--token"
    calls: list[list[str]] = []
    runner = runner_with(
        {
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "send", "--json", "rza", "--", message): (0, json.dumps({"task": {"id": "task-123", "status": {"state": "TASK_STATE_SUBMITTED"}}}), ""),
        },
        calls,
    )

    out = json.loads(fleet.hermes_fleet_dispatch(agent="rza", message=message, dry_run=False, confirm=True, runner=runner, hermes_bin=HERMES))

    assert out["success"] is True
    assert calls[-1] == [HERMES, "a2a", "send", "--json", "rza", "--", "--token"]


def test_fleet_task_rejects_dash_leading_task_id_before_registry_lookup(monkeypatch):
    enable_read_only(monkeypatch)
    calls: list[list[str]] = []
    runner = runner_with({}, calls)

    out = json.loads(fleet.hermes_fleet_task(agent="rza", task_id="-h", runner=runner, hermes_bin=HERMES))

    assert out["code"] == "INVALID_TASK_ID"
    assert calls == []


def test_valid_work_order_is_canonical_hashed_and_dry_run(monkeypatch, tmp_path):
    enable_workspace(monkeypatch)
    calls = []
    runner = runner_with({(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")}, calls)
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(), dry_run=True, confirm=True, runner=runner, hermes_bin=HERMES,
        authority_manifest=authority_manifest(tmp_path),
    ))
    assert out["success"] is True and out["dry_run"] is True
    assert out["plan"]["live_peer_verification"] == "required_before_dispatch"
    assert len(out["plan"]["prompt_sha256"]) == 64
    rendered = json.dumps(out)
    assert "Review the release" not in rendered
    assert calls == [[HERMES, "a2a", "registry", "list", "--json"]]


def confirmed_work_order_runner(calls, *, doctor_code=0, doctor_payload=None, doctor_stderr=""):
    doctor_payload = doctor_payload if doctor_payload is not None else {
        "ok": True, "name": "Wu-Tang RZA Router", "host_role": "wu_tang_host",
    }

    def runner(argv: list[str], *, timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        if argv == [HERMES, "a2a", "registry", "list", "--json"]:
            return 0, json.dumps(REGISTRY), ""
        if argv == [HERMES, "a2a", "doctor", "rza", "--timeout", "30", "--json"]:
            rendered = doctor_payload if isinstance(doctor_payload, str) else json.dumps(doctor_payload)
            return doctor_code, rendered, doctor_stderr
        if argv[:6] == [HERMES, "a2a", "send", "--json", "rza", "--"]:
            return 0, json.dumps({"task": {"id": "remote-123", "status": {"state": "TASK_STATE_SUBMITTED"}}}), ""
        return 1, "", "unexpected argv"

    return runner


def test_confirmed_work_order_verifies_matching_live_peer_before_send(monkeypatch, tmp_path):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(), dry_run=False, confirm=True,
        runner=confirmed_work_order_runner(calls), hermes_bin=HERMES,
        authority_manifest=authority_manifest(tmp_path),
    ))

    assert out["success"] is True
    assert out["live_peer_verified"] is True
    assert [call[2] for call in calls] == ["registry", "doctor", "send"]


@pytest.mark.parametrize("doctor_payload", [
    {"ok": False, "name": "Wu-Tang RZA Router", "host_role": "wu_tang_host"},
    {"ok": True, "name": "not-rza", "host_role": "wu_tang_host"},
    {"ok": True, "name": "Wu-Tang RZA Router", "host_role": "worker"},
    {"ok": True, "name": "Wu-Tang RZA Router"},
    "warning: token=super-secret\n{malformed",
])
def test_confirmed_work_order_fails_closed_on_bad_live_peer_metadata(monkeypatch, tmp_path, doctor_payload):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(), dry_run=False, confirm=True,
        runner=confirmed_work_order_runner(calls, doctor_payload=doctor_payload), hermes_bin=HERMES,
        authority_manifest=authority_manifest(tmp_path),
    ))

    rendered = json.dumps(out)
    assert out["code"] == "FLEET_PEER_VERIFICATION_FAILED"
    assert all("send" not in call for call in calls)
    assert "not-rza" not in rendered
    assert "worker" not in rendered
    assert "super-secret" not in rendered
    assert "malformed" not in rendered


def test_confirmed_work_order_fails_closed_when_live_peer_is_unavailable(monkeypatch, tmp_path):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(), dry_run=False, confirm=True,
        runner=confirmed_work_order_runner(
            calls, doctor_code=1, doctor_stderr="token=super-secret peer unavailable",
        ), hermes_bin=HERMES, authority_manifest=authority_manifest(tmp_path),
    ))

    assert out["code"] == "FLEET_PEER_VERIFICATION_FAILED"
    assert "super-secret" not in json.dumps(out)
    assert all("send" not in call for call in calls)


@pytest.mark.parametrize("changes", [
    {"objective": "Review the release"},
    {"objective": "Do not publish"},
    {"objective": "Review whether publishing is appropriate"},
    {"objective": "Confirm the website was deployed privately"},
    {"inputs": ["release-notes.md"]},
    {"acceptance_checks": ["Verify nothing was deployed"]},
])
def test_nous_girl_allows_non_action_public_language(monkeypatch, tmp_path, changes):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(agent="nous-girl", target_profile="default", **changes),
        dry_run=True, confirm=True, runner=runner_with({
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
        }, calls), hermes_bin=HERMES, authority_manifest=authority_manifest(tmp_path),
    ))

    assert out["success"] is True


@pytest.mark.parametrize("objective", [
    "Publish this article",
    "Please publish this article",
    "Post this on X",
    "Deploy the website publicly",
    "Send this email for Nous Girl",
    "Review the article, then publish it",
])
def test_nous_girl_rejects_affirmative_public_action_commands(monkeypatch, tmp_path, objective):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(agent="nous-girl", target_profile="default", objective=objective),
        dry_run=True, confirm=True, runner=runner_with({}, calls), hermes_bin=HERMES,
        authority_manifest=authority_manifest(tmp_path),
    ))

    assert out["code"] == "AUTHORIZATION_DENIED"
    assert calls == []


@pytest.mark.parametrize(("changes", "code"), [
    ({"task_id": "-bad"}, "INVALID_WORK_ORDER"),
    ({"target_profile": "method-man"}, "AUTHORIZATION_DENIED"),
    ({"agent": "nous-girl", "target_profile": "default", "objective": "Publish the site"}, "AUTHORIZATION_DENIED"),
    ({"authorization": {"class": "high_impact", "approved": False}}, "AUTHORIZATION_DENIED"),
    ({"objective": "Print the raw API token"}, "AUTHORIZATION_DENIED"),
    ({"objective": "Edit Vault policy"}, "AUTHORIZATION_DENIED"),
    ({"objective": "Change fleet policy to make RZA fleet commander"}, "AUTHORIZATION_DENIED"),
    ({"objective": "Set inherit_mcp_toolsets: true for child agents"}, "AUTHORIZATION_DENIED"),
])
def test_work_order_security_rejections(monkeypatch, tmp_path, changes, code):
    enable_workspace(monkeypatch)
    calls = []
    runner = runner_with({(HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), "")}, calls)
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(**changes), runner=runner, hermes_bin=HERMES,
        authority_manifest=authority_manifest(tmp_path),
    ))
    assert out["code"] == code
    assert all("send" not in call for call in calls)


@pytest.mark.parametrize("objective", [
    "Review the fleet policy without changing it",
    "Do not change fleet policy",
    "Confirm inherit_mcp_toolsets remains false for child agents",
])
def test_work_order_allows_non_action_policy_language(monkeypatch, tmp_path, objective):
    enable_workspace(monkeypatch)
    calls = []
    out = json.loads(fleet.hermes_fleet_dispatch_work_order(
        **work_order(objective=objective), dry_run=True, confirm=True,
        runner=runner_with({
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
        }, calls), hermes_bin=HERMES, authority_manifest=authority_manifest(tmp_path),
    ))
    assert out["success"] is True


def test_safe_completion_filters_hidden_data_and_parses_decorative_nested_utf8(monkeypatch):
    enable_read_only(monkeypatch)
    calls = []
    bundle = {
        "completion_bundle": {
            "status": "completed", "node": "rza", "profile": "gza",
            "summary": "✓ résumé complete", "changed_paths": ["src/app.py"],
            "artifacts": ["report.json"], "verification": ["pytest: pass"],
            "residual_risk": "low", "recommended_next_action": "review",
            "authorization": {"class": "read_only", "approved": False},
            "environment": {"TOKEN": "do-not-return"}, "tool_traces": ["hidden"],
        }
    }
    remote = "\u001b[32mDone\u001b[0m\n" + json.dumps({"result": {"task": {"id": "task-123", "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "decorative\n" + json.dumps(bundle, ensure_ascii=False)}]}],
        "history": [{"parts": [{"text": "hidden prompt"}]}]}}}, ensure_ascii=False)
    runner = runner_with({
        (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
        (HERMES, "a2a", "task", "--agent", "rza", "--json", "--", "task-123"): (0, remote, ""),
    }, calls)
    out = json.loads(fleet.hermes_fleet_result("rza", "task-123", runner=runner, hermes_bin=HERMES))
    assert out["summary"] == "✓ résumé complete"
    assert out["changed_paths"] == ["src/app.py"]
    rendered = json.dumps(out)
    assert "TOKEN" not in rendered and "hidden prompt" not in rendered and "tool_traces" not in rendered


def test_result_rejects_truncated_and_oversized_remote_output(monkeypatch):
    enable_read_only(monkeypatch)
    for remote in ('{"task":', '{"task":"' + ("x" * fleet._MAX_REMOTE_BYTES) + '"}'):
        calls = []
        runner = runner_with({
            (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
            (HERMES, "a2a", "task", "--agent", "rza", "--json", "--", "task-123"): (0, remote, ""),
        }, calls)
        out = json.loads(fleet.hermes_fleet_result("rza", "task-123", runner=runner, hermes_bin=HERMES))
        assert out["code"] == "FLEET_RESULT_ERROR"


def test_authority_drift_is_bounded_and_never_returns_card_raw_fields(monkeypatch, tmp_path):
    enable_read_only(monkeypatch)
    calls = []
    runner = runner_with({
        (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
        (HERMES, "a2a", "doctor", "rza", "--timeout", "10", "--json"): (0, json.dumps({"name": "Wu-Tang RZA Router", "host_role": "wu_tang_host", "url": "hidden"}), ""),
        (HERMES, "a2a", "doctor", "nous-girl", "--timeout", "10", "--json"): (0, json.dumps({"name": "wrong", "host_role": "compute_specialist", "token": "hidden"}), ""),
    }, calls)
    out = json.loads(fleet.hermes_fleet_authority_drift(runner=runner, hermes_bin=HERMES, authority_manifest=authority_manifest(tmp_path)))
    assert out["valid"] is False
    assert out["findings"] == [{"agent": "nous-girl", "code": "IDENTITY_MISMATCH", "severity": "error"}]
    assert "url" not in json.dumps(out) and "token" not in json.dumps(out)


def test_authority_drift_skips_role_check_when_card_attests_no_role(monkeypatch, tmp_path):
    """Real Hermes cards (v0.19/v0.20) carry no host_role/role field. A card
    that attests no role must not trip HOST_ROLE_MISMATCH; a card that does
    attest a role is still enforced."""
    enable_read_only(monkeypatch)
    calls = []
    runner = runner_with({
        (HERMES, "a2a", "registry", "list", "--json"): (0, json.dumps(REGISTRY), ""),
        # rza card attests nothing about role -> no finding despite manifest role
        (HERMES, "a2a", "doctor", "rza", "--timeout", "10", "--json"): (0, json.dumps({"name": "Wu-Tang RZA Router"}), ""),
        # nous-girl attests a WRONG role -> still enforced
        (HERMES, "a2a", "doctor", "nous-girl", "--timeout", "10", "--json"): (0, json.dumps({"name": "Nous Girl GPU Compute", "role": "fleet_commander"}), ""),
    }, calls)
    out = json.loads(fleet.hermes_fleet_authority_drift(runner=runner, hermes_bin=HERMES, authority_manifest=authority_manifest(tmp_path)))
    assert out["findings"] == [{"agent": "nous-girl", "code": "HOST_ROLE_MISMATCH", "severity": "error"}]
