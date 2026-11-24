"""The raster contracts are parsed once per change of their files, and stay unshared."""
import yaml

from app.config import get_settings
from app.graph.geo import schema


def _files(tmp_path, monkeypatch, contrib_layers):
    hand, contrib = tmp_path / "raster_schema.yml", tmp_path / "raster_schema.contrib.yml"
    hand.write_text(yaml.safe_dump({"layers": {"hazard_x": {"role": "hazard"}},
                                    "stacks": {"landcover_s": {"role": "landcover"}}}))
    contrib.write_text(yaml.safe_dump({"layers": contrib_layers, "roles": {"measure": {"zero": "value"}}}))
    s = get_settings()
    monkeypatch.setattr(s, "raster_schema_path", hand)
    monkeypatch.setattr(s, "raster_schema_contrib_path", contrib)
    return hand, contrib


def test_contract_files_are_read_once_per_change(tmp_path, monkeypatch):
    hand, contrib = _files(tmp_path, monkeypatch, {"measure_a": {"role": "measure"}})
    opened, real_open = [], open
    def spy(path, *a, **k):
        if str(path) in (str(hand), str(contrib)):
            opened.append(str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    for _ in range(5):                          # a pack reads the contracts many times
        assert schema.schema_for("measure_a")["role"] == "measure"
        assert schema.roles()["measure"] == {"zero": "value"}
    assert sorted(opened) == sorted([str(hand), str(contrib)])
    contrib.write_text(yaml.safe_dump({"layers": {"measure_a": {"role": "measure"},
                                                  "measure_b": {"role": "measure"}}}))
    assert schema.schema_for("measure_b") == {"role": "measure"}      # a landed contract, next read
    assert opened.count(str(contrib)) == 2


def test_one_reader_cannot_change_what_the_next_reader_sees(tmp_path, monkeypatch):
    _files(tmp_path, monkeypatch, {"measure_a": {"role": "measure"}})
    first = schema._doc()
    first["layers"]["measure_zz"] = {"role": "measure"}
    first["stacks"]["landcover_zz"] = {"role": "landcover"}
    later = schema._doc()
    assert "measure_zz" not in later["layers"] and "landcover_zz" not in later["stacks"]
    assert sorted(schema.names()) == ["hazard_x", "measure_a"]
