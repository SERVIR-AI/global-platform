"""MGV-345: the geocoder's 'correction' must not drop words of the asked name.

'Estero Puntarenas' geocodes only to rivers named 'Estero' in Puntarenas Province;
the retry with 'Estero' answered status ok for Estero, Florida (resolve) and
Santiago del Estero, Argentina (land cover, larger cap). Nominatim stubbed with
results shaped like the real ones; a WDPA-like layer contributed through the gate."""
import json

import pytest
from shapely.geometry import box, mapping

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import ingest, vectors
from app.mcp import assemble, resolve, store

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
WDPA = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": mapping(box(-84.85, 9.97, -84.75, 10.0)),
     "properties": {"NAME": "Estero Puntarenas y Manglares Asociados", "DESIG_ENG": "Wetland", "WDPAID": 3}}]}


def _river(name, lon, lat):
    return {"class": "waterway", "type": "river", "addresstype": "river", "place_rank": 19,
            "display_name": name, "lon": str(lon), "lat": str(lat),
            "geojson": {"type": "Point", "coordinates": [lon, lat]}}


def _admin(name, geom, kind="administrative", addresstype="town", rank=16, cls="boundary"):
    c = geom.centroid
    return {"class": cls, "type": kind, "addresstype": addresstype, "place_rank": rank,
            "display_name": name, "lon": str(c.x), "lat": str(c.y), "geojson": mapping(geom)}


RIVERS = [_river("Estero, Langostino, Pavón, Golfito, Puntarenas Province, 60704, Costa Rica", -83.1075, 8.4834),
          _river("Estero Mero, Caldera, Esparza, Puntarenas Province, 60206, Costa Rica", -84.7262, 9.9553),
          _river("Estero Damas, Parrita, Puntarenas Province, 60901, Costa Rica", -84.2384, 9.4787)]
ESTERO = [_admin("Estero, Lee County, Florida, United States", box(-81.86347, 26.3894, -81.72372, 26.46382)),
          _admin("Santiago del Estero, Argentina", box(-65.2, -30.0, -61.6, -25.6), addresstype="state", rank=8),
          _river("Estero, Langostino, Pavón, Golfito, Puntarenas Province, 60704, Costa Rica", -83.1075, 8.4834)]
BATTAMBANG = box(103.15, 13.05, 103.25, 13.15)


def fake_search(q):
    k = q.strip().lower()
    if k.startswith("estero puntarenas"):            # with or without ', Costa Rica'
        return RIVERS
    if k in ("estero", "esteroo"):
        return ESTERO if k == "estero" else RIVERS[:1]
    if k == "batambang":
        return [{"class": "place", "type": "city", "place_rank": 16, "lon": "103.2", "lat": "13.1",
                 "display_name": "Battambang, Battambang Province, Cambodia",
                 "geojson": {"type": "Point", "coordinates": [103.2, 13.1]}}]
    if k == "battambang":
        return [_admin("Battambang, Battambang Province, Cambodia", BATTAMBANG)]
    return []


@pytest.fixture
def gaz(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(WDPA).encode(), "wdpa.geojson"))
    monkeypatch.setattr(ingest, "_search", fake_search)
    monkeypatch.setattr(ingest, "_GAZETTEER", {})
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    monkeypatch.setenv("GRP_AUTO_APPROVE", "1")
    monkeypatch.setattr(s, "grp_auto_approve", True)
    tok = identity.bind(OWNER)
    r = staging.submit("vector", {"layer": "cr_asp_wdpa", "url": "https://x.test/wdpa.geojson",
                                  "description": "protected areas", "source": "test fixture",
                                  "license": "CC0-1.0", "vintage": "2026-10", "name_field": "NAME",
                                  "group_field": "DESIG_ENG", "title": "Protected areas (synthetic)"}, OWNER)
    assert r["status"] in ("staged", "landed", "approved"), r
    assert "cr_asp_wdpa" in vectors.landed()
    yield tmp_path
    identity.unbind(tok)
    staging.STAGED_VECTORS.clear()


SIMILAR = "contributed areas with a similar name: Estero Puntarenas y Manglares Asociados (cr_asp_wdpa)"


def test_a_name_found_only_by_dropping_words_declines_and_names_the_contributed_area(gaz, log):
    for q in ("Estero Puntarenas", "Estero Puntarenas, Costa Rica"):
        r = ingest.resolve_place(q)
        log("OUTPUT", f"{q!r}: {r.get('note') or r.get('how')}")
        assert r["status"] == "declined", r
        assert "leaves out 'puntarenas'" in r["note"] and SIMILAR in r["note"]
        assert "Florida" not in r["note"] and "Argentina" not in r["note"]
        rt = resolve.place_time(place=q)
        assert rt["status"] == "declined" and SIMILAR in rt["note"]
    # the contributed form it offers resolves
    hit = ingest.resolve_place("Estero Puntarenas y Manglares Asociados")
    assert hit["status"] == "ok" and hit["is_contributed_boundary"]


def test_every_pack_declines_it_instead_of_another_continent(gaz, log):
    lc = assemble.assemble(pack="land-cover", place="Estero Puntarenas", year="2020",
                           vectors=["cr_asp_wdpa"])
    log("OUTPUT", lc.get("note"))
    assert lc["status"] == "declined" and "Santiago del Estero" not in json.dumps(lc)
    assert SIMILAR in lc["note"]
    risk = assemble.assemble(pack="risk", place="Estero Puntarenas", hazard="flood")
    log("OUTPUT", risk.get("note"))
    assert risk["status"] == "declined" and "leaves out 'puntarenas'" in risk["note"]


def test_a_cached_word_dropping_correction_is_re_resolved(gaz, log):
    d = get_settings().cache_dir / "estero-puntarenas"
    d.mkdir(parents=True)
    (d / "admin.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": ESTERO[0]["geojson"]}]}))
    (d / "meta.json").write_text(json.dumps({
        "name": "Estero", "area_km2": 66, "counts": {}, "resolver": ingest.RESOLVER_VERSION,
        "measure": vectors.MEASURE, "how": "admin boundary ~66 km² (corrected 'Estero Puntarenas' -> 'Estero')"}))
    with pytest.raises(ValueError, match="leaves out 'puntarenas'"):
        ingest.ensure_aoi(place="Estero Puntarenas", layers=())


def test_whole_names_and_respellings_still_resolve_in_their_own_country(gaz, log):
    r = ingest.resolve_place("Estero")                       # the whole name: the geocoder's answer
    assert r["status"] == "ok" and r["is_admin_boundary"] and r["name"] == "Estero"
    r = ingest.resolve_place("Batambang")                    # a respelling is still corrected
    log("OUTPUT", r["how"])
    assert r["status"] == "ok" and r["approximate"] is False and r["name"] == "Battambang"
    assert "(corrected 'Batambang' -> 'Battambang')" in r["how"]
    # a respelling's retry never leaves the first answer's country: Costa Rica, not Florida
    r = ingest.resolve_place("Esteroo")
    log("OUTPUT", r["how"])
    assert r["status"] == "ok" and r["approximate"] is True and not r["is_admin_boundary"]
    assert -84 < r["centroid"][0] < -82 and 8 < r["centroid"][1] < 9
    assert ingest._dropped_words("Battambang Province", "Battambang") == []
    assert ingest._dropped_words("Golfo de Nicoya", "Gulf of Nicoya") == []
    assert ingest._dropped_words("Battambang Cambodia", "Battambang", "cambodia") == []
