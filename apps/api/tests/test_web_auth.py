"""Standard-app login (web_auth.py): the routes exist only when configured, the
login sends the visitor to AuthKit with PKCE, a good callback makes a session,
and a bad one is refused.

AuthKit is stubbed at the two network functions (_token_request,
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
    """An app with web login configured and AuthKit's network calls stubbed."""
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
    """Without a client id no login routes exist."""
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
    """The page a finished login lands on ("/") must answer. An unset
    GRP_WEB_DIST must mean "no web build": Path("").is_dir() is True, so an
    unset value used to mount the working directory as static files and 404 at
    "/". Empty must serve the JSON home instead."""
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


# --- the session gate -----------------------------------------------------


def test_gate_serves_the_public_shell_but_gates_the_data(app):
    """With web login on, the SPA shell at "/" is public (it carries no data, and
    an anonymous visitor must be able to reach the signed-out landing page); /api
    data, the MCP transport's own gate and the login routes stay as they are."""
    a = app()
    with TestClient(a, follow_redirects=False) as client:
        # The SPA shell renders for an anonymous visitor (the frontend then
        # shows its signed-out state once /auth/me answers 401).
        assert client.get("/").status_code == 200
        # Any other UI path an anonymous visitor asks for still goes to the
        # login page, remembering where they were headed.
        r = client.get("/docs")
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
        # The shell is public and the API is not; a session opens both.
        assert client.get("/").status_code == 200
        assert client.get("/api").status_code == 401
        client.cookies.set(SESSION_COOKIE, sid)
        assert client.get("/").status_code == 200
        assert client.get("/api").status_code == 200


# --- the web UI's /auth surface -------------------------------------------


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


def test_gate_leaves_public_embeds_and_assets_open(app):
    """An anonymous visitor can load the SPA host (embeds and the signed-out
    landing page both live at "/"), its static code and the public API set;
    only /api data stays gated."""
    with TestClient(app(), follow_redirects=False) as client:
        embed = client.get("/?embed=provenance_graph&receipt_id=1")
        assert embed.status_code not in (302, 401)           # public: renders
        for url in ("/assets/index-abc123.js", "/favicon.ico", "/", "/index.html",
                    "/runbook", "/runbook/"):
            assert client.get(url).status_code not in (302, 401)   # public surfaces
        assert client.get("/api/chat").status_code == 401    # data still gated
