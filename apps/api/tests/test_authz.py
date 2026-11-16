"""Who may see what, decided by a REAL OpenFGA store (the `openfga` binary on PATH;
skipped otherwise): a contribution's visibility level and grants, approval, the
reviewer rule, the catalog seam, the data route, the receipt's decision record, and
the fail-closed answer when the store is unreachable. Nothing is mocked: the raster is
a real GeoTIFF fetched by the gate from a real local http server."""
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from app import authz
from app.config import get_settings
from app.contrib import identity, staging
from app.graph.geo import tiffs

pytestmark = pytest.mark.skipif(shutil.which("openfga") is None,
                                reason="the openfga binary is not installed (brew install openfga)")

OWNER = identity.Caller("user_owner", "owner@hub.test", False, "bound")
OWNER_REVIEWING = identity.Caller("user_owner", "owner@hub.test", True, "bound")
A1 = identity.Caller("user_a1", "analyst1@hub.test", False, "bound")      # owner's hub
A2 = identity.Caller("user_a2", "analyst2@other.test", False, "bound")    # another hub
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")
ANON = identity.Caller(identity.ANONYMOUS, identity.ANONYMOUS, False, "anonymous")


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fga_server():
    port = _free_port()
    proc = subprocess.Popen(["openfga", "run", "--datastore-engine", "memory",
                             "--http-addr", f"127.0.0.1:{port}",
                             "--grpc-addr", f"127.0.0.1:{_free_port()}",
                             "--playground-enabled=false", "--metrics-enabled=false"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def file_host(tmp_path_factory):
    """A real http server for the rasters the gate fetches (it refuses local paths)."""
    root = tmp_path_factory.mktemp("served")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    srv.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def env(fga_server, file_host, tmp_path, monkeypatch):
    """Isolated conf, tiffs, cache and a FRESH store per test; roles and hubs written."""
    s = get_settings()
    (tmp_path / "tiffs.yml").write_text(yaml.safe_dump({}))
    (tmp_path / "raster_schema.yml").write_text(yaml.safe_dump(
        {"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {}}))
    monkeypatch.setattr(s, "tiffs_config_path", tmp_path / "tiffs.yml")
    monkeypatch.setattr(s, "raster_schema_path", tmp_path / "raster_schema.yml")
    monkeypatch.setattr(s, "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    monkeypatch.setattr(s, "raster_schema_contrib_path", tmp_path / "raster_schema.contrib.yml")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "tiffs")
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "grp_fga_url", fga_server)
    monkeypatch.setattr(s, "grp_fga_store_id", "")
    monkeypatch.setattr(s, "grp_fga_model_id", "")
    monkeypatch.setattr(s, "grp_auto_approve", False)
    monkeypatch.setattr(s, "grp_oauth_enabled", False)
    monkeypatch.setattr(s, "grp_allow_anonymous", False)
    monkeypatch.setattr(s, "grp_catalog_url", "")
    monkeypatch.setattr(s, "grp_contrib_allow_private_hosts", True)
    authz._CLIENT.clear()
    tiffs._BASE.clear()
    authz.bootstrap()
    authz.write([("user:user_rev", "reviewer", authz.PLATFORM),
                 ("user:local-dev", "operator", authz.PLATFORM),      # the box operator, as a tuple
                 ("user:user_owner", "member", "hub:mekong"),
                 ("user:user_a1", "member", "hub:mekong"),
                 ("user:user_a2", "member", "hub:lower")])
    yield tmp_path
    authz._CLIENT.clear()
    tiffs._BASE.clear()
    for k in [k for k in staging.STAGED_RASTERS if k.startswith("hazard_toyheat")]:
        staging.STAGED_RASTERS.pop(k, None)              # the in-memory staged index outlives the test


def _manifest(url, **over):
    d = {"layer": "hazard_toyheat", "url": url, "title": "Toy heat hazard", "description": "toy",
         "source": "toy institute, derived from toy reanalysis", "license": "CC-BY-4.0",
         "vintage": "2026-08", "legend": {1: "Low", 2: "High"},
         "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 5}}
    d.update(over)
    return d


def _serve_tif(file_host, name, data=None):
    import rasterio
    from rasterio.transform import from_bounds
    root, base = file_host
    data = data if data is not None else np.array([[1, 2], [2, 1]], dtype="int16")
    with rasterio.open(root / name, "w", driver="GTiff", height=2, width=2, count=1, dtype="int16",
                       crs="EPSG:4326", transform=from_bounds(103.0, 13.0, 103.1, 13.1, 2, 2)) as dst:
        dst.write(data, 1)
    return f"{base}/{name}"


def _submit(file_host, name="toyheat.tif", **over):
    url = _serve_tif(file_host, name)
    return _as(OWNER, lambda: staging.submit("raster", _manifest(url, **over), OWNER))


def _sees(caller, layer="hazard_toyheat") -> bool:
    return layer in _as(caller, tiffs.catalog)


def test_hub_visibility_from_staged_to_approved_to_shared(env, file_host, log):
    """A contributor uploads with visibility 'hub'. Staged: owner and reviewers only.
    Approved: the owner's hub sees it, another hub and anonymous do not. A grant to
    the other hub opens it; a revoke closes it; 'public' lets anonymous in."""
    up = _submit(file_host, visibility="hub")
    log("OUTPUT", {k: up.get(k) for k in ("status", "contribution_id", "sharing", "problems")})
    assert up["status"] == "staged" and up["sharing"]["visibility"] == "hub"
    assert up["sharing"]["hub"] == "mekong"
    cid = up["contribution_id"]
    assert _sees(OWNER) and _sees(REVIEWER) and not _sees(A1) and not _sees(A2)
    assert A1.may_view(cid) is False and REVIEWER.may_view(cid) is True
    # a reviewer who owns it may not approve it
    self_ok = _as(OWNER_REVIEWING, lambda: staging.approve(cid, OWNER_REVIEWING))
    assert self_ok["status"] == "declined" and "own upload" in self_ok["note"]
    ok = _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))
    assert ok["status"] == "approved"
    assert _sees(A1) and not _sees(A2) and not _sees(ANON)
    assert authz.sharing(cid)["approved"] is True
    # the other hub may not change the sharing; the owner may
    refused = _as(A2, lambda: staging.share(cid, "grant", hub="lower", caller=A2))
    assert refused["status"] == "declined"
    granted = _as(OWNER, lambda: staging.share(cid, "grant", hub="lower", caller=OWNER))
    assert granted["status"] == "ok" and granted["sharing"]["shared_hubs"] == ["lower"]
    assert _sees(A2)
    revoked = _as(OWNER, lambda: staging.share(cid, "revoke", hub="lower", caller=OWNER))
    assert revoked["sharing"]["shared_hubs"] == [] and not _sees(A2)
    public = _as(OWNER, lambda: staging.share(cid, "visibility", visibility="public", caller=OWNER))
    assert public["sharing"]["visibility"] == "public" and _sees(ANON) and _sees(A2)
    gone = _as(OWNER, lambda: staging.withdraw(cid, OWNER))
    assert gone["status"] == "withdrawn" and authz.tuples(authz.dataset(cid)) == []


def test_the_data_route_and_the_receipt_follow_the_same_answer(env, file_host):
    """The raster download route refuses with 404 for a caller the store refuses, and
    a receipt records the store's decision per cited contribution."""
    from app.api.routes import raster as route
    from app.mcp import record
    up = _submit(file_host, visibility="hub")
    cid = up["contribution_id"]
    _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))
    req = lambda who: SimpleNamespace(headers={identity.DEV_HEADER: who})
    assert route._may_view("hazard_toyheat", req("user_a1")) is True
    assert route._may_view("hazard_toyheat", req("user_a2")) is False
    assert route._may_view("hazard_toyheat", SimpleNamespace(headers={})) is True   # local-dev holds the operator tuple
    assert route._may_view("hazard_flood", req("user_a2")) is True                  # built-in: everyone
    pack = {"citations": [{"n": 1, "contribution_id": cid}]}
    a1 = _as(A1, lambda: record._access_decisions(pack))["access_decisions"]
    a2 = _as(A2, lambda: record._access_decisions(pack))["access_decisions"]
    assert a1[0]["allowed"] is True and a2[0]["allowed"] is False
    assert a1[0]["model_id"] == authz.ids()["model_id"] and a1[0]["object"] == f"dataset:{cid}"


def test_an_unreachable_store_refuses_everything(env, file_host, monkeypatch, log):
    """Fail closed: with the store down nobody is a reviewer, nothing contributed is
    viewable, and a new upload with sharing is declined in words."""
    up = _submit(file_host, visibility="platform")
    cid = up["contribution_id"]
    _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))
    assert _sees(A2)
    monkeypatch.setattr(get_settings(), "grp_fga_url", "http://127.0.0.1:9")
    authz._CLIENT.clear()
    tiffs._BASE.clear()
    assert not _sees(A2) and A2.may_view(cid) is False
    assert identity._is_reviewer("user_rev", "reviewer@hub.test", "token") is False
    declined = _submit(file_host, name="second.tif", layer="hazard_toyheat2", visibility="hub")
    log("OUTPUT", declined.get("problems"))
    assert declined["status"] == "declined" and "did not answer" in declined["problems"][0]


def test_a_replaced_store_is_followed_without_a_restart(env):
    """Another process (the demo runner, the operator CLI) may re-create the store while this
    server keeps running; the live client must follow the saved ids, not the dead store."""
    rev = ("user:user_rev", "reviewer", authz.PLATFORM)
    assert authz.check(*rev)
    old = authz.ids()
    authz._state_path().unlink()                      # as if the store were wiped and re-made elsewhere
    new = authz.bootstrap()                           # writes a fresh store id to authz.json
    assert new["store_id"] != old["store_id"]
    authz._CLIENT.clear()
    authz._CLIENT.update({"c": object(), "url": str(get_settings().grp_fga_url).rstrip("/"), "ids": old})
    assert authz.ids() == new                        # the stale client was dropped for the saved ids
    assert not authz.check(*rev)                      # the new store has no tuples yet: false, not an error
    authz.write([rev])
    assert authz.check(*rev)


def test_with_the_store_off_sharing_is_declined_in_words(env, file_host, monkeypatch):
    """GRP_FGA_URL unset keeps today's behaviour, and a sharing request says why it
    cannot be honoured instead of pretending."""
    monkeypatch.setattr(get_settings(), "grp_fga_url", "")
    authz._CLIENT.clear()
    tiffs._BASE.clear()
    plain = _submit(file_host)
    assert plain["status"] == "staged" and "sharing" not in plain
    shared = _submit(file_host, name="third.tif", layer="hazard_toyheat3", visibility="hub")
    assert shared["status"] == "declined" and "GRP_FGA_URL" in shared["problems"][0]
    assert A2.may_view("anything") is True


def test_the_model_file_and_the_json_agree():
    """model.fga is the readable twin of what the server loads: same types, relations."""
    import re
    from pathlib import Path
    dsl = (Path(authz.__file__).parent / "model.fga").read_text()
    types_dsl = set(re.findall(r"^type (\w+)", dsl, re.M))
    rel_dsl = set(re.findall(r"^\s+define (\w+):", dsl, re.M))
    js = authz.model_json()
    assert {t["type"] for t in js["type_definitions"]} == types_dsl
    rel_js = {r for t in js["type_definitions"] for r in (t.get("relations") or {})}
    assert rel_js == rel_dsl


def test_the_tool_list_differs_by_role_and_a_hidden_tool_is_refused_by_name(env, log):
    """The review tool is listed for a reviewer and not for an analyst, decided per request
    from the caller; the analyst calling it by name is refused in words that name them, and
    the reviewer's call goes through."""
    import asyncio
    from fastmcp.exceptions import ToolError
    from app.mcp import server

    def names(who):
        return {t.name for t in _as(who, lambda: asyncio.run(server.mcp.list_tools()))}

    analyst, reviewer = names(A1), names(REVIEWER)
    log("OUTPUT", f"reviewer sees, analyst does not: {sorted(reviewer - analyst)}")
    assert "contribute_review" in reviewer and "contribute_review" not in analyst
    assert analyst < reviewer and all(not t.tags for t in asyncio.run(server.mcp.list_tools()) if t.name in analyst)
    with pytest.raises(ToolError) as refused:
        _as(A1, lambda: asyncio.run(server.mcp.call_tool("contribute_review", {"action": "list"})))
    log("OUTPUT", str(refused.value))
    assert A1.label in str(refused.value) and "reviewer" in str(refused.value)
    ok = _as(REVIEWER, lambda: asyncio.run(server.mcp.call_tool("contribute_review", {"action": "list"})))
    assert ok.structured_content["status"] == "ok"


def test_an_approved_contribution_is_not_taken_down_over_mcp_but_the_operator_can(env, file_host, log):
    """Over MCP a reviewer's reject refuses an approved contribution in words and 'remove' is
    no action; the operator's command line still takes it down, and the store forgets it."""
    import asyncio
    from app.contrib import cli
    from app.mcp import server, store
    cid = _submit(file_host, visibility="hub")["contribution_id"]
    assert _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))["status"] == "approved"
    assert _sees(A1)

    def review(**args):
        return _as(REVIEWER, lambda: asyncio.run(
            server.mcp.call_tool("contribute_review", args))).structured_content

    kept = review(action="reject", contribution_id=cid, note="wrong units")
    log("OUTPUT", kept["note"])
    assert kept["status"] == "declined" and "command line" in kept["note"] and "withdraw" in kept["note"]
    gone = review(action="remove", contribution_id=cid, note="wrong units")
    assert gone["status"] == "declined" and "unknown action" in gone["note"]
    assert store.load_contribution(cid)["status"] == "approved" and _sees(A1)
    assert cli.main(["review", "reject", cid, "wrong units, taken down"]) == 0
    rec = store.load_contribution(cid)
    assert rec["status"] == "rejected" and rec["unlanded"]["removed"] is True
    assert not _sees(A1) and authz.tuples(authz.dataset(cid)) == []


def test_share_list_is_a_hubs_view_of_its_own_datasets(env, file_host, log):
    """A hub's member lists the hub's contributions they may view, each with its visibility;
    the other hub lists nothing, its own hubs or the named one; the owner's switch shows."""
    cid = _submit(file_host, visibility="hub")["contribution_id"]
    listing = lambda who, **kw: _as(who, lambda: staging.share(None, "list", caller=who, **kw))
    staged = listing(OWNER)
    assert staged["hubs"] == ["mekong"] and [r["status"] for r in staged["contributions"]] == ["pending"]
    assert listing(A1)["contributions"] == []                 # staged: owner and reviewers only
    _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))
    mine = listing(A1)
    log("OUTPUT", mine)
    assert mine["status"] == "ok" and mine["hubs"] == ["mekong"]
    assert [(r["contribution_id"], r["kind"], r["status"], r["visibility"], r["hub"])
            for r in mine["contributions"]] == [(cid, "raster", "approved", "hub", "mekong")]
    assert mine["contributions"][0]["title"].startswith("Toy heat hazard")
    assert listing(A2)["contributions"] == [] and listing(A2, hub="mekong")["contributions"] == []
    _as(OWNER, lambda: staging.share(cid, "visibility", visibility="platform", caller=OWNER))
    assert listing(A1)["contributions"][0]["visibility"] == "platform"
    assert listing(A2)["contributions"] == []                 # viewable now, but not its hub's upload
    assert staging.share(None, "visibility", visibility="hub", caller=OWNER)["status"] == "declined"


def test_with_the_store_unreachable_the_review_refusal_names_the_store(env, monkeypatch, log):
    """Fail closed with a reason: the queue is refused because the store did not answer, not
    because the person is not a reviewer — resolved as the MCP resolves it, asking the store."""
    import contextvars
    monkeypatch.setattr(get_settings(), "grp_fga_url", "http://127.0.0.1:9")
    authz._CLIENT.clear()
    out = contextvars.copy_context().run(staging.review_list)    # one request's context, as served
    log("OUTPUT", out["note"])
    assert out["status"] == "declined" and "did not answer" in out["note"]
    assert "not a reviewer" not in out["note"]
