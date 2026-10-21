"""Declarative tool registration for the hermes-gpt MCP server.

``server.register_tools`` describes the surface as ordered groups of
``ToolEntry`` values. ``register`` walks them in order and calls
``server.add_tool`` exactly as the original imperative sequence did:

- a gate is a zero-argument callable evaluated at registration time (a live
  environment read or helper call), immediately before its entry is considered;
- ``meta`` comes from ``meta_factory(entry.meta_extra)``;
- ``annotations`` is passed only when the entry sets it.

This module imports nothing from ``server`` so there is no import cycle; the
caller supplies ``meta_factory``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from mcp.types import ToolAnnotations


@dataclass(frozen=True)
class ToolEntry:
    fn: Callable[..., Any]
    gate: Callable[[], bool] | None = None
    annotations: ToolAnnotations | None = None
    meta_extra: dict[str, Any] | None = None


def register(
    server: Any,
    groups: Sequence[tuple[str, Sequence[ToolEntry]]],
    *,
    meta_factory: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> None:
    """Register every entry whose gate passes, in group and entry order."""
    for _group_name, entries in groups:
        for entry in entries:
            if entry.gate is not None and not entry.gate():
                continue
            kwargs: dict[str, Any] = {"meta": meta_factory(entry.meta_extra)}
            if entry.annotations is not None:
                kwargs["annotations"] = entry.annotations
            server.add_tool(entry.fn, **kwargs)
