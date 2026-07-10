"""MCP stdio surface for the safe Codex-focused Hermes GPT tools."""

from __future__ import annotations

import json
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from codex_core import CodexToolCore, codex_toolset


NOAUTH_META = {"securitySchemes": [{"type": "noauth"}]}


def _structured_redacted(value: Any) -> Any:
    from codex_core import redact_value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return redact_value(value)


def build_codex_server(core: CodexToolCore, *, host: str = "127.0.0.1", port: int = 7677, http: bool = False,
                       operator_tools: dict[str, Callable[..., Any]] | None = None) -> FastMCP:
    server = FastMCP(
        "hermes-gpt",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        sse_path="/sse",
        message_path="/messages/",
        stateless_http=http,
        json_response=http,
    )

    def hermes_status() -> dict[str, Any]:
        """Check the local Hermes GPT and Hermes Agent gateway state."""
        return core.status()

    def hermes_capabilities() -> dict[str, Any]:
        """Return enabled Codex capabilities and the gates required for disabled ones."""
        return core.capabilities()

    def hermes_vision_analyze(image_path: str, prompt: str, project_root: str | None = None, detail: str = "medium") -> dict[str, Any]:
        """Analyze an approved local raster image through Hermes Agent vision."""
        return core.vision(image_path, prompt, project_root, detail)

    def hermes_web_search(query: str, max_results: int = 5) -> dict[str, Any]:
        """Search the web with the configured Hermes provider. No pages are fetched here."""
        return core.search(query, max_results)

    def hermes_extract_page(url: str, max_chars: int = 12000) -> dict[str, Any]:
        """Extract readable public web content after local/private-network safety checks."""
        return core.extract_page(url, max_chars)

    def hermes_plan(goal: str, project_root: str, include_git_diff: bool = False, include_tree: bool = True, max_files: int = 80) -> dict[str, Any]:
        """Create a compact, read-only repository context pack and implementation plan."""
        return core.plan(goal, project_root, include_git_diff, include_tree, max_files)

    def hermes_author_skill(skill_name: str, goal: str, project_root: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Draft a Hermes skill; writes stay dry-run unless every write gate is enabled."""
        return core.author_skill(skill_name, goal, project_root, dry_run)

    def hermes_cron_plan(request: str, timezone: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Convert a simple natural-language schedule request into a dry-run cron plan."""
        return core.cron_plan(request, timezone, dry_run)

    def hermes_cron_create(request: str, timezone: str | None = None, confirm: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Create a cron job only after explicit confirmation and strict write gates."""
        return core.cron_create(request, timezone, confirm, dry_run)

    def hermes_gateway_diagnostics(verbose: bool = False) -> dict[str, Any]:
        """Run read-only Hermes gateway diagnostics, including PID-state checks."""
        return core.gateway_diagnostics(verbose)

    for tool in (
        hermes_status, hermes_capabilities, hermes_vision_analyze, hermes_web_search,
        hermes_extract_page, hermes_plan, hermes_author_skill, hermes_cron_plan,
        hermes_cron_create, hermes_gateway_diagnostics,
    ):
        server.add_tool(tool, meta=NOAUTH_META)
    active = codex_toolset()
    if active == "operator":
        for alias, callback in (operator_tools or {}).items():
            def wrapper(*args: Any, __callback: Callable[..., Any] = callback, **kwargs: Any) -> Any:
                return _structured_redacted(__callback(*args, **kwargs))
            wrapper.__name__ = alias
            wrapper.__doc__ = callback.__doc__ or f"Hermes Operator tool alias for {callback.__name__}."
            wrapper.__signature__ = __import__("inspect").signature(callback)  # type: ignore[attr-defined]
            server.add_tool(wrapper, name=alias, meta=NOAUTH_META)
    return server
