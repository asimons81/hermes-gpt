# hermes-gpt feasibility

Date: 2026-06-18

## Decision

Feasibility passed. The required capabilities are available in this environment, so the sidecar implementation was built.

## Probe scope

This was a bounded probe. It checked only the requested install candidates, import surfaces, function signatures, FastMCP availability, and a small SessionDB API surface.

## Hermes root detection

- `HERMES_HOME`: `C:\Users\asimo\AppData\Local\hermes`
- `HERMES_HOME` is the Hermes home/data directory, not the source root.
- Source root found through the Windows default candidate and the `hermes-agent` child under `HERMES_HOME`:
  - `C:\Users\asimo\AppData\Local\hermes\hermes-agent`
- The source root contains `tools`, `hermes_state.py`, and `skills`.
- Python package metadata also exists:
  - `hermes-agent 0.16.0`
  - `hermes_agent 0.16.0`
  - location: `C:\Users\asimo\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages`

## Import viability

The Hermes source root and bundled venv site-packages were added to `sys.path` for the probe.

- `from tools import file_tools`: pass
- `from tools import terminal_tool`: pass
- `from tools import memory_tool`: pass
- `from tools import skill_manager_tool`: pass
- `from hermes_state import SessionDB, get_hermes_home`: pass

## Function availability and signatures

- `file_tools.read_file_tool(path: str, offset: int = 1, limit: int = 500, task_id: str = 'default') -> str`
- `file_tools.write_file_tool(path: str, content: str, task_id: str = 'default', cross_profile: bool = False) -> str`
- `file_tools.patch_tool(mode: str = 'replace', path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, patch: str = None, task_id: str = 'default', cross_profile: bool = False) -> str`
- `file_tools.search_tool(pattern: str, target: str = 'content', path: str = '.', file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = 'content', context: int = 0, task_id: str = 'default') -> str`
- `terminal_tool.terminal_tool(command: str, background: bool = False, timeout: Optional[int] = None, task_id: Optional[str] = None, force: bool = False, workdir: Optional[str] = None, pty: bool = False, notify_on_complete: bool = False, watch_patterns: Optional[List[str]] = None) -> str`
- `memory_tool.memory_tool(action: str, target: str = 'memory', content: str = None, old_text: str = None, store: Optional[tools.memory_tool.MemoryStore] = None) -> str`

## FastMCP

- `from mcp.server.fastmcp import FastMCP`: pass
- Constructor supports `host`, `port`, `streamable_http_path`, `json_response`, and `stateless_http`.
- `FastMCP.run` signature: `(self, transport: Literal['stdio', 'sse', 'streamable-http'] = 'stdio', mount_path: str | None = None) -> None`
- This SDK supports `stdio`, `sse`, and `streamable-http` transports.
- Host and port must be passed to the `FastMCP(...)` constructor, not to `run(...)`.

## Capability matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Hermes root detection | pass | Found `C:\Users\asimo\AppData\Local\hermes\hermes-agent`. |
| file tools import | pass | `tools.file_tools` imports successfully. |
| terminal tool import | pass | `tools.terminal_tool` imports successfully. Runtime execution is gated in `hermes-gpt`. |
| memory tool import | pass | `tools.memory_tool` imports successfully. |
| skill discovery | pass | Local skill directories exist under `C:\Users\asimo\AppData\Local\hermes\skills` and bundled skills under the source root. |
| session search | pass | `SessionDB(read_only=True)` opens and `search_messages(...)` is available. |
| FastMCP stdio | pass | `stdio` is supported by `FastMCP.run`. |
| FastMCP streamable-http | pass | `streamable-http` is supported by `FastMCP.run`; host/port are constructor settings. |

## Build decision

All required capabilities passed:

- Hermes root detection
- FastMCP import
- file tools import
- terminal tool import
- memory tool import

Optional capabilities also passed:

- skill manager import
- session DB import
- session search via `SessionDB.search_messages`

The server was built under `~/hermes-gpt/`.
