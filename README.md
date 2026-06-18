# hermes-gpt

`hermes-gpt` is a standalone MCP sidecar for Hermes Agent. It imports selected local Hermes Agent internals at runtime and exposes them to MCP clients without modifying Hermes Agent source files.

This is a **local-dev release**. It is not a hosted service, not a fork of Hermes Agent, not a generic remote dev container, and not a replacement for DevSpace.

## Security posture

By default, `hermes-gpt` is designed for a trusted local machine:

- HTTP binds to `127.0.0.1` by default.
- Tools advertise `noauth` only for local-dev MCP clients.
- Write, patch, terminal execution, memory writes, and session search are disabled or hidden by default.
- Remote/public release is not supported until real OAuth or another ChatGPT-compatible authentication layer is added.

Do not expose this server publicly without authentication. A temporary tunnel is acceptable only for short local testing when you understand that any enabled tool is reachable through that URL.

## Prerequisites

- Python 3.10+
- A local Hermes Agent install
- MCP Python SDK and Uvicorn

Install dependencies:

```bash
cd ~/hermes-gpt
python -m pip install -r requirements.txt
```

## Local MCP clients

Stdio mode is for local MCP clients that support subprocess MCP servers:

```bash
cd ~/hermes-gpt
python server.py
```

Example client command:

```json
{
  "command": "python",
  "args": ["C:\\Users\\asimo\\hermes-gpt\\server.py"]
}
```

## Local HTTP

HTTP mode uses FastMCP streamable HTTP:

```bash
cd ~/hermes-gpt
python server.py --http --host 127.0.0.1 --port 7677
```

Local endpoint:

```text
http://127.0.0.1:7677/mcp
```

If you bind to anything other than loopback in the default `local-dev` profile, the server prints a warning. This warning means the configuration is not release-safe.

## ChatGPT local testing

ChatGPT developer mode expects a remote MCP endpoint. Do not enter a localhost URL such as `http://127.0.0.1:4750`; ChatGPT fetches the MCP configuration through its connector path, where `127.0.0.1` is not your machine.

For short local testing only:

```powershell
cd C:\Users\asimo\hermes-gpt
python server.py --http --host 127.0.0.1 --port 4750
```

In another terminal:

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:4750 --http-host-header 127.0.0.1:4750
```

In ChatGPT, configure:

- Protocol: Streaming HTTP
- MCP server URL: `https://<your-trycloudflare-host>/mcp`
- Authentication: No Authentication

This is not a production deployment. Remove and recreate the connector if ChatGPT cached older tool metadata.

## Tool gates

Default visible tools:

- `hermes_read_file(path, offset=1, limit=500)`
- `hermes_search_files(pattern, target="content", path=".", file_glob=None, limit=50)`
- `hermes_memory(action="search", target="memory", content=None, old_text=None)`
- `hermes_skill_list()`
- `hermes_skill_view(name)`

Opt-in tools and actions:

| Capability | Env var | Default |
| --- | --- | --- |
| Write file and patch tools | `HERMES_GPT_ENABLE_WRITE=1` | Hidden |
| Memory `add`, `replace`, `remove` | `HERMES_GPT_ENABLE_MEMORY_WRITE=1` | Disabled |
| Session search | `HERMES_GPT_ENABLE_SESSION_SEARCH=1` | Hidden |
| Terminal command execution | `HERMES_GPT_ENABLE_TERMINAL=1` | Hidden |

Terminal timeout is capped at 120 seconds even when enabled.

## Remote profile

`--profile remote` is intentionally blocked because authentication is not implemented:

```bash
python server.py --http --profile remote
```

For temporary experiments only, you can bypass this block with both:

```bash
HERMES_GPT_UNSAFE_REMOTE_NOAUTH=1
python server.py --http --profile remote --i-understand-this-is-unsafe
```

Do not use this bypass for release.

## Release checklist

Before publishing:

- No `*.pem` files.
- No `*.log` or `*.err.log` files.
- No `__pycache__/` or `*.pyc`.
- `python -m py_compile server.py` passes.
- `pytest` passes.
- Server binds to loopback by default.
- Terminal, write tools, memory writes, and session search are disabled by default.

## Current capability notes

The feasibility probe passed in this environment:

- Hermes source root: `C:\Users\asimo\AppData\Local\hermes\hermes-agent`
- File tools: available
- Terminal tool: available, gated by `HERMES_GPT_ENABLE_TERMINAL=1`
- Memory tool: available
- Skill discovery: available through local and bundled skill directories
- Session search: available through `SessionDB.search_messages`
- FastMCP stdio: available
- FastMCP streamable HTTP: available

See `FEASIBILITY.md` for probe details and exact signatures.

## License

MIT. See `LICENSE`.
