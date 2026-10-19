"""Place names: contributed gazetteers vs the geocoder (mangrove sprint dry run).

A contributed polygon layer with a name field is a gazetteer. The dry run showed it
hijacking places: part-of-name matches ("Gulf of Nicoya" -> the Nicoya canton,
"Bragança, Pará, Brazil" -> a district in Heredia), declines whose own suggestions
were declined again, different areas of one name merged ("Colorado"), and a 12 km
box answered as if it were a gulf. Two admin layers sharing names and a WDPA-like
layer, contributed through the gate, with the geocoder stubbed. Synthetic data."""
import json
import re

import pytest
from shapely.geometry import box, mapping, shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import ingest, vectors
from app.mcp import assemble, resolve, store

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")


def _ft(geom, **props):
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


CANTONS = {"type": "FeatureCollection", "features": [
    _ft(box(-85.7, 9.9, -85.3, 10.3), canton="Nicoya", province="Guanacaste", code="C01", area_sqkm=1300.5),
    _ft(box(-85.2, 10.1, -84.9, 10.4), canton="Abangares", province="Guanacaste", code="C02", area_sqkm=650.1),
    _ft(box(-83.9, 10.3, -83.5, 10.7), canton="Pococí", province="Limón", code="C03", area_sqkm=949.0),
    _ft(box(-84.9, 9.9, -84.7, 10.05), canton="Puntarenas", province="Puntarenas", code="C04", area_sqkm=300.0),
    _ft(box(-84.15, 10.0, -84.1, 10.05), canton="San Rafael", province="Heredia", code="C05", area_sqkm=30.0),
]}
DISTRICTS = {"type": "FeatureCollection", "features": [
    _ft(box(-85.5, 10.1, -85.4, 10.2), district="Nicoya", canton="Nicoya", code="D01", area_sqkm=120.0),
    _ft(box(-85.2, 10.1, -85.05, 10.2), district="Colorado", canton="Abangares", code="D02", area_sqkm=198.6),
    _ft(box(-83.7, 10.5, -83.5, 10.7), district="Colorado", canton="Pococí", code="D03", area_sqkm=949.0),
    _ft(box(-84.3, 9.9, -84.25, 9.95), district="Colón", canton="Mora", code="D04", area_sqkm=40.0),
    _ft(box(-84.06, 10.0, -84.04, 10.02), district="Pará", canton="Santo Domingo", code="D05", area_sqkm=3.0),
    _ft(box(-83.9, 9.9, -83.85, 9.95), district="San Rafael", canton="Oreamuno", code="D06", area_sqkm=10.0),
    _ft(box(-84.25, 10.0, -84.2, 10.05), district="San Rafael", canton="Alajuela", code="D07", area_sqkm=12.0),
    _ft(box(-84.85, 9.95, -84.8, 10.0), district="Puntarenas", canton="Puntarenas", code="D08", area_sqkm=35.0),
    _ft(box(-83.6, 8.8, -83.4, 9.0), district="Sierpe", canton="Osa", code="D09", area_sqkm=638.0),
    _ft(box(-85.0, 10.05, -84.95, 10.1), district="Chira", canton="Puntarenas", code="D10", area_sqkm=43.0),
    # one district drawn as two polygons: the same attributes but its measures
    _ft(box(-85.0, 9.9, -84.95, 9.95), district="Lepanto", canton="Puntarenas", code="D11",
        area_sqkm=30.0, center_lat=9.925),
    _ft(box(-84.95, 9.95, -84.9, 10.0), district="Lepanto", canton="Puntarenas", code="D11",
        area_sqkm=20.0, center_lat=9.975),
]}
# WDPA-like: no declared countries, so a country after a comma is checked against the
# geocoder's boundary for it; one name is two records (a park and a Ramsar site).
WDPA = {"type": "FeatureCollection", "features": [
    _ft(box(-85.4, 10.3, -85.25, 10.4), NAME="Palo Verde", DESIG_ENG="National Park", WDPAID=1, GIS_AREA=180.0),
    _ft(box(-85.42, 10.28, -85.24, 10.42), NAME="Palo Verde",
        DESIG_ENG="Ramsar Site, Wetland of International Importance", WDPAID=2, GIS_AREA=200.0),
    _ft(box(-84.85, 9.97, -84.75, 10.0), NAME="Estero Puntarenas y Manglares Asociados",
        DESIG_ENG="Wetland", WDPAID=3, GIS_AREA=50.0),
    _ft(box(-83.7, 8.8, -83.5, 9.0), NAME="Humedal Nacional Térraba-Sierpe", DESIG_ENG="Wetland",
        WDPAID=4, GIS_AREA=268.9),
]}
LAYERS = {
    "t_cantons": (CANTONS, {"name_field": "canton", "group_field": "province", "countries": ["Testland"],
                            "title": "Testland cantons (synthetic)"}),
    "t_districts": (DISTRICTS, {"name_field": "district", "group_field": "canton", "countries": ["Testland"],
                                "title": "Testland districts (synthetic)"}),
    "t_wdpa": (WDPA, {"name_field": "NAME", "group_field": "DESIG_ENG",
                      "title": "Testland protected areas, WDPA-like (synthetic)"}),
}

PANAMA = box(-82.9, 7.2, -77.2, 9.6)
TESTLAND = box(-86.0, 8.0, -82.95, 11.5)
GULF = (-85.05, 9.85)          # where the geocoder puts "Gulf of Nicoya": a point, no boundary


def _country(name, geom):
    c = geom.centroid
    return {"class": "boundary", "type": "administrative", "addresstype": "country", "place_rank": 4,
            "display_name": name, "lon": str(c.x), "lat": str(c.y), "geojson": mapping(geom)}


def fake_search(q):
    k = q.strip().lower()
    if k == "panama":
        return [_country("Panama", PANAMA)]
    if k == "testland":
        return [_country("Testland", TESTLAND)]
    if k == "colón, panama":
        return [{"class": "boundary", "type": "administrative", "addresstype": "state", "place_rank": 8,
                 "display_name": "Colón, Panama", "lon": "-80", "lat": "9.25",
                 "geojson": mapping(box(-80.5, 9.0, -79.5, 9.5))}]
    if k.startswith(("gulf of nicoya", "golfo de nicoya")):
        return [{"class": "natural", "type": "bay", "place_rank": 22,
                 "display_name": "Gulf of Nicoya, Testland", "lon": str(GULF[0]), "lat": str(GULF[1]),
                 "geojson": {"type": "Point", "coordinates": list(GULF)}}]
    return []


@pytest.fixture
def gaz(tmp_path, monkeypatch):
    """Three named polygon layers contributed through the gate (auto-approved), a
    stubbed geocoder, nothing on the network."""
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    bodies = {f"https://x.test/{layer}.geojson": json.dumps(fc).encode() for layer, (fc, _m) in LAYERS.items()}
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (bodies[url], url.rsplit("/", 1)[-1]))
    monkeypatch.setattr(ingest, "_search", fake_search)
    monkeypatch.setattr(ingest, "_GAZETTEER", {})
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    monkeypatch.setenv("GRP_AUTO_APPROVE", "1")
    monkeypatch.setattr(s, "grp_auto_approve", True)
    tok = identity.bind(OWNER)
    for layer, (_fc, m) in LAYERS.items():
        r = staging.submit("vector", {"layer": layer, "url": f"https://x.test/{layer}.geojson",
                                      "description": "synthetic polygons", "source": "test fixture",
                                      "license": "CC0-1.0", "vintage": "2026-10", **m}, OWNER)
        assert r["status"] in ("staged", "landed", "approved"), r
    assert set(LAYERS) <= set(vectors.landed())
    yield tmp_path
    identity.unbind(tok)
    staging.STAGED_VECTORS.clear()


def _bbox(r):
    return tuple(round(v, 4) for v in r["bbox"])


def test_part_of_a_name_never_hijacks_a_place(gaz, log):
    """'Gulf of Nicoya' is not the Nicoya canton; 'Bragança, Pará, Brazil' is not the
    Pará district. A part-of-name match falls to the geocoder and is only NAMED."""
    for q in ("Gulf of Nicoya", "Golfo de Nicoya", "Gulf of Nicoya, Testland"):
        r = ingest.resolve_place(q)
        log("OUTPUT", f"{q!r}: {r.get('how') or r.get('note')}")
        assert r["status"] == "ok" and not r["is_contributed_boundary"], r
        assert r["similar_contributed_areas"] == ["Nicoya (t_cantons)", "Nicoya (t_districts)"]
        assert ("contributed areas with a similar name: Nicoya (t_cantons); Nicoya (t_districts); "
                "name one exactly to use it") in r["how"]
    r = ingest.resolve_place("Bragança, Pará, Brazil")
    log("OUTPUT", r["note"])
    assert r["status"] == "declined" and "t_districts" not in r["note"] and "contributed" not in r["note"]
    # 'Térraba-Sierpe' is part of the wetland's name and holds the district's: neither is used
    r = ingest.resolve_place("Térraba-Sierpe")
    log("OUTPUT", r["note"])
    assert r["status"] == "declined" and r["note"].startswith("could not find 'Térraba-Sierpe'")
    assert ("similar name: Humedal Nacional Térraba-Sierpe (t_wdpa); Sierpe (t_districts); "
            "name one exactly to use it") in r["note"]
    # a prefix: the longer name first, then the names the query holds
    r = ingest.resolve_place("Estero Puntarenas")
    log("OUTPUT", r["note"])
    assert ("similar name: Estero Puntarenas y Manglares Asociados (t_wdpa); Puntarenas (t_cantons); "
            "Puntarenas (t_districts)") in r["note"]


def test_a_country_after_a_comma_must_hold_the_feature(gaz, log):
    """'Colón, Panama' is not the Testland district Colón: its layer declares Testland.
    A layer that declares nothing is checked against the geocoder's country boundary."""
    r = ingest.resolve_place("Colón, Panama")
    log("OUTPUT", r["how"])
    assert r["status"] == "ok" and r["is_admin_boundary"] and _bbox(r) == (-80.5, 9.0, -79.5, 9.5)
    assert r["contributed_not_used"] == [{"name": "Colón (t_districts)",
                                          "reason": "its layer covers Testland, not Panama"}]
    assert "its layer covers Testland, not Panama" in r["how"]
    assert ingest.resolve_place("Colón")["is_contributed_boundary"]
    assert ingest.resolve_place("Colón, Testland")["is_contributed_boundary"]
    # no declared countries: the geocoder's boundary decides
    out = ingest.resolve_place("Estero Puntarenas y Manglares Asociados, Panama")
    log("OUTPUT", out["note"])
    assert out["status"] == "declined" and "it does not lie in Panama" in out["note"]
    inside = ingest.resolve_place("Estero Puntarenas y Manglares Asociados, Testland")
    assert inside["status"] == "ok" and inside["is_contributed_boundary"]
    # a country the geocoder cannot place rules nothing in
    assert ingest.resolve_place("Pará, Brazil")["status"] == "declined"
    assert ingest.resolve_place("Pará")["is_contributed_boundary"]


def test_every_ambiguity_decline_offers_forms_that_resolve(gaz, log):
    """Different areas sharing a name are listed, never merged or picked — and each
    form the decline offers resolves to exactly one of them."""
    expected = {
        "Colorado": ["Colorado (t_districts: Abangares)", "Colorado (t_districts: Pococí)"],
        "Palo Verde": ["Palo Verde (t_wdpa: National Park)",
                       "Palo Verde (t_wdpa: Ramsar Site, Wetland of International Importance)"],
        "Nicoya": ["Nicoya (t_cantons)", "Nicoya (t_districts)"],
        "San Rafael": ["San Rafael (t_cantons)", "San Rafael (t_districts: Oreamuno)",
                       "San Rafael (t_districts: Alajuela)"],
        "Puntarenas": ["Puntarenas (t_cantons)", "Puntarenas (t_districts)"],
    }
    for q, forms in expected.items():
        r = ingest.resolve_place(q)
        log("OUTPUT", r["note"])
        assert r["status"] == "declined" and r["candidates"] == forms, r
        assert "Pass one of them exactly as written here" in r["note"]
        example = re.search(r"after a comma \('([^']+)'\)", r["note"])
        if example:
            assert ingest.resolve_place(example.group(1))["is_contributed_boundary"], example.group(1)
        boxes = set()
        for form in r["candidates"]:
            hit = ingest.resolve_place(form)
            log("CHECK", f"{form!r} -> {hit.get('name')} {hit.get('bbox')}")
            assert hit["status"] == "ok" and hit["is_contributed_boundary"], hit
            assert f"this is {form}" in hit["how"]
            boxes.add(_bbox(hit))
        assert len(boxes) == len(forms)
    # the parent after a comma, or in a bracket, narrows to the one area it names
    colorado = _bbox(ingest.resolve_place("Colorado (t_districts: Abangares)"))
    assert colorado == (-85.2, 10.1, -85.05, 10.2)
    for q in ("Colorado, Abangares", "Colorado (Abangares)", "colorado, ABANGARES, Testland",
              "Colorado (T_DISTRICTS: abangares)"):
        hit = ingest.resolve_place(q)
        assert hit["status"] == "ok" and _bbox(hit) == colorado and hit["name"] == "Colorado (Abangares)", q
    assert _bbox(ingest.resolve_place("Palo Verde, National Park")) == (-85.4, 10.3, -85.25, 10.4)
    # only the canton carries a province: the parent names exactly one area
    assert ingest.resolve_place("Nicoya, Guanacaste")["how"].startswith("contributed boundary: Nicoya from Testland cantons")
    # a part that holds several still declines, with the forms that tell them apart
    many = ingest.resolve_place("San Rafael, Testland")
    assert many["status"] == "declined" and many["candidates"] == expected["San Rafael"]
    # a layer that does not hold the name says what it does hold, in forms that resolve
    miss = ingest.resolve_place("Colorado (t_cantons)")
    log("OUTPUT", miss["note"])
    assert miss["status"] == "declined" and "t_cantons holds no area named 'Colorado'" in miss["note"]
    assert all(ingest.resolve_place(f)["status"] == "ok" for f in miss["candidates"])
    wrong = ingest.resolve_place("Colorado (t_districts: Heredia)")
    assert wrong["status"] == "declined" and wrong["candidates"] == expected["Colorado"]


def test_parts_of_one_area_merge_and_exact_names_still_resolve(gaz, log):
    lep = ingest.resolve_place("Lepanto")
    log("OUTPUT", lep["how"])
    assert lep["status"] == "ok" and "2 parts of this one area were merged" in lep["how"]
    assert _bbox(lep) == (-85.0, 9.9, -84.9, 10.0)
    for q, layer in (("humedal nacional terraba-sierpe", "t_wdpa"), ("Humedal Nacional Térraba-Sierpe", "t_wdpa"),
                     ("ABANGARES", "t_cantons"), ("Chira", "t_districts"), ("Pococí", "t_cantons")):
        r = ingest.resolve_place(q)
        assert r["status"] == "ok" and r["how"].startswith("contributed boundary") and f"({layer};" in r["how"], r
    aoi = ingest.ensure_aoi(place="Humedal Nacional Terraba-Sierpe", layers=())
    assert aoi["how"].startswith("contributed boundary") and not aoi["approximate"]
    # ensure_aoi declines the same way, and takes the forms it offers
    with pytest.raises(ingest.AmbiguousPlace) as exc:
        ingest.ensure_aoi(place="Colorado", layers=())
    assert exc.value.candidates == ["Colorado (t_districts: Abangares)", "Colorado (t_districts: Pococí)"]
    aoi = ingest.ensure_aoi(place=exc.value.candidates[1], layers=())
    assert aoi["name"] == "Colorado (Pococí)" and aoi["how"].startswith("contributed boundary")
    got = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
    assert tuple(round(v, 4) for v in got.bounds) == (-83.7, 10.5, -83.5, 10.7)


def test_an_approximate_box_says_so_and_offers_the_contributed_areas_near_it(gaz, log):
    r = ingest.resolve_place("Gulf of Nicoya")
    log("OUTPUT", r["how"])
    assert r["status"] == "ok" and r["approximate"] is True and not r["is_admin_boundary"]
    assert "an approximation, NOT the boundary of Gulf of Nicoya" in r["how"]
    near = r["nearby_contributed_areas"]
    kms = [n["km"] for n in near]
    assert near and kms == sorted(kms) and max(kms) <= ingest.NEAR_KM
    assert near[0]["name"] == "Lepanto (t_districts)"
    names = [n["name"] for n in near]
    assert "Sierpe (t_districts)" not in names and "Colorado (t_districts: Pococí)" not in names   # far away
    assert all(ingest.resolve_place(n)["is_contributed_boundary"] for n in names)
    assert f"contributed areas near this point: {names[0]}" in r["how"]
    # a real boundary is not approximate
    assert ingest.resolve_place("Colón, Panama")["approximate"] is False
    # ...and the AOI bundle every pack reads carries both
    aoi = ingest.ensure_aoi(place="Gulf of Nicoya", layers=())
    assert aoi["approximate"] is True and [n["name"] for n in aoi["nearby_contributed"]] == names
    # resolve_place_time passes them through
    rt = resolve.place_time(place="Gulf of Nicoya")
    assert rt["status"] == "ok" and rt["place"]["approximate"] is True
    # ...and a decline's reason and forms sit at the top, where every decline's do
    rt = resolve.place_time(place="Colorado")
    assert rt["status"] == "declined" and "names 2 different contributed areas" in rt["note"]
    assert rt["candidates"] == ["Colorado (t_districts: Abangares)", "Colorado (t_districts: Pococí)"]


def test_a_box_cached_before_it_said_approximate_is_re_resolved_alone(gaz, log):
    """Boxes cached by the previous code carry no flag: those are re-resolved; a cached
    admin boundary is served as it was, its OSM layers kept."""
    cache = get_settings().cache_dir

    def seed(slug, how, geom):
        d = cache / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "admin.geojson").write_text(json.dumps({"type": "FeatureCollection",
                                                     "features": [_ft(geom, name=slug)]}))
        (d / "roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        (d / "meta.json").write_text(json.dumps({"name": slug, "area_km2": 579, "how": how,
                                                 "counts": {"roads": 0}, "resolver": ingest.RESOLVER_VERSION}))
        return d
    old_box = seed("gulf-of-nicoya", "12 km radius box around Gulf of Nicoya (no admin boundary found)",
                   box(-85.16, 9.74, -84.94, 9.96))
    kept = seed("somewhere", "admin boundary ~579 km²", box(-84.6, 9.6, -84.4, 9.8))
    aoi = ingest.ensure_aoi(place="Gulf of Nicoya", layers=())
    assert aoi["approximate"] is True and "NOT the boundary" in aoi["how"] and aoi["nearby_contributed"]
    assert not (old_box / "roads.geojson").exists()
    aoi = ingest.ensure_aoi(place="Somewhere", layers=("roads",))
    assert aoi["how"] == "admin boundary ~579 km²" and not aoi["approximate"]
    assert (kept / "roads.geojson").exists()


def test_other_packs_and_the_within_seam_decline_the_same_way(gaz, log):
    """The land-cover and risk packs and assemble's within= all resolve places
    through the same seam: an ambiguous name declines with forms that resolve, and a
    box offers the contributed areas near it."""
    lc = assemble.assemble(pack="land-cover", place="Gulf of Nicoya", year="2020")
    log("OUTPUT", lc["note"])
    assert lc["status"] == "declined" and "contributed areas near this point: Lepanto (t_districts)" in lc["note"]
    risk = assemble.assemble(pack="risk", place="Colorado", hazard="flood")
    log("OUTPUT", risk["note"])
    assert risk["status"] == "declined" and "names 2 different contributed areas" in risk["note"]
    assert "Colorado (t_districts: Abangares)" in risk["note"]
    within = assemble.assemble(pack="land-cover", place="Palo Verde", year="2020", within="t_wdpa")
    assert within["status"] == "declined" and "Palo Verde (t_wdpa: National Park)" in within["note"]


def test_the_name_index_follows_the_layer_file(gaz, log):
    """Built once per master file, rebuilt when the file changes, gone with the layer."""
    assert ingest.resolve_place("Nuevo")["status"] == "declined"
    path = vectors.master_path(vectors.landed()["t_districts"])
    fc = json.load(open(path))
    fc["features"].append(_ft(box(-84.6, 9.6, -84.5, 9.7), district="Nuevo", canton="Mora", code="D99"))
    json.dump(fc, open(path, "w"))
    r = ingest.resolve_place("Nuevo")
    assert r["status"] == "ok" and r["is_contributed_boundary"], r
    vectors.unregister("t_districts")
    assert ingest.resolve_place("Nuevo")["status"] == "declined"
    assert ingest.resolve_place("Colorado")["status"] == "declined"
    assert "different contributed areas" not in ingest.resolve_place("Colorado")["note"]
