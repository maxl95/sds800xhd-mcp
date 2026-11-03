"""Entry point: MCP (Streamable HTTP) at /mcp and web UI at /."""

from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import ConfigStore
from .scope import Scope, ScopeError
from .tools import register_tools
from .web import register_web

MCP_PATH = "/mcp"

INSTRUCTIONS = """\
Controls a Siglent SDS800X HD oscilloscope (4 channels, 12 bit) via SCPI.
The connection is configured in the web UI and established automatically when needed.
Typical flow: read get_acquisition_status / get_channel, adjust with configure_*,
evaluate with measure or get_waveform, use screenshot for visual checks.
For functions not covered, use scpi_query / scpi_command (SDS800X HD Programming Guide).
"""


def build_app(data_dir: Path, bind_host: str):
    store = ConfigStore(data_dir / "config.json")
    scope = Scope(store)
    mcp = MCPServer("siglent-sds800xhd", version=__version__, instructions=INSTRUCTIONS)
    register_tools(mcp, scope)
    register_web(mcp, scope, MCP_PATH)
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=bind_host,
    )
    return app, scope


def _auto_connect(scope: Scope) -> None:
    cfg = scope.store.get()
    if not (cfg.auto_connect and cfg.resource):
        return
    try:
        scope.connect(cfg)
    except ScopeError as exc:
        logging.getLogger(__name__).warning("%s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP MCP server for the Siglent SDS800X HD")
    parser.add_argument("--host", default=os.environ.get("SDS_MCP_HOST", "127.0.0.1"),
                        help="Bind address (default 127.0.0.1, 0.0.0.0 for network access)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SDS_MCP_PORT", "8800")),
                        help="HTTP port (default 8800)")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("SDS_MCP_DATA_DIR", "data")),
                        help="Directory for config.json (default ./data)")
    parser.add_argument("--log-level", default=os.environ.get("SDS_MCP_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app, scope = build_app(args.data_dir.resolve(), args.host)
    threading.Thread(target=_auto_connect, args=(scope,), daemon=True).start()

    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    print(f"Web UI:        http://{shown}:{args.port}/")
    print(f"MCP endpoint:  http://{shown}:{args.port}{MCP_PATH}")
    print(f"Configuration: {args.data_dir.resolve() / 'config.json'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
