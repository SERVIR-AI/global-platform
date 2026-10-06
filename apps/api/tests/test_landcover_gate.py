"""Land-cover pack, slices 6-7: a hub's own stack through the contribution gate, year by
year, and the server-side composition row."""
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.config import get_settings
from app.contrib import rasters
from app.graph.geo import stacks
from app.mcp import compose, registry


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    import yaml
    s = get_settings()
    (tmp_path / "tiffs.yml").write_text(yaml.safe_dump({"hazard_flood": {"local_path": "tiffs/hazard_flood.tif"}}))
    (tmp_path / "raster_schema.yml").write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {}}))
    monkeypatch.setattr(s, "tiffs_config_path", tmp_path / "tiffs.yml")
    monkeypatch.setattr(s, "raster_schema_path", tmp_path / "raster_schema.yml")
    monkeypatch.setattr(s, "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    monkeypatch.setattr(s, "raster_schema_contrib_path", tmp_path / "raster_schema.contrib.yml")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "tiffs")
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    return tmp_path


def _year_file(tmp_path, year, value):
    path = tmp_path / f"src-{year}.tif"
    arr = np.full((6, 6), value, dtype="uint8")
    arr[0, :] = 15
    with rasterio.open(path, "w", driver="GTiff", width=6, height=6, count=1, dtype="uint8",
                       crs="EPSG:4326", nodata=15, transform=from_origin(100.0, 20.0, 0.01, 0.01)) as dst:
        dst.write(arr, 1)
    return str(path)


def test_a_hub_stack_lands_through_the_gate_year_by_year(gate_env, log):
    import yaml
    legend = {1: "Water", 2: "Trees", 3: "Crops"}
    common = {"stack": "landcover_toy", "title": "Toy land cover 2021", "description": "toy national map",
              "source": "toy agency", "license": "CC-BY-4.0", "vintage": "2026-01", "legend": legend,
              "outside_values": [15], "authority": "national agency (toy)",
              "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 3, "nodata": 15}}
    bad = rasters.validate_manifest({**common, "layer": "landcover_toy_2022", "year": 2021, "file": "-"})
    assert any("<stack>_<year>" in f for f in bad)
    out = rasters.add({**common, "layer": "landcover_toy_2021", "year": 2021, "stack_title": "Toy national land cover",
                       "file": _year_file(gate_env, 2021, 2)})
    assert out["status"] == "landed", out
    out2 = rasters.add({**common, "layer": "landcover_toy_2022", "year": 2022, "title": "Toy land cover 2022",
                        "file": _year_file(gate_env, 2022, 3)})
    assert out2["status"] == "landed", out2
    cat = yaml.safe_load((gate_env / "tiffs.contrib.yml").read_text())
    st = cat["landcover_toy"]
    log("OUTPUT", f"stack row: years={st['years']} extent={st['extent']}")
    assert st["kind"] == "stack" and st["years"] == [2021, 2022] and st["title"] == "Toy national land cover"
    assert st["extent"] == {"lon": [100.0, 100.06], "lat": [19.94, 20.0]}
    assert cat["landcover_toy_2022"]["stack"] == "landcover_toy" and cat["landcover_toy_2022"]["year"] == 2022
    # a year with a different legend is refused; a duplicate year is refused
    clash = rasters.add({**common, "layer": "landcover_toy_2023", "year": 2023, "legend": {1: "Other"},
                         "file": _year_file(gate_env, 2023, 1)})
    assert clash["status"] == "declined" and "legend disagrees" in clash["failures"][0]
    dup = rasters.add({**common, "layer": "landcover_toy_2022", "year": 2022, "file": _year_file(gate_env, 2022, 3)})
    assert dup["status"] == "declined"
    # the landed stack is a real stack to the pack machinery
    from app.graph.geo import schema, tiffs
    assert stacks.layer_for("landcover_toy", 2022) == "landcover_toy_2022"
    assert schema.schema_for("landcover_toy_2022")["valid_max"] == 3
    assert tiffs.catalog()["landcover_toy_2021"]["internal"] is True
    from app.land_cover import synthesis
    assert "landcover_toy" in synthesis.landcover_stacks()


def test_composition_is_registered_with_a_runner():
    assert "landcover.brief" in registry.COMPOSITIONS
    assert registry.COMPOSITIONS["landcover.brief"]["runs_llm_server_side"] is True
    out = compose.run("landcover.brief", question="")
    assert out["status"] == "declined" and "question is required" in out["note"]
