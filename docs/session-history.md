# Hermes session history

Hermes GPT exposes five optional, read-only MCP tools for finding and reviewing
existing Hermes sessions, including canonical Bot Chats. These capabilities
were originally available only through the full ChatGPT connector. The separately
installed **Hermes GPT Session History** integration brings the same read-only
operations to Codex as native tools. They query Hermes' installed session APIs;
they do not create sessions, resume conversations, rebuild search indexes, or
write exports to disk.

## Client availability

- **ChatGPT/full connector:** the tools are registered by the normal Hermes GPT
  server when the session-search gate is enabled.
- **Codex:** the curated `core` and `operator` toolsets did not originally
  include session history. Install and enable the Hermes GPT Session History
  integration to make the session-history native tool names available in Codex.

Tool availability alone does not bypass Hermes' server-side gates or privacy
controls. ChatGPT uses a frozen snapshot of an approved MCP app's tools and
inputs. A backend restart does not update that snapshot. After adding a tool or
changing its input schema, refresh the app's actions in ChatGPT workspace
settings, review and enable the new actions, and publish the update before
opening a new chat. Business workspaces that cannot update a published app must
recreate and republish it. See [OpenAI's MCP app guidance](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

## Enable locally

Session history is hidden by default. Enable it only for a trusted local MCP
server:

```powershell
$env:HERMES_GPT_ENABLE_SESSION_SEARCH="1"
python server.py
```

The five tools are:

| Tool | Purpose |
| --- | --- |
| `hermes_session_list` | Return bounded, safely projected regular-session metadata. |
| `hermes_session_search` | Search the installed read-only FTS API and return its bounded plain-text response. |
| `hermes_session_read` | Return bounded messages from an exact or uniquely prefixed session ID. |
| `hermes_session_export` | Return a bounded in-memory JSON or Markdown transcript. |
| `hermes_bot_chat_get` | Resolve a profile's canonical `Bot Chat` registry row and its current compression-tip session ID. |

All five tools accept an optional `profile` argument. It defaults to `default` for backward compatibility. Named profiles are resolved to that profile's `state.db` without changing process-global `HERMES_HOME`, and are permitted only when the profile exists and is included in `HERMES_GPT_OPERATOR_ALLOWED_PROFILES`. This makes routed Hermes bot profiles such as `project-manager`, `builder`, or `tech-ops` independently searchable while preserving the existing read-only `SessionDB(read_only=True)` boundary.

Canonical Bot Chat registry rows and compression continuations may be marked hidden by Hermes and therefore omitted from `hermes_session_list`, which intentionally lists regular visible sessions. This is not a missing-session condition. Use `hermes_bot_chat_get(profile)` to resolve the authoritative registry/current IDs and `hermes_bot_chat_send(profile, prompt)` to act on the current tip. The `current_session_id` is also actionable through `hermes_session_continue` or `hermes_session_send` when the same explicit `profile` is supplied.

Session control is a separate feature with a separate gate. Reading history
does not enable `hermes_session_continue`, `hermes_session_send`, or
`hermes_bot_chat_send`.

## Sending ChatGPT output back into Hermes sessions

Enable session control only on a trusted local MCP server:

```powershell
$env:HERMES_GPT_ENABLE_SESSION_CONTROL="1"
```

The session-control tools are profile-aware:

| Tool | Purpose |
| --- | --- |
| `hermes_session_continue` | Start one bounded asynchronous turn in an existing session. |
| `hermes_session_send` | Send terminology alias for `hermes_session_continue`. |
| `hermes_bot_chat_send` | Resolve a profile's canonical Bot Chat/current compression tip and send one bounded turn directly to it. |
| `hermes_session_job_status` | Poll the asynchronous send/continue job. |
| `hermes_session_job_result` | Return the bounded, redacted result from the completed job. |

`hermes_session_continue` and `hermes_session_send` accept
`profile="default"` for backward compatibility. The server resolves the
session ID inside that profile before dispatch and launches the Hermes
`--resume --oneshot` subprocess with the same `HERMES_PROFILE`, preserving
profile isolation.

For routed bots, prefer `hermes_bot_chat_send`:

```text
hermes_bot_chat_send(
  profile="project-manager",
  prompt="<handoff or output from ChatGPT>"
)
```

This lets ChatGPT hand work directly to Project Manager, Hermes Manager,
Builder, Tech Ops, or another authorized profile without manually copying text
into Hermes. The tool targets the current Bot Chat compression tip rather than
assuming the original registry session remains current.

The returned job ID can be checked with `hermes_session_job_status` and
`hermes_session_job_result`. Only one job may run concurrently for the same
profile+session pair.

## Privacy defaults

`hermes_session_read` and `hermes_session_export` include only `user` and
`assistant` messages by default. Keep these arguments disabled for routine
inspection:

```text
include_inactive=false
include_system_messages=false
include_tool_messages=false
include_lineage=false  # export only
```

Internal `system`, `tool`, and `function` content also requires the server-side
`HERMES_GPT_ENABLE_SESSION_INTERNAL_CONTENT=1` gate. Lineage export remains
fail-closed. Redaction and response-size bounds apply even when internal content
is deliberately enabled.

Session transcripts can contain private prompts, personal data, credentials,
local paths, and tool output. Treat every response as private local data. Do not
paste transcript content into bug reports or publish it without review.

## Clean native-tool smoke test

Use the four displayed native tools directly from Codex rather than invoking
shell commands or inspecting the source. Keep limits small and do not display
message bodies:

1. Call `hermes_session_list(limit=3, include_archived=false)`.
2. Call `hermes_session_search(query="Hermes", limit=3)`.
3. Select a valid `session_id` returned by list or search.
4. Call `hermes_session_read` with that ID, `limit=3`, and inactive, system,
   and tool content disabled.
5. Call `hermes_session_export` with the same ID, `format="markdown"`,
   `limit=3`, and inactive, lineage, system, and tool content disabled.
6. Record only each exact tool name, PASS/FAIL, bounded result counts or sizes,
   and a redacted session ID. Do not reproduce transcript text.

If the read-only Operator surface is also installed, `hermes_config_get` may be
used to inspect the configured working model without changing configuration.
Query `model`; the default working model is returned as `value.default`.
Configuration responses retain the normal secret-redaction policy.

A tool passes this smoke test when the native call completes without a tool
error. A valid empty search result is not a failure. If Hermes' read-only FTS
API is unavailable, search must report that limitation explicitly and must not
activate or rebuild FTS.

## Pagination and export bounds

List, read, and export offsets advance by database rows examined, including
rows filtered from the response. This prevents filtered internal roles from
causing duplicate or infinite pages. Responses are capped by
`MAX_RESPONSE_BYTES`; exports are additionally capped by
`MAX_EXPORT_MESSAGES`.

Exports are returned only in memory. The tool never creates a file, returns a
file path, or emits an unbounded raw database export.
