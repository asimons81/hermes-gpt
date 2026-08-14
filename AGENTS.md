# AGENTS.md

These instructions apply to the entire `hermes-gpt` repository.

## Start here

Before changing code or documentation:

1. Read `docs/README.md` for the documentation authority map.
2. Read `pyproject.toml` for the checked-out package version and shipped modules/docs.
3. Read the current operational document for the subsystem you are touching.
4. Inspect the implementation module and its tests before changing behavioral claims.

Do not treat `docs/design/*`, `docs/releases/*`, or `FEASIBILITY.md` as current runtime instructions. They preserve design and release history.

## Source-of-truth precedence

When sources disagree:

1. current implementation and tests;
2. current operational docs;
3. current release notes / CHANGELOG;
4. historical design, risk, counsel, release-plan, and feasibility artifacts.

Never change runtime behavior solely to make it match a historical plan. First determine whether the implementation or the documentation is the intended current contract.

## Product invariants

Preserve these unless the task explicitly changes the security model and includes corresponding tests/docs:

- local loopback is the default network boundary;
- public unauthenticated Operator hosting is unsupported;
- default behavior is read-only;
- mutating Operator actions are opt-in and dry-run-first;
- direct mutation requires server direct mode plus per-call gates;
- Owner Mode is break-glass and does not bypass secret-path protections;
- `.env`, auth/token stores, SSH/AWS secrets, vault secrets, and secret-looking files remain denied;
- protected subprocess paths use fixed argv and `shell=False`;
- raw prompts are not persisted in Operator audit records;
- Mission Control excludes raw messages, memory bodies, transcripts, request dumps, credentials, and profile-secret bodies;
- Work Contract completion is validated from observed state and fails closed when evidence is missing;
- Swarm orchestration has bounded concurrency/rework and a final human approval gate;
- Codex may review Swarm work but is never an implementation owner.

## Mission Control allowlist

`HERMES_GPT_MISSION_ALLOWED_SURFACES` behaves as follows:

- unset: all read-only Mission Control surfaces are available;
- comma-separated list: only valid listed surfaces are available;
- empty: all Mission Control surfaces are denied.

Do not call the unset state "deny by default".

## Codex terminology

Keep these two relationships separate:

- **Codex as MCP client:** configured with `hermes-gpt codex install`; documented in `docs/codex.md`.
- **Codex CLI as delegated worker/reviewer:** invoked through `hermes_codex_*` from the normal Operator server.

Tool names can differ by surface. In particular:

- main server extraction: `hermes_web_extract`
- curated Codex MCP extraction: `hermes_extract_page`

Verify the active tool registration before writing docs, examples, or agent prompts.

## Documentation rules

For every documentation change:

- put current behavior before historical context;
- use exact tool/env names from code;
- state defaults and required gates explicitly;
- distinguish read-only, dry-run, direct, and Owner authority;
- prefer links to canonical docs over copying large blocks;
- mark historical artifacts as historical when they could be mistaken for current instructions;
- do not imply GitHub releases and PyPI are synchronized;
- do not expose machine-specific secrets, tokens, raw prompts, transcripts, or private operational data.

If behavior changes, review at minimum:

- `README.md`
- `docs/README.md`
- the subsystem guide (`docs/operator-mode.md`, `docs/codex.md`, etc.)
- `CHANGELOG.md` when user-visible
- release notes when preparing a release

## Verification

Run the smallest relevant tests first, then the full suite for cross-cutting changes.

Common commands:

```bash
python -m pytest test_operator_mission.py
python -m pytest test_operator_contract.py
python -m pytest test_operator_swarm.py
python -m pytest test_operator_codex.py
python -m pytest
```

For release/package work also run:

```bash
python -m build
python -m twine check dist/*
python tools/check_package_hygiene.py dist/*
```

Do not weaken or skip a failing safety test just to make a change pass.

## Release discipline

`RELEASE_CHECKLIST.md` is the maintainer checklist. A historical release plan does not authorize a new tag, PyPI upload, connector mutation, or public deployment.

Before claiming a version is available through a distribution channel, verify that channel directly.
