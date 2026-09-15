"""X4.5 — a raster layer over the MCP: fetched and verified against its declared
contract before staging; staged layers appear in the catalog and the contract
lookup only for their owner and reviewers; approval lands the file and the
contrib rows; per-place clips are purged whenever a layer's identity changes."""
import pathlib

import pytest
import yaml

from app.config import get_settings
from app.contrib import fetch_policy, identity, removal, staging
from app.graph.geo import schema, tiffs
from app.mcp import store

OWNER = identity.Caller("user_owner", "hydrologist@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")

SAMPLE = pathlib.Path(__file__).resolve().parents[3] / "apps/web/public/runbook/samples/demo-hazard.tif"


@pytest.fixture
def env(monkeypatch, tmp_path):
    s = get_settings()
    cat = tmp_path / "tiffs.yml"
    cat.write_text("# HAND-AUTHORED\n" + yaml.safe_dump({"hazard_flood": {"local_path": "tiffs/hazard_flood.tif"}}))
    sch = tmp_path / "raster_schema.yml"
    sch.write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {}}))
    monkeypatch.setattr(s, "cache_dir", tmp_path)
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "tiffs")
    monkeypatch.setattr(s, "tiffs_config_path", cat)
    monkeypatch.setattr(s, "raster_schema_path", sch)
    monkeypatch.setattr(s, "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    monkeypatch.setattr(s, "raster_schema_contrib_path", tmp_path / "raster_schema.contrib.yml")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (SAMPLE.read_bytes(), "demo.tif"))
    staging.STAGED_RASTERS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    yield tmp_path
    staging.STAGED_RASTERS.clear()


def _manifest(**over):
    d = {"layer": "hazard_demoheat", "url": "https://example.org/demo-hazard.tif",
         "title": "Demo heat hazard (sample)", "description": "Severity classes 0-5, SYNTHETIC sample.",
         "source": "platform demonstration layer (synthetic)", "license": "CC0-1.0", "vintage": "2026-09",
         "legend": {1: "Very Low", 2: "Low", 3: "Moderate", 4: "High", 5: "Very High"},
         "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 5, "nodata": None},
         "usage_notes": "Demonstration layer only."}
    d.update(over)
    return d


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


def test_problems_named_at_once_and_nothing_stored(env, log):
    out = staging.submit("raster", {"layer": "heat", "file": "/x.tif", "legend": "bad",
                                    "declared": {"dtype": "int16"}, "extra": 1}, OWNER)
    log("OUTPUT", str(out["problems"]))
    joined = " ".join(out["problems"])
    assert out["status"] == "declined"
    assert "'file' is not accepted" in joined and "missing required field 'url'" in joined
    assert "namespaced" in joined and "legend must map" in joined and "declared.valid_min" in joined
    assert "unknown fields ['extra']" in joined
    assert store.list_contributions() == [] and not list((env / "tiffs").glob("*")) if (env / "tiffs").exists() else True


def test_a_file_that_contradicts_its_contract_is_refused_and_cleaned_up(env, log):
    out = staging.submit("raster", _manifest(declared={"dtype": "int16", "valid_min": 0, "valid_max": 3}), OWNER)
    log("OUTPUT", out["problems"][0][:200])
    assert out["status"] == "declined" and "DECLARED contract" in out["problems"][0]
    assert store.list_contributions() == [] and not list((env / "tiffs").glob("staged-*"))


def test_staged_layer_is_in_the_catalog_for_owner_and_reviewer_only(env, log):
    out = staging.submit("raster", _manifest(), OWNER)
    log("OUTPUT", f"{out['status']} observed={out['preview']['observed']}")
    assert out["status"] == "staged" and out["preview"]["observed"]["dtype"] == "int16"
    staged_file = pathlib.Path(out["preview"]["staged_file"])
    assert staged_file.is_file() and staged_file.parent == env / "tiffs"
    for caller, sees in ((OWNER, True), (REVIEWER, True), (OTHER, False)):
        key = _as(caller, lambda: tiffs.resolve("demoheat"))
        contract = _as(caller, lambda: schema.schema_for("hazard_demoheat"))
        legend = _as(caller, lambda: tiffs.legend("hazard_demoheat"))
        log("OUTPUT", f"{caller.label}: resolve={key} contract={'yes' if contract else 'no'} legend={len(legend)}")
        assert (key == "hazard_demoheat") is sees and (contract is not None) is sees and bool(legend) is sees
    assert _as(OWNER, lambda: tiffs.entry("hazard_demoheat"))["staged_by"] == OWNER.id
    assert "hazard_demoheat" not in tiffs.catalog(include_staged=False)


def test_a_taken_layer_name_is_refused(env, log):
    out = staging.submit("raster", _manifest(layer="hazard_flood"), OWNER)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "already in the catalog" in out["problems"][0]
    staging.submit("raster", _manifest(), OWNER)
    again = staging.submit("raster", _manifest(), OTHER)
    assert again["status"] == "declined" and "already in the catalog" in again["problems"][0]


def test_approval_lands_file_and_rows_and_purges_clips(env, log):
    out = staging.submit("raster", _manifest(), OWNER)
    clip_dir = env / "battambang"
    clip_dir.mkdir()
    (clip_dir / "hazard_demoheat.tif").write_bytes(b"stale clip")
    ok = staging.approve(out["contribution_id"], REVIEWER, "contract verified")
    log("OUTPUT", f"{ok['status']} landing={ok['landing']}")
    assert ok["status"] == "approved" and ok["landing"]["verified"] is True
    assert (env / "tiffs" / "hazard_demoheat.tif").is_file() and not list((env / "tiffs").glob("staged-*"))
    cat = yaml.safe_load((env / "tiffs.contrib.yml").read_text())
    assert cat["hazard_demoheat"]["contributed"] is True and cat["hazard_demoheat"]["license"] == "CC0-1.0"
    assert yaml.safe_load((env / "raster_schema.contrib.yml").read_text())["layers"]["hazard_demoheat"]["dtype"] == "int16"
    assert not (clip_dir / "hazard_demoheat.tif").exists()
    assert "hazard_demoheat" not in staging.STAGED_RASTERS
    assert _as(OTHER, lambda: tiffs.resolve("demoheat")) == "hazard_demoheat"
    assert "staged_by" not in _as(OTHER, lambda: tiffs.entry("hazard_demoheat"))


def test_rejection_retires_the_file_and_leaves_the_catalog_untouched(env, log):
    out = staging.submit("raster", _manifest(), OWNER)
    rej = staging.reject(out["contribution_id"], "synthetic layers are demos, not sources", REVIEWER)
    log("OUTPUT", f"{rej['status']} {rej['retired']}")
    assert rej["status"] == "rejected" and rej["retired"]["removed"]
    assert list((env / "tiffs").glob("*.retired")) and not (env / "tiffs.contrib.yml").exists()
    assert _as(OWNER, lambda: tiffs.resolve("demoheat")) is None


def test_staged_layers_survive_a_restart(env, log):
    out = staging.submit("raster", _manifest(), OWNER)
    staging.STAGED_RASTERS.clear()
    staging._STAGED_LOADED = False
    key = _as(OWNER, lambda: tiffs.resolve("demoheat"))
    log("OUTPUT", f"after 'restart': {key}")
    assert key == "hazard_demoheat" and _as(OWNER, lambda: schema.schema_for("hazard_demoheat"))["valid_max"] == 5


def test_remove_raster_purges_clips_too(env, log):
    out = staging.submit("raster", _manifest(), OWNER)
    staging.approve(out["contribution_id"], REVIEWER)
    (env / "kep").mkdir()
    (env / "kep" / "hazard_demoheat.tif").write_bytes(b"stale")
    gone = removal.remove_raster("hazard_demoheat")
    log("OUTPUT", f"{gone['status']} clips_purged={gone.get('clips_purged')}")
    assert gone["status"] == "removed" and gone["clips_purged"] == 1
    assert not (env / "kep" / "hazard_demoheat.tif").exists()
