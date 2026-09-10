"""Keep Hermes' transport defaults consistent across MCP Python SDK 1 and 2.

SDK 2 renamed FastMCP and moved transport options from the constructor to
the ASGI factories. Keep that adaptation here; tools use SDK types directly.
"""

from typing import Any

try:
    from mcp.server import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

    SDK_V2 = False
else:
    SDK_V2 = True


class HermesMCP(_Server):
    """A server with explicit, version-independent transport configuration."""

    def __init__(
        self, name: str, *, version: str, host: str = "127.0.0.1",
        port: int = 7677, streamable_http_path: str = "/mcp",
        sse_path: str = "/sse", message_path: str = "/messages/",
        stateless_http: bool = False, json_response: bool = False,
        transport_security: Any = None,
    ) -> None:
        self._hermes_http_options = {
            "host": host, "streamable_http_path": streamable_http_path,
            "stateless_http": stateless_http, "json_response": json_response,
            "transport_security": transport_security,
        }
        self._hermes_sse_options = {
            "host": host, "sse_path": sse_path, "message_path": message_path,
            "transport_security": transport_security,
        }
        if SDK_V2:
            super().__init__(name, version=version)
        else:
            super().__init__(
                name, port=port, sse_path=sse_path, message_path=message_path,
                **self._hermes_http_options,
            )
            # SDK 1 has no public app-version constructor parameter.
            self._mcp_server.version = version

    def streamable_http_app(self, **kwargs: Any) -> Any:
        if SDK_V2:
            return super().streamable_http_app(**{**self._hermes_http_options, **kwargs})
        return super().streamable_http_app(**kwargs)

    def sse_app(self, *args: Any, **kwargs: Any) -> Any:
        if SDK_V2:
            return super().sse_app(*args, **{**self._hermes_sse_options, **kwargs})
        return super().sse_app(*args, **kwargs)
