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
2. Call `hermes_session_continue(session_id, prompt, max_job_runtime_seconds)` or its `hermes_session_send` alias.
3. Save the returned `job_id`.
4. Poll `hermes_session_job_status(job_id)` until the status is `completed`, `failed`, `timed_out`, or `orphaned`.
5. Call `hermes_session_job_result(job_id)` for the bounded, redacted final output.

The start call resolves exact or unique-prefix IDs through Hermes' existing read-only `SessionDB` API before launching anything. It invokes the CLI with a fixed argument array equivalent to:

```text
hermes --resume <resolved-session-id> --oneshot <prompt>
```

No shell is used. Hermes restores the resumed session's recorded working directory using its normal CLI behavior.

## Creating a new session

`hermes_session_create(prompt, max_job_runtime_seconds=7200, profile="default", title=...)`
creates a genuinely new, distinct Hermes session in the target profile and runs its
first prompt through the same asynchronous job machinery:

1. A new session row is created in the profile's Hermes session store (id shape
   `{YYYYmmdd_HHMMSS}_{6-hex}`, explicit source `hermes-gpt`), before any CLI call.
   An optional `title` is recorded when provided.
2. The first work runs in that new session via the fixed CLI argument array:

   ```text
   hermes --resume <new-session-id> --oneshot <prompt>
   ```

3. The call returns immediately with `success`, `session_id`, `job_id`, `profile`
   and `status`.
4. Follow with `hermes_session_job_wait(job_id)` then `hermes_session_job_result(job_id)`.

The new session id is generated before the CLI starts, so the returned `session_id`
is always the id of the freshly created session. Profile is restricted through the
same policy as `hermes_session_continue` (no arbitrary profile names); a failed
session-store write returns `SESSION_CREATE_FAILED` without launching anything.

## Bounds and persistence

- Prompt: maximum 65,536 characters.
- Max job runtime: `max_job_runtime_seconds` clamped to 10–7,200 seconds; default 7,200. Independent of `hermes_session_job_wait` (max 120 s per poll, never kills).
- Returned result: clamped to 500–24,000 characters.
- Concurrency: only one session-control job may run for a given session at a time.
- Job metadata: stored under the Hermes data root in `session-jobs/`.
- Prompt privacy: raw prompts are not stored in metadata; only length and SHA-256 digest are retained.
- Output: captured locally for later result retrieval and redacted before MCP exposure.
- Restart behavior: a persisted running job not owned by the current server process is marked `orphaned`; persisted PIDs are never trusted or signaled.

Session control can consume the configured provider's quota or incur provider charges. Do not enable it on an unauthenticated public endpoint, and review returned content before sharing it.

## Validation without a real model call

The automated tests replace process launch with a fake Hermes process. They verify the fixed CLI arguments, `shell=False`, prompt-free metadata, runtime-limit bounds, restart reconciliation, redaction, tool registration gates, and status/result flow. The test suite does not resume a real session or contact a model provider.
