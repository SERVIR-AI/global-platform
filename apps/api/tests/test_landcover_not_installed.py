"""A declared year whose file is absent declines in words; the series skips it."""
import os
from app.mcp import assemble
from test_landcover_pack import lc_env  # noqa: F401


def test_missing_year_file_declines_and_series_skips(lc_env, tmp_path):
    data = tmp_path / "data"
    os.rename(data / "lc-2021.tif", data / "lc-2021.tif.away")      # 2021 declared, not installed
    out = assemble.assemble(pack="land-cover", place="Test District", year=2021)
    assert out["status"] == "declined" and "not installed on this server" in out["note"]
    assert "2 of 3 declared years are" in out["note"]
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022, classes=["Forest"])
    assert p["status"] == "ok", p.get("note")
    s = next(c for c in p["citations"] if c["kind"] == "class_series")
    assert [pt["t"] for pt in s["series"]["points"]] == ["2000", "2022"]
