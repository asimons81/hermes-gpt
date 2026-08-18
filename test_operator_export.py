from __future__ import annotations

import base64
import hashlib
import json

from mcp.types import BlobResourceContents, EmbeddedResource, TextContent

import operator_export as export
import operator_policy as op


def _configure_workspace(monkeypatch, root) -> None:
    monkeypatch.setenv(op.OPERATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "workspace")
    monkeypatch.setenv(op.OPERATOR_ALLOWED_PATHS_ENV, str(root))
    monkeypatch.delenv(export.EXPORT_ALLOWED_EXTENSIONS_ENV, raising=False)
    monkeypatch.delenv(export.EXPORT_MAX_BYTES_ENV, raising=False)


def _blob_block(result) -> EmbeddedResource:
    blocks = [item for item in result.content if isinstance(item, EmbeddedResource)]
    assert len(blocks) == 1
    return blocks[0]


def test_export_returns_mcp_embedded_blob_without_local_path(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    audit = tmp_path / "audit.jsonl"
    op.set_audit_log_override(audit)
    try:
        payload = b"PK\x03\x04fake-xlsx-binary\x00\xff"
        source = tmp_path / "report.xlsx"
        source.write_bytes(payload)

        result = export.hermes_export_file(str(source))

        assert result.isError is False
        assert result.structuredContent is not None
        metadata = result.structuredContent
        assert metadata["success"] is True
        assert metadata["filename"] == "report.xlsx"
        assert metadata["mime_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert metadata["size_bytes"] == len(payload)
        assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
        assert metadata["resource_uri"].startswith("hermes-export://sha256/")
        assert metadata["transfer"] == "mcp_embedded_resource"
        assert metadata["client_rendering"] == "client-controlled"
        assert str(tmp_path) not in json.dumps(metadata)

        text_blocks = [item for item in result.content if isinstance(item, TextContent)]
        assert len(text_blocks) == 1
        assert str(tmp_path) not in text_blocks[0].text
        assert base64.b64encode(payload).decode("ascii") not in text_blocks[0].text

        embedded = _blob_block(result)
        assert isinstance(embedded.resource, BlobResourceContents)
        assert embedded.resource.mimeType == metadata["mime_type"]
        assert str(embedded.resource.uri) == metadata["resource_uri"]
        assert base64.b64decode(embedded.resource.blob) == payload

        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        record = records[0]
        assert record["tool"] == "hermes_export_file"
        assert record["success"] is True
        assert record["changed"] is False
        assert record["path_summary"].startswith("report.xlsx (")
        assert str(tmp_path) not in json.dumps(record)
        assert base64.b64encode(payload).decode("ascii") not in json.dumps(record)
    finally:
        op.set_audit_log_override(None)


def test_export_requires_workspace_level(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv(op.OPERATOR_LEVEL_ENV, "read_only")
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
    assert result.structuredContent["code"] == "FILE_EXPORT_ERROR"


def test_export_requires_nonempty_allowed_paths(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.delenv(op.OPERATOR_ALLOWED_PATHS_ENV, raising=False)
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)


def test_export_rejects_file_outside_allowed_root(monkeypatch, tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _configure_workspace(monkeypatch, allowed)
    source = outside / "report.pdf"
    source.write_bytes(b"%PDF-test")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
    assert str(outside) not in json.dumps(result.structuredContent)


def test_export_rejects_secret_path_even_under_allowed_root(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    source = tmp_path / ".env"
    source.write_bytes(b"API_KEY=do-not-export")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
    assert "do-not-export" not in json.dumps(result.structuredContent)


def test_export_enforces_configured_size_cap(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv(export.EXPORT_MAX_BYTES_ENV, "8")
    source = tmp_path / "archive.zip"
    source.write_bytes(b"012345678")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)


def test_export_rejects_max_above_hard_cap(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv(export.EXPORT_MAX_BYTES_ENV, str(export.HARD_EXPORT_MAX_BYTES + 1))
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)


def test_export_optional_extension_allowlist(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv(export.EXPORT_ALLOWED_EXTENSIONS_ENV, "pdf, .xlsx")
    denied = tmp_path / "archive.zip"
    denied.write_bytes(b"zip")
    allowed = tmp_path / "report.pdf"
    allowed.write_bytes(b"%PDF-test")

    denied_result = export.hermes_export_file(str(denied))
    allowed_result = export.hermes_export_file(str(allowed))

    assert denied_result.isError is True
    assert allowed_result.isError is False
    assert isinstance(_blob_block(allowed_result).resource, BlobResourceContents)


def test_export_empty_extension_allowlist_fails_closed(monkeypatch, tmp_path) -> None:
    _configure_workspace(monkeypatch, tmp_path)
    monkeypatch.setenv(export.EXPORT_ALLOWED_EXTENSIONS_ENV, " , ")
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")

    result = export.hermes_export_file(str(source))

    assert result.isError is True
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
