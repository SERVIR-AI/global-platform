"""The last pass before the sprint, from a real-Claude run (Sonnet and Opus, plain
requests, Desktop-like): the agent handed work back to the person (zip and re-upload a
Drive folder, clip a zip in a shell, five questions before any call); contribute_status()
with no arguments ran 61K-168K characters, past what the client reads; declines led with
a chore for the person instead of the platform's own step; a recoded stack year landed
without the stack's nodata; a Drive folder listing that answered 5xx once was declined;
the PREPARATION summary left out the raster clip.

Everything is synthetic and local; no test touches the network."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.contrib import fetch_policy, identity, jobs, prepare, staging
from app.contrib.prepare import _drive, clip, recode
from app.graph.geo import vecio
from app.mcp import store

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")
FOLDER = "https://drive.google.com/drive/folders/1PLJZMSjFvMlFjwjghWsxTVYdopvKukk7?usp=sharing"


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = get_settings()
    for k, v in {"cache_dir": tmp_path / "cache", "grp_mattermost_webhook": "",
                 "grp_contrib_allow_private_hosts": True}.items():
        monkeypatch.setattr(s, k, v)
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(staging, "STAGED_VECTORS", {}, raising=False)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield tmp_path


# --------------------------------------------------------------------------- 1. hand-backs
def test_submit_the_link_as_given_is_said_near_the_top_and_in_the_instructions():
    """In contribute_submit's description before any kind's form, and in the server's
    instructions inside what a client keeps of them (Claude Code cuts near 4K)."""
    from app.mcp import server
    desc = staging.describe_submit()
    at = desc.index(staging.AS_GIVEN)
    assert at < 1500 and at < desc.index("kind: one of")
    for words in ("a Drive folder, a zip of many files or tiles, a file of any size, an API or "
                  "a map service", "preview: true first when unsure; it stores nothing",
                  "Never ask the contributor to zip, clip, convert, reproject, mosaic or "
                  "re-upload anything", "never say a link will not work without trying it",
                  "Ask the person only for provenance the source does not state and for the "
                  "choices the platform asks for"):
        assert words in staging.AS_GIVEN
    assert "BEFORE calling" not in desc
    said = server._instructions()
    assert staging.AS_GIVEN in said and said.index(staging.AS_GIVEN) < 3000
    assert "`contribute_submit`" in said[:said.index(staging.AS_GIVEN)]


# --------------------------------------------------------------------------- 2. the listing
def _many(n=60):
    """n contributions of four kinds, each with the bulk the real ones carry: fetched
    steps and previews of several KB."""
    bulk = [{"op": "fetch", "inputs": {"url": "https://x.test/" + "p" * 200,
                                       "page_sha256": ["a" * 64] * 20}}] * 4
    kinds = [("raster", {"layer": "landcover_gulf_{i}"}), ("vector", {"layer": "cr_rivers_{i}"}),
             ("table", {"dataset": "gulf_carbon_{i}"}), ("document", {"pack": "land-cover"})]
    ids = []
    for i in range(n):
        kind, named = kinds[i % 4]
        m = {k: v.format(i=i) for k, v in named.items()}
        preview = {"entry": {"prepared": [f"recoded at ingest 10 -> 1 ({i:,} cells) " + "x" * 300],
                             "fetched": {"steps": bulk}}, "observed": {"bulk": "y" * 3000}}
        if kind == "vector":                  # prepared by the platform, no notes of its own
            preview = {"entry": {"fetched": {"steps": bulk}}, "observed": {"bulk": "y" * 3000}}
        if kind == "document":
            preview = {"doc_id": f"doc{i:04d}", "passport": {"bulk": "z" * 3000}}
        ids.append(store.save_contribution({
            "kind": kind, "status": ("pending", "withdrawn", "failed")[i % 3],
            "manifest": m, "title": f"Contribution {i} " + "t" * 120,
            "contributor_id": OWNER.id, "contributor_label": OWNER.label, "preview": preview,
            **({"prepared_automatically": [{"fields": {"from_folder": True}, "why": "w"}]}
               if kind == "vector" else {}),
            **({"problems": ["staging failed: " + "q" * 400]} if i % 3 == 2 else {})}))
    return ids


def _call(tool, args, caller):
    """A tool called through the real FastMCP server: the text a client reads."""
    from fastmcp import Client
    from app.mcp.server import mcp

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args)
            return r.content[0].text, r.structured_content
    tok = identity.bind(caller)
    try:
        return asyncio.run(go())
    finally:
        identity.unbind(tok)


def test_the_listing_is_one_line_each_under_8k_and_pages_to_every_contribution(env):
    ids = _many(60)
    newest = [r["contribution_id"] for r in store.list_contributions(contributor_id=OWNER.id)]
    assert sorted(newest) == sorted(ids)
    # a background job still preparing is listed too, and another person's is not
    jobs._connect().close()
    for ident, owner in (("ab" * 8, OWNER.id), ("cd" * 8, OTHER.id)):
        jobs._write("INSERT INTO contribution_jobs (id, owner_id, kind, lane, state, step, "
                    "created, beat, instance, pid) VALUES (?, ?, 'raster', 'prepare', 'running', "
                    "'mosaic', strftime('%s','now'), strftime('%s','now'), ?, ?)",
                    (ident, owner, jobs.INSTANCE, 0))
    text, out = _call("contribute_status", {}, OWNER)
    assert len(text) < 8000, len(text)
    assert out["total"] == 60 and out["more"] > 0
    assert [p["contribution_id"] for p in out["preparing"]] == ["ab" * 8]
    assert out["preparing"][0]["status"] == "preparing" and "mosaic" in out["preparing"][0]["step"]
    first = out["contributions"][0]
    assert first["contribution_id"] == newest[0]
    assert set(first) >= {"contribution_id", "kind", "name", "status", "when"}
    assert all(len(json.dumps(line)) < 700 for line in out["contributions"])
    kinds = {line["kind"] for line in out["contributions"]}
    assert kinds == {"raster", "vector", "table", "document"}
    line = next(x for x in out["contributions"] if x["kind"] == "raster")
    assert line["prepared"].startswith("recoded at ingest 10 -> 1") and len(line["prepared"]) <= 160
    assert next(x for x in out["contributions"] if x["kind"] == "vector")["prepared"] \
        == "the platform applied from_folder"
    assert "contribute_status(offset=" in out["note"] and "contribution_id=<id>" in out["note"]
    # page on to every contribution, once each, newest first
    seen, offset = [], 0
    while True:
        page = staging.status(None, OWNER, offset=offset)
        assert len(json.dumps(page)) < 8000
        seen += [x["contribution_id"] for x in page["contributions"]]
        if not page.get("more"):
            break
        offset += len(page["contributions"])
    assert seen == newest
    # the full record only by id
    one = staging.status(newest[0], OWNER)
    assert one["contribution"]["preview"]
    # a reviewer's queue is compact too; audit still answers
    rev = staging.status(None, REVIEWER)
    assert len(json.dumps(rev)) < 8000 and rev["pending_review"]
    assert "preview" not in rev["pending_review"][0]
    text, audit = _call("contribute_status", {"action": "audit"}, OWNER)
    assert audit["status"] == "ok" and "serving" in audit


def test_a_listing_with_nothing_submitted_creates_no_jobs_table(env):
    out = staging.status(None, OWNER)
    assert out["contributions"] == [] and "preparing" not in out and "more" not in out
    import sqlite3
    con = sqlite3.connect(env / "store.sqlite")
    try:
        assert not con.execute("SELECT name FROM sqlite_master WHERE name = "
                               "'contribution_jobs'").fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------- 3. declines
def _vector(url, **over):
    return {"layer": "cr_admin2", "url": url, "title": "t", "description": "d", "source": "s",
            "license": "unstated", "vintage": "unstated", **over}


def test_a_drive_folder_decline_leads_with_what_works_for_every_kind():
    """A raster, a table and a document hear the file's own link first; a vector (with
    the platform's own preparation off) hears from_folder first — never a .zip to make."""
    for kind, m in (("raster", {"url": FOLDER}), ("table", {"url": FOLDER}),
                    ("document", {"url": FOLDER})):
        said = staging._url_problems(kind, m)
        assert len(said) == 1, said
        assert said[0].startswith("this is a Google Drive FOLDER link: give the link of the file "
                                  "inside it"), said
        assert "one file per contribution" not in said[0] and ".zip" not in said[0]
        assert "from_folder" not in said[0]
    said = staging._url_problems("vector", _vector(FOLDER))
    assert said == ["this is a Google Drive FOLDER link: resubmit the same folder link with "
                    "from_folder: true — the platform lists the folder and its subfolders, "
                    "downloads the layer's parts (.shp, .shx, .dbf, .prj, .cpg) and zips them "
                    "itself, nothing to zip by hand; if the folder holds several layers, add "
                    "layer_name: '<the shapefile's name without .shp>'"]
    # asked for, the refusal is answered by the op itself
    assert staging._url_problems("vector", _vector(FOLDER, from_folder=True)) == []


def test_an_archive_over_the_cap_proposes_the_platforms_own_clip_and_layer(env, monkeypatch):
    def too_big(url, max_bytes=None):
        raise fetch_policy.TooLarge("worded any way", size=3 * (1 << 30), cap=50 << 20)
    monkeypatch.setattr(fetch_policy, "fetch", too_big)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 50 << 20)
    url = "https://data.example.org/cri_adm_all_levels.zip"
    with pytest.raises(staging.Declined) as said:
        staging._prepare_vector(_vector(url))
    assert str(said.value) == (
        f"{url} is 3 GB as a download, over the 50 MB cap — resubmit the same link with clip: "
        "<the area> — ASK the contributor which area, never guess one — and, for one layer of "
        "an archive of several, layer_name: <that layer>: the platform downloads the file to "
        "disk itself (streamed, under its preparation budget) and keeps that layer's features "
        "in the area; or with whole: true to keep the whole layer — the cap applies to the "
        "layer as stored")
    # a raster over the cap keeps the clip ask it had
    monkeypatch.setattr("app.contrib.remote_clip.probe", lambda u: (_ for _ in ()).throw(
        fetch_policy.FetchRefused("no probe here")))
    with pytest.raises(staging.Declined) as ras:
        staging._raster_source({"url": "https://hub.test/cr_dem.tif"}, 50 << 20, env)
    assert str(ras.value).startswith("missing required field 'clip': ")


def test_a_truncated_service_export_leads_with_whole_true():
    url = ("https://services.arcgis.com/x/arcgis/rest/services/Suelos/FeatureServer/0/query"
           "?where=1%3D1&outFields=*&f=geojson")
    fc = json.dumps({"type": "FeatureCollection", "exceededTransferLimit": True,
                     "features": [{"type": "Feature", "properties": {},
                                   "geometry": {"type": "Point", "coordinates": [-84, 10]}}]})
    with pytest.raises(ValueError) as exc:
        vecio.to_geojson(fc.encode(), "query.geojson")
    said = prepare.declined("vector", _vector(url), f"{url}: {exc.value}", "vector_unreadable",
                            url=url, error=str(exc.value))
    assert said.startswith(f"{url}: the server truncated this export at 1 features "
                           "(exceededTransferLimit) — resubmit the same link with whole: true")
    assert "full download" not in said
    # a file export that is not the service's own query: the service's url with whole
    file_url = "https://hub.example.org/datasets/suelos.geojson"
    said = prepare.declined("vector", _vector(file_url), f"{file_url}: {exc.value}",
                            "vector_unreadable", url=file_url, error=str(exc.value))
    assert "— give the service's own query url" in said and "with whole: true" in said
    assert clip._cut(str(exc.value))


# --------------------------------------------------------------------------- 4. the stack's nodata
def _mismatch(m, as_lands):
    return {"event": "stack_legend_mismatch", "kind": "raster", "manifest": m,
            "stack": "landcover_gulf", "have": {1: "Mangrove", 2: "Non-mangrove"},
            "want": {10: "mangrove", 20: "Non mangrove"}, "as_lands": as_lands}


def test_the_recode_proposal_carries_the_stacks_nodata_and_outside_values():
    """1956 landed nodata 3 / outside [3]; 1985, delivered with no nodata, landed None /
    [0]. The proposal and the auto-applied fields now carry the stack's."""
    m = {"layer": "landcover_gulf_1985", "stack": "landcover_gulf", "year": 1985,
         "legend": {10: "mangrove", 20: "Non mangrove"},
         "declared": {"dtype": "uint8", "valid_min": 10, "valid_max": 20}}
    stack = {"nodata": 3, "dtype": "uint8", "outside_values": [3]}
    p = recode.auto(_mismatch(m, stack))
    assert p["fields"]["declared"] == {"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3}
    # the stack's, its nodata, and the year's own default outside value (0) left in the file
    assert p["fields"]["outside_values"] == [3, 0]
    assert set(p["restates"]) == {"legend", "declared", "outside_values"}
    said = recode.suggest(_mismatch(m, stack))
    assert ("and declared valid_min 1, valid_max 2, nodata 3 and outside_values: [3, 0] (the "
            "file as it lands, as the stack's years do)") in said
    # a year delivered as int8 -128: its nodata is rewritten to the stack's, so -128 goes
    m8 = {**m, "declared": {"dtype": "int8", "valid_min": 10, "valid_max": 20, "nodata": -128},
          "outside_values": [-128]}
    p = recode.auto(_mismatch(m8, stack))
    assert p["fields"]["declared"]["nodata"] == 3 and p["fields"]["outside_values"] == [3]
    # a stack nodata the year's dtype cannot hold brings the stack's dtype
    p = recode.auto(_mismatch(m8, {"nodata": 255, "dtype": "uint8", "outside_values": [255]}))
    assert p["fields"]["declared"]["dtype"] == "uint8" and p["fields"]["declared"]["nodata"] == 255
    # a stack whose years name no nodata: the year keeps its own
    p = recode.auto(_mismatch(m8, {}))
    assert p["fields"]["declared"]["nodata"] == -128 and "outside_values" not in p["fields"]
    # labels that differ stay a question — with the stack's nodata in what to resubmit
    asked = recode.suggest({**_mismatch(m, stack), "want": {10: "Manglar", 20: "Otro"}})
    assert "declared nodata 3 and outside_values: [3, 0]" in asked and "ASK" in asked


# --------------------------------------------------------------------------- 5. Drive 5xx
class _Listing:
    def __init__(self, fails):
        self.fails, self.asked, self.port = fails, 0, None


def _listing_handler(state):
    page = (b'<html><body class="flip-embedded"><div class="flip-entries">'
            b'<div class="flip-entry" id="entry-f001xxxxxxxxxxxxxxxxxxxx"><a href="https://drive.'
            b'google.com/file/d/f001xxxxxxxxxxxxxxxxxxxx/view"><div class="flip-entry-title">'
            b'rios.shp</div></a></div></div></body></html>')

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state.asked += 1
            if state.asked <= state.fails:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
    return H


@pytest.mark.parametrize("fails", [1, 2])
def test_a_drive_listing_that_answers_5xx_is_asked_once_more(env, monkeypatch, fails):
    monkeypatch.setattr(_drive, "RETRY_SECONDS", 0.0)
    state = _Listing(fails)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _listing_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = SimpleNamespace(check_time=lambda: None)
    listing = f"http://127.0.0.1:{srv.server_address[1]}/listing/F001"
    try:
        if fails == 1:
            text = _drive.page(listing, FOLDER, ctx)
            assert [e[1] for e in _drive.entries(text)] == ["rios.shp"]
        else:
            with pytest.raises(prepare.PrepRefused, match="answered HTTP 503"):
                _drive.page(listing, FOLDER, ctx)
        assert state.asked == 2                  # once more, never a third time
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- 6. PREPARATION
def test_the_preparation_summary_lists_the_raster_clip_derived_from_the_forms():
    desc = staging.describe_submit()
    raster = desc.split("when a file is not yet in shape (raster: ", 1)[1].split(";", 1)[0].split(", ")
    derived = sorted(set(prepare.fields("raster"))
                     | (prepare._own_fields("raster") & prepare.all_fields()))
    assert raster == derived and "clip" in raster
    assert {"mosaic", "rasterise", "recode"} <= set(raster)
    vector = desc.split("vector: ", 1)[1].split(")", 1)[0].split(", ")
    assert set(prepare.fields("vector")) <= set(vector)
    assert "table:" not in desc.split("PREPARATION", 1)[1].split(")", 1)[0]
