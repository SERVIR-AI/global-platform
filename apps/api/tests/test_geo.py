"""The deterministic spatial ops over a known fixture: exact counts, and the flood
severity threshold actually filtering. These are the numbers the LLM may only quote.
"""
from app.graph.geo import store


def test_count_features(aoi):
    assert store.count_features(aoi, "hospitals")["count"] == 2
    assert store.count_features(aoi, "schools")["count"] == 3


def test_count_in_flood_threshold(aoi):
    assert store.count_in_flood(aoi, "hospitals", min_severity=1)["count"] == 1
    assert store.count_in_flood(aoi, "schools", min_severity=1)["count"] == 1
    # nothing reaches severity 4 in the fixture
    assert store.count_in_flood(aoi, "hospitals", min_severity=4)["count"] == 0


def test_roads_in_flood(aoi):
    r = store.roads_in_flood(aoi, min_severity=1)
    assert 0 < r["length_km"] <= r["total_road_km"]
    assert store.roads_in_flood(aoi, min_severity=4)["length_km"] == 0


def test_result_carries_source(aoi):
    # finalize must cite this; it should always be present and non-empty
    assert store.roads_in_flood(aoi)["source"]
    assert store.count_features(aoi, "hospitals")["source"]
