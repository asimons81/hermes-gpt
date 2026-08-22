# Hermes GPT documentation map

This file tells humans and agents which documents are current, which are historical, and where to verify behavior before making changes.

## Source-of-truth order

When documents disagree, use this precedence:

1. **Runtime code and tests** for the checked-out version.
2. **Current operational docs** listed below.
3. **Current release notes and CHANGELOG** for version history and known limitations.
4. **Design, risk, counsel, and release-planning artifacts** for historical intent and provenance only.
5. **FEASIBILITY.md** for the original machine-specific feasibility probe only.

A design document describes intended architecture. It does not override implemented code or tests.

## Current version context

Repository version: **0.8.0**. The GitHub release target is `v0.8.0`; verify the public GitHub Releases and PyPI channels independently.

PyPI is an independent distribution channel. Check the PyPI badge in the root README before assuming `pip install hermes-gpt` contains a particular feature set; v0.8 Fabric behavior requires a published PyPI version of 0.8.0 or newer.

## Current operational docs

| Document | Authority | Use it for |
| --- | --- | --- |
| [`../README.md`](../README.md) | current | project overview, current release, quickstart, safety invariants, entry-point selection |
| [`oauth.md`](oauth.md) | current | static bearer and confidential-client OAuth configuration, token lifecycle, refresh rotation, and remote authentication limits |
| [`mcp-compatibility.md`](mcp-compatibility.md) | current | pinned MCP protocol revisions, transport matrix, trusted-client auth metadata |
| [`file-export.md`](file-export.md) | current | bounded binary file transfer, workspace/denied-path gates, size/extension limits, MCP embedded-resource semantics |
| [`openai-secure-mcp-tunnel.md`](openai-secure-mcp-tunnel.md) | current | outbound-only private access from supported OpenAI products to loopback Hermes GPT |
| [`cloudflare-tunnel.md`](cloudflare-tunnel.md) | current | public Cloudflare HTTPS proxy deployment and Host allowlist behavior |
| [`operator-mode.md`](operator-mode.md) | current | Operator / Owner policy, Mission Control, fleet routing, Work Contracts, Swarm Orchestration, v0.8 Fabric execution, and Flight Deck surfaces |
| [`missions.md`](missions.md) | current | v0.9 first-class Mission lifecycle, bounded context/skills manifests, attachments, reconciliation, and Owner approval |
| [`live-events.md`](live-events.md) | current | v0.9 durable event cursor/long-poll and authenticated WebSocket wake-up stream |
| [`delegations.md`](delegations.md) | current | v0.9 normalized delegation lifecycle across Pi/OpenCode/Codex/Fabric with durable lineage and reconciliation |
| [`codex.md`](codex.md) | current | Codex-as-MCP-client setup and delegated Codex CLI jobs |
| [`windows-chatgpt-codex.md`](windows-chatgpt-codex.md) | current | Windows ChatGPT -> Hermes GPT -> Codex CLI deployment |
| [`updating.md`](updating.md) | current | check-first Git and PyPI update behavior |
| [`retention-policy.md`](retention-policy.md) | current | local diagnostic artifact retention and cleanup |
| [`session-history.md`](session-history.md) | current | gated read-only session history surface and privacy defaults |
| [`session-control.md`](session-control.md) | current | gated asynchronous session continue/send jobs |
| [`ui-security-boundary.md`](ui-security-boundary.md) | current | conversational UI browser security boundary and opt-in UI mount |
| [`flight-deck-coverage.md`](flight-deck-coverage.md) | current | Flight Deck browser coverage and mutation safety decisions |
| [`release-notes-v0.8.0.md`](release-notes-v0.8.0.md) | current release record | shipped v0.8 Fabric behavior, G6 acceptance boundary, and known limitation |
| [`release-notes-v0.7.0.md`](release-notes-v0.7.0.md) | historical release record | shipped v0.7 behavior and gates |
| [`release-notes-v0.6.0.md`](release-notes-v0.6.0.md) | current release record | shipped v0.6 behavior and known limitations |
| [`../RELEASE_CHECKLIST.md`](../RELEASE_CHECKLIST.md) | maintainer | release verification and publication gates |
| [`../CHANGELOG.md`](../CHANGELOG.md) | historical/current | concise version history |

## Tool-name namespaces

Do not infer tool names from a different MCP surface.

The main Hermes GPT server and the curated Codex MCP server overlap but are not identical.

Important example:

- main server page extraction: `hermes_web_extract`
- Codex-focused MCP extraction: `hermes_extract_page`

Before generating a tool call, verify the active server/toolset and exact registered name.

## Current safety invariants

These should remain true across documentation and implementation:

- local loopback is the default network boundary;
- public unauthenticated Operator hosting is unsupported;
- default behavior is read-only;
- mutating Operator tools are opt-in and dry-run-first;
- direct mutation requires server direct mode plus the individual call's mutation/confirmation gates;
- Owner Mode is break-glass and does not bypass secret-path restrictions;
- protected execution paths use fixed argv and `shell=False`;
- raw prompts are not persisted in Operator audit records;
- Mission Control excludes raw messages, memory bodies, transcripts, request dumps, credentials, and profile-secret bodies;
- Work Contract validation is observed-state and fail-closed;
- Swarm final approval is a human Owner-level gate;
- Codex is never a Swarm implementation owner.

## Mission Control allowlist semantics

`HERMES_GPT_MISSION_ALLOWED_SURFACES` is restrictive when configured:

- unset: all read-only Mission Control surfaces are available;
- comma-separated list: only listed valid surfaces are available;
- empty value: no Mission Control surfaces are available.

Do not describe the unset state as "deny by default". The implementation defaults to all read-only Mission Control surfaces when the variable is absent.

## Historical release notes

These are version records, not current setup instructions:

- [`release-notes-v0.5.0.md`](release-notes-v0.5.0.md)
- [`release-notes-v0.5.0b2.md`](release-notes-v0.5.0b2.md)
- [`release-notes-v0.5.0b1.md`](release-notes-v0.5.0b1.md)
- [`release-notes-v0.4.0.md`](release-notes-v0.4.0.md)
- [`release-notes-v0.3.0.md`](release-notes-v0.3.0.md)
- [`release-notes-v0.2.0.md`](release-notes-v0.2.0.md)

## Historical and internal artifacts

The following directories contain valuable provenance, but they are not operational source-of-truth documents:

- `design/` - technical design documents written before or during implementation. v0.6 designs are historical; v0.7 Flight Deck architecture and ADRs are current design artifacts for the v0.7 release cycle.
- `releases/` - release brief, integrated plan, risk reviews, counsel packet, and surface manifest created during pre-release work. v0.6 artifacts are historical; v0.7 Flight Deck research package, risk register, and implementation plan are current release-program artifacts.

Some of these files intentionally preserve phrases such as "candidate", "gate", or "before release" because they record the state at the time they were written. Agents must not treat those historical status statements as the current release state.

When using an internal artifact:

1. read it for rationale or constraints;
2. verify every implementation claim against current code/tests;
3. verify every release-status claim against the current GitHub release and current operational docs;
4. do not copy machine-specific paths, identities, metrics, or approval history into new public docs unless required and still accurate.

## Agent reading protocol

For repository work, use this sequence:

1. Read `AGENTS.md`.
2. Read this file.
3. Read `pyproject.toml` to establish the checked-out version and package surface.
4. Read the relevant current operational doc.
5. Inspect the implementation module and its tests before changing behavioral documentation.
6. If working from a design/release artifact, treat it as a hypothesis until code/tests confirm it.
7. After a behavior change, update the smallest canonical doc set that prevents drift.

## Documentation maintenance rules

- Put current instructions before historical context.
- Prefer exact environment variable and tool names over prose aliases.
- State defaults explicitly.
- Distinguish read-only, dry-run, direct, and Owner authority.
- Separate "Codex as MCP client" from "Codex CLI as delegated worker".
- Keep examples minimal and copyable.
- Link to one canonical explanation instead of duplicating large sections across files.
- Mark time-sensitive distribution state clearly. Do not imply PyPI and GitHub releases are automatically synchronized.
- Never document secrets, live credentials, bearer tokens, private keys, or raw prompt/transcript content.
