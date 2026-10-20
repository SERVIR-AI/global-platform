"""Sprint dry run (2026-10-19): the pieces the five fixes hand each other at the seam.

assemble_pack passes a geometry and the asked measures through to a pack, so a vector
question about a coast with no land-cover map is answered for the area with no place
label and no year; a map service's own XML is named as such, never read as a raster
with "no coordinate reference system"; a Drive folder link says what a private or
missing folder means. All on synthetic data, never presented as real."""
import json

from shapely.geometry import box, mapping

from app.contrib import fetch_policy, staging
from app.mcp import assemble

from test_sprint_fix_landcover import FAR_AWAY, OWNER, lc_env  # noqa: F401  (fixture)


def test_a_vector_question_outside_every_stack_needs_neither_place_nor_year(lc_env, monkeypatch, log):
    w, s_, e, n = FAR_AWAY
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NAME": "Mangrove stand A"},
         "geometry": mapping(box(w + 0.01, s_ + 0.01, w + 0.02, s_ + 0.02))}]}
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(fc).encode(), "m.geojson"))
    out = staging.submit("vector", {"layer": "test_far_seam", "url": "https://x.test/m.geojson",
                                    "title": "Far mangroves (synthetic)", "description": "synthetic", "source": "test",
                                    "license": "CC0-1.0", "vintage": "2026", "name_field": "NAME"}, OWNER)
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="land-cover", geometry=FAR_AWAY, vectors=["test_far_seam"])
    log("OUTPUT", f"{p['status']} | {[c['kind'] for c in p.get('citations', [])]} | {p.get('note', '')[:200]}")
    assert p["status"] == "ok", p.get("note")
    assert [c["kind"] for c in p["citations"]][:2] == ["area", "vector_summary"]
    assert p["vectors"]["test_far_seam"]["features_inside"] == 1
    assert any("no land-cover class is counted" in g for g in p["gaps"])
    staging.STAGED_VECTORS.clear()


def test_a_map_service_description_is_named_not_read_as_a_raster(log):
    caps = (b'<?xml version="1.0" encoding="UTF-8"?>\n<WMS_Capabilities version="1.3.0" '
            b'xmlns="http://www.opengis.net/wms"><Service><Name>WMS</Name></Service></WMS_Capabilities>')
    said = staging._service_xml_problem(caps, "https://example.test/wms?request=GetCapabilities", "raster file")
    log("OUTPUT", said)
    assert "WMS service description (XML), not a raster file" in said and "WCS GetCoverage" in said
    err = (b'<?xml version="1.0"?><ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1">'
           b'<ows:Exception><ows:ExceptionText>Coverage not found</ows:ExceptionText></ows:Exception>'
           b'</ows:ExceptionReport>')
    assert "Coverage not found" in staging._service_xml_problem(err, "https://example.test/wcs", "raster file")
    assert staging._service_xml_problem(b"II*\x00 not xml", "https://example.test/x.tif", "raster file") is None


def test_a_drive_folder_link_says_what_a_private_or_missing_folder_means():
    said = fetch_policy.check_url("https://drive.google.com/drive/folders/1PLJZMSjFvMlFjwjghWsxTVYdopvKukk7")
    text = " ".join(said)
    assert "FOLDER link" in text and "not shared or the link is wrong" in text
    assert text.startswith("this is a Google Drive FOLDER link: give the link of the file inside it")
    assert "one file per contribution" not in text and ".zip" not in text
