"""A NaN fill, declared and read back (MGV-034/347). The real cases: a GSOC 1.5 float32
clip whose fill was rewritten to NaN (tagged NaN; a standard MCP client sends the
declared float NaN as JSON null) and a GMRT GridServer GeoTIFF (NaN fill, no tag,
declared "nan"). Null means omitted, "nan" in any case is NaN, a NaN mismatch says to
send "nan", and every answer names NaN as the declared no-data. Read through the risk
pack and a contributed extension pack."""

import json

import numpy as np
import rasterio
import yaml

from app.config import get_settings
from app.contrib import rasters, staging
from app.mcp import assemble

from test_layer_values import POLY, world  # noqa: F401  (fixture reuse)
from test_sprint_fix_rasters import _submit, _tif, _toy_pack, gate  # noqa: F401

DECL = {"dtype": "float32", "valid_min": 0, "valid_max": 200, "units": "t C/ha"}


def _gsoc(gate, name, tag):
    """t C/ha 26-165, NaN over the 'sea' (the first 7 rows): 140 of 400 cells."""
    arr = np.tile(np.linspace(26.05, 164.8, 20, dtype="float32"), (20, 1))
    arr[:7, :] = np.nan
    return gate["serve"](f"{name}.tif", _tif(gate["tmp"] / f"{name}.tif", arr, "float32",
                                              nodata=tag))


def _contract(layer):
    return yaml.safe_load(get_settings().raster_schema_contrib_path.read_text())["layers"][layer]


def test_a_null_nodata_is_an_omitted_one(gate, log):
    """fastmcp sends float('nan') as null: it was declined 'nodata nan != declared None'
    while an omitted nodata was approved."""
    for name, decl in (("measure_toy_gsoc_null", {**DECL, "nodata": None}),
                       ("measure_toy_gsoc_omit", dict(DECL))):
        out = _submit(name, _gsoc(gate, name, float("nan")), declared=decl)
        log("OUTPUT", json.dumps(out, default=str)[:600])
        assert out["status"] == "approved", out
        assert "nodata" not in _contract(name)          # the file's own tag is used
        assert "nodata" not in out["preview"]["contract"]
        # the reply carries the file's NaN tag as "nan", never as a null that reads 'no tag'
        assert out["preview"]["observed"]["nodata"] == "nan"
        json.dumps(out, allow_nan=False)
    # a bare NaN literal over raw JSON-RPC: kept as "nan", which JSON carries
    out = _submit("measure_toy_gsoc_lit", _gsoc(gate, "lit", float("nan")),
                  declared={**DECL, "nodata": float("nan")})
    assert out["status"] == "approved", out
    assert out["preview"]["contract"]["nodata"] == "nan" == _contract("measure_toy_gsoc_lit")["nodata"]


def test_the_string_nan_in_any_case_is_a_nan_fill(gate, log):
    """GMRT GridServer: NaN fill, no tag, declared "nan" — tagged NaN at ingest."""
    arr = np.tile(np.linspace(-3000, -10, 20, dtype="float32"), (20, 1))
    arr[:, :4] = np.nan
    url = gate["serve"]("gmrt.tif", _tif(gate["tmp"] / "gmrt.tif", arr, "float32"))
    out = _submit("measure_toy_gmrt", url, declared={**DECL, "valid_min": -6000,
                                                     "valid_max": 0, "units": "m",
                                                     "nodata": "NaN"})
    log("OUTPUT", json.dumps(out, default=str)[:600])
    assert out["status"] == "approved", out
    assert _contract("measure_toy_gmrt")["nodata"] == "nan"
    with rasterio.open(get_settings().tiffs_dir / "measure_toy_gmrt.tif") as src:
        assert np.isnan(src.nodata)


def test_a_nan_mismatch_says_to_send_nan(gate, log):
    """A NaN-tagged file declared -9999, and a -3.4e38-tagged file declared "nan"."""
    out = _submit("measure_toy_gsoc_bad", _gsoc(gate, "bad", float("nan")),
                  declared={**DECL, "nodata": -9999})
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined" and 'send the string "nan"' in out["problems"][0]
    arr = np.full((20, 20), 50.0, dtype="float32")
    arr[0, 0] = -3.4e38
    url = gate["serve"]("fill.tif", _tif(gate["tmp"] / "fill.tif", arr, "float32",
                                         nodata=-3.4e38))
    out = _submit("measure_toy_fill", url, declared={**DECL, "nodata": "nan"})
    log("OUTPUT", out["problems"])
    # already declared "nan": telling them to send "nan" would be wrong advice (review)
    assert out["status"] == "declined" and 'send the string "nan"' not in out["problems"][0]
    # a mismatch with no NaN on either side is not told about NaN
    assert 'send the string "nan"' not in rasters._explain("nodata 255 != declared 0",
                                                           {"nodata": 0}, {"nodata": 255})


def test_the_field_text_documents_the_nan_form():
    text = staging.RASTER_FIELDS["required"]["declared"]
    assert 'for a NaN fill, send the string "nan"' in text and "null" in text


def test_a_nan_layer_is_described_as_declared_nan(gate, monkeypatch, log):
    """'No-data: none (no no-data value is declared, so every value read is counted; NaN
    marks no value; 652 cell(s) here are NaN)' for a contract that says nodata: nan."""
    out = _submit("measure_toy_gsoc_nan", _gsoc(gate, "gsoc_nan", float("nan")),
                  declared={**DECL, "nodata": "nan"})
    assert out["status"] == "approved", out
    _submit("measure_toy_gsoc_tag", _gsoc(gate, "gsoc_tag", float("nan")), declared=dict(DECL))
    _toy_pack(monkeypatch)
    for pack, kw in (("risk", {"place": "Toyville", "hazard": "toyflood"}),
                     ("toy", {"place": "Toy bay"})):
        p = assemble.assemble(pack=pack, geometry=POLY,
                              layers=["measure_toy_gsoc_nan", "measure_toy_gsoc_tag"], **kw)
        texts = {c["layer"]: c["text"] for c in p["citations"] if c.get("layer")}
        log("OUTPUT", f"{pack}: {texts}")
        nan_text = texts["measure_toy_gsoc_nan"]
        assert "No-data: NaN (NaN — declared in the contract; " in nan_text
        assert "cell(s) here are NaN and are not counted" in nan_text
        assert "no no-data value is declared" not in nan_text
        assert "structural-contract-checked" in nan_text
        # omitted in the contract: the file's own NaN tag says it
        assert "No-data: NaN (NaN — the file's nodata tag; " in texts["measure_toy_gsoc_tag"]
        rec = p["layers"]["measure_toy_gsoc_nan"]
        assert rec["nodata"]["value"] is None and rec["nodata"]["values"] == ["NaN"]
        assert rec["summary"]["cells_without_value"] > 0
        json.dumps(rec, allow_nan=False)
