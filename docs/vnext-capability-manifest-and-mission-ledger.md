# Derived Capability Manifest + Mission Ledger (vNext slice-1, Phase 1)

Two **read-only derived views** added in the Hermes GPT sidecar (proposal §17
item 3, §2.2 rows 2+4, §6.2). Both are *read models*: they are computed by
reading the existing authoritative registries/stores and merging them into a
single query surface. Neither introduces a new source of truth, a new durable
table, or a mutation path. INV-9 data containment holds throughout — refs,
bounded metadata, and content-addressed hashes only; no raw prompt, transcript,
memory body, credential, or secret-path content ever crosses the surface.

## `hermes_capability_manifest` — derived capability manifest

Answers "what can X do" by folding the authoritative registries into one
normalized capability registry:

- Fabric node registry (`operator_fabric.load_node_registry`)
- Fleet authority manifest (`<root>/config/fleet-authority.json`)
- Profile toolsets & skills (per-profile `skills/` dir + config)
- Provider dimension (derived from each profile's configured model/provider)

Each entity carries `capability_sha256` (content address) and `derived_from`
(source refs) for provenance. The `placement_cache` object is a **TTL-bounded
derived snapshot** that is rebuilt from the registries when it expires; it is
in-process only, never a durable store.

Read-only by construction: all sources open read-only (`mode=ro` / `r`);
no write path exists.

Args: `source` (fabric|fleet|profile|provider, empty = all), `include_cache`,
`limit`. Allowlist env: `HERMES_GPT_CAPABILITY_ALLOWED_SOURCES`.

## `hermes_mission_ledger` / `hermes_mission_ledger_replay` — per-mission ledger

An append-only, replayable **merged cursor stream** per mission, reconciled at
query time from the existing authoritative stores (it does not replace them):

- `mission_events` — `<root>/missions/missions.db`
- `delegation_events` — `<root>/delegations/delegations.db` (linked via the
  mission's delegations; tasks carried for the kanban join)
- operator audit — `<root>/logs/hermes_gpt_operator_audit.jsonl`
- kanban `task_events` — `<root>/kanban/boards/<slug>/kanban.db` (for the
  tasks owned by this mission's delegations)

Every event gets a monotonic merged `cursor`; the stream is deterministic
given the same store state, so `hermes_mission_ledger_replay` reproduces the
mission's event history.

Args: `mission_id`, `source` (mission|delegation|audit|kanban, empty = all),
`cursor` (resume after this append-only cursor), `limit`, `replay`. Allowlist
env: `HERMES_GPT_LEDGER_ALLOWED_SOURCES`.

Read-only by construction: all SQLite sources open `mode=ro`; no mutation path
exists (verified by tests that assert row counts and file sets are unchanged).
