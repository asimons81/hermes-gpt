# Hermes session history

Hermes GPT exposes four optional, read-only MCP tools for finding and reviewing
existing Hermes sessions. These capabilities were originally available only
through the full ChatGPT connector. The separately installed **Hermes GPT
Session History** integration brings the same four operations to Codex as
native tools. They query Hermes' installed session APIs; they do not create
sessions, resume conversations, rebuild search indexes, or write exports to
disk.

## Client availability

- **ChatGPT/full connector:** the tools are registered by the normal Hermes GPT
  server when the session-search gate is enabled.
- **Codex:** the curated `core` and `operator` toolsets did not originally
  include session history. Install and enable the Hermes GPT Session History
  integration to make the four exact native tool names available in Codex.

Tool availability alone does not bypass Hermes' server-side gates or privacy
controls. After installing or updating an integration, restart or reconnect the
client so it refreshes the native tool manifest.

## Enable locally

Session history is hidden by default. Enable it only for a trusted local MCP
server:

```powershell
$env:HERMES_GPT_ENABLE_SESSION_SEARCH="1"
python server.py
```

The four tools are:

| Tool | Purpose |
| --- | --- |
| `hermes_session_list` | Return bounded, safely projected session metadata. |
| `hermes_session_search` | Search the installed read-only FTS API and return its bounded plain-text response. |
| `hermes_session_read` | Return bounded messages from an exact or uniquely prefixed session ID. |
| `hermes_session_export` | Return a bounded in-memory JSON or Markdown transcript. |

Session control is a separate feature with a separate gate. Reading history
does not enable `hermes_session_continue` or `hermes_session_send`.

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
