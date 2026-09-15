"""X4.3 — a CSV table over the MCP: staged as a feed row its owner and reviewers
can query; approval lands it as a platform feed without a restart; rejection
retires the staged copy; and remove-feed no longer deletes shipped feeds."""
import copy

import pytest
import yaml

from app.config import get_settings
from app.contrib import fetch_policy, identity, removal, staging
from app.mcp import feeds, registry, store

OWNER = identity.Caller("user_owner", "hydrologist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")

CSV = "Month,Stage_m,Rain_mm\n2026-06,3.9,170\n2026-07,4.8,190\n2026-08,5.5,220\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path)
    monkeypatch.setattr(s, "feeds_conf_dir", tmp_path / "feeds")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    snapshot = copy.deepcopy(registry.FEEDS)
    staging.STAGED_FEEDS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    yield tmp_path
    registry.FEEDS.clear()
    registry.FEEDS.update(snapshot)
    staging.STAGED_FEEDS.clear()


def _manifest(**over):
    d = {"dataset": "sangkae_river_stage", "title": "Sangkae River monthly stage (SYNTHETIC)",
         "description": "SYNTHETIC demonstration series for one imaginary gauge.",
         "source": "hub hydrologist demonstration series (synthetic)",
         "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026-09",
         "cadence": "monthly", "columns": {"month": "Month", "stage_m": "Stage_m", "rain_mm": "Rain_mm"},
         "units": "metres above gauge datum; mm per month", "as_of_field": "month",
         "usage_notes": "SYNTHETIC single-station series; never generalise.", "csv_text": CSV}
    d.update(over)
    return d


def _query_as(caller, ds="sangkae_river_stage"):
    tok = identity.bind(caller)
    try:
        return feeds.query(ds, {"limit": 12})
    finally:
        identity.unbind(tok)


def test_header_mismatch_and_bad_fields_are_named_and_nothing_is_stored(env, log):
    out = staging.submit("table", _manifest(columns={"month": "Month", "flow": "Flow_m3s"},
                                            license=None), OWNER)
    log("OUTPUT", str(out["problems"]))
    assert out["status"] == "declined"
    joined = " ".join(out["problems"])
    assert "missing required field 'license'" in joined and "Flow_m3s" in joined
    assert store.list_contributions() == [] and not list((env / "tables" / "staged").glob("*.csv"))


def test_a_name_that_is_already_a_feed_is_refused(env, log):
    out = staging.submit("table", _manifest(dataset="enso_oni"), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "already a platform feed" in out["problems"][0]


def test_staged_table_serves_owner_and_reviewer_only(env, log):
    out = staging.submit("table", _manifest(), OWNER)
    log("OUTPUT", f"{out['status']} {out['contribution_id']} rows={out['preview']['rows']}")
    assert out["status"] == "staged" and out["preview"]["rows"] == 3
    mine = _query_as(OWNER)
    assert mine["status"] == "ok" and mine["records"][-1]["stage_m"] == 5.5
    assert mine["passport"]["staged"]["contribution_id"] == out["contribution_id"]
    assert "SYNTHETIC" in mine["note"]
    other = _query_as(OTHER)
    log("OUTPUT", f"other -> {other['status']}: {other['note']}")
    assert other["status"] == "declined" and "unknown dataset" in other["note"]
    assert "sangkae_river_stage" not in other["available"]
    assert _query_as(REVIEWER)["status"] == "ok"
    tok = identity.bind(OWNER)
    try:
        caps = registry.capabilities()
    finally:
        identity.unbind(tok)
    assert "sangkae_river_stage" in caps["staged_feeds"] and "sangkae_river_stage" not in caps["feeds"]
    tok = identity.bind(OTHER)
    try:
        assert registry.capabilities()["staged_feeds"] == {}
    finally:
        identity.unbind(tok)


def test_approval_lands_a_platform_feed_without_restart(env, log):
    out = staging.submit("table", _manifest(), OWNER)
    ok = staging.approve(out["contribution_id"], REVIEWER, "checked")
    log("OUTPUT", f"{ok['status']} landing={ok['landing']['feed_row']}")
    assert ok["status"] == "approved"
    row_file = env / "feeds" / "sangkae_river_stage.yml"
    assert row_file.is_file() and yaml.safe_load(row_file.read_text())["contributed"] is True
    assert "sangkae_river_stage" in registry.FEEDS and registry.FEEDS["sangkae_river_stage"]["declarative"]
    assert "sangkae_river_stage" not in staging.STAGED_FEEDS
    assert not list((env / "tables" / "staged").glob("*.csv"))
    everyone = _query_as(OTHER)
    assert everyone["status"] == "ok" and "staged" not in everyone["passport"]
    assert everyone["passport"]["residency"] == "platform-hosted copy"


def test_rejection_retires_the_staged_copy(env, log):
    out = staging.submit("table", _manifest(), OWNER)
    rej = staging.reject(out["contribution_id"], "synthetic data is not a source", REVIEWER)
    log("OUTPUT", f"{rej['status']} {rej['retired']}")
    assert rej["status"] == "rejected" and rej["retired"]["removed"]
    assert list((env / "tables" / "staged").glob("*.retired")) and not list((env / "tables" / "staged").glob("*.csv"))
    assert _query_as(OWNER)["status"] == "declined"
    assert not (env / "feeds" / "sangkae_river_stage.yml").exists()


def test_staged_rows_survive_a_restart(env, log):
    out = staging.submit("table", _manifest(), OWNER)
    staging.STAGED_FEEDS.clear()
    staging._STAGED_LOADED = False                     # what a fresh process looks like
    again = _query_as(OWNER)
    log("OUTPUT", f"after 'restart': {again['status']} contribution={again['passport'].get('staged')}")
    assert again["status"] == "ok" and again["passport"]["staged"]["contribution_id"] == out["contribution_id"]


def test_remove_feed_refuses_shipped_rows_unless_forced(env, log):
    shipped = env / "feeds" / "enso_soi.yml"
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shipped.write_text("dataset: enso_soi\ntitle: shipped\n")
    refused = removal.remove_feed("enso_soi")
    log("OUTPUT", refused["failures"][0])
    assert refused["status"] == "declined" and shipped.is_file() and "--force" in refused["failures"][0]
    assert removal.remove_feed("enso_soi", force=True)["status"] == "removed" and not shipped.exists()
    out = staging.submit("table", _manifest(), OWNER)
    staging.approve(out["contribution_id"], REVIEWER)
    gone = removal.remove_feed("sangkae_river_stage")
    assert gone["status"] == "removed" and not (env / "feeds" / "sangkae_river_stage.yml").exists()


def test_reload_drops_stale_rows_and_never_collides(env, log):
    (env / "feeds").mkdir(parents=True, exist_ok=True)
    (env / "feeds" / "demo_feed.yml").write_text(yaml.safe_dump({
        "dataset": "demo_feed", "title": "t", "description": "d", "source": "s",
        "validation": "unvalidated", "residency": "external call-out", "cadence": "monthly",
        "adapter": "generic_json", "fetch": {"url": "https://example.org/x", "records_path": "a", "fields": {"v": "v"}}}))
    names = registry.reload_declarative_feeds()
    assert "demo_feed" in names and "demo_feed.yml" not in registry.FEEDS
    names2 = registry.reload_declarative_feeds()
    log("OUTPUT", f"two reloads -> {names2}, collisions: {[k for k in registry.FEEDS if k.endswith('.yml')]}")
    assert names2 == names and not [k for k in registry.FEEDS if k.endswith(".yml")]
    assert registry.FEEDS["demo_feed"]["pack"] == "food-security"
