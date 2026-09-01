"""Removal — stops future use, never rewrites history."""

import hashlib

import numpy as np
import pytest
import yaml

from app.config import get_settings
from app.contrib import removal, sources, tables
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus


class HashEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "feeds_conf_dir", tmp_path / "feeds")
    monkeypatch.setattr(ProviderEmbedder, "embed",
                        lambda self, texts: HashEmbedder().embed(texts))
    return tmp_path


def test_doc_removal_keeps_the_raw_archive(env, monkeypatch, log):
    monkeypatch.setattr(sources, "_fetch",
                        lambda url: (b"Rainfall outlook text." * 20, "r.txt"))
    r = sources.contribute([{
        "pack": "food-security", "url": "https://x/r.txt", "source": "S",
        "title": "T", "pub_date": "2026-08", "temporal": "forecast",
        "validation": "unvalidated"}])["results"][0]
    out = removal.remove_doc("food-security", r["doc_id"])
    log("OUTPUT", f"removed={out['removed_chunks']} archive_kept={out['raw_archive_kept']}")
    assert out["status"] == "removed" and out["raw_archive_kept"] is True
    c = Corpus("food-security")
    assert c.count() == 0                                    # gone from the index
    assert c.raw_path(r["doc_id"]) is not None               # bytes stay for replay
    again = removal.remove_doc("food-security", r["doc_id"])
    assert again["status"] == "declined"                     # honest double-remove


def test_table_removal_retires_the_copy_and_row(env, log):
    csv = env / "t.csv"
    csv.write_text("Month,V\n2026-08,1\n")
    tables.add({"dataset": "scratch_t", "file": str(csv), "title": "t",
                "description": "d", "source": "s", "validation": "unvalidated",
                "license": "CC0-1.0", "vintage": "2026-08", "cadence": "monthly",
                "columns": {"month": "Month", "v": "V"}, "units": "u"})
    out = removal.remove_feed("scratch_t")
    log("OUTPUT", str(out.get("archived_copy")))
    assert out["status"] == "removed"
    assert not (env / "feeds" / "scratch_t.yml").exists()
    assert (env / "tables" / "scratch_t.retired").exists()   # kept, aside
    assert removal.remove_feed("scratch_t")["status"] == "declined"
    assert removal.remove_feed("enso_oni")["status"] == "declined"   # code rows refuse


def test_raster_removal_only_touches_contributions(monkeypatch, tmp_path, log):
    cat = tmp_path / "tiffs.yml"
    cat.write_text(yaml.safe_dump({
        "hazard_flood": {"local_path": "tiffs/hazard_flood.tif"},
        "hazard_scratch": {"local_path": "tiffs/hazard_scratch.tif",
                           "contributed": True}}))
    sch = tmp_path / "schema.yml"
    sch.write_text(yaml.safe_dump({"layers": {"hazard_scratch": {"dtype": "int16"}}}))
    monkeypatch.setattr(get_settings(), "tiffs_config_path", cat)
    monkeypatch.setattr(get_settings(), "raster_schema_path", sch)
    monkeypatch.setattr(get_settings(), "tiffs_dir", str(tmp_path))
    (tmp_path / "hazard_scratch.tif").write_bytes(b"x")
    out = removal.remove_raster("hazard_scratch")
    log("OUTPUT", out["file"])
    assert out["status"] == "removed"
    assert "hazard_scratch" not in yaml.safe_load(cat.read_text())
    assert (tmp_path / "hazard_scratch.tif.retired").exists()
    builtin = removal.remove_raster("hazard_flood")
    assert builtin["status"] == "declined"
    assert "built-in" in builtin["failures"][0]


def test_pack_removal_only_touches_contributions(log):
    out = removal.remove_pack("risk")
    log("OUTPUT", out["failures"][0][:70])
    assert out["status"] == "declined" and "built-in" in out["failures"][0]
