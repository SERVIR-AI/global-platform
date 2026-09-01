"""X1: the source-contribution gate — provenance enforced, refusals loud."""

import hashlib

import numpy as np
import pytest
import yaml

from app.config import get_settings
from app.contrib import sources
from app.rag.embed import ProviderEmbedder


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
    monkeypatch.setattr(ProviderEmbedder, "embed",
                        lambda self, texts: HashEmbedder().embed(texts))
    return tmp_path


def _entry(**over):
    d = {"pack": "food-security",
         "url": "https://example.org/bulletin.txt",
         "source": "WMO", "title": "El Nino update", "pub_date": "2026-08",
         "temporal": "forecast", "validation": "single-agency"}
    d.update(over)
    return d


def test_every_missing_field_is_named_at_once(log):
    fails = sources.validate_entry({"pack": "food-security"})
    log("OUTPUT", "; ".join(fails)[:120])
    missing = {f.split("'")[1] for f in fails if "missing required" in f}
    assert missing == {"url", "source", "title", "pub_date", "temporal", "validation"}


def test_vocabulary_violations_explain_themselves(log):
    fails = sources.validate_entry(_entry(temporal="someday", validation="trust me"))
    log("OUTPUT", "; ".join(fails)[:160])
    assert any("temporal must be one of" in f for f in fails)
    assert any("unvalidated" in f for f in fails)          # the honest escape is offered


def test_unknown_fields_are_refused_not_ignored(log):
    fails = sources.validate_entry(_entry(vibes="good"))
    log("OUTPUT", fails[0])
    assert any("unknown fields" in f for f in fails)


def test_corpusless_pack_declines_honestly(env, log):
    out = sources.contribute([_entry(pack="risk")])
    log("OUTPUT", out["results"][0]["failures"][0][:100])
    assert out["declined"] == 1
    assert "declared gap" in out["results"][0]["failures"][0]


def test_dry_run_validates_but_never_ingests(env, monkeypatch, log):
    called = []
    monkeypatch.setattr(sources, "_fetch", lambda url: called.append(url))
    out = sources.contribute([_entry()], dry_run=True)
    log("OUTPUT", out["results"][0]["status"])
    assert out["results"][0]["status"].startswith("valid")
    assert not called                                       # no network on dry-run


def test_valid_entry_ingests_with_full_passport(env, monkeypatch, log):
    monkeypatch.setattr(sources, "_fetch",
                        lambda url: (b"El Nino conditions strengthen over 2026." * 10,
                                     "bulletin.txt"))
    out = sources.contribute([_entry()])
    r = out["results"][0]
    log("OUTPUT", f"{r['status']} doc={r.get('doc_id')} chunks={r.get('chunks')}")
    assert r["status"] == "ingested" and out["declined"] == 0
    assert r["passport"]["validation"] == "single-agency"
    from app.rag.store import Corpus
    c = Corpus("food-security")
    assert c.raw_path(r["doc_id"]) is not None              # original bytes archived
    hit = c.search("El Nino conditions 2026", k=1, min_relevance=-1.0)[0]
    assert hit["metadata"]["source"] == "WMO"


def test_one_bad_entry_never_blocks_a_good_one(env, monkeypatch, log):
    monkeypatch.setattr(sources, "_fetch",
                        lambda url: (b"Rainfall outlook for the region." * 10, "a.txt"))
    out = sources.contribute([_entry(), _entry(title="", url="")])
    log("OUTPUT", f"ingested={out['ingested']} declined={out['declined']}")
    assert out["ingested"] == 1 and out["declined"] == 1


def test_manifest_loads_bare_list_and_wrapped(tmp_path, log):
    p1 = tmp_path / "a.yml"
    p1.write_text(yaml.safe_dump([_entry()]))
    p2 = tmp_path / "b.yml"
    p2.write_text(yaml.safe_dump({"sources": [_entry()]}))
    assert len(sources.load_manifest(str(p1))) == 1
    assert len(sources.load_manifest(str(p2))) == 1
    with pytest.raises(ValueError):
        (tmp_path / "c.yml").write_text("just a string")
        sources.load_manifest(str(tmp_path / "c.yml"))
    log("CHECK", "both manifest shapes load; junk refuses")
