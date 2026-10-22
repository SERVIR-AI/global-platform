"""Size caps by kind: rasters stream to disk and keep the raster cap; documents, tables
and vectors are parsed in memory whole and keep the lower grp_contrib_max_bytes_other."""
import pytest

from app.config import get_settings
from app.contrib import fetch_policy, staging
from app.mcp import store


@pytest.fixture
def cap_env(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(s, "grp_contrib_max_bytes", 5 * 1024 ** 3)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    # no automatic preparation: the direct fetch path is the one under test
    monkeypatch.setattr(s, "grp_prep_auto", False)
    return s


def test_other_kinds_have_their_own_lower_cap(cap_env):
    assert cap_env.grp_contrib_max_bytes_other == 500 * 1024 * 1024
    assert cap_env.grp_contrib_max_bytes_other < cap_env.grp_contrib_max_bytes
    assert staging._other_cap() == 500 * 1024 * 1024


def test_a_vector_over_the_other_cap_is_declined_naming_that_cap(cap_env, monkeypatch, log):
    seen = {}

    def too_big(url, max_bytes=None):
        seen["cap"] = max_bytes
        raise fetch_policy.TooLarge("over the cap", size=600 * 1024 * 1024, cap=max_bytes)
    monkeypatch.setattr(fetch_policy, "fetch", too_big)
    r = staging.submit("vector", {"layer": "test_big_sites", "url": "https://x.test/sites.geojson",
                                  "title": "sites", "description": "test polygons",
                                  "source": "test", "license": "CC0-1.0", "vintage": "2026-10"})
    assert r["status"] == "declined", r
    note = " ".join(r.get("problems") or [r.get("note", "")])
    assert "500 MB" in note, note
    assert seen["cap"] == 500 * 1024 * 1024          # the fetch itself was asked to stop there


def test_a_raster_keeps_the_raster_cap(cap_env, monkeypatch, log):
    def too_big(url, max_bytes=None):
        raise fetch_policy.TooLarge("over the cap", size=6 * 1024 ** 3, cap=max_bytes)
    monkeypatch.setattr(fetch_policy, "fetch", too_big)
    r = staging.submit("raster", {
        "layer": "hazard_test_big", "url": "https://x.test/big.tif", "title": "big",
        "description": "a test hazard", "source": "test", "license": "CC0-1.0", "vintage": "2026-10",
        "legend": {"1": "Low", "2": "Mid", "3": "High", "4": "Very high", "5": "Extreme"},
        "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5, "nodata": 0}})
    assert r["status"] == "declined", r
    note = " ".join(r.get("problems") or [r.get("note", "")])
    assert "500 MB" not in note, note
    assert "5 GB" in note or "5.0 GB" in note, note
