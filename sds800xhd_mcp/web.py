"""Minimal web UI and JSON API for the connection parameters."""

from __future__ import annotations

from pathlib import Path

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from .scope import Scope, ScopeError
from .tools import capture_png

STATIC = Path(__file__).parent / "static"


def register_web(mcp: MCPServer, scope: Scope, mcp_path: str) -> None:
    def error(msg: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)

    async def json_body(request: Request) -> dict:
        # Accept JSON only: this way foreign web pages cannot trigger changes
        # without a CORS preflight.
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise ValueError("Content-Type application/json expected")
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def state() -> dict:
        return {"ok": True, "config": scope.store.get().to_dict(), "status": scope.status(), "mcp_path": mcp_path}

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/state", methods=["GET"], include_in_schema=False)
    async def get_state(request: Request) -> Response:
        return JSONResponse(state())

    @mcp.custom_route("/api/config", methods=["POST"], include_in_schema=False)
    async def save_config(request: Request) -> Response:
        try:
            data = await json_body(request)
            connect = bool(data.pop("connect", False))
            cfg = scope.store.update(data)
        except ValueError as exc:
            return error(str(exc))
        if connect:
            try:
                await anyio.to_thread.run_sync(lambda: scope.connect(cfg))
            except ScopeError as exc:
                return JSONResponse({**state(), "ok": False, "error": str(exc)})
        return JSONResponse(state())

    @mcp.custom_route("/api/connect", methods=["POST"], include_in_schema=False)
    async def connect(request: Request) -> Response:
        try:
            await json_body(request)
            await anyio.to_thread.run_sync(scope.connect)
        except (ValueError, ScopeError) as exc:
            return JSONResponse({**state(), "ok": False, "error": str(exc)})
        return JSONResponse(state())

    @mcp.custom_route("/api/disconnect", methods=["POST"], include_in_schema=False)
    async def disconnect(request: Request) -> Response:
        try:
            await json_body(request)
        except ValueError as exc:
            return error(str(exc))
        await anyio.to_thread.run_sync(scope.disconnect)
        return JSONResponse(state())

    @mcp.custom_route("/api/screenshot", methods=["GET"], include_in_schema=False)
    async def screenshot(request: Request) -> Response:
        try:
            png = await anyio.to_thread.run_sync(lambda: capture_png(scope))
        except ScopeError as exc:
            return error(str(exc), 503)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
