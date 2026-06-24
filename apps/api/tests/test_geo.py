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
    log("CHECK", "fixture has 2 hospitals and 3 schools inside the boundary")
    assert h["count"] == 2
    assert s["count"] == 3


def test_count_in_flood_threshold(aoi, log):
    """min_severity must actually filter: only POIs in the flooded (left) half count, and none reach sev 4."""
    a = store.count_in_flood(aoi, "hospitals", min_severity=1)
    log("CALL", "store.count_in_flood(aoi, 'hospitals', min_severity=1)")
    log("OUTPUT", a)
    b = store.count_in_flood(aoi, "schools", min_severity=1)
    log("CALL", "store.count_in_flood(aoi, 'schools', min_severity=1)")
    log("OUTPUT", b)
    c = store.count_in_flood(aoi, "hospitals", min_severity=4)
    log("CALL", "store.count_in_flood(aoi, 'hospitals', min_severity=4)  # nothing reaches sev 4")
    log("OUTPUT", c)
    log("CHECK", "1 hospital + 1 school sit in the flooded half; sev>=4 yields 0")
    assert a["count"] == 1
    assert b["count"] == 1
    assert c["count"] == 0


def test_roads_in_flood(aoi, log):
    """Flooded road length is positive, bounded by total road length, and zero when sev>=4."""
    r = store.roads_in_flood(aoi, min_severity=1)
    log("CALL", "store.roads_in_flood(aoi, min_severity=1)")
    log("OUTPUT", r)
    hi = store.roads_in_flood(aoi, min_severity=4)
    log("CALL", "store.roads_in_flood(aoi, min_severity=4)")
    log("OUTPUT", hi)
    log("CHECK", f"0 < flooded({r['length_km']}) <= total({r['total_road_km']}); sev>=4 -> 0.0")
    assert 0 < r["length_km"] <= r["total_road_km"]
    assert hi["length_km"] == 0


def test_result_carries_source(aoi, log):
    """Every result carries a non-empty `source` — finalize must cite it."""
    src1 = store.roads_in_flood(aoi)["source"]
    src2 = store.count_features(aoi, "hospitals")["source"]
    log("OUTPUT", f"roads source    = {src1}")
    log("OUTPUT", f"features source = {src2}")
    log("CHECK", "both sources are present and non-empty")
    assert src1
    assert src2
