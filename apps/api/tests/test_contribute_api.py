"""POST /api/contribute — a hub's own server adding a source with no chat and no VPN."""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.mcp import store

TABLE_YAML = """
kind: table
dataset: api_test_damage
title: API test table
description: A tiny depth-damage table submitted over the REST API in a test.
source: test fixture
validation: unvalidated
license: CC0-1.0
vintage: "2026-09"
cadence: irregular
columns: {depth_m: Depth_m, residential: Residential}
units: fraction of replacement cost
pack: risk
csv_text: |
  Depth_m,Residential
  0.5,0.25
  1.0,0.40
"""


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds", raising=False)
    (tmp_path / "feeds").mkdir()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    return TestClient(app)


def test_yaml_body_lands_as_the_mcp_would(isolated):
    r = isolated.post("/api/contribute", content=TABLE_YAML,
                      headers={"content-type": "application/x-yaml"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "table"
    assert body["status"] in ("staged", "approved")
    assert body["contribution_id"]
    # the record is attributed to the dev-box operator, never anonymous
    rec = store.load_contribution(body["contribution_id"])
    assert rec["contributor_id"] != "anonymous"


def test_status_can_be_polled(isolated):
    cid = isolated.post("/api/contribute", content=TABLE_YAML.replace("api_test_damage", "api_test_poll"),
                        headers={"content-type": "application/x-yaml"}).json()["contribution_id"]
    r = isolated.get(f"/api/contribute/{cid}")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["contribution"]["contribution_id"] == cid


def test_multipart_file_works_too(isolated):
    r = isolated.post("/api/contribute",
                      files={"file": ("m.yml", TABLE_YAML.replace("api_test_damage", "api_test_file"), "application/x-yaml")})
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("staged", "approved")


def test_a_bad_manifest_is_declined_not_500(isolated):
    r = isolated.post("/api/contribute", content="layer: [unclosed", headers={"content-type": "text/plain"})
    assert r.status_code == 200
    assert r.json()["status"] == "declined"
    assert any("not valid YAML" in p for p in r.json()["problems"])


def test_kind_query_param_overrides(isolated):
    r = isolated.post("/api/contribute?kind=vector", content="layer: evacuation_centres\n",
                      headers={"content-type": "application/x-yaml"})
    assert r.json()["kind"] == "vector"
    assert r.json()["status"] == "declined"          # missing url etc., named


def test_oauth_on_requires_a_bearer_and_challenges(isolated, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_oauth_enabled", True)
    monkeypatch.setattr(s, "grp_public_url", "https://example.test")
    r = isolated.post("/api/contribute", content=TABLE_YAML, headers={"content-type": "application/x-yaml"})
    assert r.status_code == 401
    assert "oauth-protected-resource/mcp" in r.headers.get("www-authenticate", "")


def test_oauth_on_rejects_a_bad_bearer(isolated, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_oauth_enabled", True)
    monkeypatch.setattr(s, "grp_public_url", "https://example.test")

    class _Prov:
        async def verify_token(self, token):
            return None
    from app.mcp import auth as mcp_auth
    monkeypatch.setattr(mcp_auth, "provider", lambda: _Prov())
    r = isolated.post("/api/contribute", content=TABLE_YAML,
                      headers={"content-type": "application/x-yaml", "authorization": "Bearer nope"})
    assert r.status_code == 401


def test_oauth_on_accepts_a_verified_bearer_and_attributes_it(isolated, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_oauth_enabled", True)
    monkeypatch.setattr(s, "grp_public_url", "https://example.test")

    class _Tok:
        claims = {"sub": "user_TEST123", "email": "hub@example.test"}

    class _Prov:
        async def verify_token(self, token):
            return _Tok() if token == "good" else None
    from app.mcp import auth as mcp_auth
    monkeypatch.setattr(mcp_auth, "provider", lambda: _Prov())
    r = isolated.post("/api/contribute", content=TABLE_YAML.replace("api_test_damage", "api_test_bearer"),
                      headers={"content-type": "application/x-yaml", "authorization": "Bearer good"})
    assert r.status_code == 200, r.text
    rec = store.load_contribution(r.json()["contribution_id"])
    assert rec["contributor_id"] == "user_TEST123"
    assert rec["contributor_label"] == "hub@example.test"


def test_no_token_at_all_still_gets_the_challenge(monkeypatch):
    """The session gate answers before the route; a server client must still be
    told where to log in."""
    s = get_settings()
    monkeypatch.setattr(s, "grp_oauth_enabled", True)
    monkeypatch.setattr(s, "grp_public_url", "https://example.test")
    from app import main as main_mod
    client = TestClient(main_mod.app)
    r = client.post("/api/contribute", content="kind: vector", headers={"content-type": "application/x-yaml"})
    assert r.status_code == 401
    assert "oauth-protected-resource/mcp" in r.headers.get("www-authenticate", "")
