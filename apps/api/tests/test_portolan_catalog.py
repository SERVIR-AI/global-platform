"""The published catalog, both directions: the gate publishes an approved raster as a
Portolan collection (contrib/portolan.py) and a server reads such a catalog as a third,
non-shadowing source (graph/geo/stac_catalog.py), mirrors the file with its checksum
verified, lists the root's health and declares a gap when it cannot be read."""
import hashlib
import json
import os

import numpy as np
import pytest
import yaml

from app.config import get_settings
from app.contrib import portolan
from app.graph.geo import ingest, schema, stac_catalog, tiffs


def _reset():
    """Forget every catalog memo: the tests rewrite files under the refresh interval."""
    stac_catalog._STATE.update({"key": None, "at": 0.0, "roots": {}, "stamp": ""})
    tiffs._BASE.clear()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated conf files, tiffs dir and Portolan root; no catalog root configured yet."""
    cat = tmp_path / "tiffs.yml"
    cat.write_text(yaml.safe_dump({}))
    sch = tmp_path / "raster_schema.yml"
    sch.write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
                                   "layers": {}}))
    s = get_settings()
    monkeypatch.setattr(s, "tiffs_config_path", cat)
    monkeypatch.setattr(s, "raster_schema_path", sch)
    monkeypatch.setattr(s, "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    monkeypatch.setattr(s, "raster_schema_contrib_path", tmp_path / "raster_schema.contrib.yml")
    monkeypatch.setattr(s, "tiffs_dir", str(tmp_path / "tiffs"))
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "grp_portolan_root", str(tmp_path / "published"))
    monkeypatch.setattr(s, "grp_catalog_url", "")
    monkeypatch.setattr(s, "grp_catalog_mirror", True)
    _reset()
    yield tmp_path
    _reset()


ROW = {"title": "Heat days (test)", "description": "a tiny hazard layer",
       "legend": {1: "low", 2: "mid", 3: "high"}, "source": "platform test fixture",
       "license": "CC-BY-4.0", "vintage": "2024-03", "countries": ["Cambodia"],
       "role": "hazard", "contributed": True, "contribution_id": "c-1",
       "local_path": "tiffs/hazard_test.tif"}
CONTRACT = {"dtype": "int16", "valid_min": 0, "valid_max": 3, "nodata": None, "role": "hazard"}


def _sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _publish(env, tif_writer, data=None, name="hazard_test"):
    os.makedirs(env / "tiffs", exist_ok=True)
    data = data if data is not None else np.array([[1, 2], [3, 0]], dtype="int16")
    src = tif_writer(data, name=f"{name}_src.tif")
    return portolan.publish_raster(name, ROW, CONTRACT, src), src


def test_publish_writes_a_portolan_collection_with_checksums(env, tif_writer, log):
    """An approved raster becomes collection.json + README + AGENTS + an immutable version
    folder holding a COG and a thumbnail; the collection's data asset states the sha256."""
    result, _ = _publish(env, tif_writer)
    log("OUTPUT", result)
    assert result["status"] == "published"
    root = env / "published"
    vdir = root / "hazard_test" / result["version"]
    cog = vdir / "hazard_test.tif"
    assert cog.exists() and (vdir / "thumbnail.png").exists()
    assert (root / "catalog.json").exists() and (root / "README.md").exists()
    assert (root / "hazard_test" / "AGENTS.md").exists() and (root / "hazard_test" / "README.md").exists()
    coll = json.loads((root / "hazard_test" / "collection.json").read_text())
    asset = coll["assets"]["data"]
    assert asset["file:checksum"] == "1220" + _sha(cog) and asset["file:size"] == cog.stat().st_size
    assert asset["type"] == portolan.COG_TYPE and asset["roles"] == ["data"]
    assert asset["href"] == f"./{result['version']}/hazard_test.tif"
    assert coll["assets"]["thumbnail"]["roles"] == ["thumbnail"]
    assert coll["servir:layer"] == "hazard_test" and coll["version"] == result["version"]
    assert coll["servir:contract"]["valid_max"] == 3
    assert [c["value"] for c in asset["bands"][0]["classification:classes"]] == [1, 2, 3]
    assert asset["bands"][0]["raster:bits_per_sample"] == 16
    assert coll["license"] == "CC-BY-4.0"
    assert coll["providers"][-1]["roles"] == ["host"]
    assert portolan.SPEC_SCHEMA in coll["stac_extensions"]
    assert {lk["rel"] for lk in coll["links"]} >= {"root", "self", "describedby", "agents"}
    cat = json.loads((root / "catalog.json").read_text())
    assert any(lk["rel"] == "child" and lk["href"] == "./hazard_test/collection.json"
               for lk in cat["links"])
    import rasterio
    with rasterio.open(cog) as ds:
        assert ds.profile.get("tiled") or ds.block_shapes[0] == (512, 512) or ds.width < 512
        assert ds.read(1).tolist() == [[1, 2], [3, 0]]


def test_same_bytes_unchanged_new_bytes_new_version_old_archived(env, tif_writer, log):
    """Nothing is ever overwritten: the same file reports unchanged; changed bytes add
    a new version folder and the previous file stays as an archive asset with its sha."""
    first, _ = _publish(env, tif_writer)
    again, _ = _publish(env, tif_writer)
    assert again["status"] == "unchanged" and again["version"] == first["version"]
    second, _ = _publish(env, tif_writer, data=np.array([[3, 3], [3, 3]], dtype="int16"))
    log("OUTPUT", {"first": first["version"], "second": second["version"], "superseded": second["superseded"]})
    assert second["status"] == "published" and second["version"] != first["version"]
    assert second["superseded"] == [first["version"]]
    coll = json.loads((env / "published" / "hazard_test" / "collection.json").read_text())
    assert coll["version"] == second["version"]
    assert coll["assets"]["data"]["file:checksum"] == "1220" + second["sha256"]
    old = coll["assets"][f"archive-{first['version']}"]
    assert old["roles"] == ["archive"] and old["file:checksum"] == "1220" + first["sha256"]
    assert (env / "published" / "hazard_test" / first["version"] / "hazard_test.tif").exists()
    assert [h["deprecated"] for h in coll["servir:versions"]] == [True, False]


def test_server_reads_the_catalog_as_a_third_source_and_mirrors_the_file(env, tif_writer, monkeypatch, log):
    """A layer that exists ONLY in the published catalog is a normal catalog row: same
    fields, a contract, a mirrored file verified by checksum, and capabilities say
    where it came from."""
    result, src = _publish(env, tif_writer)
    monkeypatch.setattr(get_settings(), "grp_catalog_url", str(env / "published" / "catalog.json"))
    _reset()
    cat = tiffs.catalog()
    assert "hazard_test" in cat and cat["hazard_test"]["stac"]["item"] == result["version"]
    row = cat["hazard_test"]
    log("OUTPUT", {k: row[k] for k in ("title", "legend", "source", "license", "vintage", "countries", "role")})
    assert row["title"] == ROW["title"] and row["license"] == "CC-BY-4.0"
    assert row["legend"] == {1: "low", 2: "mid", 3: "high"}
    assert row["vintage"] == "2024-03" and row["countries"] == ["Cambodia"] and row["role"] == "hazard"
    assert row["contributed"] is True and row["extent"]["lon"][0] == pytest.approx(103.0, abs=1e-6)
    contract = schema.schema_for("hazard_test")
    assert contract["dtype"] == "int16" and contract["valid_max"] == 3 and contract["role"] == "hazard"
    path = ingest.source_raster("hazard_test")
    assert path.startswith(str(env / "tiffs")) and _sha(path) == result["sha256"]
    assert ingest.source_raster("hazard_test") == path            # mirrored once
    from app.mcp import registry
    caps = registry.capabilities()
    me = next(l for l in caps["raster_layers"] if l["name"] == "hazard_test")
    assert me["source"] == "catalog" and me["contributed"] is True
    assert caps["catalog_sources"][0]["state"] == "ok"
    assert caps["catalog_sources"][0]["layers"] == ["hazard_test"]
    assert stac_catalog.gap_lines() == []


def test_a_local_row_wins_and_the_shadowing_is_said(env, tif_writer, monkeypatch):
    """The catalog cannot shadow a hand or contributed row; the clash is reported."""
    _publish(env, tif_writer)
    (env / "tiffs.contrib.yml").write_text(yaml.safe_dump({"hazard_test": {
        "local_path": "tiffs/hazard_test.tif", "title": "the local one", "contributed": True}}))
    monkeypatch.setattr(get_settings(), "grp_catalog_url", str(env / "published" / "catalog.json"))
    _reset()
    cat = tiffs.catalog()
    assert cat["hazard_test"]["title"] == "the local one" and not cat["hazard_test"].get("stac")
    assert stac_catalog.shadowed(["hazard_test"]) == ["hazard_test"]
    from app.mcp import registry
    note = [e for e in registry.capabilities()["catalog_sources"] if "shadowed_by_local" in e]
    assert note and note[0]["shadowed_by_local"] == ["hazard_test"]


def test_an_unreachable_root_is_a_declared_gap_never_a_silent_fallback(env, monkeypatch, log):
    """A root that cannot be read: status says so, every pack gets a gap line, and the
    rest of the catalog keeps working."""
    monkeypatch.setattr(get_settings(), "grp_catalog_url",
                        f"{env / 'missing' / 'catalog.json'}, http://127.0.0.1:9/catalog.json")
    _reset()
    assert tiffs.catalog() == {}                                    # nothing else declared
    states = {e["root"]: e["state"] for e in stac_catalog.status()}
    log("OUTPUT", states)
    assert set(states.values()) == {"unreachable"}
    lines = stac_catalog.gap_lines()
    assert len(lines) == 2 and all("could not be read" in line for line in lines)
    from app.mcp import registry
    assert {e.get("state") for e in registry.capabilities()["catalog_sources"]} == {"unreachable"}


def test_a_root_that_was_read_once_serves_its_last_copy_and_says_since_when(env, tif_writer, monkeypatch, log):
    """Once read, a root that then disappears is 'stale': its rows still serve, and the
    gap line names the time of the last good read."""
    _publish(env, tif_writer)
    root = env / "published" / "catalog.json"
    monkeypatch.setattr(get_settings(), "grp_catalog_url", str(root))
    _reset()
    assert "hazard_test" in tiffs.catalog()
    root.rename(env / "gone.json")
    stac_catalog.refresh(force=True)
    tiffs._BASE.clear()
    entry = stac_catalog.status()[0]
    log("OUTPUT", f"{entry['state']}: {entry['detail'][:60]}")
    assert entry["state"] == "stale" and "hazard_test" in tiffs.catalog()
    assert "served from the copy read at" in stac_catalog.gap_lines()[0]


def test_a_tampered_file_is_refused_by_its_checksum(env, tif_writer, monkeypatch):
    """The mirror verifies the bytes against the item's sha256 and leaves nothing behind."""
    result, _ = _publish(env, tif_writer)
    cog = env / "published" / "hazard_test" / result["version"] / "hazard_test.tif"
    with open(cog, "ab") as f:
        f.write(b"\0")
    monkeypatch.setattr(get_settings(), "grp_catalog_url", str(env / "published" / "catalog.json"))
    _reset()
    with pytest.raises(RuntimeError, match="checksum"):
        ingest.source_raster("hazard_test")
    assert not list((env / "tiffs").glob("stac__*")) if (env / "tiffs").exists() else True


def test_read_through_only_under_a_configured_root(env, monkeypatch):
    """With mirroring off a remote file is opened in place, and only when it lies under
    a configured catalog root: a row cannot send GDAL to another host."""
    monkeypatch.setattr(get_settings(), "grp_catalog_url", "https://data.example.org/org/product/catalog.json")
    assert stac_catalog.remote_allowed("/vsicurl/https://data.example.org/org/product/x/y.tif")
    assert not stac_catalog.remote_allowed("/vsicurl/https://data.example.org/other/y.tif")
    assert not stac_catalog.remote_allowed("https://elsewhere.example/y.tif")
    assert stac_catalog.remote_allowed("/plain/local/file.tif")
    env_vars = stac_catalog.remote_env()
    assert env_vars["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR" and env_vars["CPL_VSIL_CURL_USE_HEAD"] == "NO"


def test_licence_and_vintage_mapping():
    """Contributor text becomes an SPDX id when it is one, else 'other' with the text kept."""
    assert portolan.license_id("CC-BY-4.0") == ("CC-BY-4.0", None)
    assert portolan.license_id("cc0 1.0") == ("CC0-1.0", None)
    assert portolan.license_id("ADPC internal, derived from JRC") == ("other", "ADPC internal, derived from JRC")
    assert portolan._vintage_datetime("2016-11") == "2016-11-01T00:00:00Z"
    assert portolan._vintage_datetime("2026") == "2026-01-01T00:00:00Z"
    assert portolan._vintage_datetime("n/a") is None


def test_other_kinds_declare_that_they_are_not_published_yet(env):
    """A vector or table landing says the publisher does not handle it, never silence."""
    out = portolan.annotate("vector", {"status": "landed"})
    assert out["published"]["status"] == "not published" and "vector" in out["published"]["note"]
    assert "published" not in portolan.annotate("raster", {"status": "landed"})
