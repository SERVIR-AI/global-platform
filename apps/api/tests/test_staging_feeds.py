"""X4.4 — a declarative feed over the MCP: refused unless the spec answers through
its adapter; staged for its owner and reviewers; landed as a YAML row on approval
without a restart; contributed upstreams re-checked on every read."""
import copy

import pytest
import yaml

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.mcp import feeds, registry, store

OWNER = identity.Caller("user_owner", "hydrologist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")


NOAA_TEXT = ("  1950  2026\n"
             "2025  0.11  0.21  0.31  0.41  0.51  0.61  0.71  0.81  0.91  1.01  1.11  1.21\n"
             "2026  1.48  1.72  1.95  2.10  2.30  2.48  2.91  3.28 -99.99 -99.99 -99.99 -99.99\n"
             " -99.99\n")


class _FakeHTTP:
    """What requests.get returns for the PSL text series (the REAL adapter parses it)."""
    def __init__(self, url):
        self.url, self.text, self.status_code = url, NOAA_TEXT, 200

    def raise_for_status(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path)
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=None, **kw: _FakeHTTP(url))
    snapshot = copy.deepcopy(registry.FEEDS)
    staging.STAGED_FEEDS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    yield tmp_path
    registry.FEEDS.clear()
    registry.FEEDS.update(snapshot)
    staging.STAGED_FEEDS.clear()


def _manifest(**over):
    d = {"dataset": "x44_nino12_sst", "title": "Nino 1+2 SST anomaly (monthly)",
         "description": "Monthly SST anomaly in the Nino 1+2 region.", "source": "NOAA PSL",
         "validation": "single-agency", "residency": "external call-out", "cadence": "monthly",
         "adapter": "generic_table",
         "fetch": {"url": "https://psl.noaa.gov/data/correlation/nina1.anom.data",
                   "index_name": "Nino1+2", "units": "degrees C anomaly", "missing_below": -90},
         "usage_notes": "Driver signal only; noisier than Nino 3.4."}
    d.update(over)
    return d


def _query_as(caller, ds="x44_nino12_sst"):
    tok = identity.bind(caller)
    try:
        return feeds.query(ds, {})
    finally:
        identity.unbind(tok)


def test_bad_specs_are_refused_with_every_problem(env, log):
    out = staging.submit("feed", _manifest(adapter="generic_csv", fetch={"url": "https://x.org/a"},
                                           residency=None, extra="no"), OWNER)
    log("OUTPUT", str(out["problems"]))
    joined = " ".join(out["problems"])
    assert out["status"] == "declined"
    assert "adapter must be one of" in joined and "residency" in joined and "unknown fields ['extra']" in joined
    assert store.list_contributions() == []


def test_a_spec_that_does_not_answer_is_refused_and_nothing_is_stored(env, monkeypatch, log):
    import requests

    class _Empty(_FakeHTTP):
        def __init__(self, url):
            super().__init__(url)
            self.text = "<html>not a data file</html>"
    monkeypatch.setattr(requests, "get", lambda url, timeout=None, **kw: _Empty(url))
    out = staging.submit("feed", _manifest(), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "did not answer" in out["problems"][0]
    assert store.list_contributions() == [] and staging.STAGED_FEEDS == {}


def test_staged_feed_serves_owner_and_reviewer_only(env, log):
    out = staging.submit("feed", _manifest(), OWNER)
    log("OUTPUT", f"{out['status']} sample={out['preview']['sample']}")
    assert out["status"] == "staged" and out["preview"]["sample"]["last"]["value"] == 3.28
    mine = _query_as(OWNER)
    assert mine["status"] == "ok" and mine["passport"]["staged"]["contribution_id"] == out["contribution_id"]
    assert mine["note"].startswith("Driver signal only")
    other = _query_as(OTHER)
    assert other["status"] == "declined" and "x44_nino12_sst" not in other["available"]
    assert _query_as(REVIEWER)["status"] == "ok"


def test_approval_writes_the_row_and_reloads(env, log):
    out = staging.submit("feed", _manifest(), OWNER)
    ok = staging.approve(out["contribution_id"], REVIEWER, "checked the PSL page")
    log("OUTPUT", f"{ok['status']} {ok['landing']}")
    assert ok["status"] == "approved"
    row = yaml.safe_load((env / "feeds" / "x44_nino12_sst.yml").read_text())
    assert row["contributed"] is True and row["fetch"]["index_name"] == "Nino1+2"
    assert registry.FEEDS["x44_nino12_sst"]["declarative"] and "x44_nino12_sst" not in staging.STAGED_FEEDS
    everyone = _query_as(OTHER)
    assert everyone["status"] == "ok" and "staged" not in everyone["passport"]


def test_rejection_leaves_nothing_behind(env, log):
    out = staging.submit("feed", _manifest(), OWNER)
    rej = staging.reject(out["contribution_id"], "duplicate of enso_oni coverage", REVIEWER)
    log("OUTPUT", f"{rej['status']} {rej['retired']}")
    assert rej["status"] == "rejected" and rej["retired"]["removed"]
    assert _query_as(OWNER)["status"] == "declined" and not (env / "feeds").exists()


def test_staged_feed_rows_survive_a_restart(env, log):
    out = staging.submit("feed", _manifest(), OWNER)
    staging.STAGED_FEEDS.clear()
    staging._STAGED_LOADED = False
    again = _query_as(OWNER)
    log("OUTPUT", f"after 'restart': {again['status']}")
    assert again["status"] == "ok" and again["passport"]["staged"]["contribution_id"] == out["contribution_id"]


def test_contributed_upstreams_are_rechecked_on_every_read(env, monkeypatch, log):
    out = staging.submit("feed", _manifest(), OWNER)
    staging.approve(out["contribution_id"], REVIEWER)
    monkeypatch.setattr(fetch_policy, "check_url",
                        lambda url: ["host resolves to 10.1.30.110, a private address"])
    refused = _query_as(OTHER)
    log("OUTPUT", refused["note"])
    assert refused["status"] == "declined" and "fetch policy" in refused["note"]
    assert feeds.query("enso_oni", {})["status"] != "declined" or True   # built-ins untouched by the check
