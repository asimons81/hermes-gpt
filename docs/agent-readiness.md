# Agent readiness

Hermes GPT 0.18.0 adds a read-only Agent readiness page to Flight Deck. It separates the server's selected Agent source, the CLI selected for session control, source-level protocol declarations, and any previous authorized session-preflight observation.

Enable the existing browser UI and Operator read access, then open **Agent readiness**. `GET /api/ops/agent-readiness` requires the Mission Control `health` surface. An empty `HERMES_GPT_MISSION_ALLOWED_SURFACES` denies access; an explicit list must include `health`; an unset variable permits all read-only Mission Control surfaces. Direct or Owner mode is not required for this read.

## Interpret the observations

- **Server source:** identifies the source location selected by this server, with a revision when supported local Git metadata is available. A directory or revision is not proof that every Agent dependency imports, that the tree is clean, or that a gateway uses that code.
- **Selected CLI:** uses session control's executable selection: the selected Agent root's `venv` executable when present, then `hermes` on PATH. A PATH fallback can refer to another installation. Location containment is not proof of imported-code identity; unknown association stays unknown.
- **Source protocol declaration:** statically inspects the fixed `hermes_cli/_parser.py` file, bounded to 64 KiB, for a recognized chat-parser declaration of `--format` with the literal `stream-json` choice. It parses source without importing or executing it. The source digest identifies the inspected bytes. Missing, oversized or unfamiliar source produces an unavailable observation. A declaration is not proof that the selected CLI implements or advertises it at runtime.
- **Previous session preflight:** displays a sanitized, timestamped observation only if this server already performed the finite-protocol check during an authorized session-continuation request for the same executable identity and file metadata. Otherwise it reports `not_observed`. The bounded in-memory record contains no prompts, help output, stderr or credentials; a server restart clears it and a changed executable invalidates its match. It does not prove that a turn was submitted or completed.
- **Session control:** reports its existing environment gate. Observations never enable it or permit submission by themselves.
- **Live acceptance:** remains `not_verified`. Nothing on this page proves provider access, Desktop integration, successful Mission execution or recipient delivery.

Refresh reads local metadata and existing observations. It cannot install, update, configure, dispatch, send or resend, and it never launches Agent code or a subprocess. Identities are opaque hashes of locations, not private paths. No diagnostic result is Work Contract evidence or Owner approval. Do not submit work merely to obtain a positive diagnostic.

If the CLI is missing, inspect the server's intended Agent installation and PATH locally. If its location differs from the source, reconcile that configuration before relying on version-specific behavior. An unfamiliar source format requires review, not a claim of unsupported runtime behavior. Preserve original identifiers for uncertain existing work; a changed diagnostic is never a reason to retry a session.

## Why this page does not run CLI help

The current Agent entrypoint can perform installation and home maintenance before processing `--help`. Calling help from a read-only browser page would cross that boundary. Readiness therefore inspects source and previously observed metadata only.

The existing session-control execution path retains its CLI help preflight before an authorized continuation. That probe uses fixed argv, a minimal child environment, at most 10 seconds and 64 KiB of stdout, and closed failure codes. Missing executables, failures, timeouts and oversized output refuse submission. A successful response must advertise both `--format` and `stream-json`. This is still capability evidence only, not live session acceptance.

## Skill-loader compatibility

Skill validation prefers the Agent's public `find_all_skills` catalog API and falls back to legacy `_find_all_skills`. The catalog remains diagnostic provenance. The selected profile's explicit `skill_view(..., preprocess=False)` result controls admission, without executing inline-shell preprocessing. Missing or malformed loader interfaces fail closed.

CI exercises real loader code from exact modern and legacy Agent revisions. The modern lane isolates loader dependencies because that upstream revision's full runtime dependency pins conflict. Passing these tests proves the tested loader contract, not that an entire current Agent installation is compatible. Hermes GPT does not repair or upgrade the Agent automatically.

See [session control](session-control.md), [Daily Operations](daily-operations.md), [runtime provenance](runtime-checkout.md) and [Operator Mode](operator-mode.md) for related authority boundaries.
