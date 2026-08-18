from __future__ import annotations

import asyncio
import base64

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource

import operator_policy as op
import server


def _configure_workspace(monkeypatch, root) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(root))


def test_export_tool_is_registered_read_only() -> None:
    mcp = server.build_server()
    tools = asyncio.run(mcp.list_tools())
    tool = next(item for item in tools if item.name == "hermes_export_file")

    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True


def test_export_tool_round_trips_binary_through_fastmcp(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    op.set_audit_log_override(tmp_path / "audit.jsonl")
    try:
        payload = b"PK\x03\x04mcp-export-integration\x00\xff"
        source = tmp_path / "report.xlsx"
        source.write_bytes(payload)

        mcp = server.build_server()
        result = asyncio.run(mcp.call_tool("hermes_export_file", {"path": str(source)}))

        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert result.structuredContent["filename"] == "report.xlsx"
        embedded = next(item for item in result.content if isinstance(item, EmbeddedResource))
        assert isinstance(embedded.resource, BlobResourceContents)
        assert base64.b64decode(embedded.resource.blob) == payload
    finally:
        op.set_audit_log_override(None)


def test_export_tool_refuses_without_workspace_authority(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(tmp_path))
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")

    mcp = server.build_server()
    result = asyncio.run(mcp.call_tool("hermes_export_file", {"path": str(source)}))

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
