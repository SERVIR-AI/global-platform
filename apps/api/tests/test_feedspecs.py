"""X2: declarative feeds — YAML rows, generic adapters, honest invalidity."""

import pytest

from app.contrib import feedspecs
from app.mcp import feeds, registry


def _spec(**over):
    d = {"dataset": "toy_index", "title": "Toy", "description": "d",
         "source": "Toy Org", "validation": "single-agency",
         "residency": "external call-out", "cadence": "monthly",
         "adapter": "generic_table",
         "fetch": {"url": "https://x/toy.data", "index_name": "TOY",
                   "units": "anomaly"}}
    d.update(over)
    return d


def test_spec_failures_all_named_at_once(log):
    fails = feedspecs.validate_spec({"dataset": "x", "adapter": "generic_table"})
    log("OUTPUT", "; ".join(fails)[:140])
    named = {f.split("'")[1] for f in fails if "missing required" in f}
    assert {"title", "description", "source", "validation",
            "residency", "cadence"} <= named
    assert any("fetch" in f for f in fails)


def test_unknown_adapter_points_at_the_dev_path(log):
    fails = feedspecs.validate_spec(_spec(adapter="my_scraper"))
    log("OUTPUT", fails[0][:120])
    assert any("needs a dev" in f for f in fails)


def test_invalid_specs_register_visibly_not_silently(tmp_path, log):
    (tmp_path / "bad.yml").write_text("dataset: bad\nadapter: generic_table\n")
    rows = feedspecs.load_dir(tmp_path)
    log("OUTPUT", rows["bad"]["reason"][:100])
    assert rows["bad"]["status"] == "invalid"
    assert "spec failures" in rows["bad"]["reason"]
    out = feeds.query("bad") if "bad" in registry.FEEDS else None
    # not merged into the live registry here — decline shape is covered below


def test_collision_with_code_row_refuses(log):
    live = {"enso_oni": {"status": "available"}}
    feedspecs.merge_into(live, {"enso_oni": {"status": "available",
                                             "declarative": "enso_oni.yml"}})
    log("OUTPUT", live["enso_oni.yml"]["reason"][:80])
    assert live["enso_oni"] == {"status": "available"}      # code row untouched
    assert "collides" in live["enso_oni.yml"]["reason"]


def test_generic_table_parses_the_noaa_family(monkeypatch, log):
    payload = ("  1949  2026\n"
               "2025  0.5  1.2  -0.3  0.0  0.7  1.1  0.2  -0.6  0.4  1.5  -1.2  0.9\n"
               "2026  -1.4  -2.1  -0.8  -99.99  -99.99  -99.99  -99.99  -99.99  -99.99  -99.99  -99.99  -99.99\n")
    class R:
        text = payload
        def raise_for_status(self): pass
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    from app.mcp import climate_indices
    monkeypatch.setattr(climate_indices, "cached", lambda key, builder, ttl=0: builder())
    spec = _spec(declarative="toy.yml")
    spec["fetch"]["missing_below"] = -90
    spec["fetch"]["bands"] = [{"max": -1.0, "label": "strongly negative"}]
    out = feeds._adapt_generic_table({"limit": 3}, spec)
    log("OUTPUT", out["summary"])
    assert out["as_of"] == "2026-03"                        # sentinel months skipped
    assert len(out["records"]) == 3
    assert out["records"][-2]["classification"] == "strongly negative"  # 2026-02 -2.1
    assert "toy.data" in out["query_receipt"]


def test_generic_json_maps_dot_paths(monkeypatch, log):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"rows": [{"d": "2026-07", "obs": {"v": 3}},
                                      {"d": "2026-08", "obs": {"v": 5}}]}}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    from app.mcp import climate_indices
    monkeypatch.setattr(climate_indices, "cached", lambda key, builder, ttl=0: builder())
    spec = _spec(adapter="generic_json", declarative="j.yml")
    spec["fetch"] = {"url": "https://x/api", "records_path": "data.rows",
                     "fields": {"date": "d", "value": "obs.v"},
                     "as_of_field": "date"}
    out = feeds._adapt_generic_json({}, spec)
    log("OUTPUT", out["summary"])
    assert out["records"][-1] == {"date": "2026-08", "value": 5}
    assert out["as_of"] == "2026-08"


def test_the_real_soi_row_is_registered_and_valid(log):
    row = registry.FEEDS.get("enso_soi")
    log("OUTPUT", f"status={row and row.get('status')} via {row and row.get('declarative')}")
    assert row is not None and row["status"] == "available"
    assert row["declarative"] == "enso_soi.yml"
    assert row["source"] == "NOAA PSL"                      # passport present


def test_feeds_query_declines_invalid_declarative_rows(monkeypatch, log):
    monkeypatch.setitem(registry.FEEDS, "half_baked",
                        {"status": "invalid", "declarative": "half.yml",
                         "reason": "spec failures: missing required field 'source'"})
    out = feeds.query("half_baked")
    log("OUTPUT", out["note"][:100])
    assert out["status"] == "declined"
    assert "missing required field" in out["note"]
