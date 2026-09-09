"""FastAPI application entrypoint.

Run locally:
    uv run uvicorn app.main:app --reload --app-dir apps/api/src

ONE service, ONE origin: the MCP transport mounts at /mcp, the REST twin at
/api, and the built web app (the embed host) at / — so trust chrome resolves
against the platform same-origin and CORS never enters the picture.
"""

from __future__ import annotations

import anyio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api.routes import api_router
from .config import get_settings
from .mcp import auth, store
from .mcp.server import _http_transport, mcp
from . import web_auth

log = logging.getLogger(__name__)

# Paths that never ask for a login. Everything else is gated by the web login
# (SessionGate) or the MCP transport's own OAuth. The SPA host at "/" is exempt
# in SessionGate below: its shell and signed-out landing page carry no data, and
# embeds live at that host.
_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/resolve/",
    "/api/food-security/rag/document/",
    # Raster tiles: the hazard_map embed's raster option must load for anonymous
    # visitors, like the resolver it rides on.
    "/api/raster/",
    # The web app's static code and favicon: the UI is public, the data it
    # calls is not.
    "/assets/",
    "/favicon.ico",
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
                     f"no endpoint at {scope.get('path')!r} — the MCP transport is "
                     "POST /mcp; the REST twin is under /api."),
                 "mcp": "/mcp"},
                status_code=404,
            )(scope, receive, send)
            return
        await self.static(scope, receive, send)


# /mcp, /auth/* and /.well-known/* always pass the gate: /mcp has its own
# transport-level OAuth, and /auth/* and /.well-known/* are how a login is
# started and discovered. _PUBLIC_PREFIXES, the SPA host at "/" and /assets/*
# are the public-by-design exceptions.


def _header(scope: Scope, name: bytes) -> bytes:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v
    return b""


class SessionGate:
    """Requires a session cookie for everything except the public set: the
    always-open prefixes and SPA assets above, /mcp (the transport's own OAuth),
    /auth/* and /.well-known/* (how a login starts), and the SPA host at "/",
    whose shell and signed-out landing page carry no data.

    Raw ASGI, so the MCP transport keeps streaming. Gated UI paths (GET/HEAD)
    redirect to /auth/login; /api and everything else gets a 401.
    """

    def __init__(self, app: ASGIApp, web_auth) -> None:
        self.app, self._web_auth = app, web_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and not await self._authorized(scope):
            path = scope.get("path", "")
            if scope.get("method") in ("GET", "HEAD") and not path.startswith("/api"):
                location = f"/auth/login?next={quote(path)}"
                await RedirectResponse(location, status_code=302)(scope, receive, send)
            else:
                await JSONResponse({"detail": "login required"},
                                   status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _authorized(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        if path.startswith(_PUBLIC_PREFIXES):
            return True
        if path.startswith(("/mcp", "/auth/", "/.well-known/")):
            return True
        method = scope.get("method")
        # The SPA host: anonymous GETs render the shell and its signed-out
        # landing page; only the /api data behind it stays gated.
        if method in ("GET", "HEAD") and path in ("/", "/index.html"):
            return True
        sid = self._session_cookie(scope)
        if not sid:
            return False
        # session_for may refresh via AuthKit; keep the event loop free.
        return await anyio.to_thread.run_sync(self._web_auth.session_for, sid) is not None

    def _session_cookie(self, scope: Scope) -> str | None:
        raw = _header(scope, b"cookie").decode("latin-1")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(web_auth.SESSION_COOKIE + "="):
                return part.split("=", 1)[1] or None
        return None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """The MCP session manager must be running or the transport answers nothing.
    Starlette does not run a MOUNTED app's lifespan, so the MCP app's own lifespan is
    chained here rather than assumed."""
    store.init()
    async with app.state.mcp_app.lifespan(app):
        yield


def create_app() -> FastAPI:
    """Build the app: CORS, the /api router, the /mcp transport, and the web app."""
    settings = get_settings()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)

    # Standard-app login (web_auth.py): built only when the OAuth switch and a
    # client id are set, so an MCP-only deployment is untouched. The gate below
    # and the /auth routes key off this same instance.
    if settings.grp_oauth_enabled and settings.grp_authkit_client_id.strip():
        app.state.web_auth = web_auth.WebAuth(
            authkit_domain=settings.grp_authkit_domain,
            public_url=settings.grp_public_url,
            client_id=settings.grp_authkit_client_id)
    else:
        app.state.web_auth = None

    # ONE MCP app, carried on the app so the lifespan can reach it: http_app()
    # builds a new session manager per call, so the mounted object must be the
    # one _lifespan starts. path="/" because the mount supplies the "/mcp"
    # prefix; the rest of its HTTP behaviour and its auth provider come from
    # _http_transport(), shared with the standalone `--http` entry point.
    app.state.mcp_app = mcp.http_app(path="/", **_http_transport())

    # Middleware nests in reverse of add order: McpPathNormalize -> CORS ->
    # <auth gate> -> router. CORS must sit outside the gate, or preflights get a
    # bare 401 and no cross-origin browser can reach a gated endpoint.
    if app.state.web_auth is not None:
        # /mcp stays with the transport's own OAuth (never gate it here); /auth/*
        # and /.well-known/* stay open because a client reads them precisely when
        # it has no session yet.
        app.add_middleware(SessionGate, web_auth=app.state.web_auth)
    else:
        log.warning("web login off — the standard app is served WITHOUT "
                    "authentication. Acceptable locally; the deployment refuses "
                    "to boot this way unless GRP_ALLOW_ANONYMOUS=1.")

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

    # The login routes, built above whenever web auth is configured. Added
    # before the "/" mount below so a static catch-all cannot swallow /auth/*,
    # and they must stay reachable without a session.
    if app.state.web_auth is not None:
        app.state.web_auth.add_routes(app)

    # OAuth discovery, when the switch is on. These belong to the ORIGIN ROOT,
    # not the /mcp mount (see auth.well_known_routes), and must be reachable with
    # no credential. Added before the static mount so "/" cannot swallow them.
    app.router.routes.extend(auth.well_known_routes(mcp.auth))

    @app.get("/api")
    def api_root() -> dict:
        return {"service": settings.app_name, "mcp": "/mcp", "docs": "/docs"}

    # The MCP ASGI app: the transport plus (once configured) its auth middleware
    # and discovery routes.
    app.mount("/mcp", app.state.mcp_app)

    # Mounted LAST so /api and /mcp win; html=True serves index.html at "/".
    # An empty GRP_WEB_DIST must mean "no web build": Path("") is the current
    # directory and is_dir() is True, so it would mount the repo as static files.
    web_dist = os.environ.get("GRP_WEB_DIST", "").strip()
    if web_dist and Path(web_dist).is_dir():
        app.mount("/", StaticOrNotFound(app, StaticFiles(directory=web_dist, html=True)),
                  name="web")
    else:
        @app.get("/")
        def root() -> dict:
            return {"service": settings.app_name, "mcp": "/mcp", "docs": "/docs"}

    return app


app = create_app()
