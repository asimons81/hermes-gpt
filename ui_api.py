"""Route composition for the Hermes ChatGPT UI (v0.7 conversational shell).

This is a COMPOSITION-ONLY file (architecture.md §16): it owns the route
registry, the JSON envelope shape, and static serving of the built SPA. It
does not implement feature routes. Each feature card contributes a
``<module>_routes()`` function:

- ``ui_security_routes()`` (t_7266e74c) — boundary, /api/me, /api/connection
- ``ui_chat_routes()``     (t_9d31cd92) — sessions + chat SSE
- ``ui_ops_routes()``      (t_1135e15b) — operator adapters + gated mutations

The registry composes whatever sibling modules are present. In parallel
worktrees a sibling module may be absent mid-build; a missing or broken
sibling degrades the registry instead of crashing the server (its own tests
exercise the module directly).

The JSON envelope (``ok`` / ``err``) is implemented in ``ui_security`` next
to the redaction boundary and re-exported here so every handler that builds a
browser payload goes through redaction by construction. ``ui_chat`` passes
``content_allowed=True`` only for the user's own conversation text.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

import ui_security


class _SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback: any missing path returns index.html."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        from starlette.exceptions import HTTPException
        from starlette.responses import FileResponse

        directory = str(self.directory) if self.directory is not None else ""
        index_path = (Path(directory) / "index.html").resolve()
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return FileResponse(str(index_path))
            raise
        if response.status_code == 404:
            return FileResponse(str(index_path))
        return response


# Envelope + redaction boundary (canonical implementation in ui_security).
ok = ui_security.ok
err = ui_security.err
redact_browser = ui_security.redact_browser
error_envelope = ui_security.error_envelope


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


def _index_file() -> Any:
    """Return a FileResponse for the SPA shell (web/dist/index.html)."""
    from starlette.responses import FileResponse

    index = ui_security.ui_dir() / "index.html"
    return FileResponse(index)


async def _ui_not_built_app(scope, receive, send):  # type: ignore[no-untyped-def]
    """ASGI placeholder returned when the built SPA directory is absent."""
    from starlette.responses import PlainTextResponse

    response = PlainTextResponse("UI static assets not built", status_code=404)
    await response(scope, receive, send)


def _ui_not_built(_request: Any) -> Any:
    """HTTP handler placeholder returned when the built SPA directory is absent."""
    return JSONResponse(
        {"ok": False, "err": "UI static assets not built"},
        status_code=404,
    )


def _static_routes() -> list[BaseRoute]:
    """Serve the built SPA (web/dist) under /ui.

    The route and mount are always registered so the URL contract is stable
    even when the built SPA directory is absent (e.g. in CI before ``npm run
    build``). When the dist is missing, both the bare /ui path and any /ui/*
    subpath return a 404 placeholder instead of being omitted entirely.

    The Mount("/ui") makes StaticFiles serve the directory. Because a Starlette
    Mount only matches when the path has a trailing slash, we also add an exact
    Route("/ui") so the bare /ui path returns the SPA shell. A custom
    _SPAStaticFiles subclass falls back to index.html for any missing path so
    the React Router client routes resolve. Assets are referenced by the Vite
    build with base=/ui/ and therefore load under /ui/assets/..., which this
    mount satisfies.
    """
    dist = ui_security.ui_dir()
    if dist.is_dir() and (dist / "index.html").is_file():
        return [
            Route("/ui", lambda _request: _index_file(), methods=["GET"]),
            Mount(
                "/ui",
                app=_SPAStaticFiles(directory=str(dist), html=True),
                name="ui-static",
            ),
        ]
    return [
        Route("/ui", _ui_not_built, methods=["GET"]),
        Mount(
            "/ui",
            app=_ui_not_built_app,
            name="ui-static",
        ),
    ]


def _sibling_routes(module_name: str) -> list[BaseRoute]:
    """Import a sibling routes module and return its routes, or []."""
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 — sibling may be absent mid-build
        eprint(f"UI module {module_name} unavailable: {exc.__class__.__name__}")
        return []
    factory = getattr(module, f"{module_name}_routes", None)
    if factory is None:
        eprint(f"UI module {module_name} has no {module_name}_routes()")
        return []
    try:
        result = factory()
    except Exception as exc:  # noqa: BLE001
        eprint(f"UI module {module_name} routes() failed: {exc.__class__.__name__}")
        return []
    return list(result) if isinstance(result, list) else []


def routes() -> list[BaseRoute]:
    """Compose the full UI route list (mounted before the MCP catch-all)."""
    result: list[BaseRoute] = []
    # Security/boundary routes are the skeleton every card builds against;
    # chat and flight compose in when their modules land.
    for module_name in ("ui_security", "ui_chat", "ui_ops"):
        result.extend(_sibling_routes(module_name))
    result.extend(_static_routes())
    return result


def ui_routes() -> list[BaseRoute]:
    """Compatibility name for the independently tested UI route factory."""
    return routes()
