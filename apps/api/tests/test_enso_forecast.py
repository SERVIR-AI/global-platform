"""IRI ENSO forecast adapter — the three upstream traps, offline.

Each test here exists because the upstream can mislead quietly rather than fail
loudly: a zero-based month, a 200 that answers a different month than the one
asked for, and a label table longer than the data it labels. All three would
produce a confident, wrong citation rather than an error.
"""

import json

import pytest

from app.mcp import climate_indices, enso_forecast, feeds


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _plume_payload(month0, n_models=3, span=9):
    """month0 is what the upstream ECHOES BACK — the trap lives in this field."""
    return {"month": month0,
            "models": [{"model": f"M{i}", "type": "Dynamical",
                        "data": [1.2] * span} for i in range(n_models)],
            "observed": [{"month": "AMJ", "data": 0.98}, {"month": "Jun", "data": 1.55}],
            "observed_roni": [], "averages": {"total": [1.2] * span}}


@pytest.fixture(autouse=True)
def _no_cache():
    """The module cache is process-global; a leaked entry would let one test's
    stub answer another test's call."""
    climate_indices._CACHE.clear()
    yield
    climate_indices._CACHE.clear()


def test_month_is_converted_to_the_upstreams_zero_based_index(monkeypatch, log):
    """We take a human 1-12 month; the upstream wants 0-11. Off by one here
    mislabels every lead season in the citation."""
    seen = []

    def fake(url):
        seen.append(url)
        return _Resp(payload=_plume_payload(month0=6))

    monkeypatch.setattr(enso_forecast, "_get", fake)
    out = enso_forecast.plume(year=2026, month=7)
    log("CALL", seen[0])
    log("OUTPUT", f"issued_for={out['issued_for']}")
    assert seen[0].endswith("/2026/6")               # July asked -> 6 requested
    assert out["issued_for"] == "July 2026"
    # July's first forecast season is JJA, per IRI's own figure table.
    assert out["lead_seasons"][0] == "JJA"


def test_unissued_month_answered_with_another_is_flagged(monkeypatch, log):
    """Asking for a month not yet issued returns the PREVIOUS month with HTTP 200
    and no warning. Unflagged, a brief would date its evidence a month early."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(payload=_plume_payload(month0=6)))
    out = enso_forecast.plume(year=2026, month=8)
    log("OUTPUT", f"requested={out['requested']} got={out['issued_for']}")
    assert out["substituted"] is True
    assert out["requested"] == "August 2026"
    assert out["issued_for"] == "July 2026"
    assert "not published" in out["substitution_note"]


def test_latest_is_not_reported_as_a_substitution(monkeypatch, log):
    """Walking back to last month IS the right answer when no month was named —
    flagging it would put a staleness caveat on every default call."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(payload=_plume_payload(month0=6)))
    out = enso_forecast.plume()
    log("OUTPUT", f"requested={out['requested']} substituted={out['substituted']}")
    assert out["substituted"] is False
    assert out["requested"] == "latest available"
    assert out["substitution_note"] is None


def test_lead_seasons_never_exceed_the_data(monkeypatch, log):
    """IRI's label table lists ten forecast seasons but the models carry nine.
    Advertising the tenth claims a lead season no model answered for."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(payload=_plume_payload(month0=6, span=9)))
    out = enso_forecast.plume(year=2026, month=7)
    log("OUTPUT", f"{len(out['lead_seasons'])} seasons for a 9-point series")
    assert len(out["lead_seasons"]) == 9
    assert all(len(m["forecast"]) <= 9 for m in out["models"])


def test_missing_sentinel_is_dropped_not_reported(monkeypatch, log):
    """-999 means "this model stops here". Reported as a value it would read as a
    catastrophic cold anomaly."""
    payload = _plume_payload(month0=6)
    payload["models"][0]["data"] = [1.2, 1.3, -999, -999, -999, -999, -999, -999, -999]
    monkeypatch.setattr(enso_forecast, "_get", lambda url: _Resp(payload=payload))
    out = enso_forecast.plume(year=2026, month=7)
    vals = [f["anomaly"] for f in out["models"][0]["forecast"]]
    log("OUTPUT", f"kept {vals}")
    assert vals == [1.2, 1.3]
    assert all(v > -900 for m in out["models"] for v in
               [f["anomaly"] for f in m["forecast"]])


def test_model_counts_carry_the_not_a_probability_warning(monkeypatch, log):
    """Model counts are not IRI's probability forecast. The band counts are the
    single most misreadable thing we serve, so the warning ships with them."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(payload=_plume_payload(month0=6)))
    out = enso_forecast.plume(year=2026, month=7)
    agree = out["model_agreement"][0]
    log("OUTPUT", json.dumps(agree))
    assert agree["el_nino"] == 3 and agree["models_reporting"] == 3
    assert "NOT PROBABILITIES" in out["model_agreement_caveat"]
    # Counts, never a percentage — a percentage invites being read as a chance.
    assert "%" not in json.dumps(out["model_agreement"])


def test_ensemble_means_and_roni_models_do_not_vote(monkeypatch, log):
    """The `* AVG` rows are typed "Other" and are byte-identical to `averages`, and
    "Relative" rows carry RONI, not absolute Nino-3.4. Counting either inflates the
    model count and lets the ensemble mean vote in the statistic summarising it."""
    payload = _plume_payload(month0=6, n_models=4)
    payload["models"] += [
        {"model": "AUS-RELATIVE", "type": "Relative", "data": [0.9] * 9},
        {"model": "DYN AVG", "type": "Other", "data": [1.2] * 9},
    ]
    monkeypatch.setattr(enso_forecast, "_get", lambda url: _Resp(payload=payload))
    out = enso_forecast.plume(year=2026, month=7)
    log("OUTPUT", f"{out['model_count']} voting of {out['models_listed']} listed")
    assert out["model_count"] == 4 and out["models_listed"] == 6
    assert out["model_agreement"][0]["models_reporting"] == 4
    excluded = {m["model"] for m in out["models"] if not m["counts_toward_agreement"]}
    assert excluded == {"AUS-RELATIVE", "DYN AVG"}
    # excluded models are still LISTED — dropping them would hide real forecasts
    assert "RONI" in next(m["basis"] for m in out["models"] if m["model"] == "AUS-RELATIVE")


def test_relative_observed_series_is_carried(monkeypatch, log):
    """CPC now leads with the relative index while IRI's prose quotes the absolute
    one. Dropping RONI left the two irreconcilable."""
    payload = _plume_payload(month0=6)
    payload["observed_roni"] = [{"month": "AMJ", "data": 0.49}, {"month": "Jun", "data": 1.04}]
    monkeypatch.setattr(enso_forecast, "_get", lambda url: _Resp(payload=payload))
    out = enso_forecast.plume(year=2026, month=7)
    log("OUTPUT", f"absolute={out['observed']} relative={out['observed_relative']}")
    assert out["observed_relative"] == [{"period": "AMJ", "anomaly": 0.49},
                                        {"period": "Jun", "anomaly": 1.04}]
    assert out["observed"] != out["observed_relative"]


def test_narrative_is_sectioned_and_served_verbatim(monkeypatch, log):
    """The official probabilities live in this prose. It is quoted, never parsed —
    so the parser must preserve sentences intact under its heading."""
    html = """<div class="large-9 columns page-content enso-forecast">
        <h2>June 2026 Quick Look</h2><h4>Published: June 22, 2026</h4>
        <p>El Ni&ntilde;o probabilities are assigned at 100% from JJA through SON.</p>
        <script>var x = "should never appear";</script>
        </div><div class="nav"><p>skip me</p></div>"""
    monkeypatch.setattr(enso_forecast, "_get", lambda url: _Resp(text=html))
    out = enso_forecast.outlook(year=2026, month=6)
    log("OUTPUT", json.dumps(out["sections"]))
    assert out["published"] == "June 22, 2026"
    assert out["sections"][0]["heading"] == "June 2026 Quick Look"
    assert out["sections"][0]["paragraphs"] == [
        "El Niño probabilities are assigned at 100% from JJA through SON."]
    assert "should never appear" not in json.dumps(out)
    assert "skip me" not in json.dumps(out)          # outside page-content


def test_layout_change_declines_rather_than_returning_empty_prose(monkeypatch, log):
    """If IRI restyles the page our selector goes quiet. Silence must decline, not
    mint a receipt citing an empty narrative."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(text="<html><body><p>redesigned</p></body></html>"))
    with pytest.raises(climate_indices.IndexUnavailable):
        enso_forecast.outlook(year=2026, month=6)
    log("CHECK", "declined instead of citing nothing")


def test_feed_surfaces_substitution_as_staleness(monkeypatch, log):
    """A month we did not ask for is staleness by another name, so it rides the
    same passport field a cache-serve does and reaches the receipt. Exercised on
    enso_outlook since IRI retired the plume JSON API (see registry)."""
    html = ('<div class="page-content"><h2>June 2026 Quick Look</h2>'
            '<h4>Published: June 22, 2026</h4><p>El Nino conditions continue.</p></div>')
    # Only June exists, so asking for August forces the walk-back that substitutes.
    # A stub answering every month would never trip it, which is the real-world
    # shape too: IRI has published nothing since June.
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(text=html) if "June" in url else _Resp(status=404))
    res = feeds.query("enso_outlook", {"year": 2026, "month": 8})
    stale = res["passport"]["stale_data"]
    log("OUTPUT", json.dumps(stale))
    assert res["status"] == "ok"
    assert stale["served_stale"] is True
    assert "August 2026" in stale["reason"]


def test_structured_probabilities_stay_a_declared_gap(log):
    """Shipping the narrative does not close the structured-probability gap, and
    the decline must point at what we DO have."""
    res = feeds.query("enso_probabilities", {})
    log("OUTPUT", res["note"][:120])
    assert res["status"] == "declined"
    assert "enso_outlook" in res["note"]
    # Derived from the registry, not hardcoded: enso_plume left this list when IRI
    # retired its JSON API, and a hardcoded name would have silently gone stale.
    from app.mcp import registry
    live = {k for k, v in registry.FEEDS.items() if v.get("status") == "available"}
    assert set(res["available"]) == live and "enso_outlook" in live


def test_presence_of_stale_data_is_not_staleness(log):
    """The rule lives in ONE place because it was fixed in feeds.query and then
    shipped again in record — a healthy climate feed always carries a stale_data
    dict, so testing presence flags every live read as cache-served."""
    healthy = {"cadence": "monthly", "retrieved_at": "2026-08-12T00:00:00Z",
               "served_from_cache": False, "served_stale": False, "reason": None}
    assert feeds.is_stale(healthy) is False
    assert feeds.is_stale({**healthy, "served_stale": True}) is True
    # the conditions feed attaches the key ONLY on fallback, so presence IS the signal
    assert feeds.is_stale({"last_good_fetch": "2026-08-11T09:00:00Z"}) is True
    assert feeds.is_stale(None) is False and feeds.is_stale({}) is False
    log("CHECK", "one predicate, both shapes")


def test_a_healthy_pull_mints_a_receipt_with_no_stale_flag(log):
    """End to end: the receipt must not caveat a live read. This is the assertion
    that would have caught the deploy-time regression."""
    from app.mcp import record
    pack = {"citations": [
        {"n": 1, "kind": "index", "source": "NOAA CPC", "pub_date": "MJJ 2026",
         "stale_data": {"cadence": "monthly", "served_stale": False}},
        {"n": 2, "kind": "index", "source": "IRI", "pub_date": "July 2026",
         "stale_data": {"cadence": "monthly", "served_stale": True,
                        "reason": "upstream unreachable"}},
    ]}
    f = record._freshness(pack)
    srcs = record._sources(pack)
    log("OUTPUT", f"pulled={f['pulled_sources']} stale={f['stale_sources']}")
    assert f["pulled_sources"] == [1, 2] and f["stale_sources"] == [2]
    assert "caveat" not in srcs[0] and "caveat" in srcs[1]
    assert srcs[0]["stale_data"]["cadence"] == "monthly"   # provenance still carried


def test_a_param_a_feed_does_not_take_declines_rather_than_raising(log):
    """`enso_discussion` accepts no params, so {"year": ...} reached discussion()
    and raised an uncaught TypeError — escaping as a transport error instead of a
    governed decline, which breaks rule 2 (declines say why). The sibling
    climate-index adapter always caught this; this one did not."""
    res = feeds.query("enso_discussion", {"year": 2026})
    log("OUTPUT", res.get("note", "")[:110])
    assert res["status"] == "declined"
    assert "does not accept those params" in res["note"]


def test_every_available_feed_survives_a_junk_param(log):
    """Generic: no registered feed may raise on an unexpected param. One adapter
    catching TypeError and another not is exactly how this slipped through."""
    from app.mcp import registry
    for name, spec in registry.FEEDS.items():
        if spec.get("status") != "available":
            continue
        res = feeds.query(name, {"definitely_not_a_param": 1})
        assert res["status"] in ("ok", "empty", "declined"), f"{name} -> {res}"
    log("CHECK", "all available feeds returned a governed status")


def test_upstream_refusal_is_not_reported_as_nothing_published(monkeypatch, log):
    """IRI moved hosts and the replacement 403s everything. Reporting that as
    "no plume issued in the last 6 months" blames the publisher for our outage
    and points the reader at the wrong problem."""
    monkeypatch.setattr(enso_forecast, "_get", lambda url: _Resp(status=403))
    with pytest.raises(climate_indices.IndexUnavailable) as exc:
        enso_forecast.plume()
    msg = str(exc.value)
    log("OUTPUT", msg[:150])
    assert "upstream refused" in msg and "403" in msg
    assert "NOT evidence that nothing was published" in msg


def test_a_genuine_publishing_gap_still_reads_as_one(monkeypatch, log):
    """The opposite case must keep its own wording: a 404 walk-back that finds
    nothing is a real publishing gap, not an outage."""
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda url: _Resp(text="<html>redesigned</html>"))
    with pytest.raises(climate_indices.IndexUnavailable) as exc:
        enso_forecast.outlook()
    log("OUTPUT", str(exc.value)[:120])
    assert "no quick look issued" in str(exc.value)


def test_latest_outlook_reads_the_current_page_not_a_dated_slug(monkeypatch, log):
    """IRI publishes the NEWEST issue only at /current/ and archives older ones at
    dated URLs. A dated walk-back can therefore never see the latest month, which
    silently cost us a month of freshness: July existed while we served June."""
    seen = []
    cur = ('<div class="page-content"><h2>July 2026 Quick Look</h2>'
           '<h4>Published: July 20, 2026</h4><p>El Nino continues.</p></div>')

    def fake(url):
        seen.append(url)
        return _Resp(text=cur) if url.endswith("/current/") else _Resp(status=404)

    monkeypatch.setattr(enso_forecast, "_get", fake)
    out = enso_forecast.outlook()
    log("OUTPUT", f"{out['issued_for']} from {seen[0].rsplit('/', 2)[-2]}")
    assert seen[0].endswith("/current/"), "must try /current/ first"
    assert out["issued_for"] == "July 2026"      # derived from the page's own heading
    assert out["published"] == "July 20, 2026"


def test_an_explicit_month_still_uses_the_dated_archive(monkeypatch, log):
    """/current/ is only right for "latest". Asking for a specific month must go to
    the archive, or history would silently return today's issue."""
    seen = []
    html = ('<div class="page-content"><h2>June 2026 Quick Look</h2>'
            '<h4>Published: June 22, 2026</h4><p>text</p></div>')
    monkeypatch.setattr(enso_forecast, "_get",
                        lambda u: (seen.append(u), _Resp(text=html))[1])
    out = enso_forecast.outlook(year=2026, month=6)
    log("OUTPUT", seen[0])
    assert "/current/" not in seen[0] and "2026-June-quick-look" in seen[0]
    assert out["issued_for"] == "June 2026"
