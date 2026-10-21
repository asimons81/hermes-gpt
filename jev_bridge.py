"""Default-off, bounded MCP forwarding to the pinned community Jev adapter.

No provider API implementation lives here. The managed Node adapter owns Jev
schemas and API validation; Hermes keeps its existing client auth/authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from pydantic import TypeAdapter

ENABLE_ENV = "HERMES_GPT_ENABLE_JEV"
ROOT_ENV = "HERMES_GPT_JEV_ADAPTER_ROOT"
NODE_ENV = "HERMES_GPT_JEV_NODE"
ADAPTER_REVISION = "fc4d4bef26af92d1d5a3b9e05bffb46d90a37eae"
TOOL_MAP = {
    "hermes_jev_yes_no": "jev_yes_no",
    "hermes_jev_choose": "jev_choose",
    "hermes_jev_score": "jev_score",
}
MAX_INPUT_BYTES = 131072
MAX_OUTPUT_BYTES = 262144
MAX_WIRE_BYTES = 524288  # structured + text copies of the same result
CALL_TIMEOUT = 25
_SLOTS = threading.BoundedSemaphore(4)
_MESSAGE = TypeAdapter(types.JSONRPCMessage)
_PINNED_FILES = {
    "src/cli.js": "ae916b1535ec47e2a42e4d835777f80017d3765ab8e03b0f4515193c221c7d03",
    "src/server.js": "327b2b60929615b42093391501e0a399b0c4c4405a6ff1db2e2a1538e5c01e69",
    "src/decisions.js": "b49971569f89b268cd2a869183bd522b05efac692d3e5171fad1cc528f5b97ea",
    "src/http.js": "50ed6dbdfe68060beaa21cc488c4cbdbd898dd3f3fff0a5519941615f091512c",
    "package.json": "3f8810dcc7c5dc01505b65c7dc691c70089a96c113717d02586fc0897addcee9",
    "package-lock.json": "c4ac370d3bbca56fb480eb70415c51e071355ce9d07f104f4bc13808eb5a8007",
}
_MESSAGES = {
    "DISABLED": "The Jev bridge is disabled on this server.",
    "INVALID_INPUT": "Invalid decision input. Check the tool schema and size limits.",
    "NOT_CONFIGURED": "Configure the pinned Jev adapter, Node executable and TypeSafe key privately.",
    "BUSY": "The Jev bridge is busy. Try again after an active call finishes.",
    "TIMEOUT": "The Jev adapter request timed out.",
    "UNAVAILABLE": "The Jev adapter is unavailable or returned an invalid response.",
    "AUTHENTICATION": "TypeSafe rejected the server credential. Check it privately.",
    "RATE_LIMITED": "TypeSafe rate limit reached. Try again later.",
    "INVALID_RESPONSE": "TypeSafe returned an unexpected response.",
}


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class _Launch:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def _launch_config() -> _Launch:
    """Only operator-managed paths; no tool-supplied command, URL or env."""
    root = Path(os.environ.get(ROOT_ENV, ""))
    node = Path(os.environ.get(NODE_ENV, ""))
    key = os.environ.get("TYPESAFE_API_KEY", "")
    model = os.environ.get("TYPESAFE_MODEL", "jev-latest")
    if not root.is_absolute() or not node.is_absolute():
        raise BridgeError("NOT_CONFIGURED")
    root, node = root.resolve(), node.resolve()
    if not node.is_file() or not os.access(node, os.X_OK):
        raise BridgeError("NOT_CONFIGURED")
    if not re.fullmatch(r"[\x21-\x7e]{1,4096}", key) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", model):
        raise BridgeError("NOT_CONFIGURED")
    try:
        for name, digest in _PINNED_FILES.items():
            with (root / name).open("rb") as stream:
                data = stream.read(1048577)
            if len(data) > 1048576 or hashlib.sha256(data).hexdigest() != digest:
                raise BridgeError("NOT_CONFIGURED")
        for name, version in (("@modelcontextprotocol/sdk", "1.32.1"), ("zod", "3.25.76")):
            with (root / "node_modules" / name / "package.json").open("rb") as stream:
                package = json.loads(stream.read(65536))
            if package.get("version") != version:
                raise BridgeError("NOT_CONFIGURED")
    except (OSError, ValueError):
        raise BridgeError("NOT_CONFIGURED") from None
    env = {"TYPESAFE_API_KEY": key, "TYPESAFE_MODEL": model, "PATH": str(node.parent)}
    if os.name == "nt" and os.environ.get("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]
    return _Launch((str(node), str(root / "src" / "cli.js")), root, env)


def _validate_input(name: str, args: dict[str, Any]) -> None:
    # Transport admission only. The pinned adapter remains the Jev schema owner.
    if name not in TOOL_MAP.values() or set(args) - {"state", "instructions", "criteria"}:
        raise BridgeError("INVALID_INPUT")
    for field, maximum in (("state", 16000), ("instructions", 4000)):
        value = args.get(field)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise BridgeError("INVALID_INPUT")
    criteria = args.get("criteria")
    if name == "jev_choose":
        if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 32:
            raise BridgeError("INVALID_INPUT")
        if any(not isinstance(k, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", k) for k in criteria):
            raise BridgeError("INVALID_INPUT")
    elif name == "jev_score":
        if not isinstance(criteria, list) or not 2 <= len(criteria) <= 32:
            raise BridgeError("INVALID_INPUT")
    elif criteria is not None and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"}):
        raise BridgeError("INVALID_INPUT")
    values = criteria.values() if isinstance(criteria, dict) else criteria or []
    if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000 for value in values):
        raise BridgeError("INVALID_INPUT")
    if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_BYTES:
        raise BridgeError("INVALID_INPUT")


async def _stop(process: Any) -> None:
    # Shield cleanup from caller cancellation. Reap only this owned child.
    with anyio.CancelScope(shield=True):
        if process.stdin:
            try:
                await process.stdin.aclose()
            except (anyio.BrokenResourceError, OSError):
                pass
        with anyio.move_on_after(0.25):
            await process.wait()
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            with anyio.move_on_after(0.5):
                await process.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


@asynccontextmanager
async def _stdio(launch: _Launch):
    """Bounded SDK transport, without inherited env or parser/stderr logging.

    The SDK's default stdio helper inherits HOME/PATH and buffers lines without
    a byte ceiling. Keep its session/protocol implementation, but bound this hop.
    """
    incoming_send, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(0)
    process = await anyio.open_process(launch.argv, cwd=launch.cwd, env=launch.env, stderr=subprocess.DEVNULL)

    async def read() -> None:
        buffer = b""
        total = 0
        try:
            async with incoming_send:
                while True:
                    chunk = await process.stdout.receive(8192)
                    total += len(chunk)
                    if total > MAX_WIRE_BYTES:
                        raise BridgeError("UNAVAILABLE")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            message = _MESSAGE.validate_json(line)
                        except ValueError:
                            raise BridgeError("UNAVAILABLE") from None
                        await incoming_send.send(SessionMessage(message))
        except anyio.EndOfStream:
            if buffer:
                raise BridgeError("UNAVAILABLE") from None

    async def write() -> None:
        async with outgoing_receive:
            async for message in outgoing_receive:
                data = message.message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8") + b"\n"
                if len(data) > MAX_WIRE_BYTES:
                    raise BridgeError("INVALID_INPUT")
                await process.stdin.send(data)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(read)
            tasks.start_soon(write)
            try:
                yield incoming, outgoing
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await _stop(process)
        with anyio.CancelScope(shield=True):
            await process.aclose()
            await incoming.aclose()
            await outgoing.aclose()


async def _request(launch: _Launch, name: str, args: dict[str, Any]) -> dict[str, Any]:
    async with _stdio(launch) as (incoming, outgoing), ClientSession(incoming, outgoing) as session:
        await session.initialize()
        result = await session.call_tool(name, args)
        return result.model_dump(by_alias=True, exclude_none=True)


def _error(code: str) -> types.CallToolResult:
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=json.dumps({"code": code, "message": _MESSAGES[code]}))])


async def call(name: str, args: dict[str, Any]) -> types.CallToolResult:
    if not enabled():
        return _error("DISABLED")
    acquired = False
    try:
        _validate_input(name, args)
        launch = _launch_config()
        acquired = _SLOTS.acquire(blocking=False)
        if not acquired:
            return _error("BUSY")
        with anyio.fail_after(CALL_TIMEOUT):
            raw = await _request(launch, name, args)
        if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_WIRE_BYTES:
            raise BridgeError("UNAVAILABLE")
        if raw.get("isError"):
            # Do not trust or forward arbitrary child exception messages.
            code = "UNAVAILABLE"
            try:
                candidate = json.loads(raw["content"][0]["text"]).get("code")
                if candidate in _MESSAGES:
                    code = candidate
            except (KeyError, IndexError, TypeError, ValueError):
                pass
            return _error(code)
        data = raw.get("structuredContent")
        expected = {"jev_yes_no": "noul", "jev_choose": "choice", "jev_score": "score"}[name]
        if not isinstance(data, dict) or set(data) != {"model", "answer", "usage"} or not isinstance(data["answer"], dict) or data["answer"].get("type") != expected:
            raise BridgeError("UNAVAILABLE")
        text = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise BridgeError("UNAVAILABLE")
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structuredContent=data)
    except BridgeError as error:
        return _error(error.code)
    except TimeoutError:
        return _error("TIMEOUT")
    except Exception:  # noqa: BLE001 - adapter/session exceptions must never expose raw details
        return _error("UNAVAILABLE")
    finally:
        if acquired:
            _SLOTS.release()


async def hermes_jev_yes_no(state: str, instructions: str, criteria: dict[str, str] | None = None) -> types.CallToolResult:
    """Ask Jev for a probability of yes about supplied text; sends text to TypeSafe and may incur API charges. Does not authorize actions."""
    args: dict[str, Any] = {"state": state, "instructions": instructions}
    if criteria is not None:
        args["criteria"] = criteria
    return await call("jev_yes_no", args)


async def hermes_jev_choose(state: str, instructions: str, criteria: dict[str, str]) -> types.CallToolResult:
    """Ask Jev to choose among described options; sends text to TypeSafe and may incur API charges. Does not authorize actions."""
    return await call("jev_choose", {"state": state, "instructions": instructions, "criteria": criteria})


async def hermes_jev_score(state: str, instructions: str, criteria: list[str]) -> types.CallToolResult:
    """Ask Jev for a weighted position on ordered descriptive levels; sends text to TypeSafe and may incur API charges. Does not authorize actions."""
    return await call("jev_score", {"state": state, "instructions": instructions, "criteria": criteria})
