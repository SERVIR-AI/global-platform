"""X2c: tabular contributions — landed copy, sha teeth, feed row."""

import yaml

import pytest

from app.config import get_settings
from app.contrib import tables
from app.mcp import feeds


CSV = "Month,Rainfall_mm\n2026-05,12.5\n2026-06,3.0\n2026-07,0.5\n"


def _manifest(path, **over):
    d = {"dataset": "btb_station_rain", "file": str(path),
         "title": "Battambang station rainfall", "description": "d",
         "source": "provincial hydromet office", "validation": "unvalidated",
         "license": "CC-BY-4.0", "vintage": "2026-08", "cadence": "monthly",
         "columns": {"month": "Month", "rainfall_mm": "Rainfall_mm"},
         "units": "mm/month", "as_of_field": "month"}
    d.update(over)
    return d


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(get_settings(), "feeds_conf_dir", tmp_path / "feeds")
    csv_path = tmp_path / "rain.csv"
    csv_path.write_text(CSV)
    return csv_path


def test_header_mismatch_named_exactly(env, log):
    fails = tables.validate_manifest(_manifest(env, columns={"x": "NoSuchCol"}))
    log("OUTPUT", [f for f in fails if "header" in f][0][:110])
    assert any("missing mapped column" in f for f in fails)


def test_landing_archives_and_writes_the_feed_row(env, log):
    out = tables.add(_manifest(env))
    log("OUTPUT", f"{out['status']} rows={out['rows']} sha={out['sha256'][:8]}")
    assert out["status"] == "landed" and out["rows"] == 3
    spec = yaml.safe_load(open(out["feed_row"]))
    assert spec["adapter"] == "generic_csv"
    assert spec["residency"] == "platform-hosted copy"
    assert spec["fetch"]["sha256"] == out["sha256"]


def test_the_feed_serves_the_landed_copy(env, log):
    out = tables.add(_manifest(env))
    spec = yaml.safe_load(open(out["feed_row"]))
    res = feeds._adapt_generic_csv({}, spec)
    log("OUTPUT", res["summary"][:90])
    assert res["records"][-1] == {"month": "2026-07", "rainfall_mm": 0.5}
    assert res["as_of"] == "2026-07"
    assert "sha256" in res["query_receipt"]


def test_out_of_band_edits_refuse_to_serve(env, log):
    out = tables.add(_manifest(env))
    spec = yaml.safe_load(open(out["feed_row"]))
    with open(out["archived"], "a") as f:
        f.write("2026-08,99.9\n")                       # tamper after landing
    with pytest.raises(feeds.FeedDecline) as exc:
        feeds._adapt_generic_csv({}, spec)
    log("OUTPUT", exc.value.note[:100])
    assert "modified out-of-band" in exc.value.note


def test_no_overwrite_of_landed_datasets(env, log):
    tables.add(_manifest(env))
    out = tables.add(_manifest(env))
    log("OUTPUT", out["failures"][0][:80])
    assert out["status"] == "declined"
    assert "do not overwrite" in out["failures"][0]


def test_landed_tables_never_claim_cache_staleness(env, log):
    """UAT catch: is_stale reads a flag-less stale_data dict as 'served from
    fallback', so landed tables carried a false cache warning into answers."""
    out = tables.add(_manifest(env))
    spec = yaml.safe_load(open(out["feed_row"]))
    res = feeds._adapt_generic_csv({}, spec)
    log("OUTPUT", str(res["stale_data"]))
    assert feeds.is_stale(res["stale_data"]) is False
