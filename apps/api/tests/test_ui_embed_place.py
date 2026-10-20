"""An embed binds what its component declares: a receipt, or a place for the area monitor."""
from app.mcp import ui


def test_the_area_monitor_binds_a_place_and_the_others_a_receipt():
    out = ui.embed("area_monitor", place="Chomes district")
    assert out["status"] == "ok" and out["binds"] == "place"
    assert out["src"].endswith("/?embed=area_monitor&place=Chomes%20district")
    assert 'src="' + out["src"] in out["html"]
    assert ui.embed("area_monitor")["src"].endswith("place={place}")
    assert ui.embed("hazard_map", receipt_id="0123456789abcdef")["src"].endswith("receipt_id=0123456789abcdef")


def test_the_catalog_lists_the_area_monitor_as_embeddable():
    names = ui.embed("no_such_component")["available"]
    assert "area_monitor" in names and "hazard_map" in names
