"""OAuth 2.1 on the MCP transport: what the server refuses, what it publishes, and
what stays open regardless.

Each test builds its own app with its own settings, because the switch is read once
at startup and the defaults in config.py deliberately name no provider.
"""
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from app.config import get_settings
from app.main import create_app
from app.mcp.server import _http_transport, mcp

AUTHKIT = "https://example-staging.authkit.app"
ORIGIN = "http://127.0.0.1:8001"
RESOURCE = f"{ORIGIN}/mcp"
DOC_PATH = "/.well-known/oauth-protected-resource/mcp"

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def server(monkeypatch):
    """Start an app with OAuth on or off. (No client id: web login is off, so the
    session gate is not added and the standard app stays open — this module tests
    the MCP transport.)"""
    @contextmanager
    def _server(*, oauth: bool):
        settings = get_settings()
        monkeypatch.setattr(settings, "grp_oauth_enabled", oauth)
        monkeypatch.setattr(settings, "grp_authkit_domain", AUTHKIT if oauth else "")
        monkeypatch.setattr(settings, "grp_public_url", ORIGIN if oauth else "")
        monkeypatch.setattr(settings, "grp_authkit_client_id", "")
        with TestClient(create_app()) as client:
            yield client

    yield _server
    mcp.auth = None


@pytest.fixture
def oauth_settings(monkeypatch):
    """The settings a configured server reads, without building one."""
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", True)
    monkeypatch.setattr(settings, "grp_authkit_domain", AUTHKIT)
    monkeypatch.setattr(settings, "grp_public_url", ORIGIN)
    yield settings
    mcp.auth = None


def test_the_switch_off_leaves_the_transport_exactly_as_it_was(server):
    """Off means off: anonymous callers still get the tools and nothing is published."""
    with server(oauth=False) as client:
        listed = client.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)
        assert listed.status_code == 200
        assert client.get(DOC_PATH).status_code == 404


def test_anonymous_is_refused_and_told_where_to_log_in(server):
    """A 401 whose WWW-Authenticate names a document that does not resolve is worse
    than no 401 at all, so follow the pointer — and it must point at the resource-
    path document, the one RFC 9728 puts the metadata under."""
    with server(oauth=True) as client:
        refused = client.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)
        assert refused.status_code == 401
        challenge = refused.headers["WWW-Authenticate"]
        assert "resource_metadata=" in challenge

        pointer = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        assert urlparse(pointer).path == DOC_PATH
        followed = client.get(urlparse(pointer).path)
        assert followed.status_code == 200
        assert followed.json()["resource"] == RESOURCE


def test_the_token_checker_and_the_metadata_agree_on_our_name(server):
    """The audience the transport verifies and the resource the document advertises
    are the same name, and that name is the MOUNT-FUL one, <origin>/mcp — never the
    bare origin the transport is literally built at (main.py mounts it with path="/").
    ResourceServer pins the resource URL to MCP_PATH at the provider, so this holds
    whatever order the transport and the discovery routes are built in."""
    with server(oauth=True) as client:
        assert str(mcp.auth.token_verifier.audience) == RESOURCE
        assert client.get(DOC_PATH).json()["resource"] == RESOURCE


def test_the_advertised_issuer_carries_no_trailing_slash(server):
    """The client compares this against the provider's published issuer with `!=`,
    and AuthKit publishes it bare. One character kills the login."""
    with server(oauth=True) as client:
        assert client.get(DOC_PATH).json()["authorization_servers"] == [AUTHKIT]


def test_the_document_lives_under_the_resource_path_only(server):
    """RFC 9728 puts the document at the well-known prefix plus the resource path,
    and the 401 points there. The bare /.well-known/oauth-protected-resource would
    describe the origin ROOT, which this server does not protect, so it is not
    served: no document may advertise a resource the transport does not check."""
    with server(oauth=True) as client:
        under_resource = client.get(DOC_PATH)
        assert under_resource.status_code == 200
        assert under_resource.json()["resource"] == RESOURCE
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_mcp_oauth_does_not_gate_the_standard_app_without_web_login(server):
    """MCP-only OAuth (no web client id) leaves the standard app open: the session
    gate is added only when the web login is configured, and /mcp stays on the
    transport's own gate either way."""
    with server(oauth=True) as client:
        refused = client.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)
        assert refused.status_code == 401
        assert "resource_metadata=" in refused.headers["WWW-Authenticate"]
        assert client.get("/api").status_code == 200
        assert client.get("/api/health").status_code == 200


def test_the_resolver_stays_public_with_oauth_on(server):
    """A receipt nobody can resolve attests nothing. 404 is a pass for an id that
    does not exist: the route answered without asking for a credential."""
    with server(oauth=True) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/resolve/receipt/0000000000000000").status_code in (200, 404)


def test_oauth_without_a_provider_refuses_to_start(monkeypatch):
    """Half-configured would publish discovery documents pointing nowhere, and every
    client would fail at the login step."""
    settings = get_settings()
    monkeypatch.setattr(settings, "grp_oauth_enabled", True)
    monkeypatch.setattr(settings, "grp_authkit_domain", "")
    monkeypatch.setattr(settings, "grp_public_url", ORIGIN)
    with pytest.raises(RuntimeError, match="GRP_AUTHKIT_DOMAIN"):
        create_app()


def test_a_good_token_gets_in_and_every_wrong_one_does_not(server):
    """A verifier that refuses everything is indistinguishable from a working one
    until something valid gets through, so mint four tokens and check all four.

    Only the KEYS are swapped, for a local pair, because the real JWKS is a network
    fetch. The issuer and audience being checked are the ones the server configured.
    The wrong-audience token carries <origin>/, the bare-origin form of the resource
    URL: the audience is pinned to the mount-ful name, so the bare-origin form must
    be refused."""
    signer, forger = RSAKeyPair.generate(), RSAKeyPair.generate()
    with server(oauth=True) as client:
        mcp.auth.token_verifier.public_key = signer.public_key

        def present(token: str) -> int:
            return client.post("/mcp", json=LIST_TOOLS,
                               headers={**ACCEPT, "Authorization": f"Bearer {token}"}
                               ).status_code

        assert present(signer.create_token(issuer=AUTHKIT, audience=RESOURCE)) == 200
        assert present(signer.create_token(issuer="https://evil.example",
                                           audience=RESOURCE)) == 401
        assert present(signer.create_token(issuer=AUTHKIT, audience=f"{ORIGIN}/")) == 401
        assert present(forger.create_token(issuer=AUTHKIT, audience=RESOURCE)) == 401


def test_the_standalone_http_entry_point_enforces_and_publishes_the_same_document(oauth_settings):
    """`python -m app.mcp.server --http` builds its own transport from
    _http_transport(), the same builder uvicorn's path uses. Attach the provider
    anywhere else and the switch is inert on whichever entry point was missed, which
    serves every tool to anyone who finds the port.

    The standalone serves discovery from routes the transport itself embeds — there
    is no outer app to mount corrected ones — so the document it publishes must be
    the same corrected one the FastAPI surface serves: bare authorization-server URL
    and the mount-ful resource. The library's own document carries the trailing
    slash, which a client compares against AuthKit's bare issuer with `!=` and dies
    on before a browser opens."""
    mcp.auth = None
    standalone = mcp.http_app(**_http_transport())   # what run(transport="http") builds
    assert mcp.auth is not None
    with TestClient(standalone) as client:
        refused = client.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)
        assert refused.status_code == 401
        challenge = refused.headers["WWW-Authenticate"]
        assert "resource_metadata=" in challenge

        pointer = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        doc = client.get(urlparse(pointer).path)
        assert doc.status_code == 200
        body = doc.json()
        assert body["resource"] == RESOURCE
        assert body["authorization_servers"] == [AUTHKIT]
        assert body["bearer_methods_supported"] == ["header"]
