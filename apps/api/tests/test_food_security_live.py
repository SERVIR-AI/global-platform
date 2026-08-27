"""Live round-trip against the real data.cropmonitor.org FeatureServer.

Proves A2.4 from our production HTTP client: the incomplete TLS chain verifies via
requests/certifi, timeouts/retries hold, and the real service honors our queries.
Opt-in (network): run with CROPMONITOR_LIVE=1; skipped otherwise so the default
suite stays offline.
"""

import os

import pytest

from app.config import get_settings
from app.food_security import cropmonitor

pytestmark = pytest.mark.skipif(
    os.environ.get("CROPMONITOR_LIVE", "").lower() not in ("1", "true", "yes"),
    reason="set CROPMONITOR_LIVE=1 to hit the live Crop Monitor service")


def test_live_layer_index_and_kenya_maize_conditions(monkeypatch, tmp_path, log):
    """Real service: layer list resolves, December is genuinely absent, and a
    Kenya maize query returns expert assessments with drivers."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)   # never touch cache/

    index = cropmonitor._layer_index()[0]
    log("LAYERS", f"{len(index)} monthly layers, {min(index)} -> {max(index)}")
    assert "2022-01" in index
    assert "2022-12" not in index                    # the December gap is real

    res = cropmonitor.conditions(crop="maize", place="Kenya")    # latest month
    log("QUERY", res["query"]["where"])
    log("RESULT", f"as_of={res['as_of']} count={res['count']} summary={res['summary']}")
    assert res["crops_matched"] and all("Maize" in c for c in res["crops_matched"])
    assert res["count"] > 0, "Kenya maize should be assessed year-round by CM4EW"
    assert all(r["country"] == "Kenya" for r in res["records"])
    valid = {"Exceptional", "Favourable", "Watch", "Poor", "Failure", "No Data",
             "Out of season"}
    assert set(res["summary"]) <= valid
