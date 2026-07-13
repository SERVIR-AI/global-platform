"""Food-security slices A1 (module scaffold) + A2 (thin crop-conditions query).

No network — the HTTP seam is monkeypatched with canned FeatureServer responses
(`_fetch_json` for client-logic tests; the `_SESSION` stub for the error-body /
classification tests one level down); month/crop resolution, SQL building,
caching, pagination, and the routes stay real. The live service round-trip lives
in test_food_security_live.py (opt-in, CROPMONITOR_LIVE=1).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.food_security import cropmonitor
from app.main import app

# The service's real quirks, miniaturized: December is skipped (no 202212), crop
# names are season-level, and a blank Conditions value means out of season.
LAYERS = {"layers": [
    {"id": 0, "name": "Global_Synthesis_202210"},
    {"id": 1, "name": "Global_Synthesis_202211"},
    {"id": 2, "name": "Global_Synthesis_202301"},
    {"id": 3, "name": "Global_Synthesis_202604"},
]}
# "Rice" vs "Rice 1" pins exact-match precedence over containment (not a live
# pair — the live list has only Rice 1/2/3 — but the precedence rule needs it).
CROPS = {"features": [{"attributes": {"Crop": c}} for c in
                      ["Winter Wheat", "Spring Wheat", "Maize 1", "Maize 2",
                       "Millet", "Teff", "Rice", "Rice 1"]]}
ROWS = {"features": [
    {"attributes": {"Country": "Kenya", "Region": "Rift Valley", "Crop": "Maize 1",
                    "Conditions": "Watch", "Drivers": "Dry"}},
    {"attributes": {"Country": "Kenya", "Region": "Western", "Crop": "Maize 1",
                    "Conditions": "Favourable", "Drivers": ""}},
    # The live service pads out-of-season blanks (" "), not "" — keep that quirk here.
    {"attributes": {"Country": "Kenya", "Region": "Coast", "Crop": "Maize 2",
                    "Conditions": " ", "Drivers": " "}},
]}


def _serve(url: str, params: dict) -> dict:
    if url.endswith("FeatureServer"):
        return LAYERS
    if params.get("returnDistinctValues") == "true":
        return CROPS
    return ROWS


@pytest.fixture
def fs_env(monkeypatch, tmp_path):
    """Isolated cache dir + the canned service. Yields the (url, params) call log."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    calls = []
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: (calls.append((url, dict(params))),
                                             _serve(url, params))[1])
    return calls


def test_health_route_mounts_without_touching_risk_routes():
    """A1: the module mounts at /api/food-security; the existing health route is unaffected."""
    client = TestClient(app)
    r = client.get("/api/food-security/health")
    assert r.status_code == 200
    assert r.json()["module"] == "food-security"
    assert client.get("/api/health").json()["status"] == "ok"


def test_resolve_month_reads_the_live_layer_list(fs_env):
    """A2.1: month -> layer id comes from the service's layer list, both input formats."""
    assert cropmonitor.resolve_month("2022-11") == ("2022-11", 1)
    assert cropmonitor.resolve_month("202211") == ("2022-11", 1)
    assert cropmonitor.resolve_month("2023-1") == ("2023-01", 2)
    assert cropmonitor.resolve_month(None) == ("2026-04", 3)   # None -> latest published


def test_resolve_month_handles_the_december_gap(fs_env):
    """A2.1: a skipped month declines with the nearest published alternative, no guessing."""
    with pytest.raises(cropmonitor.MonthNotAvailable) as exc:
        cropmonitor.resolve_month("2022-12")
    assert exc.value.nearest in ("2022-11", "2023-01")
    assert "not published" in str(exc.value)


def test_resolve_month_rejects_garbage(fs_env):
    """Malformed months are caller errors (400), not coverage gaps (404) — including
    the ambiguous 5-digit form ('20261' could be Jan or Dec plus a typo)."""
    for bad in ("april", "2026-13", "20261"):
        with pytest.raises(ValueError):
            cropmonitor.resolve_month(bad)


def test_resolve_crop_maps_synonyms_to_season_names(fs_env):
    """A2.3: 'wheat' -> both season rows; an exact name beats containment matches."""
    assert cropmonitor.resolve_crop("wheat", 3) == ["Spring Wheat", "Winter Wheat"]
    assert cropmonitor.resolve_crop("maize", 3) == ["Maize 1", "Maize 2"]
    assert cropmonitor.resolve_crop("rice", 3) == ["Rice"]       # exact, not Rice+Rice 1
    assert cropmonitor.resolve_crop("ric", 3) == ["Rice", "Rice 1"]
    with pytest.raises(cropmonitor.CropNotFound) as exc:
        cropmonitor.resolve_crop("banana", 3)
    assert "Teff" in exc.value.available


def test_conditions_reports_ratings_with_provenance(fs_env):
    """A2.2: real per-region ratings + drivers, summarized by class, and the where
    clause is both echoed as provenance AND actually sent to the service."""
    res = cropmonitor.conditions(month="2026-04", crop="maize", place="Kenya")
    assert res["summary"] == {"Watch": 1, "Favourable": 1, "Out of season": 1}
    assert res["count"] == 3
    assert res["records"][0] == {"country": "Kenya", "region": "Rift Valley",
                                 "crop": "Maize 1", "conditions": "Watch", "drivers": "Dry"}
    assert res["crops_matched"] == ["Maize 1", "Maize 2"]
    assert res["as_of"] == "2026-04"
    assert "GEOGLAM" in res["source"]
    assert "stale_data" not in res                   # live data carries no stale marker

    expected_where = ("Crop IN ('Maize 1', 'Maize 2') AND "
                      "(UPPER(Country) = UPPER('Kenya') OR UPPER(Region) = UPPER('Kenya'))")
    assert res["query"]["where"] == expected_where
    url, sent = fs_env[-1]                           # the echo must match the wire
    assert url.endswith("/3/query")
    assert sent["where"] == expected_where
    assert sent["f"] == "json" and sent["returnGeometry"] == "false"
    assert sent["outFields"] == "Country,Region,Crop,Conditions,Drivers"
    assert "features" not in res                     # attributes-only unless geometry=True


def test_conditions_quotes_sql_literals(fs_env):
    """A place with an apostrophe cannot break (or inject into) the where clause."""
    res = cropmonitor.conditions(month="2026-04", place="Côte d'Ivoire")
    assert "d''Ivoire" in res["query"]["where"]


def test_conditions_treats_whitespace_filters_as_absent(fs_env):
    """'?place=%20' must not become UPPER('') (matches nothing, then reads like a
    coverage gap); blank filters are no filters."""
    res = cropmonitor.conditions(month="2026-04", crop="  ", place=" ")
    assert res["query"]["where"] == "1=1"
    assert res["crop"] is None and res["place"] is None
    assert res["crops_matched"] is None


def test_conditions_empty_result_declines_honestly(fs_env, monkeypatch):
    """A2/A6 seed: zero rows -> an explicit note naming the exact-match rule,
    never an invented rating."""
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: LAYERS if url.endswith("FeatureServer")
                        else CROPS if params.get("returnDistinctValues") == "true"
                        else {"features": []})
    res = cropmonitor.conditions(month="2026-04", crop="teff", place="Zambia")
    assert res["summary"] == {} and res["records"] == []
    assert "no Teff assessment" in res["note"] and "Zambia" in res["note"]
    assert "exactly match" in res["note"]


def test_conditions_geometry_returns_the_map_layer(fs_env, monkeypatch):
    """geometry=True asks the service for GeoJSON and carries it through untouched."""
    geo = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []},
         "properties": {"Country": "Kenya", "Region": "Rift Valley", "Crop": "Maize 1",
                        "Conditions": "Watch", "Drivers": "Dry"}}]}
    calls = []
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: (calls.append((url, dict(params))),
                                             LAYERS if url.endswith("FeatureServer")
                                             else CROPS if params.get("returnDistinctValues") == "true"
                                             else geo)[1])
    res = cropmonitor.conditions(month="2026-04", crop="maize", geometry=True)
    assert res["features"]["type"] == "FeatureCollection"
    assert res["records"] == [{"country": "Kenya", "region": "Rift Valley",
                               "crop": "Maize 1", "conditions": "Watch", "drivers": "Dry"}]
    _, sent = calls[-1]
    assert sent["f"] == "geojson" and sent["returnGeometry"] == "true"


@pytest.mark.parametrize("fmt", ["json", "geojson"])
def test_conditions_paginates_past_the_transfer_limit(fs_env, monkeypatch, fmt):
    """A capped response is followed up with resultOffset until the rows run out —
    with the continuation flag where each format actually puts it."""
    offsets = []

    def paged(url, params):
        if url.endswith("FeatureServer"):
            return LAYERS
        if params.get("returnDistinctValues") == "true":
            return CROPS
        offset = params["resultOffset"]
        offsets.append(offset)
        batch = ROWS["features"][offset:offset + 2]           # 2-row pages, offset-honest
        page = {"features": batch}
        if offset + 2 < len(ROWS["features"]):
            if fmt == "geojson":
                page["properties"] = {"exceededTransferLimit": True}
            else:
                page["exceededTransferLimit"] = True
        return page
    monkeypatch.setattr(cropmonitor, "_fetch_json", paged)
    res = cropmonitor.conditions(month="2026-04", crop="maize", geometry=(fmt == "geojson"))
    assert res["count"] == 3
    assert offsets == [0, 2]                          # broken offset math can't pass


def test_conditions_endpoint_round_trip(fs_env):
    """A1+A2 through HTTP: the curl the demo runs, minus the network."""
    r = TestClient(app).get("/api/food-security/conditions",
                            params={"crop": "maize", "month": "2026-04", "place": "Kenya"})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["Watch"] == 1
    assert body["query"]["layer"] == "Global_Synthesis_202604"


def test_endpoint_declines_missing_period_with_alternative(fs_env):
    """Grounding at the HTTP boundary: period_missing 404 names the nearest month
    and does not present the available span as gapless."""
    r = TestClient(app).get("/api/food-security/conditions", params={"month": "2022-12"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "period_missing"
    assert detail["nearest_available"] in ("2022-11", "2023-01")
    assert "skips December" in detail["available"]["note"]


def test_endpoint_declines_unknown_crop_with_menu(fs_env):
    """crop_not_found 404 lists what the service does assess."""
    r = TestClient(app).get("/api/food-security/conditions",
                            params={"crop": "banana", "month": "2026-04"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "crop_not_found"
    assert "Millet" in detail["available"]


def test_endpoint_rejects_malformed_month(fs_env):
    r = TestClient(app).get("/api/food-security/conditions", params={"month": "april"})
    assert r.status_code == 400


# --- the HTTP seam: error classification (transport retries are urllib3's job) ---

class _Resp:
    def __init__(self, status=200, body=None, text="not json"):
        self.status_code = status
        self._body = body
        self._text = text

    def json(self):
        if self._body is None:
            raise ValueError(self._text)
        return self._body


@pytest.fixture
def http_env(monkeypatch, tmp_path):
    """Drive _fetch_json against a scripted session stub. This bypasses urllib3 —
    the transport retry policy itself is pinned by test_transport_retry_config."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(cropmonitor, "_verify", lambda: True)

    def scripted(responses):
        it = iter(responses)
        gets = []

        class Session:
            def get(self, url, params=None, **kw):
                gets.append(url)
                nxt = next(it)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
        monkeypatch.setattr(cropmonitor, "_SESSION", Session())
        return gets
    return scripted


def test_transport_retry_config():
    """The session's urllib3 Retry does the 5xx/backoff work; pin the policy."""
    retry = cropmonitor._SESSION.get_adapter("https://data.cropmonitor.org").max_retries
    assert retry.total == cropmonitor._RETRIES - 1
    assert retry.backoff_factor == cropmonitor._BACKOFF
    assert 500 in retry.status_forcelist and 503 in retry.status_forcelist
    assert 404 not in retry.status_forcelist         # deterministic; never retried


def test_arcgis_error_body_is_a_rejection(http_env):
    """ArcGIS reports bad queries as HTTP 200 + {"error": ...}: deterministic
    rejection, one request, no stale fallback."""
    gets = http_env([_Resp(200, {"error": {"code": 400, "message": "Invalid query"}})])
    with pytest.raises(cropmonitor.CropMonitorError, match="ArcGIS error"):
        cropmonitor._fetch_json("https://example.invalid/q", {"f": "json"})
    assert len(gets) == 1


def test_status_classification(http_env):
    """4xx -> the service said no (CropMonitorError, no fallback); surviving 5xx and
    transport errors -> ServiceUnreachable (eligible for the last-good fallback)."""
    import requests as requests_lib
    gets = http_env([_Resp(404)])
    with pytest.raises(cropmonitor.CropMonitorError, match="rejected the request"):
        cropmonitor._fetch_json("https://example.invalid/q", {"f": "json"})
    assert len(gets) == 1

    http_env([_Resp(500)])
    with pytest.raises(cropmonitor.ServiceUnreachable):
        cropmonitor._fetch_json("https://example.invalid/q", {"f": "json"})

    http_env([requests_lib.ConnectionError("boom")])
    with pytest.raises(cropmonitor.ServiceUnreachable):
        cropmonitor._fetch_json("https://example.invalid/q", {"f": "json"})


def test_nonjson_200_is_an_outage_not_an_answer(http_env):
    """An HTML maintenance page behind a 200 is classified unreachable — never
    parsed around, so the last-good cache can answer instead."""
    http_env([_Resp(text="<html>maintenance</html>")])
    with pytest.raises(cropmonitor.ServiceUnreachable, match="non-JSON"):
        cropmonitor._fetch_json("https://example.invalid/q", {"f": "json"})


# --- cache behavior: repeats, corruption, outages, degenerate responses ---

def test_cache_serves_repeats_and_survives_outages(fs_env, monkeypatch):
    """A2.4/A12: a repeat query is served from disk; an outage serves last-good."""
    calls = fs_env
    cropmonitor._layer_index()
    cropmonitor._layer_index()
    assert len(calls) == 1                           # second read came from the cache

    def down(url, params):
        raise cropmonitor.ServiceUnreachable("network down")
    monkeypatch.setattr(get_settings(), "cropmonitor_cache_ttl_hours", 0)  # force refetch
    monkeypatch.setattr(cropmonitor, "_fetch_json", down)
    assert cropmonitor._layer_index()[0]["2026-04"] == 3       # stale cache still answers
    with pytest.raises(cropmonitor.ServiceUnreachable):    # nothing cached -> honest failure
        cropmonitor._get("https://example.invalid/uncached", {"f": "json"})


def test_corrupt_cache_file_self_heals(fs_env, tmp_path):
    """A truncated cache file (crash mid-write) is a cache miss, not a permanent 400:
    the module refetches, overwrites the bad file, and answers."""
    calls = fs_env
    cropmonitor._layer_index()
    for f in (tmp_path / "cropmonitor").glob("*.json"):
        f.write_text('{"fetched_at": 17')            # truncated JSON
    assert cropmonitor._layer_index()[0]["2026-04"] == 3
    assert len(calls) == 2                           # healed by refetching
    r = TestClient(app).get("/api/food-security/conditions",
                            params={"crop": "maize", "month": "2026-04"})
    assert r.status_code == 200                      # and never surfaces as an HTTP error


def test_degenerate_200_never_overwrites_last_good(fs_env, monkeypatch):
    """An HTTP-200 {"layers": []} is flakiness, not truth: last-good survives it, and
    with no last-good it is an unreachable-service error, not an empty index."""
    cropmonitor._layer_index()                        # prime last-good
    monkeypatch.setattr(get_settings(), "cropmonitor_cache_ttl_hours", 0)
    monkeypatch.setattr(cropmonitor, "_fetch_json", lambda url, params: {"layers": []})
    assert cropmonitor._layer_index()[0]["2026-04"] == 3   # served from last-good

    monkeypatch.setattr(get_settings(), "cache_dir", get_settings().cache_dir / "empty")
    with pytest.raises(cropmonitor.ServiceUnreachable):
        cropmonitor._layer_index()                    # no last-good -> honest failure


def test_degenerate_crop_menu_never_overwrites_last_good(fs_env, monkeypatch):
    """The crop menu gets the same protection: a 200 with zero crop names is a
    hiccup, and a crop decline made off a stale menu says so."""
    cropmonitor.distinct_crops(3)                    # prime last-good
    monkeypatch.setattr(get_settings(), "cropmonitor_cache_ttl_hours", 0)
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: LAYERS if url.endswith("FeatureServer")
                        else {"features": []})
    assert "Maize 1" in cropmonitor.distinct_crops(3)   # served from last-good
    with pytest.raises(cropmonitor.CropNotFound) as exc:
        cropmonitor.resolve_crop("banana", 3)        # decline off the stale menu hedges
    assert "cached crop list" in str(exc.value)


def test_stale_served_conditions_carry_a_visible_marker(fs_env, monkeypatch):
    """Grounding: an outage answer is fine, an UNMARKED outage answer is not.
    conditions() flags last-good data; a decline off a stale layer list hedges."""
    fresh = cropmonitor.conditions(month="2026-04", crop="maize", place="Kenya")
    assert "stale_data" not in fresh

    def down(url, params):
        raise cropmonitor.ServiceUnreachable("network down")
    monkeypatch.setattr(get_settings(), "cropmonitor_cache_ttl_hours", 0)
    monkeypatch.setattr(cropmonitor, "_fetch_json", down)
    stale = cropmonitor.conditions(month="2026-04", crop="maize", place="Kenya")
    assert stale["summary"] == fresh["summary"]      # same last-good data...
    assert stale["stale_data"]["last_good_fetch"]    # ...but visibly marked
    assert "could not be reached" in stale["stale_data"]["message"]

    with pytest.raises(cropmonitor.MonthNotAvailable) as exc:
        cropmonitor.resolve_month("2022-12")         # decline from a stale list hedges
    assert "newer months may exist" in str(exc.value)


def test_cache_writes_are_atomic(fs_env, tmp_path, monkeypatch):
    """Writers go through temp-file + os.replace, so a reader can never observe a
    partial cache file."""
    replaced = []
    real_replace = cropmonitor.os.replace
    monkeypatch.setattr(cropmonitor.os, "replace",
                        lambda src, dst: (replaced.append((str(src), str(dst))),
                                          real_replace(src, dst))[1])
    cropmonitor._layer_index()
    assert replaced and replaced[0][0].endswith(".tmp")
    cached = json.loads((tmp_path / "cropmonitor").glob("*.json").__next__().read_text())
    assert cached["data"] == LAYERS
