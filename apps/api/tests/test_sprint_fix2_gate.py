"""Mangrove sprint re-test, the gate: what a participant still hit — each fixed at the
seam every kind and every pack passes through, exercised on synthetic data, no network.

Feeds: a JSON feed is filtered by its own output fields and a bbox, declines what it
does not take, and declares its own bbox scope like a table. Withdrawal: an APPROVED
contribution of any kind is taken down by its contributor or a reviewer, never by a
stranger, and stops being served (catalog, stack choice, registry, library). Preview:
every staged source carries an 'unreviewed preview' review the readers cite. Declines:
a loose shapefile part says to zip the parts; a figshare article is resolved through
its API. The developer CLI fetches through the same policy."""
import copy
import hashlib
import ipaddress
import json

import numpy as np
import pytest
import yaml
from requests.structures import CaseInsensitiveDict
from shapely.geometry import box

from app.config import get_settings
from app.contrib import fetch_policy, identity, sources, staging
from app.graph.geo import combine, tiffs, vectors
from app.mcp import assemble, climate_indices, feeds, registry, store
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus

from test_layer_values import B, POLY, _tif, world  # noqa: F401  (fixture reuse)

OWNER = identity.Caller("user_owner", "scientist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")

_REAL_FETCH = fetch_policy.fetch
NICOYA = [-85.2977, 9.9337, -84.7561, 10.2968]
TERRABA = [-83.70, 8.75, -83.30, 9.10]


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


class HashEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """A hermetic gate: tmp cache, store and feeds dir; fetch answers from `bodies`;
    an upstream JSON API answers from `api`."""
    s = get_settings()
    for k, v in {"cache_dir": tmp_path, "tiffs_dir": tmp_path / "tiffs",
                 "feeds_conf_dir": tmp_path / "feeds", "grp_mattermost_webhook": "",
                 "grp_contrib_hourly_cap": 0, "rag_min_relevance": -1.0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ProviderEmbedder, "embed", lambda self, texts: HashEmbedder().embed(texts))
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    bodies, api = {}, {}
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (bodies[url], url.rsplit("/", 1)[-1]))
    import requests

    class R:
        def __init__(self, doc):
            self.doc = doc

        def raise_for_status(self):
            pass

        def json(self):
            return self.doc
    monkeypatch.setattr(requests, "get", lambda url, **k: R(api[url]))
    monkeypatch.setattr(climate_indices, "_CACHE", {})
    snapshot = copy.deepcopy(registry.FEEDS)
    for g in ("STAGED_FEEDS", "STAGED_VECTORS", "STAGED_AMENDS"):
        monkeypatch.setattr(staging, g, {})
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield {"bodies": bodies, "api": api}
    registry.FEEDS.clear()
    registry.FEEDS.update(snapshot)


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)


# ------------------------------------------------------------------- 1. feeds
GBIF_URL = "https://api.gbif.example/v1/occurrence/search?country=CR&limit=300"
GBIF = {"count": 1984, "results": [
    {"species": "Rhizophora mangle", "decimalLatitude": 10.10, "decimalLongitude": -85.10,
     "eventDate": "2024-01-05"},
    {"species": "Pelliciera rhizophorae", "decimalLatitude": 8.90, "decimalLongitude": -83.50,
     "eventDate": "2024-03-01"},
    {"species": "Avicennia germinans", "decimalLatitude": 10.05, "decimalLongitude": -85.00,
     "eventDate": "2024-02-11"},
    {"species": "Rhizophora mangle", "decimalLatitude": None, "decimalLongitude": None,
     "eventDate": "2023-12-30"}]}
OBIS_URL = "https://api.obis.example/v3/checklist?geometry=POLYGON((...))&size=1000"
OBIS = {"total": 3, "results": [
    {"scientificName": "Crocodylus acutus", "category": "VU", "records": 28},
    {"scientificName": "Anadara tuberculosa", "category": None, "records": 42},
    {"scientificName": "Pristis pristis", "category": "CR", "records": 2}]}


def _feed(ds, url, fields, pack="land-cover", **over):
    return {"dataset": ds, "title": f"{ds} (synthetic)", "description": "synthetic API feed",
            "source": "test fixture", "validation": "unvalidated", "residency": "external call-out",
            "cadence": "irregular", "adapter": "generic_json", "pack": pack,
            "fetch": {"url": url, "records_path": "results", "fields": fields}, **over}


GBIF_FIELDS = {"species": "species", "lat": "decimalLatitude", "lon": "decimalLongitude",
               "event_date": "eventDate"}
OBIS_FIELDS = {"scientific_name": "scientificName", "iucn_category": "category",
               "obis_records": "records"}


def test_a_json_feed_filters_by_its_own_fields_and_a_bbox_and_declines_the_rest(hub, auto, log):
    """GBIF/OBIS feeds took only `limit`: {species: ...}, {iucn_category: 'CR'} and a bbox
    answered every record of the page, status ok, and an unknown param was dropped."""
    hub["api"][GBIF_URL] = GBIF
    hub["api"][OBIS_URL] = OBIS
    gbif = _feed("test_gbif", GBIF_URL, GBIF_FIELDS)
    gbif["fetch"]["as_of_field"] = "event_date"
    out = staging.submit("feed", gbif, OWNER)
    assert out["status"] == "approved", out
    one = _as(OWNER, lambda: feeds.query("test_gbif", {"species": "pelliciera RHIZOPHORAE"}))
    log("OUTPUT", one["summary"])
    assert one["status"] == "ok" and [r["species"] for r in one["records"]] == ["Pelliciera rhizophorae"]
    assert one["summary"].startswith("1 of the 4 records read match species = ")
    assert "the upstream matched 1,984; the feed reads one page and filters only what it read" in one["summary"]
    assert "where species = " in one["passport"]["query"]
    gulf = _as(OWNER, lambda: feeds.query("test_gbif", {"bbox": NICOYA}))
    assert sorted(r["species"] for r in gulf["records"]) == ["Avicennia germinans", "Rhizophora mangle"]
    none = _as(OWNER, lambda: feeds.query("test_gbif", {"species": "Laguncularia racemosa"}))
    assert none["status"] == "empty" and "no record matches species = 'Laguncularia racemosa'" in none["note"]
    no = _as(OWNER, lambda: feeds.query("test_gbif", {"country": "Costa Rica"}))
    log("OUTPUT", no["note"])
    assert no["status"] == "declined" and "this feed does not take ['country']" in no["note"]
    assert "accepted: limit, bbox, or an equality filter on an output field: species, lat, lon" in no["note"]
    # another pack's feed, with no coordinates: filtered by its fields, a bbox declined
    assert staging.submit("feed", _feed("test_obis", OBIS_URL, OBIS_FIELDS, pack="risk"),
                          OWNER)["status"] == "approved"
    cr = _as(OWNER, lambda: feeds.query("test_obis", {"iucn_category": "CR"}))
    assert [r["scientific_name"] for r in cr["records"]] == ["Pristis pristis"]
    nobox = _as(OWNER, lambda: feeds.query("test_obis", {"bbox": NICOYA}))
    assert nobox["status"] == "declined"
    assert "this feed has no latitude/longitude output fields, so it cannot be filtered by bbox" in nobox["note"]
    # the tool text says what each feed takes, from its own fields
    text = feeds.describe()
    assert "equality filter on an output field (scientific_name, iucn_category, obis_records)" in text


def test_every_other_feed_declines_a_param_it_does_not_take(hub, log):
    """The seam: a parameter no adapter reads was dropped without a word on every feed,
    not only JSON ones. `limit` still passes everywhere — every pack sends it."""
    registry.FEEDS["test_series"] = {"status": "available", "adapter": "generic_table",
                                     "declarative": "test_series.yml",
                                     "fetch": {"url": "https://x/s.data", "index_name": "S",
                                               "units": "u"}}
    out = feeds.query("test_series", {"place": "Nicoya"})
    log("OUTPUT", out["note"])
    assert out["status"] == "declined" and "does not accept those params: place" in out["note"]
    assert "accepted: limit" in out["note"]
    gone = feeds.query("enso_discussion", {"year": 2026})
    assert gone["status"] == "declined" and "accepted: none" in gone["note"]


def test_a_feed_declares_its_bbox_like_a_table_and_the_pack_reads_it(hub, auto, log):
    """A Gulf of Nicoya checklist could declare only countries, so it was cited in a
    Térraba-Sierpe answer: "865 of its 865 row(s) are about Terraba-Sierpe"."""
    from app.land_cover.synthesis import _scope_miss
    hub["api"][OBIS_URL] = OBIS
    out = staging.submit("feed", _feed("test_nicoya_checklist", OBIS_URL, OBIS_FIELDS,
                                       countries="Costa Rica", bbox=NICOYA), OWNER)
    assert out["status"] == "approved", out
    landed = yaml.safe_load((get_settings().feeds_conf_dir / "test_nicoya_checklist.yml").read_text())
    log("OUTPUT", f"countries={landed['countries']} bbox={landed['bbox']}")
    assert landed["countries"] == ["Costa Rica"] and landed["bbox"] == NICOYA
    row = registry.FEEDS["test_nicoya_checklist"]
    assert row["bbox"] == NICOYA and row["params"]["<field>"].startswith("equality filter")
    miss = _scope_miss(row, ["Costa Rica"], box(*TERRABA))
    assert miss and miss[1] == "this area lies outside that box"
    assert _scope_miss(row, ["Costa Rica"], box(*NICOYA)) is None
    # a bad box is declined in the table's words
    bad = staging.submit("feed", _feed("test_bad_box", OBIS_URL, OBIS_FIELDS, bbox=[10, 5, -10, 6]), OWNER)
    assert bad["status"] == "declined" and any("bbox must be [minLon, minLat, maxLon, maxLat]" in p
                                               for p in bad["problems"])


def test_a_staged_feed_carries_its_scope_or_says_it_has_none_and_how_to_filter(hub, log):
    hub["api"][GBIF_URL] = GBIF
    out = staging.submit("feed", _feed("test_gbif_staged", GBIF_URL, GBIF_FIELDS, pack="risk"), OWNER)
    assert out["status"] == "staged", out
    pv = out["preview"]
    log("OUTPUT", pv["how_to_test"])
    assert "no countries or bbox declared" in pv["hints"][0]
    assert "on any output field (species, lat, lon, event_date) or a bbox" in pv["how_to_test"]
    assert staging.STAGED_FEEDS["test_gbif_staged"]["params"]["bbox"].startswith("[minLon")
    boxed = staging.submit("feed", _feed("test_gbif_boxed", GBIF_URL, GBIF_FIELDS, pack="food-security",
                                         countries=["Costa Rica"], bbox=TERRABA), OWNER)
    assert boxed["status"] == "staged" and "hints" not in boxed["preview"]
    assert staging.STAGED_FEEDS["test_gbif_boxed"]["bbox"] == TERRABA


# ------------------------------------------------------------------- 2. withdrawal
GEOJSON = json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": "A"}, "geometry": {"type": "Point", "coordinates": [-85.2, 10.1]}},
    {"type": "Feature", "properties": {"name": "B"}, "geometry": {"type": "Point", "coordinates": [-85.0, 10.2]}}]}).encode()


def _vector(layer, licence="CC-BY-4.0", url="https://x.example/pts.geojson"):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)", "description": "synthetic points",
            "source": "test fixture", "license": licence, "vintage": "2026-10", "countries": ["Costa Rica"]}


def _table(ds, **over):
    return {"dataset": ds, "pack": "land-cover", "title": "Mangrove plots (synthetic)",
            "description": "SYNTHETIC rows", "source": "test fixture", "validation": "unvalidated",
            "license": "CC-BY-4.0", "vintage": "2021-03", "cadence": "irregular",
            "columns": {"country": "Country", "agb": "AGB"}, "units": "Mg/ha",
            "csv_text": "Country,AGB\nCosta Rica,1\n", **over}


def test_the_owner_withdraws_an_approved_layer_a_stranger_cannot(hub, auto, log):
    """A participant's mistaken layer stayed live under auto-approve: approved
    contributions could not be withdrawn or rejected over MCP."""
    hub["bodies"]["https://x.example/pts.geojson"] = GEOJSON
    out = staging.submit("vector", _vector("test_wrong_pts"), OWNER)
    assert out["status"] == "approved" and "action='withdraw'" in out["next"]
    assert "UNREVIEWED" not in json.dumps(out)              # landed: the preview marker went
    cid = out["contribution_id"]
    master = vectors.master_path(vectors.landed()["test_wrong_pts"])
    stranger = staging.withdraw(cid, OTHER)
    assert stranger["status"] == "declined" and stranger["note"] == f"unknown contribution {cid!r}"
    assert "test_wrong_pts" in vectors.landed()
    w = staging.withdraw(cid, OWNER)
    log("OUTPUT", json.dumps(w["unlanded"]))
    assert w["status"] == "withdrawn" and w["unlanded"]["removed"] is True
    assert "remain replayable" in w["unlanded"]["note"]
    assert "test_wrong_pts" not in vectors.landed() and "test_wrong_pts" not in vectors.visible()
    assert not master.exists() and master.with_name(master.name + ".retired").exists()
    rec = staging.status(cid, OWNER)["contribution"]
    assert rec["status"] == "withdrawn" and rec["unlanded"]["registry_row"] == "removed"
    assert staging.withdraw(cid, OWNER)["status"] == "declined"           # nothing left to withdraw
    assert staging.reconcile(OTHER)["approved"] == 0
    # the name is free again
    assert staging.submit("vector", _vector("test_wrong_pts"), OWNER)["status"] == "approved"


def test_a_reviewer_removes_anyones_approved_table_and_says_so(hub, auto, log):
    out = staging.submit("table", _table("test_wrong_tbl"), OWNER)
    assert out["status"] == "approved" and "test_wrong_tbl" in registry.FEEDS
    cid = out["contribution_id"]
    # a reviewer is pointed at the review action rather than told "unknown"
    told = staging.withdraw(cid, REVIEWER)
    assert told["status"] == "declined" and "contribute_review(action='reject'" in told["note"]
    assert staging.reject(cid, "", REVIEWER)["status"] == "declined"     # a note is required
    assert staging.reject(cid, "not mine to remove", OTHER)["status"] == "declined"
    rej = staging.reject(cid, "the factor is per plot, not per hectare", REVIEWER)
    log("OUTPUT", json.dumps(rej["unlanded"]))
    assert rej["status"] == "rejected" and rej["unlanded"]["dataset"] == "test_wrong_tbl"
    assert rej["decision_note"] == "removed after approval: the factor is per plot, not per hectare"
    assert "test_wrong_tbl" not in registry.FEEDS
    assert not (get_settings().feeds_conf_dir / "test_wrong_tbl.yml").exists()
    assert "moved aside" in rej["unlanded"]["archived_copy"]
    q = feeds.query("test_wrong_tbl", {})
    assert q["status"] == "declined" and "unknown dataset" in q["note"]


def test_a_feed_withdrawn_and_resubmitted_under_its_name_reads_the_new_upstream(hub, auto, log):
    """The fetch cache holds six hours and was keyed by the feed's name alone: the
    corrected feed would have answered with the mistaken one's rows."""
    hub["api"][GBIF_URL] = GBIF
    hub["api"][OBIS_URL] = OBIS
    wrong = staging.submit("feed", _feed("test_checklist", GBIF_URL, {"species": "species"}), OWNER)
    assert wrong["status"] == "approved"
    assert _as(OWNER, lambda: feeds.query("test_checklist", {"limit": 50}))["count"] == 4
    w = staging.withdraw(wrong["contribution_id"], OWNER)
    assert w["status"] == "withdrawn" and "test_checklist" not in registry.FEEDS
    right = staging.submit("feed", _feed("test_checklist", OBIS_URL, OBIS_FIELDS), OWNER)
    assert right["status"] == "approved", right
    res = _as(OWNER, lambda: feeds.query("test_checklist", {"limit": 50}))
    log("OUTPUT", res["summary"])
    assert res["count"] == 3 and "scientific_name" in res["records"][0]


def test_a_withdrawn_document_leaves_the_library_and_its_archive_stays(hub, auto, log):
    hub["bodies"]["https://repo.example/memo.txt"] = ("Memoria técnica del mapa de manglar. " * 30).encode()
    doc = {"pack": "risk", "url": "https://repo.example/memo.txt", "source": "CATIE",
           "title": "Memoria (test copy)", "pub_date": "unstated", "temporal": "retrospective",
           "validation": "single-agency"}
    out = staging.submit("document", doc, OWNER)
    assert out["status"] == "approved", out
    doc_id, lib = out["landing"]["doc_id"], out["landing"]["corpus"]
    assert Corpus(lib).find(doc_id)
    w = staging.withdraw(out["contribution_id"], OWNER)
    assert w["status"] == "withdrawn" and w["unlanded"]["raw_archive_kept"] is True
    assert Corpus(lib).find(doc_id) is None and Corpus(lib).raw_path(doc_id) is not None


def test_a_withdrawn_correction_restores_what_it_replaced(hub, auto, log):
    hub["bodies"]["https://x.example/pts.geojson"] = GEOJSON
    assert staging.submit("vector", _vector("test_amended_pts"), OWNER)["status"] == "approved"
    old = vectors.landed()["test_amended_pts"]["description"]
    fix = _as(OWNER, lambda: staging.submit("amend", {"layer": "test_amended_pts", "fields": {
        "description": "one point per health post"}, "rationale": "it is health posts"}, OWNER))
    assert fix["status"] == "approved", fix
    assert vectors.landed()["test_amended_pts"]["description"] == "one point per health post"
    w = staging.withdraw(fix["contribution_id"], OWNER)
    log("OUTPUT", json.dumps(w["unlanded"]))
    row = vectors.landed()["test_amended_pts"]
    assert w["status"] == "withdrawn" and row["description"] == old
    assert row["amended"][-1] == {"contribution_id": fix["contribution_id"], "withdrawn": True,
                                  "restored": {"description": old}}
    assert fix["contribution_id"] not in str(row.get("review") or "")


# ---------------------------------------------- rasters, stacks and weights (any pack)
@pytest.fixture
def lands(world, monkeypatch):
    """The layer-values world, auto-approving, with a fetcher that serves files by URL."""
    files = {}
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    monkeypatch.setattr(get_settings(), "grp_contrib_hourly_cap", 0)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (files[url], url.rsplit("/", 1)[-1]))

    def serve(name, arr, dtype, nodata=None):
        path = _tif(world / name, arr, dtype, nodata=nodata)
        url = f"https://hub.test/{name}"
        files[url] = path.read_bytes()
        return url
    return serve


def _raster(layer, url, caller=OWNER, **over):
    m = {"layer": layer, "url": url, "title": f"{layer} (synthetic)", "description": "synthetic layer",
         "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10",
         "countries": ["Testland"], **over}
    return _as(caller, lambda: staging.submit("raster", m, caller))


def _change_map(lands):
    a = np.ones((20, 20), dtype="uint8")
    a[:, 10:] = 2
    return _raster("landcover_toychange_1985", lands("change.tif", a, "uint8", nodata=3),
                   stack="landcover_toychange", year=1985, stack_title="Toy change map",
                   legend={1: "Stable", 2: "Loss"}, outside_values=[3],
                   declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3})


def test_a_withdrawn_stack_year_leaves_the_catalog_and_the_default_stack_choice(lands, log):
    """A change map contributed as a land-cover year became the default stack for its
    area; once approved, nothing over MCP could take it down."""
    from app.land_cover import synthesis as lc
    out = _change_map(lands)
    assert out["status"] == "approved", out
    assert "landcover_toychange" in lc.landcover_stacks()
    assert lc._pick_stack(box(*B))[0] == "landcover_toychange"
    assert staging.withdraw(out["contribution_id"], OTHER)["status"] == "declined"
    w = staging.withdraw(out["contribution_id"], OWNER)
    log("OUTPUT", json.dumps(w["unlanded"]))
    assert w["status"] == "withdrawn" and "removed with it" in w["unlanded"]["stack"]
    assert "landcover_toychange_1985" not in tiffs.catalog()
    assert "landcover_toychange" not in lc.landcover_stacks()
    with pytest.raises(lc.NoStackHere):
        lc._pick_stack(box(*B))


def test_a_reviewer_takes_down_an_approved_layer_any_pack_was_reading(lands, log):
    arr = np.tile((1000 + 10 * np.arange(20)).astype("int16"), (20, 1))
    out = _raster("measure_toy_agb", lands("agb.tif", arr, "int16"),
                  declared={"dtype": "int16", "valid_min": 0, "valid_max": 3000, "units": "Mg/ha",
                            "scale_factor": 0.1})
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["measure_toy_agb"])
    assert "measure_toy_agb" in p["layers"]
    rej = staging.reject(out["contribution_id"], "biomass is x10 twice", REVIEWER)
    assert rej["status"] == "rejected" and rej["unlanded"]["catalog_row"] == "removed"
    assert "measure_toy_agb" not in tiffs.catalog()
    again = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                              layers=["measure_toy_agb"])
    log("OUTPUT", again.get("note") or again.get("gaps"))
    assert "measure_toy_agb" not in (again.get("layers") or {})


def test_withdrawn_weights_return_the_recipe_in_force_before(lands, log):
    first = _as(OWNER, lambda: staging.submit("weights", {
        "hazard": "toyflood", "weights": {"vulnerability_toy": 0.5, "vulnerability_flat_index": 0.5},
        "rationale": "halve the reclass"}, OWNER))
    assert first["status"] == "approved", first
    second = _as(OTHER, lambda: staging.submit("weights", {
        "hazard": "toyflood", "weights": {"vulnerability_toy": 0.7, "vulnerability_flat_index": 0.3},
        "rationale": "lean on the reclass"}, OTHER))
    assert second["status"] == "approved", second
    assert combine.adjustment_for("hazard_toyflood")["contribution_id"] == second["contribution_id"]
    # the first is no longer in force: withdrawing it changes nothing, and says so
    w1 = staging.withdraw(first["contribution_id"], OWNER)
    assert w1["status"] == "withdrawn" and w1["unlanded"]["removed"] is False
    assert "a later adjustment" in w1["unlanded"]["note"]
    w2 = staging.withdraw(second["contribution_id"], OTHER)
    log("OUTPUT", w2["unlanded"]["now"])
    assert "platform recipe" in w2["unlanded"]["now"]          # the first was withdrawn already
    assert combine.adjustment_for("hazard_toyflood") is None
    assert combine.weights_for("hazard_toyflood") == {"vulnerability_toy": 1.0}


def test_withdrawing_the_newer_weights_puts_the_older_approved_ones_back(lands, log):
    a = _as(OWNER, lambda: staging.submit("weights", {
        "hazard": "toyflood", "weights": {"vulnerability_toy": 0.5, "vulnerability_flat_index": 0.5},
        "rationale": "halve the reclass"}, OWNER))
    b = _as(OTHER, lambda: staging.submit("weights", {
        "hazard": "toyflood", "weights": {"vulnerability_toy": 0.9, "vulnerability_flat_index": 0.1},
        "rationale": "mostly the reclass"}, OTHER))
    w = staging.withdraw(b["contribution_id"], OTHER)
    assert w["unlanded"]["now"] == f"the earlier approved adjustment {a['contribution_id']} is in force again"
    adj = combine.adjustment_for("hazard_toyflood")
    assert adj["contribution_id"] == a["contribution_id"] and "auto-approved" in adj["review"]
    assert combine.weights_for("hazard_toyflood") == {"vulnerability_toy": 0.5,
                                                      "vulnerability_flat_index": 0.5}


# ------------------------------------------------------------------- 3. preview marker
WCMC = "terms of use: no sub-licensing or redistribution without written permission"


def test_a_layer_held_for_review_is_cited_as_an_unreviewed_preview_in_every_pack(lands, monkeypatch, log):
    """Held for its licence, the layer was usable by its contributor at once — and its
    citation read like an approved one's; only the pack-level note said PREVIEW."""
    from app.mcp import packs
    arr = np.tile((1000 + 10 * np.arange(20)).astype("int16"), (20, 1))
    out = _raster("measure_toy_held", lands("held.tif", arr, "int16"), license=WCMC,
                  declared={"dtype": "int16", "valid_min": 0, "valid_max": 3000, "units": "Mg/ha"})
    assert out["status"] == "staged" and "restricts redistribution" in out["held_for_review"]
    p = _as(OWNER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                             geometry=POLY, layers=["measure_toy_held"]))
    cit = next(c for c in p["citations"] if c.get("layer") == "measure_toy_held")
    log("OUTPUT", cit["text"][-260:])
    assert "Review: UNREVIEWED PREVIEW — staged contribution" in cit["text"]
    assert "held for a human reviewer because its licence restricts redistribution" in cit["text"]
    assert cit["review_status"].startswith("UNREVIEWED PREVIEW")

    def gather(target, focus, trace, extras):          # a contributed pack with an area
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [],
                {"queries": None, "_area": {"name": "Toy bay", "how": "test polygon", "geometry": POLY}})
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",), "target_doc": {"place": "site"},
        "gather": gather, "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})
    t = _as(OWNER, lambda: assemble.assemble(pack="toy", place="Toy bay", layers=["measure_toy_held"]))
    tc = next(c for c in t["citations"] if c.get("layer") == "measure_toy_held")
    assert "UNREVIEWED PREVIEW" in tc["text"]
    # approved by a human, the marker goes: the landed row is reviewed
    assert staging.approve(out["contribution_id"], REVIEWER)["status"] == "approved"
    assert "review" not in tiffs.catalog()["measure_toy_held"]


def test_every_staged_kind_carries_the_marker_and_the_landed_one_drops_it(hub, log):
    hub["bodies"]["https://x.example/pts.geojson"] = GEOJSON
    v = staging.submit("vector", _vector("test_preview_pts"), OWNER)
    t = staging.submit("table", _table("test_preview_tbl"), OWNER)
    staff = "Equipo: staff.one@catie.example, staff.two@sinac.example. "
    hub["bodies"]["https://repo.example/staff.txt"] = (("Informe de campo. " * 30) + staff).encode()
    d = staging.submit("document", {"pack": "land-cover", "url": "https://repo.example/staff.txt",
                                    "source": "CATIE", "title": "Informe", "pub_date": "unstated",
                                    "temporal": "retrospective", "validation": "single-agency"}, OWNER)
    assert v["status"] == t["status"] == d["status"] == "staged"
    pts = _as(OWNER, lambda: vectors.visible()["test_preview_pts"])
    tbl = _as(OWNER, lambda: staging.visible_staged_feed("test_preview_tbl"))
    meta = _as(OWNER, lambda: Corpus("land-cover").find(d["preview"]["doc_id"]))["metadata"]
    log("OUTPUT", meta["review"])
    for row in (pts, tbl, meta):
        assert row["review"].startswith("UNREVIEWED PREVIEW — staged contribution")
    assert "held for a human reviewer because its text holds 2 e-mail addresses" in meta["review"]
    from app.land_cover.synthesis import _document_citation
    assert _document_citation(1, {"metadata": meta}, None)["review_status"].startswith("UNREVIEWED PREVIEW")
    # a row staged before the marker existed is marked on the way out
    staging.STAGED_FEEDS["test_preview_tbl"].pop("review")
    assert _as(OWNER, lambda: staging.visible_staged_feed("test_preview_tbl"))["review"].startswith(
        "UNREVIEWED PREVIEW")
    assert staging.approve(v["contribution_id"], REVIEWER)["status"] == "approved"
    assert "review" not in vectors.landed()["test_preview_pts"]
    # and the approved record's preview no longer says "not yet approved"
    assert "UNREVIEWED" not in json.dumps(staging.status(v["contribution_id"], OWNER)["contribution"])


# ------------------------------------------------------------------- 4. loose shapefile
def _shp_header() -> bytes:
    return (b"\x00\x00\x27\x0a" + b"\x00" * 20 + (50).to_bytes(4, "big")
            + (1000).to_bytes(4, "little") + (1).to_bytes(4, "little") + b"\x00" * 64)


@pytest.mark.parametrize("url,body,said", [
    ("https://drive.example/cambios_8514.shp", _shp_header(), "the .shp file 'cambios_8514.shp'"),
    ("https://drive.example/cambios_8514.dbf", b"\x03\x7a\x01\x01" + b"\x00" * 60, "the .dbf file"),
    ("https://drive.example/uc?id=1gYet1", _shp_header(), "a shapefile's .shp (or .shx) file"),
], ids=["shp-by-name", "dbf-by-name", "shp-by-header"])
def test_a_loose_shapefile_part_is_told_to_zip_the_parts(hub, url, body, said, log):
    hub["bodies"][url] = body
    out = staging.submit("vector", _vector("test_loose_shp", url=url), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and said in out["problems"][0]
    assert "Put the .shp, .shx, .dbf and .prj" in out["problems"][0] and "ONE .zip" in out["problems"][0]


# ------------------------------------------------------------------- 5. figshare
class Resp:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code, self.reason = status, ""
        self.headers = CaseInsensitiveDict(headers or {})
        self._body = body

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class Net:
    script, calls = {}, []

    def __init__(self, hostname, scheme):
        pass

    def get(self, url, timeout=None, stream=None, allow_redirects=None, headers=None):
        path = "/" + url.split("/", 3)[-1].split("?")[0]
        Net.calls.append((headers["Host"], path, headers["User-Agent"]))
        return Net.script[headers["Host"] + path]

    def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    Net.script, Net.calls = {}, []
    monkeypatch.setattr(fetch_policy, "_addresses", lambda host: [ipaddress.ip_address("93.184.216.34")])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: Net(hostname, scheme))
    return Net


def _article(aid, files):
    return Resp(200, {"content-type": "application/json"}, json.dumps({
        "id": aid, "title": "Mangrove forest structure (test)",
        "files": [{"id": i, "name": n, "size": s, "is_link_only": False,
                   "download_url": f"https://ndownloader.figshare.com/files/{i}"} for i, n, s in files]}).encode())


def test_a_figshare_article_is_resolved_to_its_one_file(net, log):
    """The DOI/article page answers a server with a WAF challenge; the decline never
    pointed at the file link. The article's API lists the file, and it is fetched."""
    net.script["api.figshare.com/v2/articles/13570601/versions/1"] = _article(13570601, [
        (26040947, "GEB-2020-0221-TabS1.csv", 431254)])
    net.script["ndownloader.figshare.com/files/26040947"] = Resp(
        200, {"content-disposition": 'attachment; filename="GEB-2020-0221-TabS1.csv"'}, b"a,b\n1,2\n")
    raw, name = fetch_policy.fetch("https://figshare.com/articles/dataset/Mangrove_forest/13570601/1",
                                   max_bytes=10_000)
    log("OUTPUT", [c[:2] for c in net.calls])
    assert raw == b"a,b\n1,2\n" and name == "GEB-2020-0221-TabS1.csv"
    assert [c[0] for c in net.calls] == ["api.figshare.com", "ndownloader.figshare.com"]
    assert "SERVIR" in net.calls[0][2]


def test_a_doi_to_a_figshare_portal_with_several_files_lists_them(net, log):
    net.script["doi.org/10.25573/serc.21565671"] = Resp(
        302, {"location": "https://smithsonian.figshare.com/articles/dataset/Database_CCN/21565671"})
    net.script["api.figshare.com/v2/articles/21565671"] = _article(21565671, [
        (59698871, "CCN_cores.csv", 5111526), (59698883, "CCN_depthseries.csv", 30303567),
        (59698892, "README.txt", 4520)])
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://doi.org/10.25573/serc.21565671", max_bytes=10_000)
    msg = str(exc.value)
    log("OUTPUT", msg)
    assert msg.startswith("https://doi.org/10.25573/serc.21565671 is figshare article 21565671")
    assert "holds 3 files, and a contribution is one file" in msg
    assert "CCN_cores.csv (5.1 MB) https://ndownloader.figshare.com/files/59698871" in msg
    assert "README.txt (5 KB) https://ndownloader.figshare.com/files/59698892" in msg
    assert not any(c[0] == "smithsonian.figshare.com" for c in Net.calls)   # the page is never asked


def test_a_figshare_link_that_names_its_file_goes_straight_to_it(net, log):
    net.script["ndownloader.figshare.com/files/59698871"] = Resp(
        200, {"content-disposition": 'attachment; filename="CCN_cores.csv"'}, b"core_id\n1\n")
    raw, name = fetch_policy.fetch("https://smithsonian.figshare.com/articles/dataset/Database_CCN/"
                                   "21565671?file=59698871", max_bytes=10_000)
    assert name == "CCN_cores.csv" and [c[0] for c in Net.calls] == ["ndownloader.figshare.com"]
    # a challenge on a figshare host points at the file link too
    net.script["ndownloader.figshare.com/files/1"] = Resp(202, {"x-amzn-waf-action": "challenge"})
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://ndownloader.figshare.com/files/1", max_bytes=10_000)
    log("OUTPUT", str(exc.value))
    assert "On figshare, give the file's own link instead" in str(exc.value)


def test_a_figshare_article_whose_list_cannot_be_read_points_at_the_file_link(net, log):
    net.script["api.figshare.com/v2/articles/999999"] = Resp(404, {"content-type": "application/json"},
                                                             b'{"message": "Entity not found"}')
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://figshare.com/articles/999999", max_bytes=10_000)
    log("OUTPUT", str(exc.value))
    assert "https://ndownloader.figshare.com/files/<file id>" in str(exc.value)
    assert fetch_policy.figshare_article("https://ndownloader.figshare.com/files/26040947") is None
    assert fetch_policy.figshare_article("https://figshare.com/articles/13570601") == ("13570601", None)


# ------------------------------------------------------------------- 6. developer CLI
def test_the_contrib_cli_fetches_through_the_policy(net, hub, monkeypatch, log):
    """`contribute` (documents from a YAML manifest) used raw requests with a browser
    User-Agent, which zenodo answers 403; the MCP path had moved on."""
    # the real fetch policy, over the scripted transport
    monkeypatch.setattr(fetch_policy, "fetch", _REAL_FETCH)
    net.script["zenodo.example/records/1/files/bulletin.txt"] = Resp(
        200, {"content-disposition": 'attachment; filename="bulletin.txt"'}, b"El Nino conditions. " * 20)
    raw, name = sources._fetch("https://zenodo.example/records/1/files/bulletin.txt")
    assert name == "bulletin.txt" and "SERVIR" in Net.calls[0][2]
    net.script["zenodo.example/records/2"] = Resp(200, {"content-type": "text/html"},
                                                 b"<!DOCTYPE html><html><body>record page</body></html>")
    out = sources.contribute([{"pack": "food-security", "url": "https://zenodo.example/records/2",
                               "source": "WMO", "title": "Update", "pub_date": "2026-09",
                               "temporal": "forecast", "validation": "single-agency"}])
    log("OUTPUT", out["results"][0])
    assert out["declined"] == 1 and "returned a web page, not a document" in out["results"][0]["failures"][0]
