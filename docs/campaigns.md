# Campaign orchestration (master-only)

Status: **current contract for the repository's campaign orchestration series, under active integration.** This guide describes code that exists on repository master and is exercised by its tests, but is not yet part of a published package (see [Boundaries and gates](#boundaries-and-gates)). Later AUTO-NN increments may extend this surface; when this file and the code disagree, the implementation and its tests win.

Source-of-truth order for this subsystem, as everywhere in this repository: implementation and tests first, then this operational guide. Design authority for reuse decisions is [`BOUNDARY.md`](../BOUNDARY.md) (reuse-first, thin additive surfaces). The internal campaign-plan document that named the AUTO-01..04 increments is not part of the tree and is deliberately not linked here.

The series is four library modules. None of them is an MCP tool, a server wiring change, or an authority of its own:

| Increment | Module | Role |
| --- | --- | --- |
| AUTO-01 | [`operator_campaign_compiler.py`](../operator_campaign_compiler.py) | campaign → MissionPlan compiler |
| AUTO-02 | [`operator_campaign_controller.py`](../operator_campaign_controller.py) | persistent campaign controller |
| AUTO-03 | [`operator_campaign_lane.py`](../operator_campaign_lane.py) | durable adoption lane |
| AUTO-04 | [`operator_work_class.py`](../operator_work_class.py) | owner-approval work-class policy |

## AUTO-01 — campaign → MissionPlan compiler

`compile_campaign(campaign_json) -> dict` deterministically compiles one `hermes.campaign/v1` campaign document (tracks of tasks with typed evidence gates) into a canonical `hermes.mission-plan/v1` plan document that the existing `hermes_plan_create` accepts verbatim after canonicalization. There is no LLM, no new store, and no Mission-lifecycle or dispatch access in this module: the output is a plan document only.

- Typed evidence gates: every campaign task declares its required evidence classes from the closed tuple `EVIDENCE_TYPES` (`implementation`, `ci`, `merge`, `deployment`, `runtime`, `sustained`); the compiler maps them onto the plan node's `capability_req` and inserts one validation node per track, so completion can only be claimed with the declared typed evidence.
- Validation is fail-closed: malformed specs raise `CampaignSpecError` (a `ValueError`); ids are regex-checked, budgets are bounded (`est_minutes` 0–10,000 and `est_tokens` 0–10,000,000), artifacts must be unique path-free basenames, and cross-task `depends_on` must resolve inside the campaign.
- Bounds: at most 8 tracks, 24 tasks per track, and 48 tasks total; work beyond the cap fails closed instead of truncating.
- INV-9 data containment: task titles and objectives are hashed, never stored raw.

## AUTO-02 — persistent campaign controller

Real campaigns are bigger than one Mission and must survive restarts, so `operator_campaign_controller.py` drives one campaign — declared as a `hermes.campaign-manifest/v1` manifest of ordered segments, each segment carrying its own `hermes.campaign/v1` document — as a sequence of **finite Missions**, not one immortal Mission.

- Manifest validation is fail-closed (`CampaignManifestError`): schema is checked, each manifest is sha256-pinned into state, segment dependencies must be acyclic, and the size/segment caps apply (256 KiB, 24 segments).
- The controller's only writable surface is the durable state document `<data_root>/campaigns/<campaign_id>/state.json` — orchestration metadata only. Mission, plan, node, and delegation truth stays in the existing stores; this module is a caller, not a competing owner. Deleting `state.json` is safe: the next tick rebuilds it from truth.
- `campaign_controller_tick` is an idempotent reconcile pass: every segment's phase is re-derived from the authoritative stores (mission exists? plan exists? autopilot run alive? mission terminal?) before anything is written — read-then-adopt, so a restart never duplicates work — and at most one phase transition happens per segment per tick (`pending -> minted -> active -> terminal`).
- Gates stay gates: a Mission at `awaiting_approval` parks the segment (`parked_owner_gate`) while independent segments proceed, and approval is never forged or bypassed. With the Autopilot machine gate `HERMES_GPT_AUTOPILOT` unset (default OFF) a segment parks at `parked_autopilot_gate`; arming the gate is an operator act, never the controller's.
- All mutations run through the standard Operator policy gate with dry-run-first semantics: `apply=False` plans the tick — every phase decision computed, nothing written. `campaign_controller_status` is the read-only view.

## AUTO-03 — durable adoption lane

`operator_campaign_lane.py` owns exactly one durable artifact — the ordered manifest registry `<data_root>/campaigns/lane.json` — plus its own lane state document (`campaigns/lane-state.json`). Everything else is delegated to the controller.

- `register_campaign` validates a manifest through exactly one dry-run controller tick and fails closed: a manifest that does not validate is never registered. The registry is idempotent by manifest path and `campaign_id`, refuses the same `campaign_id` from a different path (one campaign, one manifest, ever), and is bounded at 32 campaigns. `unregister_campaign` is containment only: controller state and the campaign's missions/plans stay untouched.
- `campaign_lane_run` reads the registry first (restart ⇒ read-then-adopt; the registry is the only lane-owned truth), then makes one bounded pass ticking every registered campaign through the existing `campaign_controller_tick` and persisting per-campaign event cursors and `last_tick`. A campaign whose manifest is missing or fails validation fails that campaign only; the others are still ticked.
- Wakeups are event-cursor based: in `serve` mode the lane waits on the live-event bus across all registered campaigns' mission ids, and on an event every campaign's cursor jumps to the store's safe high-water mark instead of walking the backlog. An event is a notification, never proof — the next pass re-reads durable state exactly as it would after a timer wakeup — and a missing, delayed, or erroring event store degrades to the timer with no stall and no busy spin. The idle poll is `HERMES_GPT_CAMPAIGN_LANE_IDLE_SECONDS`, clamped 0.5–300 seconds.
- Bounds: `max_ticks`, `max_runtime`, and `mode="once"` for systemd timer cadence — the lane timer is the single trigger per family. CLI: `python -m operator_campaign_lane register|unregister|run|status`, with `--hermes-root` (default `HERMES_HOME` / `~/.hermes`), `--apply`, and `--confirm` where mutation applies.

## AUTO-04 — owner-approval work-class policy

`operator_work_class.py` is a thin, fail-closed classifier over the native authorization axis `operator_mission_plan.AUTH_CLASSES` (`read_only`, `reversible_write`, `high_impact`). It is a caller of mission-runtime primitives only: no new store, and **the series adds no new authority-bearing write path** — mission approval still flows only through the existing owner-only `hermes_mission_approve`; the series' own writes are its durable campaign/lane state files and plan/mission mints through the existing gated primitives.

- `classify_plan_node` and `mission_approval_policy_for_nodes` classify plan nodes. **Invariant:** routine work classes — the closed tuple `ROUTINE_AUTH_CLASSES` (`read_only`, `reversible_write`) — auto-resolve; everything else (`high_impact`, approval-kind nodes, missing or unknown `authorization_class`, malformed nodes) is owner-gated and can never be preauthorized.
- `campaign_preauthorized` and `effective_approval_policy`: a Mission may resolve its final Owner gate without a human click only when BOTH hold — the campaign document explicitly declares `"preauthorized": true` (or the equivalent closed `work_class` object) AND every plan node classifies as routine. A declaration can never elevate: any approval-kind or high-impact node forces the `owner` policy regardless of the declaration.
- `effective_approval_policy` only ever sets the mint-time MissionSpec `approval_policy` field (`owner` or `auto_when_evidence_verified`; `max_wait_seconds` is bounded 0–86400 in the spec, and this table's default is 900 seconds). The Mission still parks at `awaiting_approval` and still completes only through `hermes_mission_approve`; `final_approval_required` stays `True` for every controller mint.
- `surface_owner_gate` is the read-side view of a mission's gate; `resolve_owner_gate` is the owner-side resolver: it re-verifies evidence through read primitives, probes Owner Mode without fabricating it (no owner env vars, no ack string — ever), and then delegates the single mutation to `hermes_mission_approve`. It grants no new authority inside Autopilot: the approval frontier and Autopilot still never approve anything.

## Boundaries and gates

- **No MCP tools.** None of the four modules registers a tool, and `server.py` does not import them.
- **Not in published packages.** The modules are absent from `pyproject.toml` `py-modules`, so they are not in the 0.14.0 wheel or sdist; `pip install hermes-gpt` at 0.14.0 does not contain them. Run from a repository checkout to use this series, and do not assume GitHub releases and PyPI are synchronized.
- **No new authority.** Every mutation reuses existing mission/plan/approval primitives through their existing gates (Operator policy, dry-run-first, Owner-only approval). The default surface for installed packages is unchanged.
- **Known flagged issue (cited, not fixed here):** the fleet peer-version fallback literal `'0.20.5'` (`server.py:3075`/`:3117`, `HERMES_GPT_FLEET_PEER_VERSION`) disagrees with package version 0.14.0; it is to be tracked under the pending roadmap ledger item rm-164 (the on-disk ROADMAP does not list it yet).

## Verification

From a repository checkout:

```bash
python -m pytest test_operator_campaign_compiler.py test_operator_campaign_controller.py test_operator_campaign_lane.py test_operator_work_class.py
```

## Historical note

The AUTO-01..04 numbering refers to the internal campaign plan that sequenced this work; it is not a public version scheme. The series landed on master as #43 (compiler), #44 (controller), #47 (adoption lane), and the auto-04 work-class commit (re-landed on `origin/master` as #48).
