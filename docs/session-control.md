# Hermes session control

Hermes GPT can send one bounded non-interactive turn to an existing Hermes session and expose its status and result as an asynchronous MCP job. This lets an MCP client use the model/provider already configured in Hermes without invoking Codex.

## Enable locally

Session control is off and hidden by default. Enable it only on a trusted local MCP server:

```powershell
$env:HERMES_GPT_ENABLE_SESSION_CONTROL="1"
python server.py
```

Read-only history remains separately controlled by `HERMES_GPT_ENABLE_SESSION_SEARCH=1`. Enable both when the client needs to list or inspect sessions before choosing one to continue. See [session history](session-history.md) for its four-tool read-only workflow and privacy defaults.

## Workflow

1. Find a session ID with `hermes_session_list` when history is enabled.
2. Call `hermes_session_continue(session_id, prompt, timeout)` or its `hermes_session_send` alias.
3. Save the returned `job_id`.
4. Poll `hermes_session_job_status(job_id)` until the status is `completed`, `failed`, `timed_out`, or `orphaned`.
5. Call `hermes_session_job_result(job_id)` for the bounded, redacted final output.

The start call resolves exact or unique-prefix IDs through Hermes' existing read-only `SessionDB` API before launching anything. It invokes the CLI with a fixed argument array equivalent to:

```text
hermes chat --resume <resolved-session-id> -q <prompt> --format stream-json
```

No shell is used. Hermes restores the resumed session's recorded working directory using its normal CLI behavior.

## Bounds and persistence

- Prompt: maximum 65,536 characters.
- Timeout: clamped to 10–3600 seconds; default 900.
- Returned result: clamped to 500–24,000 characters.
- Concurrency: only one session-control job may run for a given session at a time.
- Job metadata: stored under the Hermes data root in `session-jobs/`.
- Prompt privacy: raw prompts are not stored in metadata; only length and SHA-256 digest are retained.
- Output: only the bounded, redacted terminal answer is retained for later retrieval.
- Restart behavior: a persisted running job not owned by the current server process is marked `orphaned`; persisted PIDs are never trusted or signaled.

Session control can consume the configured provider's quota or incur provider charges. Do not enable it on an unauthenticated public endpoint, and review returned content before sharing it.

## Validation without a real model call

The automated tests replace process launch with a fake Hermes process. They verify the fixed CLI arguments, `shell=False`, prompt-free metadata, timeout bounds, restart reconciliation, redaction, tool registration gates, and status/result flow. The test suite does not resume a real session or contact a model provider.

## Structured execution and uncertain outcomes (v0.16.2)

The CLI must advertise `--format stream-json` in `hermes chat --help`; otherwise the call refuses before submitting. This finite turn does not request unattended autoapproval. Hermes Agent's existing session policy remains authoritative; a turn requiring approval may wait for it. `HERMES_YOLO_MODE` is not forwarded.

A matching terminal `result` with exit code zero, no error, and a successful process exit is required for completion. Other stdout events are not the answer. The parser bounds each JSONL record to 256 KiB and total stdout to 8 MiB; it persists only up to 24,000 redacted final-answer characters. Stderr and intermediate tool bodies are not retained by this bridge.

Before spawning, a durable session reservation and job intent are written. After timeout, missing/malformed terminal output, process failure, restart, or persistence failure, `outcome_unknown=true` and `retry_safe=false` preserve the ambiguity. The reservation refuses another submission even across server processes. Terminating the CLI does not prove gateway work stopped. Inspect the original job and session through the existing read-only tools; do not resend automatically. A successful verified result releases the reservation for a new authorized turn. An administrator may retire an unresolved reservation only after independently reconciling the original turn; no automatic expiry or unlock tool is provided.

Offline protocol/dispatch regressions pass; real Desktop continuation on Windows and other installations is not yet certified. The upstream terminal envelope has no turn identifier, so this bridge binds it to the requested session and this one CLI invocation, without claiming stronger gateway correlation.
