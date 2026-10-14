"""The contribution gate around layer values: the coarseness advisory every raster
preview carries, the `amend` kind that corrects an approved layer's record (author's
preview first, then landed with its history), and the auto-approve switch."""

import json

import numpy as np
import yaml

from app.config import get_settings
from app.contrib import fetch_policy, staging
from app.graph.geo import layer_values
from app.mcp import assemble

from test_layer_values import OTHER, OWNER, POLY, REVIEWER, _as, _tif, world  # noqa: F401


def test_granularity_flags_a_continuous_layer_repeated_over_blocks(tmp_path, log):
    """The gate's advisory: a float layer whose values repeat over 50-cell blocks says
    its unit is coarser than a cell; one that varies cell by cell does not."""
    blocks = np.kron(np.arange(16, dtype="float32").reshape(4, 4) / 20, np.ones((50, 50)))
    coarse = _tif(tmp_path / "coarse.tif", blocks, "float32", bounds=(100, 14, 100.5, 14.5))
    fine = _tif(tmp_path / "fine.tif", np.random.default_rng(1).random((200, 200)),
                "float32", bounds=(100, 14, 100.5, 14.5))
    g1, g2 = layer_values.granularity(str(coarse)), layer_values.granularity(str(fine))
    log("OUTPUT", g1["note"][:160])
    assert g1["typical_run_km"][0] > 10 and "check it before approval" in g1["note"]
    assert g2["typical_run_km"][0] < 1 and "check it" not in g2["note"]


def test_a_correction_is_previewed_by_its_author_then_lands_with_its_history(world, monkeypatch, log):
    """The `amend` kind: a contributed layer's record says one thing and the data
    another. The author previews the corrected record; a reviewer lands it; the row
    keeps what it replaced."""
    src = _tif(world / "district.tif", np.full((20, 20), 0.061), "float32", nodata=-9999)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None:
                        (src.read_bytes(), "district.tif"))
    man = {"layer": "vulnerability_child_index", "url": "https://example.org/c.tif",
           "title": "Child sensitivity", "description": "one value per sub-district (tambon)",
           "source": "a hub", "license": "unstated", "vintage": "2026-09",
           "legend": {1: "Low", 5: "High"},
           "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 1, "nodata": -9999}}
    up = _as(OWNER, lambda: staging.submit("raster", man, OWNER))
    _as(REVIEWER, lambda: staging.approve(up["contribution_id"], REVIEWER))
    fix = {"layer": "vulnerability_child_index",
           "fields": {"description": "one value per DISTRICT (amphoe), repeated over its tambons",
                      "usage_notes": "Cannot show where inside a district; compare districts only.",
                      "units": "index 0-1"},
           "rationale": "all 21 tambons of one district share the same value"}
    bad = _as(OWNER, lambda: staging.submit("amend", {**fix, "layer": "hazard_toyflood"}, OWNER))
    assert bad["status"] == "declined" and "not an approved contributed layer" in " ".join(bad["problems"])
    out = _as(OWNER, lambda: staging.submit("amend", fix, OWNER))
    log("OUTPUT", json.dumps(out["preview"]["changed"])[:200])
    assert out["status"] == "staged" and set(out["preview"]["changed"]) == {
        "description", "usage_notes", "units"}
    mine = _as(OWNER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                geometry=POLY, layers=["vulnerability_child_index"]))
    rec = mine["layers"]["vulnerability_child_index"]
    assert rec["unit"] == "index 0-1" and "DISTRICT" in rec["description"]
    assert mine.get("staged_by") == OWNER.id                     # a preview, scoped
    theirs = _as(OTHER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                  geometry=POLY, layers=["vulnerability_child_index"]))
    assert "sub-district" in theirs["layers"]["vulnerability_child_index"]["description"]
    _as(REVIEWER, lambda: staging.approve(out["contribution_id"], REVIEWER))
    row = yaml.safe_load(get_settings().tiffs_contrib_path.read_text())["vulnerability_child_index"]
    assert row["description"].startswith("one value per DISTRICT")
    assert row["amended"][-1]["replaced"]["description"] == "one value per sub-district (tambon)"
    landed = _as(OTHER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                  geometry=POLY, layers=["vulnerability_child_index"]))
    assert landed["layers"]["vulnerability_child_index"]["unit"] == "index 0-1"
    assert "staged_by" not in landed
    audit = staging._landed_state({"kind": "amend", "preview": out["preview"], "manifest": fix})
    assert audit["live"] is True


def test_with_auto_approve_on_a_layer_and_its_correction_land_at_once(world, monkeypatch, log):
    """The product default for now: no reviewer step. A contributed raster is live for
    everyone on submit, its correction lands on submit too, and both records say no
    human reviewed them."""
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    src = _tif(world / "now.tif", np.full((20, 20), 0.3), "float32", nodata=-9999)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None:
                        (src.read_bytes(), "now.tif"))
    man = {"layer": "vulnerability_elderly_index", "url": "https://example.org/e.tif",
           "title": "Elderly sensitivity", "description": "one value per sub-district",
           "source": "a hub", "license": "unstated", "vintage": "2026-09",
           "legend": {1: "Low", 5: "High"},
           "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 1, "nodata": -9999}}
    up = _as(OWNER, lambda: staging.submit("raster", man, OWNER))
    log("OUTPUT", f"{up['status']} — {up.get('review', '')[:60]}")
    assert up["status"] == "approved" and "AUTO-APPROVED" in up["review"]
    seen = _as(OTHER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                geometry=POLY, layers=["vulnerability_elderly_index"]))
    assert seen["status"] == "ok" and "staged_by" not in seen
    assert "auto-approved" in seen["layers"]["vulnerability_elderly_index"]["review"]
    fix = _as(OWNER, lambda: staging.submit("amend", {
        "layer": "vulnerability_elderly_index",
        "fields": {"description": "one value per district"},
        "rationale": "the values repeat across every sub-district of a district"}, OWNER))
    assert fix["status"] == "approved"
    row = yaml.safe_load(get_settings().tiffs_contrib_path.read_text())["vulnerability_elderly_index"]
    assert row["description"] == "one value per district"
    assert row["amended"][-1]["review"].startswith("auto-approved")


def _land_a_layer(world, monkeypatch, layer="vulnerability_child_index", title="Child sensitivity",
                  value=0.061):
    src = _tif(world / f"{layer}.tif", np.full((20, 20), value), "float32", nodata=-9999)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None:
                        (src.read_bytes(), f"{layer}.tif"))
    man = {"layer": layer, "url": f"https://example.org/{layer}.tif", "title": title,
           "description": "one value per sub-district (tambon)", "source": "a hub",
           "license": "unstated", "vintage": "2026-09", "legend": {1: "Low", 5: "High"},
           "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 1, "nodata": -9999}}
    up = _as(OWNER, lambda: staging.submit("raster", man, OWNER))
    _as(REVIEWER, lambda: staging.approve(up["contribution_id"], REVIEWER))
    return layer


def test_only_the_contributor_or_a_reviewer_may_correct_and_never_unmark_a_demo(world, monkeypatch, log):
    """A record is corrected by whoever stands behind it, or a reviewer; and nobody can
    turn a declared demonstration layer into an undeclared one."""
    layer = _land_a_layer(world, monkeypatch)
    fix = {"layer": layer, "fields": {"description": "one value per district"},
           "rationale": "the tambons of a district share one value"}
    theirs = _as(OTHER, lambda: staging.submit("amend", fix, OTHER))
    log("OUTPUT", theirs["problems"][0])
    assert theirs["status"] == "declined" and "only the contributor" in theirs["problems"][0]
    assert _as(REVIEWER, lambda: staging.submit("amend", fix, REVIEWER))["status"] == "staged"
    demo = _land_a_layer(world, monkeypatch, layer="vulnerability_demo_index",
                         title="Synthetic demonstration index")
    out = _as(OWNER, lambda: staging.submit("amend", {
        "layer": demo, "fields": {"title": "Elderly index"}, "rationale": "tidy"}, OWNER))
    assert out["status"] == "declined" and "demonstration" in " ".join(out["problems"])


def test_a_units_only_correction_is_still_a_preview_and_auto_approval_marks_the_row(world, monkeypatch, log):
    """Staged units alone still make the author's pack a preview; an auto-approved
    correction writes its own review note onto the row every citation reads; a
    correction superseded by a later one is reported as superseded, not inert."""
    layer = _land_a_layer(world, monkeypatch)
    out = _as(OWNER, lambda: staging.submit("amend", {
        "layer": layer, "fields": {"units": "index 0-1"}, "rationale": "units were missing"}, OWNER))
    mine = _as(OWNER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                geometry=POLY, layers=[layer]))
    assert mine["layers"][layer]["unit"] == "index 0-1" and mine.get("staged_by") == OWNER.id
    _as(OWNER, lambda: staging.withdraw(out["contribution_id"], OWNER))
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    a = _as(OWNER, lambda: staging.submit("amend", {
        "layer": layer, "fields": {"description": "per district"}, "rationale": "r1"}, OWNER))
    b = _as(OWNER, lambda: staging.submit("amend", {
        "layer": layer, "fields": {"description": "per district (amphoe)"}, "rationale": "r2"}, OWNER))
    row = yaml.safe_load(get_settings().tiffs_contrib_path.read_text())[layer]
    log("OUTPUT", row["review"])
    assert "corrected by auto-approve" in row["review"] and len(row["amended"]) == 2
    rec_a = staging.store.load_contribution(a["contribution_id"])
    st = staging._landed_state(rec_a)
    assert st["live"] is None and b["contribution_id"] in st["why"]
    seen = _as(OTHER, lambda: assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                                                geometry=POLY, layers=[layer]))
    cit = next(c for c in seen["citations"] if c.get("layer") == layer)
    assert "record was corrected (2 time(s)" in cit["text"] and "auto-approve" in cit["text"]
