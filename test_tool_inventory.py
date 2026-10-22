"""Pin the default connector tool inventory.

Safety net for the tool-profile work: the unset-profile surface must match this
committed snapshot exactly, with no duplicate registrations. Any tool addition
or removal has to update this list in the same change.
"""

import asyncio
from pathlib import Path

import pytest

import server
import tool_profiles
from test_server import clear_gate_envs


# Committed snapshot of the default (unset HERMES_GPT_TOOL_PROFILE) connector surface.
# Generated from build_server() with all gates cleared and finance enabled, the
# same environment as test_v09_connector_surface_acceptance. Update deliberately
# when a tool is added or removed; a profile change must not edit this list.
DEFAULT_TOOL_INVENTORY = [
    "hermes_budget_check",
    "hermes_budget_get",
    "hermes_budget_record",
    "hermes_budget_set",
    "hermes_capability_manifest",
    "hermes_codex_cancel",
    "hermes_codex_job_result",
    "hermes_codex_job_status",
    "hermes_codex_jobs",
    "hermes_codex_plan",
    "hermes_codex_review_start",
    "hermes_codex_start",
    "hermes_codex_status",
    "hermes_config_get",
    "hermes_config_patch",
    "hermes_config_set",
    "hermes_contract_define",
    "hermes_contract_dispatch",
    "hermes_contract_status",
    "hermes_contract_validate",
    "hermes_controller_lease_list",
    "hermes_controller_plan_list",
    "hermes_controller_reconcile",
    "hermes_controller_status",
    "hermes_controller_trigger",
    "hermes_cron_copy",
    "hermes_cron_create",
    "hermes_cron_list",
    "hermes_cron_move",
    "hermes_cron_pause",
    "hermes_cron_run",
    "hermes_cron_status",
    "hermes_delegation_cancel",
    "hermes_delegation_dispatch",
    "hermes_delegation_get",
    "hermes_delegation_list",
    "hermes_delegation_reconcile",
    "hermes_env_copy_nonsecret",
    "hermes_env_set_nonsecret",
    "hermes_env_status",
    "hermes_events_query",
    "hermes_events_tail",
    "hermes_export_file",
    "hermes_failure_classify",
    "hermes_failure_taxonomy",
    "hermes_finance_analyze",
    "hermes_fleet_authority_drift",
    "hermes_fleet_dispatch",
    "hermes_fleet_dispatch_work_order",
    "hermes_fleet_list",
    "hermes_fleet_result",
    "hermes_fleet_status",
    "hermes_fleet_task",
    "hermes_gateway_restart",
    "hermes_gateway_status",
    "hermes_git_diff",
    "hermes_git_status",
    "hermes_job_status",
    "hermes_job_wait",
    "hermes_live_events_cursor",
    "hermes_live_events_since",
    "hermes_memory",
    "hermes_mission_approvals",
    "hermes_mission_approve",
    "hermes_mission_attach",
    "hermes_mission_audit",
    "hermes_mission_codex",
    "hermes_mission_create",
    "hermes_mission_cron",
    "hermes_mission_delegations",
    "hermes_mission_failures",
    "hermes_mission_fleet",
    "hermes_mission_get",
    "hermes_mission_health",
    "hermes_mission_ledger",
    "hermes_mission_ledger_replay",
    "hermes_mission_list",
    "hermes_mission_overview",
    "hermes_mission_profiles",
    "hermes_mission_reconcile",
    "hermes_mission_transition",
    "hermes_mission_update",
    "hermes_mission_usage",
    "hermes_mission_vault",
    "hermes_oauth_revoke",
    "hermes_oauth_status",
    "hermes_operator_audit_tail",
    "hermes_operator_doctor",
    "hermes_operator_policy",
    "hermes_operator_recover",
    "hermes_operator_snapshot",
    "hermes_operator_status",
    "hermes_owner_inbox",
    "hermes_owner_inbox_ack",
    "hermes_owner_patch",
    "hermes_owner_run_command",
    "hermes_owner_write_file",
    "hermes_placement_candidates",
    "hermes_placement_get",
    "hermes_placement_list",
    "hermes_placement_score",
    "hermes_plan_approve_gate",
    "hermes_plan_create",
    "hermes_plan_decompose",
    "hermes_plan_get",
    "hermes_plan_list",
    "hermes_plan_node_transition",
    "hermes_plan_review",
    "hermes_plan_set_status",
    "hermes_plan_validate",
    "hermes_read_file",
    "hermes_recovery_matrix",
    "hermes_release_doctor",
    "hermes_review_accept",
    "hermes_runner_cancel",
    "hermes_runner_list",
    "hermes_runner_status",
    "hermes_search_files",
    "hermes_skill_copy",
    "hermes_skill_create",
    "hermes_skill_delete",
    "hermes_skill_diff",
    "hermes_skill_edit",
    "hermes_skill_list",
    "hermes_skill_patch",
    "hermes_skill_sync_to_default",
    "hermes_skill_view",
    "hermes_skill_write_file",
    "hermes_swarm_approve",
    "hermes_swarm_reconcile",
    "hermes_swarm_stage_advance",
    "hermes_swarm_stage_dispatch",
    "hermes_swarm_workflow_create",
    "hermes_swarm_workflow_list",
    "hermes_swarm_workflow_status",
    "hermes_swarm_workflow_validate",
    "hermes_workspace_patch",
    "hermes_workspace_read",
    "hermes_workspace_run_test",
    "hermes_workspace_write_file",
]


def _default_surface_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    clear_gate_envs(monkeypatch)
    monkeypatch.delenv(tool_profiles.TOOL_PROFILE_ENV, raising=False)
    monkeypatch.setenv(server.op_finance.ENABLE_FINANCE_ENV, "1")
    monkeypatch.setenv("HERMES_HOME", str(Path(server.__file__).resolve().parent))
    return [tool.name for tool in asyncio.run(server.build_server().list_tools())]


def test_default_tool_inventory_matches_committed_snapshot(monkeypatch):
    names = _default_surface_names(monkeypatch)

    assert len(names) == len(set(names)), "duplicate tool registration"
    assert sorted(names) == DEFAULT_TOOL_INVENTORY
    assert DEFAULT_TOOL_INVENTORY == sorted(DEFAULT_TOOL_INVENTORY)
    assert len(DEFAULT_TOOL_INVENTORY) == len(set(DEFAULT_TOOL_INVENTORY))
