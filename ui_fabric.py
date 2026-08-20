"""Read-only browser adapters for Hermes GPT v0.8 Fabric Flight Deck.

G4-D intentionally lives beside ``ui_ops`` rather than inside it. These routes
only invoke observational Fabric read models. They do not expose a POST route,
peer transport, credentials, remote paths, reconcile, cancel, retry, artifact
collection, or any other mutation primitive.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import operator_fabric as fabric
import operator_fabric_view as view
import operator_mission as op_mission
import ui_security


def _root() -> Any:
    return op_mission._resolve_root(None)


def _response(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(ui_security.ok(data), status_code=status)


async def _nodes(_request: Request) -> JSONResponse:
    data = await run_in_threadpool(view.nodes_view, hermes_root=_root())
    return _response(data)


async def _attempts(request: Request) -> JSONResponse:
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(int(raw_limit), 200))
    except ValueError:
        limit = 50
    task_id = request.query_params.get("task_id", "")[:128]
    data = await run_in_threadpool(
        view.attempts_view,
        hermes_root=_root(),
        limit=limit,
        task_id=task_id,
    )
    return _response(data)


async def _attempt_detail(request: Request) -> JSONResponse:
    attempt_id = request.path_params.get("attempt_id", "")
    if not isinstance(attempt_id, str) or not fabric._ID_RE.fullmatch(attempt_id):
        return ui_security.err("FABRIC_ATTEMPT_INVALID", "Fabric attempt id is invalid.", status_code=400)
    data = await run_in_threadpool(view.attempt_detail, attempt_id, hermes_root=_root())
    status = 404 if data.get("code") == "FABRIC_ATTEMPT_NOT_FOUND" else 200
    return _response(data, status=status)


async def _routing(request: Request) -> JSONResponse:
    raw_limit = request.query_params.get("limit", "50")
    try:
        limit = max(1, min(int(raw_limit), 200))
    except ValueError:
        limit = 50
    rows = await run_in_threadpool(
        view.routing_decisions_view,
        hermes_root=_root(),
        limit=limit,
    )
    return _response(
        {
            "schema": view.VIEW_SCHEMA,
            "success": True,
            "available": True,
            "decisions": rows,
            "count": len(rows),
        }
    )


def ui_fabric_routes() -> list[Route]:
    """Compose Fabric GET-only routes. No mutation route exists in this module."""
    return [
        Route("/api/ops/fabric/nodes", _nodes, methods=["GET"]),
        Route("/api/ops/fabric/attempts", _attempts, methods=["GET"]),
        Route("/api/ops/fabric/attempts/{attempt_id}", _attempt_detail, methods=["GET"]),
        Route("/api/ops/fabric/routing", _routing, methods=["GET"]),
    ]


__all__ = ["ui_fabric_routes"]
