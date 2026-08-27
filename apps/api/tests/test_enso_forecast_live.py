"""Live round-trip against IRI's real plume endpoint and quick-look pages.

The offline suite stubs the transport, so it cannot catch IRI changing their page
layout or retiring the JSON endpoint. This can. Opt-in (network): run with
IRI_LIVE=1; skipped otherwise so the default suite stays offline.
"""

import os

import pytest

from app.mcp import enso_forecast, feeds

pytestmark = pytest.mark.skipif(
    os.environ.get("IRI_LIVE", "").lower() not in ("1", "true", "yes"),
    reason="set IRI_LIVE=1 to hit the live IRI endpoints")


def test_live_latest_plume_and_narrative_resolve(log):
    """The real endpoints answer, and 'latest' lands on a published issuance
    without being mislabelled as substituted."""
    p = enso_forecast.plume()
    log("OUTPUT", f"plume {p['issued_for']}: {p['model_count']} models, "
                  f"{len(p['lead_seasons'])} seasons")
    assert p["model_count"] > 10
    assert p["substituted"] is False
    # lead_seasons is the SPAN across all models; individual models legitimately
    # stop short (AUS-ACCESS runs four seasons, then -999).
    spans = [len(m["forecast"]) for m in p["models"]]
    log("OUTPUT", f"model spans {min(spans)}-{max(spans)} seasons")
    assert max(spans) == len(p["lead_seasons"]) <= 10
    assert all(f["season"] in p["lead_seasons"] for m in p["models"] for f in m["forecast"])

    o = enso_forecast.outlook()
    log("OUTPUT", f"quick look {o['issued_for']}, published {o['published']}, "
                  f"{len(o['sections'])} sections")
    assert o["published"]
    assert any(s["paragraphs"] for s in o["sections"])


def test_live_narrative_still_carries_the_official_percentages(log):
    """The whole reason we serve this prose. If IRI moves the probabilities out of
    the narrative, the structured gap needs revisiting — this is the tripwire."""
    o = enso_forecast.outlook()
    pct = [p for s in o["sections"] for p in s["paragraphs"] if "%" in p]
    for p in pct[:3]:
        log("OUTPUT", p[:150])
    assert pct, "no percentage prose found — IRI page layout or content changed"


def test_live_historic_el_nino_window_is_reachable(log):
    """The 2015/16 El Nino anchors the demo, so the archive must stay addressable
    by month, not just 'latest'."""
    res = feeds.query("enso_plume", {"year": 2015, "month": 10})
    log("OUTPUT", res["summary"])
    assert res["status"] == "ok"
    assert res["as_of"] == "October 2015"
    assert not res["passport"]["stale_data"]["served_stale"]
    peak = max(f["anomaly"] for m in res["records"] for f in m["forecast"])
    log("CHECK", f"peak model anomaly {peak} C — a very strong event")
    assert peak > 2.0
