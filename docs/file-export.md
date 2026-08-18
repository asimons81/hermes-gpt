# Binary file export

`hermes_export_file(path)` transfers one existing local file to a trusted MCP client as an MCP-native embedded binary resource.

This surface exists for outputs such as spreadsheets, PDFs, archives, images, and other files that should not be forced through UTF-8 text decoding. It is read-only on the host, but it is intentionally authorized at **Operator `workspace` level** because transferring raw bytes can disclose data even when nothing is mutated.

## Security contract

A file is exportable only when all of these conditions are true:

- `HERMES_GPT_OPERATOR_ENABLED=1`;
- `HERMES_GPT_OPERATOR_LEVEL=workspace` or `owner`;
- `HERMES_GPT_OPERATOR_ALLOWED_PATHS` is non-empty;
- the fully resolved file path remains under one of those allowed roots;
- the resolved path is not denied by the existing secret/credential/vault/token path policy;
- the file is a regular file;
- the file is within the configured byte limit;
- when an extension allowlist is configured, the file suffix is allowed.

Owner Mode does **not** bypass the denied-path policy.

Unlike `hermes_workspace_read`, binary export does not fall back to broad non-secret filesystem reads when `HERMES_GPT_OPERATOR_ALLOWED_PATHS` is empty. A non-empty workspace root is mandatory.

## Size limits

Default maximum:

```text
4 MiB
```

Override it with a byte count:

```text
HERMES_GPT_EXPORT_MAX_BYTES=8388608
```

The hard ceiling is **16 MiB**. Values above the hard ceiling, zero/negative values, and invalid integers fail closed.

Hermes GPT checks the file size before opening it and also reads at most one byte beyond the configured limit so a file that grows between the metadata check and the read cannot silently exceed the cap.

## Optional extension allowlist

Leave `HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS` unset to allow any file extension that passes the workspace and denied-path checks.

To narrow the surface:

```text
HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS=.pdf,.xlsx,.png,.zip
```

Leading dots are optional and matching is case-insensitive. If the variable is set but contains no valid extensions, export fails closed.

## MCP result shape

A successful call returns safe structured metadata including:

- `filename`
- `mime_type`
- `size_bytes`
- `sha256`
- an opaque `hermes-export://sha256/...` resource URI
- `transfer: "mcp_embedded_resource"`
- `client_rendering: "client-controlled"`

The file bytes are returned only as:

```text
CallToolResult
  -> EmbeddedResource
     -> BlobResourceContents
```

The local absolute path is not returned to the MCP client. The base64 representation required by the MCP wire format is not duplicated into text or structured metadata.

Hermes GPT does **not** fall back to pasting base64 into the conversation when a client does not render the binary resource.

## Client rendering

MCP defines embedded binary resources, but it deliberately leaves presentation to the client.

Hermes GPT therefore guarantees the protocol-native binary result. It does **not** guarantee that ChatGPT, Codex, or another MCP client will always show a save/download button, filename chip, inline preview, or other particular UI.

That distinction matters: successful MCP transfer and a specific client attachment UX are separate capabilities.

## Audit behavior

Every success or refusal is audited through the Operator audit system.

Successful records include bounded metadata such as MIME type, byte size, and SHA-256. The existing audit path summarizer records only a basename/length summary rather than the full local path. Raw binary contents and base64 payloads are never written to the audit record.

## Example

With an approved workspace:

```text
HERMES_GPT_OPERATOR_ENABLED=1
HERMES_GPT_OPERATOR_LEVEL=workspace
HERMES_GPT_OPERATOR_ALLOWED_PATHS=C:\work\reports
```

A trusted MCP client can call:

```text
hermes_export_file(path="C:\work\reports\quarterly.xlsx")
```

The tool refuses the same file when it is outside the approved root, resolves through a symlink outside the root, matches a denied secret path, exceeds the byte limit, or violates an optional extension allowlist.

## Related docs

- [Operator Mode](operator-mode.md)
- [MCP compatibility](mcp-compatibility.md)
- [OpenAI Secure MCP Tunnel](openai-secure-mcp-tunnel.md)
