# Hermes GPT v0.18.0 — Agent readiness and compatibility

Inspect the Agent source and CLI used by Hermes GPT before relying on a capability. Flight Deck's new read-only **Agent readiness** page separates source identity, CLI selection, static protocol declarations, prior authorized session-preflight observations, and the session-control gate. Unknown, failed and unsupported observations stay visible. It never launches Agent code or a subprocess, submits a turn, or changes configuration.

The existing session-control execution path retains a help probe, now bounded by time and output size. Readiness never invokes it: current Agent startup may perform maintenance even for help. Raw help, stderr and private paths are not exposed by readiness. See [Agent readiness](agent-readiness.md) for interpretation and limits.

Skill validation now supports the current public Agent `find_all_skills` API and the legacy `_find_all_skills` API. Previously, a current Agent without the private alias was rejected before explicit loading could be checked. Explicit loading remains authoritative, profile scoped and invoked with preprocessing disabled.

## Compatibility and limits

Default tools remain **139**, or **140** with Finance enabled. No MCP schema change, new mutation authority or default gate change. The new browser read requires the existing Operator and Mission Control `health` gates. Existing launch receipts, review approval and final Mission approval retain their separate contracts.

Validation covers both MCP SDK families, frontend tests/build, package hygiene, installed-wheel assets and MCP initialization, and CI's supported OS/Python matrix. Real skill-loader tests cover exact modern and legacy Agent revisions. The modern loader environment uses focused dependencies because the upstream full Agent dependency set has conflicting pins; this is not a full Agent installation certification.

Live Windows/Desktop session acceptance remains unverified. A supported help response is not an executed session, accepted deliverable, working provider, live gateway or successful notification. No fleet rollout, connector recreation or installed-service upgrade is included.

GitHub and PyPI are independent channels. Verify the version and artifact hashes on each before installation. Release artifacts come from the tag's validated CI distributions.
