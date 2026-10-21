"""Connector tool profiles (``HERMES_GPT_TOOL_PROFILE``).

A profile can only REMOVE tools from the surface that ``register_tools`` would
otherwise register. It never adds a tool, never bypasses an env gate, and never
changes a tool's metadata or authority. Unset/empty selects ``full``, which is
the unfiltered registration (byte-identical to the pre-profile surface).

Tool names here must match the names registered in ``server.register_tools``.
Gated tools (for example ``HERMES_GPT_AUTOPILOT=1``) stay absent unless their
own gate is on, whatever the profile says.
"""

from __future__ import annotations

import os
from typing import Any

TOOL_PROFILE_ENV = "HERMES_GPT_TOOL_PROFILE"

FULL_PROFILE = "full"
OPERATOR_PROFILE = "operator"
CHAT_LEAN_PROFILE = "chat-lean"
DEFAULT_PROFILE = FULL_PROFILE
PROFILE_NAMES = (FULL_PROFILE, OPERATOR_PROFILE, CHAT_LEAN_PROFILE)

# ``operator`` = ``full`` minus raw host primitives meant for local development.
OPERATOR_EXCLUDED_TOOLS = frozenset(
    {
        "hermes_read_file",
        "hermes_write_file",
        "hermes_patch",
        "hermes_search_files",
        "hermes_run_command",
        "hermes_memory",
    }
)

# ``chat-lean`` = read-only status/oversight plus owner decisions for chat
# clients with small tool budgets. Names that are gated off stay unregistered.
CHAT_LEAN_TOOLS = frozenset(
    {
        # Operator status
        "hermes_operator_policy",
        "hermes_operator_status",
        "hermes_operator_doctor",
        # Mission Control and first-class missions
        "hermes_mission_overview",
        "hermes_mission_health",
        "hermes_mission_approvals",
        "hermes_mission_create",
        "hermes_mission_get",
        "hermes_mission_list",
        "hermes_mission_approve",
        "hermes_mission_reconcile",
        "hermes_mission_ledger",
        # Plans, delegations, budgets, contracts, reviews
        "hermes_plan_get",
        "hermes_plan_list",
        "hermes_delegation_get",
        "hermes_delegation_list",
        "hermes_budget_get",
        "hermes_budget_check",
        "hermes_contract_status",
        "hermes_contract_validate",
        "hermes_review_accept",
        # Autopilot (gated by HERMES_GPT_AUTOPILOT=1)
        "hermes_autopilot_start",
        "hermes_autopilot_status",
        "hermes_autopilot_stop",
        # Events and live events
        "hermes_events_query",
        "hermes_events_tail",
        "hermes_live_events_cursor",
        "hermes_live_events_since",
        # Sessions and bot chat (gated by session env flags)
        "hermes_session_list",
        "hermes_session_read",
        "hermes_bot_chat_get",
        "hermes_bot_chat_send",
        # Skills and background jobs
        "hermes_skill_list",
        "hermes_skill_view",
        "hermes_job_status",
        # External typed judgments (gated by HERMES_GPT_ENABLE_JEV=1)
        "hermes_jev_yes_no",
        "hermes_jev_choose",
        "hermes_jev_score",
        # Named for a change that adds them; not registered today, harmless to list.
        "hermes_owner_inbox",
        "hermes_review_assess",
    }
)

_ALLOWLISTS: dict[str, frozenset[str] | None] = {
    # None means "every registered tool" (no filtering).
    FULL_PROFILE: None,
    OPERATOR_PROFILE: None,
    CHAT_LEAN_PROFILE: CHAT_LEAN_TOOLS,
}


def resolve_profile(value: str | None) -> str:
    """Return the canonical profile name for ``value``; fail closed otherwise."""
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_PROFILE
    if raw in PROFILE_NAMES:
        return raw
    raise ValueError(
        f"{TOOL_PROFILE_ENV} must be one of {', '.join(PROFILE_NAMES)} "
        f"(got {raw!r}). Unset it to use the default '{DEFAULT_PROFILE}' profile."
    )


def profile_from_env() -> str:
    return resolve_profile(os.environ.get(TOOL_PROFILE_ENV))


def profile_allows(profile: str, name: str) -> bool:
    """True when ``profile`` permits registering a tool called ``name``."""
    if profile == OPERATOR_PROFILE:
        return name not in OPERATOR_EXCLUDED_TOOLS
    allowlist = _ALLOWLISTS[profile]
    return allowlist is None or name in allowlist


class ProfileFilteredServer:
    """Forwards ``add_tool`` calls, skipping tools the profile does not allow.

    ``register_tools`` only calls ``add_tool``, so this wrapper is the whole
    filter. Gates stay in ``register_tools``; this class can only skip.
    """

    def __init__(self, server: Any, profile: str) -> None:
        self._server = server
        self.profile = profile
        self.skipped: list[str] = []

    def add_tool(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        name = kwargs.get("name") or fn.__name__
        if not profile_allows(self.profile, name):
            self.skipped.append(name)
            return None
        return self._server.add_tool(fn, *args, **kwargs)
