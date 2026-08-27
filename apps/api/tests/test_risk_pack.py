"""The RISK pack end to end, offline — the second domain riding the same loop.

Uses the hermetic Testville AOI (conftest): flood severity 3 on the west half;
1 of 2 hospitals, 1 of 3 schools, 1 of 2 buildings and part of one road exposed.
Numbers the gate can check, no network, no Drive."""

import pytest

from app.mcp import assemble, packs, publish
from app.risk import synthesis as risk


@pytest.fixture
def offline_aoi(monkeypatch, aoi):
    """Route the gatherer at Testville instead of Nominatim/Overpass/Drive."""
    monkeypatch.setattr(risk.ingest, "ensure_aoi",
                        lambda place=None, geometry=None, layers=None: dict(aoi))
    monkeypatch.setattr(risk.ingest, "hazard_clip",
                        lambda a, layer: a["hazard_flood"])
    return aoi


def test_the_risk_pack_assembles_with_real_numbers(offline_aoi, log):
    p = assemble.assemble(place="Testville", hazard="flood")
    assert p["status"] == "ok", p.get("note")
    log("OUTPUT", f"pack={p['pack']} citations={len(p['citations'])}")
    assert p["pack"] == "risk"
    assert p["target"] == {"place": "Testville", "hazard": "flood"}
    assert "country" not in p                       # FS keys stay FS-only
    assert p["required_sections"][0] == "## What the numbers show"
    by_title = {c["title"]: c for c in p["citations"]}
    hosp = by_title["hospitals vs hazard_flood"]
    log("OUTPUT", hosp["text"][:90])
    assert "1 of 2 hospitals" in hosp["text"]       # the number IS in the text
    assert hosp["retrieval"] == "computed-at-pack-time"
    assert hosp["series"]["categorical"] is True
    assert sum(pt["v"] for pt in hosp["series"]["points"]) == 1
    # the hazard layer passport declares flood lineage; method is config
    assert "JRC GLOFAS" in by_title["Flood hazard (severity classes)"]["text"] \
        if "Flood hazard (severity classes)" in by_title else True
    kinds = {c["kind"] for c in p["citations"]}
    assert {"exposure", "hazard_layer", "method"} <= kinds
    # gaps are content
    assert any("no risk document corpus" in g for g in p["gaps"])


def test_the_pack_carries_a_resolvable_viz(offline_aoi, log):
    from app.mcp import store
    p = assemble.assemble(place="Testville", hazard="flood")
    stored = store.load_pack(p["pack_id"])
    viz = stored["viz"]
    log("OUTPUT", f"viz keys: {sorted(viz)[:6]}")
    assert viz["hazard"] == "hazard_flood"
    assert viz["aoi"]["type"] == "Feature"
    assert viz["legend"] and viz["bounds"]
    assert viz["hazard_layer"]["geojson"]["features"]     # vectorized polygons
    assert "viz" not in p["stats"]                        # not duplicated in stats


def test_the_full_risk_loop_gates_and_receipts(offline_aoi, log):
    p = assemble.assemble(place="Testville", hazard="flood")
    draft = (
        "## What the numbers show\n\n"
        "1 of 2 hospitals and 1 of 3 schools fall in flood hazard class 3 [1][2].\n\n"
        "## Method and validation\n\n"
        "Assets are sampled against the clipped hazard raster; severity classes are "
        "the provider's [6].\n\n"
        "## What's missing and how to weigh it\n\n"
        "No risk document corpus exists yet, and raster vintages are unrecorded [5].\n\n"
        "## Reading the severity scale\n\n"
        "Classes run 1 to 5; exposed assets here sit in class 3 [5].")
    out = publish.answer(p["pack_id"], draft, question="flood exposure in Testville")
    log("OUTPUT", f"passed={out.get('passed')} failures={out.get('failures')}")
    assert out["status"] == "ok" and out["passed"] is True
    assert out["target"] == {"place": "Testville", "hazard": "flood"}
    f = out["evidence_freshness"]
    assert f["pulled_sources"] == []                     # nothing live in risk v0
    assert len(f["computed_sources"]) == 5               # 4 exposure + hazard layer
    assert out["insight"]["series"]                      # categorical series flow
    assert out["insight"]["pack"] == "risk"


def test_unknown_hazard_declines_with_the_menu(offline_aoi, log):
    p = assemble.assemble(place="Testville", hazard="volcano")
    log("OUTPUT", p.get("note", "")[:90])
    assert p["status"] == "declined"
    assert "available" in p["note"] and "flood" in p["note"]


def test_a_clip_straddling_the_raster_extent_is_georeferenced_correctly(tif_writer, tmp_path, log):
    """CRITICAL, from adversarial review: read() crops the array to the raster's
    extent but window_transform described the uncropped request, shifting the
    whole clip by the out-of-extent margin — every exposure lookup then sampled
    the wrong pixel, silently."""
    import json as _json
    import numpy as np
    import rasterio
    from app.graph.geo import ingest

    # raster spans lon 100.0-100.1; AOI requests from 99.95 (straddles the west edge)
    data = np.arange(100, dtype="int16").reshape(10, 10) % 5 + 1
    src = tif_writer(data, bounds=(100.0, 13.0, 100.1, 13.1), name="src_x.tif")
    adir = tmp_path / "aoi"; adir.mkdir(exist_ok=True)
    admin = adir / "admin.geojson"
    admin.write_text(_json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [99.95, 13.02], [100.05, 13.02], [100.05, 13.08],
            [99.95, 13.08], [99.95, 13.02]]]}}]}))
    aoi = {"admin": str(admin), "name": "edge-case"}

    import app.graph.geo.ingest as ing
    orig = ing.source_raster
    ing.source_raster = lambda layer: src
    try:
        clip = ingest.hazard_clip(aoi, "hazard_x")
    finally:
        ing.source_raster = orig
    with rasterio.open(clip) as c:
        left = c.transform.c
        log("OUTPUT", f"clip left edge lon = {left}")
        # the clip must START at the raster's true edge, not the requested 99.95-ish
        assert left >= 100.0 - 1e-9, "clip georeferenced into the void west of the raster"


def test_fully_outside_the_raster_declines_rather_than_tracebacks(tif_writer, tmp_path, log):
    import json as _json
    import numpy as np
    from app.graph.geo import ingest
    data = np.ones((4, 4), dtype="int16")
    src = tif_writer(data, bounds=(100.0, 13.0, 100.1, 13.1), name="src_y.tif")
    adir = tmp_path / "aoi"; adir.mkdir(exist_ok=True)
    admin = adir / "admin.geojson"
    admin.write_text(_json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [36.7, -1.4], [36.9, -1.4], [36.9, -1.2], [36.7, -1.2], [36.7, -1.4]]]}}]}))
    aoi = {"admin": str(admin), "name": "Nairobi-ish"}
    import app.graph.geo.ingest as ing
    orig = ing.source_raster
    ing.source_raster = lambda layer: src
    try:
        import pytest as _pytest
        with _pytest.raises(ValueError, match="outside"):
            ingest.hazard_clip(aoi, "hazard_y")
    finally:
        ing.source_raster = orig
    log("CHECK", "outside-coverage raises ValueError -> governed decline upstream")
