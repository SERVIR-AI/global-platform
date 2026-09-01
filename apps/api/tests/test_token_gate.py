"""The interim auth gate: GRP_API_TOKEN gates the tools; the trust surfaces
(resolver, archived originals, rasters, health, web/runbook) stay public —
a receipt nobody can resolve attests nothing."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("GRP_API_TOKEN", "test-secret-token")
    from app.main import create_app
    c = TestClient(create_app())
    # this file probes the gate itself: shed conftest's default auth header
    c.headers.pop("Authorization", None)
    return c


def test_no_token_refuses_to_boot(monkeypatch, log):
    """Local and deployed must match: an ungated instance may not exist."""
    # import BEFORE scrubbing: app.main builds a module-level app on first
    # import, which must succeed under the suite's armed test token
    from app.main import create_app
    monkeypatch.delenv("GRP_API_TOKEN", raising=False)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "grp_api_token", None)
    with pytest.raises(RuntimeError) as exc:
        create_app()
    log("OUTPUT", str(exc.value)[:80])
    assert "openssl rand -hex 32" in str(exc.value)          # the fix ships in the error


def test_tools_refuse_without_the_token(gated, log):
    r = gated.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18",
                                            "capabilities": {},
                                            "clientInfo": {"name": "x", "version": "0"}}})
    log("OUTPUT", f"{r.status_code} {r.json().get('note', '')[:60]}")
    assert r.status_code == 401
    assert "Bearer" in r.json()["note"]
    assert "/api/resolve/" in r.json()["note"]      # the 401 teaches the public path


def test_wrong_token_is_a_401_not_a_500(gated, log):
    r = gated.post("/mcp", headers={"Authorization": "Bearer nope"}, json={})
    log("OUTPUT", str(r.status_code))
    assert r.status_code == 401
    # (the non-ascii-header case is enforced server-side with a bytes compare;
    # httpx refuses to SEND such a header, so it cannot be exercised from here)


def test_right_token_passes_the_gate(gated, log):
    # /api is gated and needs no MCP lifespan: with the token it must reach the
    # endpoint (200), never bounce at the gate (401).
    assert gated.get("/api").status_code == 401
    r = gated.get("/api", headers={"Authorization": "Bearer test-secret-token"})
    log("OUTPUT", str(r.status_code))
    assert r.status_code == 200


def test_trust_surfaces_stay_public(gated, log):
    for path in ("/api/health", "/api/resolve/receipt/0000000000000000"):
        r = gated.get(path)
        log("OUTPUT", f"{path} -> {r.status_code}")
        assert r.status_code != 401                 # may 404; never gated


def test_docs_disappear_when_the_gate_is_up(gated, log):
    for path in ("/docs", "/openapi.json"):
        assert gated.get(path).status_code in (401, 404)
    log("CHECK", "schema not served to anonymous callers")
