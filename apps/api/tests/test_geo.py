"""The deterministic spatial ops over a known fixture: exact counts, and the flood
severity threshold actually filtering. These are the numbers the LLM may only quote.
"""
from app.graph.geo import store


def test_count_features(aoi, log):
    """Count hospitals/schools inside the AOI boundary — exact integers the LLM may only quote."""
    h = store.count_features(aoi, "hospitals")
    log("CALL", "store.count_features(aoi, 'hospitals')")
    log("OUTPUT", h)
    s = store.count_features(aoi, "schools")
    log("CALL", "store.count_features(aoi, 'schools')")
    log("OUTPUT", s)
    b = store.count_features(aoi, "buildings")
    log("CALL", "store.count_features(aoi, 'buildings')")
    log("OUTPUT", b)
    log("CHECK", "fixture has 2 hospitals, 3 schools, 2 buildings inside the boundary")
    assert h["count"] == 2
    assert s["count"] == 3
    assert b["count"] == 2


def test_count_in_flood_threshold(aoi, log):
    """min_severity must actually filter: only POIs in the flooded (left) half count, and none reach sev 4."""
    a = store.count_in_hazard(aoi, "hazard_flood", "hospitals", min_severity=1)
    log("CALL", "store.count_in_hazard(aoi, 'hazard_flood', 'hospitals', min_severity=1)")
    log("OUTPUT", a)
    b = store.count_in_hazard(aoi, "hazard_flood", "schools", min_severity=1)
    log("CALL", "store.count_in_hazard(aoi, 'hazard_flood', 'schools', min_severity=1)")
    log("OUTPUT", b)
    c = store.count_in_hazard(aoi, "hazard_flood", "hospitals", min_severity=4)
    log("CALL", "store.count_in_hazard(aoi, 'hazard_flood', 'hospitals', min_severity=4)  # nothing reaches sev 4")
    log("OUTPUT", c)
    log("CHECK", "1 hospital + 1 school sit in the flooded half; sev>=4 yields 0")
    assert a["count"] == 1
    assert b["count"] == 1
    assert c["count"] == 0


def test_roads_in_flood(aoi, log):
    """Flooded road length is positive, bounded by total road length, and zero when sev>=4."""
    r = store.roads_in_hazard(aoi, "hazard_flood", min_severity=1)
    log("CALL", "store.roads_in_hazard(aoi, 'hazard_flood', min_severity=1)")
    log("OUTPUT", r)
    hi = store.roads_in_hazard(aoi, "hazard_flood", min_severity=4)
    log("CALL", "store.roads_in_hazard(aoi, 'hazard_flood', min_severity=4)")
    log("OUTPUT", hi)
    log("CHECK", f"0 < flooded({r['length_km']}) <= total({r['total_road_km']}); sev>=4 -> 0.0")
    assert 0 < r["length_km"] <= r["total_road_km"]
    assert hi["length_km"] == 0


def test_result_carries_source(aoi, log):
    """Every result carries a non-empty `source` — finalize must cite it."""
    src1 = store.roads_in_hazard(aoi, "hazard_flood")["source"]
    src2 = store.count_features(aoi, "hospitals")["source"]
    log("OUTPUT", f"roads source    = {src1}")
    log("OUTPUT", f"features source = {src2}")
    log("CHECK", "both sources are present and non-empty")
    assert src1
    assert src2


# --- place resolution must never centre an AOI on a building ------------------

def _poi(name, rank=30, cls="office"):
    return {"class": cls, "type": "government", "place_rank": rank,
            "display_name": f"{name}, Street 3, Battambang, Cambodia",
            "lon": "103.19", "lat": "13.09", "geojson": {"type": "Point"}}


def _admin(name, ring, rank=12):
    return {"class": "boundary", "type": "administrative", "place_rank": rank,
            "display_name": f"{name}, Cambodia", "lon": "103.2", "lat": "13.1",
            "geojson": {"type": "Polygon", "coordinates": [ring]}}


def test_a_point_of_interest_never_becomes_an_area(monkeypatch, log):
    """'Battambang Province' returned only a records office and a police station, and
    the platform answered about a 12 km box around the office — confidently, with
    citations. Dropping the administrative noun finds the real boundary."""
    from app.graph.geo import ingest
    ring = [[103.15, 13.05], [103.25, 13.05], [103.25, 13.15], [103.15, 13.15], [103.15, 13.05]]
    calls = []

    def fake_search(q):
        calls.append(q)
        if "province" in q.lower():
            return [_poi("Archives of Battambang Province"),
                    _poi("Gendarmerie Royale de la Province de Battambang", cls="amenity")]
        return [_admin("Battambang", ring)]

    monkeypatch.setattr(ingest, "_search", fake_search)
    km2, name, geom, how = ingest._boundary("Battambang Province, Cambodia")
    log("OUTPUT", f"{calls} -> {name!r} [{how}]")
    assert calls == ["Battambang Province, Cambodia", "Battambang, Cambodia"]
    assert name == "Battambang" and "admin boundary" in how


def test_a_genuine_building_query_declines_instead_of_inventing_an_area(monkeypatch, log):
    from app.graph.geo import ingest
    monkeypatch.setattr(ingest, "_search", lambda q: [_poi("Archives of Battambang Province")])
    try:
        ingest._boundary("Archives of Battambang Province")
        raise AssertionError("should have declined")
    except ValueError as exc:
        log("OUTPUT", str(exc))
        assert "points of interest" in str(exc) and "not a place" in str(exc)


def test_an_over_cap_admin_area_is_named_and_sized_not_silently_boxed(monkeypatch, log):
    """A province over the area cap must say so by name, never hand back a box that
    reads like the province."""
    from app.graph.geo import ingest
    big = [[100.0, 10.0], [106.0, 10.0], [106.0, 16.0], [100.0, 16.0], [100.0, 10.0]]
    monkeypatch.setattr(ingest, "_search", lambda q: [_admin("Battambang", big, rank=8)])
    km2, name, geom, how = ingest._boundary("Battambang")
    log("OUTPUT", f"{name!r} [{how}]")
    assert "box at its centre" in name and "over the" in how and "NOT the whole" in how
