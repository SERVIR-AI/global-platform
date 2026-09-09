"""Login for the STANDARD app (web UI + REST) through AuthKit hosted login.

Here the server is an OAuth CLIENT, not a resource server: it sends people to
AuthKit's hosted page, receives them back with a one-time code, exchanges it for
tokens, and keeps a browser session. `mcp/auth.py` is the other role (resource
server) and talks to the same AuthKit domain; the two do not share code.

Activated only when the OAuth master switch AND a client id are configured
(grp_oauth_enabled + grp_authkit_client_id). The client is a PKCE public client
— there is deliberately no client secret. With either unset, no routes are
mounted and the app behaves exactly as it did before any of this.

All AuthKit interaction lives HERE behind a small surface — issue a login URL,
handle a callback code, read/end a session — so the gate (a later commit) and
any future swap (managed sessions, M2M tokens) touch only this module.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

log = logging.getLogger(__name__)

# The scopes the login asks for. offline_access is what makes a refresh token
# come back, so the session can renew itself instead of dying with the access
# token. App-specific scopes are deliberately absent.
SCOPE = "openid profile email offline_access"

SESSION_COOKIE = "grp_session"
# How long a half-finished login (state + PKCE verifier) is kept before it can
# no longer complete. Longer than a human needs to click through AuthKit.
_LOGIN_TTL = 900

_HTTP_TIMEOUT = 15


class WebAuthError(Exception):
    """AuthKit refused the exchange (bad code, expired token, network)."""


def _token_request(url: str, form: dict) -> dict:
    """POST an OAuth form to the AuthKit token endpoint and return the JSON.

    Module-level so tests can stub it; everything that talks to AuthKit goes
    through here or _userinfo_request.
    """
    try:
        r = requests.post(url, data=form, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise WebAuthError(f"token request failed: {e}") from e
    if r.status_code >= 400:
        raise WebAuthError(
            f"token endpoint refused ({r.status_code}): {r.text[:300]}")
    return r.json()


def _userinfo_request(url: str, access_token: str) -> dict:
    """Ask AuthKit who an access token belongs to."""
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"},
                         timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise WebAuthError(f"userinfo failed: {e}") from e


def _pkce_pair() -> tuple[str, str]:
    """A code_verifier and its S256 code_challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class Session:
    """A logged-in browser session: the AuthKit tokens plus who they belong to."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    sub: str | None
    email: str | None
    created: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class SessionStore:
    """In-memory sessions.

    The deployment runs exactly ONE instance, so a process-local dict is
    correct. A redeploy logs everyone out — accepted for now; swapping this
    store for a persistent one is the documented cheap upgrade.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def put(self, sid: str, session: Session) -> None:
        with self._lock:
            self._sessions[sid] = session

    def get(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)


class WebAuth:
    """Routes and helpers for the standard-app login.

    Constructed once in create_app() when configured; the routes it adds are the
    only place the app talks to AuthKit as a client.
    """

    def __init__(self, *, authkit_domain: str, public_url: str,
                 client_id: str) -> None:
        self._domain = authkit_domain.rstrip("/")
        self._client_id = client_id
        # Secure cookies only when the public URL is https: the same image runs
        # over http locally and https behind Cloud Run, and a Secure cookie set
        # on http would silently never be sent back.
        self._secure_cookie = public_url.startswith("https://")
        self._redirect_uri = f"{public_url.rstrip('/')}/auth/callback"
        self._authorize_url = f"{self._domain}/oauth2/authorize"
        self._token_url = f"{self._domain}/oauth2/token"
        self._userinfo_url = f"{self._domain}/oauth2/userinfo"

        self.sessions = SessionStore()
        # state -> (code_verifier, expires_at, next_path) for logins in flight.
        self._pending: dict[str, tuple[str, float, str]] = {}
        self._lock = threading.Lock()

    # -- the routes ---------------------------------------------------------

    def add_routes(self, app: FastAPI) -> None:
        app.add_api_route("/auth/login", self.login, methods=["GET"])
        app.add_api_route("/auth/callback", self.callback, methods=["GET"])
        app.add_api_route("/auth/logout", self.logout, methods=["POST"])

    def login(self, request: Request) -> RedirectResponse:
        """Send the visitor to AuthKit's hosted login."""
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        next_path = request.query_params.get("next", "/")
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        with self._lock:
            self._pending[state] = (verifier, time.time() + _LOGIN_TTL, next_path)
        url = f"{self._authorize_url}?{urlencode({
            'response_type': 'code',
            'client_id': self._client_id,
            'redirect_uri': self._redirect_uri,
            'scope': SCOPE,
            'state': state,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })}"
        return RedirectResponse(url, status_code=302)

    def callback(self, request: Request):
        """AuthKit returns the user here with a one-time code; make a session."""
        params = request.query_params
        if params.get("error"):
            # The user declined or AuthKit refused; no session. Send them home.
            return RedirectResponse(params.get("next", "/"), status_code=302)
        state, code = params.get("state"), params.get("code")
        if not state or not code:
            return JSONResponse({"detail": "missing state or code"}, status_code=400)

        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending[1] < time.time():
            return JSONResponse({"detail": "unknown or expired login"},
                                status_code=400)
        verifier, _expires, next_path = pending

        try:
            tokens = _token_request(self._token_url, {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "client_id": self._client_id,
                "code_verifier": verifier,
            })
            profile = _userinfo_request(self._userinfo_url,
                                        tokens["access_token"])
        except WebAuthError as e:
            log.warning("login failed: %s", e)
            return JSONResponse({"detail": "login failed"}, status_code=502)

        sid = secrets.token_urlsafe(32)
        session = Session(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token"),
            expires_at=time.time() + int(tokens.get("expires_in", 3600)),
            sub=profile.get("sub"),
            email=profile.get("email"),
        )
        self.sessions.put(sid, session)
        log.info("logged in %s", session.email or session.sub or "?")
        response = RedirectResponse(next_path, status_code=302)
        response.set_cookie(SESSION_COOKIE, sid, httponly=True,
                            samesite="lax", secure=self._secure_cookie,
                            path="/")
        return response

    def logout(self, request: Request) -> RedirectResponse:
        """End the local session. (AuthKit's own session is untouched, so the
        next login is a click rather than a password.)"""
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            self.sessions.delete(sid)
        response = RedirectResponse("/", status_code=302)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- what a request is worth ---------------------------------------------

    def get_session(self, request: Request) -> Session | None:
        """The session behind this request's cookie, refreshing it if needed.

        Used by logout today and by the gate that gates the app in the next
        commit; nothing else in the app should read the cookie directly.
        """
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        session = self.sessions.get(sid)
        if session is None:
            return None
        if session.expired and self._maybe_refresh(session):
            self.sessions.put(sid, session)  # refresh updated the record in place
        return session if not session.expired else None

    def _maybe_refresh(self, session: Session) -> bool:
        """Use the refresh token to renew an expired session. False if it cannot."""
        if not session.refresh_token:
            return False
        try:
            tokens = _token_request(self._token_url, {
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
                "client_id": self._client_id,
            })
        except WebAuthError as e:
            log.info("session refresh failed: %s", e)
            return False
        session.access_token = tokens["access_token"]
        session.refresh_token = tokens.get("refresh_token") or session.refresh_token
        session.expires_at = time.time() + int(tokens.get("expires_in", 3600))
        return True
