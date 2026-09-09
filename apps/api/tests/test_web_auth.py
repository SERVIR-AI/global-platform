"""Standard-app login (web_auth.py): the routes exist only when configured, the
login sends the visitor to AuthKit with PKCE, a good callback makes a session,
and a bad one is refused.

AuthKit itself is stubbed at the two network functions (_token_request,
_userinfo_request); the flow under test is our side of the handshake.
"""
from __future__ import annotations

from time import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import get_settings
from app.main import create_app
from app.mcp.server import mcp
from app.web_auth import SESSION_COOKIE, Session
from app import web_auth as wa

AUTHKIT = "https://example-staging.authkit.app"
ORIGIN = "http://127.0.0.1:8001"
CLIENT = "client_test123"
CALLBACK = f"{ORIGIN}/auth/callback"

TOKENS = {"access_token": "at", "refresh_token": "rt",
          "expires_in": 3600, "token_type": "Bearer"}
PROFILE = {"sub": "usr_test", "email": "test@example.com"}


@pytest.fixture
def app(monkeypatch):
    """An app with web login configured, AuthKit's network calls stubbed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", True)
    monkeypatch.setattr(settings, "grp_authkit_domain", AUTHKIT)
    monkeypatch.setattr(settings, "grp_public_url", ORIGIN)
    monkeypatch.setattr(settings, "grp_authkit_client_id", CLIENT)

    def token_stub(url, form):
        assert url == f"{AUTHKIT}/oauth2/token"
        return dict(TOKENS)

    def userinfo_stub(url, token):
        assert url == f"{AUTHKIT}/oauth2/userinfo"
        assert token == TOKENS["access_token"]
        return dict(PROFILE)

    monkeypatch.setattr(wa, "_token_request", token_stub)
    monkeypatch.setattr(wa, "_userinfo_request", userinfo_stub)
    yield create_app
    mcp.auth = None


def _authorize(app, client) -> str:
    """GET /auth/login and return the AuthKit authorize URL we were sent to."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"]


def test_not_mounted_without_a_client_id(monkeypatch):
    """MCP-only configuration: no login routes exist, nothing else changes."""
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", False)
    monkeypatch.setattr(settings, "grp_authkit_client_id", CLIENT)
    with TestClient(create_app()) as client:
        assert client.get("/auth/login").status_code == 404
        assert client.get("/auth/callback").status_code == 404


def test_login_redirects_to_authkit_with_pkce(app):
    with TestClient(app(), follow_redirects=False) as client:
        url = _authorize(app, client)
    assert url.startswith(f"{AUTHKIT}/oauth2/authorize?")
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [CLIENT]
    assert q["redirect_uri"] == [CALLBACK]
    assert q["scope"] == ["openid profile email offline_access"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] and q["state"]


def test_a_good_callback_makes_a_session(app):
    a = app()
    with TestClient(a) as client:
        r = client.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = client.get(f"/auth/callback?state={state}&code=good",
                       follow_redirects=False)
        # Landed back at the app's home ("/") with a session cookie. The home
        # route itself is asserted separately (test_the_home_page...).
        assert r.status_code == 302
        sid = client.cookies.get(SESSION_COOKIE)
        assert sid
        session = a.state.web_auth.sessions.get(sid)
        assert session is not None
        assert session.email == "test@example.com"


def test_a_bad_callback_is_refused(app):
    with TestClient(app()) as client:
        assert client.get("/auth/callback").status_code == 400
        assert client.get("/auth/callback?state=nope&code=x").status_code == 400
        assert client.cookies.get(SESSION_COOKIE) is None


def test_logout_ends_the_session(app):
    a = app()
    with TestClient(a, follow_redirects=False) as client:
        r = client.get("/auth/login")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        client.get(f"/auth/callback?state={state}&code=good")
        assert client.cookies.get(SESSION_COOKIE)
        r = client.post("/auth/logout")
        assert r.status_code == 302
        assert a.state.web_auth.sessions.__dict__["_sessions"] == {}


def test_the_home_page_is_the_api_root_when_no_web_dist(monkeypatch):
    """The page a finished login lands on ("/") must answer.

    Path("").is_dir() is True (an empty path is the current directory), so an
    unset GRP_WEB_DIST used to mount the working directory as static files and
    404 at "/". Empty must mean "no web build": serve the JSON home instead.
    """
    monkeypatch.delenv("GRP_WEB_DIST", raising=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", False)
    with TestClient(create_app()) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["mcp"] == "/mcp"


def test_an_expired_session_refreshes_its_token(app, monkeypatch):
    a = app()
    web_auth = a.state.web_auth
    web_auth.sessions.put("s1", Session(access_token="old", refresh_token="rt",
                                        expires_at=time() - 10,
                                        sub="usr_test", email="e@x.com"))
    refreshed = {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}
    monkeypatch.setattr(wa, "_token_request",
                        lambda url, form: dict(refreshed))
    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [(b"cookie", f"{SESSION_COOKIE}=s1".encode())],
             "query_string": b""}
    session = web_auth.get_session(Request(scope))
    assert session is not None
    assert session.access_token == "new"
    assert session.expires_at > time()


# --- the session gate (the commit after the login routes) ------------------


def test_gate_refuses_anonymous_standard_app_access(app):
    """With web login on, the standard app is gated but the public set, the MCP
    transport's own gate and the login routes stay reachable."""
    a = app()
    with TestClient(a, follow_redirects=False) as client:
        # UI page -> off to the login page, remembering where we were headed.
        r = client.get("/")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/auth/login?next=")
        # REST -> a clean 401, not a redirect a program cannot follow.
        r = client.get("/api")
        assert r.status_code == 401
        assert r.json() == {"detail": "login required"}
        # The always-public set and the login routes are untouched.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/resolve/receipt/0000000000000000").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
        assert client.get("/auth/login").status_code == 302
        # /mcp belongs to the transport's OAuth gate, not this one.
        mcp_call = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"})
        assert mcp_call.status_code == 401
        assert "resource_metadata=" in mcp_call.headers.get("WWW-Authenticate", "")


def test_gate_lets_a_session_through(app):
    a = app()
    web_auth = a.state.web_auth
    sid = "s1"
    web_auth.sessions.put(sid, Session(access_token="at", refresh_token="rt",
                                       expires_at=time() + 3600,
                                       sub="usr_test", email="t@e.com"))
    with TestClient(a, follow_redirects=False) as client:
        r = client.get("/")
        assert r.status_code == 302                # no cookie yet: off to login
        assert "/auth/login" in r.headers["location"]
        client.cookies.set(SESSION_COOKIE, sid)
        assert client.get("/").status_code == 200
        assert client.get("/api").status_code == 200


# --- backend additions that make the frontend possible -----------------------


def test_auth_me_reports_the_session(app):
    a = app()
    web_auth = a.state.web_auth
    sid = "s1"
    web_auth.sessions.put(sid, Session(access_token="at", refresh_token="rt",
                                       expires_at=time() + 3600,
                                       sub="usr_test", email="t@e.com"))
    with TestClient(a) as client:
        assert client.get("/auth/me").status_code == 401     # anonymous
        client.cookies.set(SESSION_COOKIE, sid)
        r = client.get("/auth/me")
        assert r.status_code == 200
        assert r.json() == {"sub": "usr_test", "email": "t@e.com"}


def test_auth_me_is_404_when_web_login_is_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", False)
    with TestClient(create_app()) as client:
        assert client.get("/auth/me").status_code == 404     # routes not mounted


def test_login_forwards_prompt_to_authkit(app):
    with TestClient(app(), follow_redirects=False) as client:
        r = client.get("/auth/login?prompt=login")
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["prompt"] == ["login"]
