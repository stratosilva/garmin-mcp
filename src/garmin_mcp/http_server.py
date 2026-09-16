"""Streamable HTTP entrypoint for the Garmin MCP server, with bearer auth.

Responsibilities (kept deliberately small and robust):
- Refuse to start unless ``MCP_ACCESS_TOKEN`` is set (never serve unprotected).
- Bind ``0.0.0.0`` on the port Railway injects via ``$PORT`` (fallback 8000).
- Serve the MCP protocol at ``/mcp`` (bearer-protected) and an open ``/healthz``
  for the Railway health check.

The MCP application itself (Garmin login + tool registration) is built by
``garmin_mcp.build_app``. Binding/port is controlled here via uvicorn, not via
FastMCP settings, so ``$PORT`` is always respected.
"""

import os
import sys

from starlette.responses import JSONResponse
from starlette.routing import Route

from garmin_mcp import build_app
from garmin_mcp.auth import BearerAuthMiddleware

MIN_TOKEN_LEN = 16


async def _healthz(_request):
    return JSONResponse({"status": "ok", "service": "garmin-mcp"})


def build_asgi():
    """Build the fully wrapped ASGI app (auth + health + MCP). Returns the app."""
    token = os.environ.get("MCP_ACCESS_TOKEN", "")
    if len(token) < MIN_TOKEN_LEN:
        sys.stderr.write(
            "FATAL: MCP_ACCESS_TOKEN is missing or shorter than "
            f"{MIN_TOKEN_LEN} chars. Refusing to start so the MCP endpoint is "
            "never exposed without authentication.\n"
        )
        raise SystemExit(1)

    mcp_path = os.environ.get("GARMIN_MCP_PATH", "/mcp")

    # Build the Garmin-configured FastMCP app and its Streamable HTTP ASGI app.
    mcp = build_app(streamable_http_path=mcp_path)
    asgi = mcp.streamable_http_app()

    # Open health route for Railway (added before auth wrapping so it stays open).
    asgi.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))

    # Only this synthetic, self-contained demo is public. Personal APIs stay protected.
    from garmin_mcp.demo import demo_page
    asgi.router.routes.append(Route("/demo", demo_page, methods=["GET"]))

    # Live dashboard (/dashboard + /api/dashboard), bearer-protected like /mcp.
    try:
        from garmin_mcp import get_client
        from garmin_mcp.dashboard import add_dashboard_routes

        add_dashboard_routes(asgi, get_client())
    except Exception as e:  # noqa: BLE001 - dashboard is optional, never block startup
        print(f"garmin-mcp: dashboard routes not added: {e}")

    return BearerAuthMiddleware(asgi, token=token, exempt_paths=("/healthz", "/favicon.ico", "/demo"))


def serve():
    import uvicorn

    host = os.environ.get("GARMIN_MCP_HOST", "0.0.0.0")
    # Railway injects $PORT; fall back to GARMIN_MCP_PORT then 8000.
    port_str = os.environ.get("PORT") or os.environ.get("GARMIN_MCP_PORT") or "8000"
    try:
        port = int(port_str)
    except ValueError:
        port = 8000

    app = build_asgi()
    print(f"garmin-mcp: serving Streamable HTTP on {host}:{port} "
          f"(MCP at {os.environ.get('GARMIN_MCP_PATH', '/mcp')}, health at /healthz)")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    serve()
