"""Bounded MCP-native binary file export for Hermes GPT.

The export surface is intentionally stricter than ordinary text reads:

- Operator Mode must be enabled at ``workspace`` level or higher.
- ``HERMES_GPT_OPERATOR_ALLOWED_PATHS`` must contain at least one root.
- The resolved file must stay under an allowed root and outside every denied
  secret/credential path.
- File size is capped before and during the read.
- Optional extension allowlisting can narrow the export surface further.
- Binary bytes are returned only as an MCP ``EmbeddedResource`` containing
  ``BlobResourceContents``. They are never copied into text or structured
  metadata.
- Audit records contain bounded metadata and the existing path summary, not
  raw file bytes or the full local path.

MCP clients decide how embedded resources are rendered. Hermes GPT therefore
advertises this as an MCP-native binary transfer, not as a guaranteed download
button in every client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    TextContent,
)

import operator_policy as op

EXPORT_MAX_BYTES_ENV = "HERMES_GPT_EXPORT_MAX_BYTES"
EXPORT_ALLOWED_EXTENSIONS_ENV = "HERMES_GPT_EXPORT_ALLOWED_EXTENSIONS"
DEFAULT_EXPORT_MAX_BYTES = 4 * 1024 * 1024
HARD_EXPORT_MAX_BYTES = 16 * 1024 * 1024


def _configured_max_bytes() -> int:
    raw = os.environ.get(EXPORT_MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_EXPORT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{EXPORT_MAX_BYTES_ENV} must be an integer number of bytes.") from exc
    if value <= 0:
        raise ValueError(f"{EXPORT_MAX_BYTES_ENV} must be greater than zero.")
    if value > HARD_EXPORT_MAX_BYTES:
        raise ValueError(
            f"{EXPORT_MAX_BYTES_ENV} exceeds the hard maximum of "
            f"{HARD_EXPORT_MAX_BYTES} bytes."
        )
    return value


def _allowed_extensions() -> set[str] | None:
    raw = os.environ.get(EXPORT_ALLOWED_EXTENSIONS_ENV)
    if raw is None:
        return None
    values: set[str] = set()
    for item in raw.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        if not normalized.startswith("."):
            normalized = "." + normalized
        values.add(normalized)
    if not values:
        raise ValueError(
            f"{EXPORT_ALLOWED_EXTENSIONS_ENV} is set but contains no valid extensions."
        )
    return values


def _mime_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name, strict=False)
    return guessed or "application/octet-stream"


def hermes_export_file(path: str) -> CallToolResult:
    """Return one authorized local file as an MCP embedded binary resource.

    The response contains safe metadata in text/structured form and the file
    bytes only in ``BlobResourceContents``. The local absolute path is never
    returned to the MCP client.
    """

    policy: op.OperatorPolicy | None = None
    resolved: Path | None = None
    try:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path is required.")

        policy = op.OperatorPolicy()
        policy.require_level("workspace")

        # Export is intentionally stricter than hermes_workspace_read: a
        # non-empty allowlist is mandatory because this operation transfers
        # raw bytes off-host to the MCP client.
        resolved = op._normalize_path(path)
        policy.require_workspace_path(resolved)

        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError("Requested export file does not exist or is not a regular file.")

        allowed_extensions = _allowed_extensions()
        suffix = resolved.suffix.lower()
        if allowed_extensions is not None and suffix not in allowed_extensions:
            raise PermissionError(
                f"File extension {suffix or '<none>'!r} is not allowed by "
                f"{EXPORT_ALLOWED_EXTENSIONS_ENV}."
            )

        max_bytes = _configured_max_bytes()
        stat_size = resolved.stat().st_size
        if stat_size > max_bytes:
            raise ValueError(
                f"File is {stat_size} bytes, exceeding the configured export maximum of "
                f"{max_bytes} bytes."
            )

        # Read one byte beyond the cap so growth between stat() and read() also
        # fails closed instead of producing an oversized response.
        with resolved.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(
                f"File exceeded the configured export maximum of {max_bytes} bytes during read."
            )

        digest = hashlib.sha256(data).hexdigest()
        mime_type = _mime_type(resolved)
        resource_uri = f"hermes-export://sha256/{digest}"
        metadata: dict[str, Any] = {
            "success": True,
            "filename": resolved.name,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "sha256": digest,
            "resource_uri": resource_uri,
            "transfer": "mcp_embedded_resource",
            "client_rendering": "client-controlled",
        }

        op.audit_record(
            tool="hermes_export_file",
            level=policy.level,
            apply_mode=policy.apply_mode,
            dry_run=False,
            success=True,
            changed=False,
            summary=f"exported {resolved.name} ({len(data)} bytes)",
            path=str(resolved),
            extra={
                "mime_type": mime_type,
                "size_bytes": len(data),
                "sha256": digest,
            },
        )

        blob = BlobResourceContents(
            uri=resource_uri,
            mimeType=mime_type,
            blob=base64.b64encode(data).decode("ascii"),
        )
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(metadata, indent=2)),
                EmbeddedResource(type="resource", resource=blob),
            ],
            structuredContent=metadata,
            isError=False,
        )
    except Exception as exc:  # noqa: BLE001 - normalize failures into a safe MCP envelope
        envelope = op.error_from_exception(
            exc,
            layer="workspace",
            code="FILE_EXPORT_ERROR",
            suggested_action=(
                "Check Operator workspace level, allowed_paths, denied-path policy, "
                "file size, and optional export extension restrictions."
            ),
        )
        op.audit_record(
            tool="hermes_export_file",
            level=policy.level if policy is not None else "unknown",
            apply_mode=policy.apply_mode if policy is not None else "unknown",
            dry_run=False,
            success=False,
            changed=False,
            error=str(envelope.get("safe_message") or "file export failed"),
            path=str(resolved) if resolved is not None else path,
        )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(envelope, indent=2))],
            structuredContent=envelope,
            isError=True,
        )
