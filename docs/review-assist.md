# Advisory review triage (Jev-backed)

Status: unreleased source after v0.14.0; default off. The `hermes_review_assess` function exists in `operator_review_assist.py`, but this source revision does not register it as an MCP tool. Registration is a separate change. Verify the active tool inventory before relying on it.

## Current behavior

`hermes_review_assess(contract_sha256, artifact=None, hermes_root=None)` asks the pinned Jev adapter for an advisory score of how well a dispatched Work Contract's state fits its completion criteria. It is an opinion for a human reviewer. It is never review acceptance and never Work Contract evidence.

### Gates

Both gates must be on. Otherwise the tool returns `{"success": false, "code": "DISABLED", ...}` without reading the contract, opening a file or contacting Jev.

| Gate | Value | Owner |
|---|---|---|
| `HERMES_GPT_ENABLE_JEV` | exactly `1` | shared Jev bridge gate; see [`jev.md`](jev.md) |
| `HERMES_GPT_REVIEW_ASSIST` | exactly `1` | review-assist gate; any other value is off |

No Operator level, apply mode, owner acknowledgement or confirmation is required or bypassed. The tool does not change Operator authority.

### Lookup

- `contract_sha256` must be 64 lowercase hex characters, otherwise `INVALID_INPUT`.
- The contract is read by digest from the delegation store (`<hermes root>/delegations/delegations.db`), using its durable validation manifest. The manifest is the prompt-free contract subset. A contract that was never dispatched through a delegation has no such manifest and returns `NOT_FOUND`.
- The store does not keep the objective, inputs, constraints or review evidence text. Instructions therefore come from the durable completion criteria, not the original objective.

### What is sent to TypeSafe

Each call sends the text below to TypeSafe through the Jev adapter. Calls may incur API charges. Do not use this tool on credentials or sensitive operational records.

- `instructions` (at most 4,000 characters): the run outcomes that count as success, and whether artifacts, tests, review and the no-forbidden-actions rule are required.
- `state` (at most 16,000 characters): the names of the contract's expected artifacts. With `artifact`, it also includes the text of that one artifact, limited to its first 12,000 bytes.
- `criteria`: five fixed ordered levels, from "Does not meet the acceptance criteria" to "Fully meets the acceptance criteria".

### Artifact admission

When `artifact` is given it must be the basename of exactly one expected artifact of the contract. Otherwise the call is refused.

| Condition | Result |
|---|---|
| Not an expected artifact, a path with separators, or a secret-like name | `DENIED` |
| A symlink in the path, or a path that resolves outside the contract workspace | `DENIED` |
| Expected but not present as a file | `NOT_FOUND` |
| Not a regular file, not valid UTF-8, or contains a NUL byte | `UNSUPPORTED_ARTIFACT` |
| Content matches the Operator secret-looking-content patterns | `DENIED` |
| Larger than 12,000 bytes | sent truncated to the first 12,000 bytes |

Artifact location reuses the contract validator's logic: `operator_contract._resolve_artifact_paths` (`operator_contract.py:283`) against the contract's `allowed_scope.workspaces`. Autopilot-issued contracts use `<hermes root>/missions/artifacts/<task_id>` as that workspace (`operator_autopilot.py:966`).

### Result

On success the tool returns:

```json
{"success": true, "advisory": true, "evidence": false, "score": {...}, "notice": "Advisory only. Not review acceptance and not Work Contract evidence."}
```

`score` contains the Jev score, and confidence and probabilities when Jev returns them. On a Jev error it returns `{"success": false, "code": <code>}` with the bridge code, such as `TIMEOUT`, `RATE_LIMITED` or `UNAVAILABLE`, and writes nothing.

### Storage

Each success appends exactly one JSON line to `<hermes root>/missions/review_advisories.jsonl`:

```json
{"contract_sha256": "...", "artifact": "report.md", "score": {...}, "model": "...", "assessed_at": "...", "advisory": true, "evidence": false}
```

- `artifact` is `null` when no artifact was requested.
- No objective, instructions, state or artifact text is stored.
- The file is append-only and each line is bounded to 8 KiB.
- `read_advisories(hermes_root, contract_sha256=None, limit=50)` returns the newest matching records, bounded to the last 1 MiB of the file.

### Advisory only

Advisories are never evidence. The following modules must not read `review_advisories.jsonl` or reference `operator_review_assist`. Tests enforce this:

- `operator_contract.py` (Work Contract validation);
- `operator_review.py` (review acceptance);
- `operator_delegations.py` (delegation reconcile);
- `operator_autopilot.py` (Autopilot).

A validation verdict is identical before and after an advisory is written. An advisory cannot satisfy a contract, approve work, relax permissions or change a delegation state.

The operator audit log does not record advisory calls. Audit rows that share a contract's `task_id` feed the validator's forbidden-action scan and its `audit_count`, so an advisory audit row would change validation results.

## Offline verification

`python -m pytest test_operator_review_assist.py` covers the gates, lookup, artifact admission, payload bounds, the store format, failure paths, and the evidence-isolation invariant. It uses no Jev network calls and no Node process.

## Historical context

The v0.15 design deliberately keeps Jev out of the validator. Work Contract completion remains defined by observed run, artifact, test and review state. Jev advisories help a human reviewer prioritize, and they are not part of that definition.
