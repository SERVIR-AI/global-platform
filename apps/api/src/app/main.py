"""FastAPI application entrypoint.

Run locally:
    uv run uvicorn app.main:app --reload --app-dir apps/api/src

ONE service, ONE origin: the MCP transport mounts at /mcp, the REST twin at
/api, and the built web app (the embed host) at / — so trust chrome resolves
against the platform same-origin and CORS never enters the picture.
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api.routes import api_router
from .config import get_settings
from .mcp import store
from .mcp.server import mcp

log = logging.getLogger(__name__)

# Reachable WITHOUT a token, by design: a receipt nobody can resolve attests
# nothing, so the resolver and the evidence it points at stay open at every
# tier. The token gates the TOOLS, which cost money to run.
_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/resolve/",
    "/api/food-security/rag/document/",
)


class McpPathNormalize:
    """Serve /mcp as well as /mcp/. A Mount matches only the trailing-slash form,
    and the 307 the router issues instead is a redirect not every MCP client
    follows on a POST."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


class StaticOrNotFound:
    """Serve the web app for GET/HEAD; anything else on an unmatched path is a
    404 that says where the MCP endpoint is.

    Without this, StaticFiles answers every non-GET with 405 Method Not Allowed —
    so a client POSTing to the wrong path (mcp-remote probing /sse, say) is told
    its METHOD is wrong rather than its PATH, and goes looking for a transport
    bug that isn't there. Cost a real user an afternoon."""

    def __init__(self, app: ASGIApp, static: ASGIApp) -> None:
        self.app, self.static = app, static

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") not in ("GET", "HEAD"):
            # Shaped as an OAuth error ({error, error_description}) ON PURPOSE.
            # MCP clients probe /register and /.well-known/* for OAuth before
            # falling back to a bearer header, and they parse the 404 body against
            # the OAuth error schema. A friendlier body fails that parse and the
            # client dies with "Invalid OAuth error response" instead of falling
            # back — which is exactly what a human-readable 404 caused here.
            await JSONResponse(
                {"error": "not_found",
                 "error_description": (
                     f"no endpoint at {scope.get('path')!r} — this server does not "
                     "implement OAuth. The MCP transport is POST /mcp with an "
                     "'Authorization: Bearer <token>' header; the REST twin is under /api."),
                 "mcp": "/mcp"},
                status_code=404,
            )(scope, receive, send)
            return
        await self.static(scope, receive, send)


class TokenGate:
    """Bearer/X-API-Key gate over the tools. Raw ASGI rather than
    BaseHTTPMiddleware so the MCP transport keeps streaming."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token = app, token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and not self._authorized(scope):
            await JSONResponse(
                {"status": "declined",
                 "note": "missing or invalid API token — send it as "
                         "'Authorization: Bearer <token>'. The receipt resolver "
                         "under /api/resolve/ needs no token."},
                status_code=401,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        if path.startswith(_PUBLIC_PREFIXES):
            return True
        if not path.startswith(("/api", "/mcp", "/docs", "/redoc", "/openapi.json")):
            return True
        # Compared as BYTES: a header carrying non-UTF-8 or non-ASCII must be a
        # 401, never a decode traceback turned into a 500.
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        presented = (headers.get(b"authorization", b"").removeprefix(b"Bearer ").strip()
                     or headers.get(b"x-api-key", b"").strip())
        return bool(presented) and secrets.compare_digest(presented, self.token)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """The MCP session manager must be running or the transport answers nothing."""
    store.init()
    async with mcp.session_manager.run():
        yield


def create_app() -> FastAPI:
    """Build the app: CORS, the /api router, the /mcp transport, and the web app."""
    settings = get_settings()
    token = os.environ.get("GRP_API_TOKEN", "").strip()

    # Publishing the schema of gated endpoints to anonymous callers defeats the
    # point of gating them, so the docs go away exactly when the gate goes up.
    docs = {} if not token else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan, **docs)

    # Middleware nests in REVERSE of add order, so this yields
    # McpPathNormalize -> CORS -> TokenGate -> router. CORS must sit OUTSIDE the
    # gate or preflights get a bare 401 and no browser can ever reach a gated
    # endpoint cross-origin.
    if token:
        app.add_middleware(TokenGate, token=token)
    else:
        log.warning("GRP_API_TOKEN unset — tools are served WITHOUT authentication. "
                    "Acceptable locally; deploy/entrypoint.sh refuses to start this way.")

    # A consuming app on ITS own origin must be able to resolve our receipts, so
    # '*' is a legitimate deployed value here. Credentials cannot ride a wildcard
    # origin (browsers reject that pairing) and the resolver needs none.
    wildcard = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(McpPathNormalize)

    app.include_router(api_router, prefix="/api")

    @app.get("/api")
    def api_root() -> dict:
        return {"service": settings.app_name, "mcp": "/mcp", "docs": "/docs"}

    # Forces session-manager creation. The RAW ASGI app is mounted rather than
    # FastMCP's Starlette wrapper so no inner router can redirect a tool call.
    mcp.streamable_http_app()
    app.mount("/mcp", StreamableHTTPASGIApp(mcp.session_manager))

    # Mounted LAST so /api and /mcp win; html=True serves index.html at "/".
    web_dist = Path(os.environ.get("GRP_WEB_DIST", ""))
    if web_dist.is_dir():
        app.mount("/", StaticOrNotFound(app, StaticFiles(directory=web_dist, html=True)),
                  name="web")
    else:
        @app.get("/")
        def root() -> dict:
            return {"service": settings.app_name, "mcp": "/mcp", "docs": "/docs"}

    return app


app = create_app()
