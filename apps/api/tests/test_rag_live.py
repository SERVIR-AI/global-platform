"""Live E1–E3 round trip: a REAL report PDF fetched from FAO GIEWS, extracted,
chunked, embedded through the real provider embeddings API, persisted, and
retrieved — nothing mocked. Opt-in (network + API key): RAG_LIVE=1.
"""

import os

import pytest

from app.config import get_settings
from app.rag import docloader
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus

_KEY_FOR = {"gemini": "google_api_key", "openai": "openai_api_key",
            "claude": "anthropic_api_key"}
_provider = get_settings().embedding_provider
pytestmark = pytest.mark.skipif(
    os.environ.get("RAG_LIVE", "").lower() not in ("1", "true", "yes")
    or not getattr(get_settings(), _KEY_FOR[_provider]),
    reason=f"set RAG_LIVE=1 (and the {_provider} API key) for the live round trip")

FAO_KENYA_PDF = "https://www.fao.org/giews/countrybrief/country/KEN/pdf/KEN.pdf"


def test_live_embeddings_are_sane(log):
    """Real provider embeddings: unit-normalized, and related texts score higher
    than unrelated ones."""
    emb = ProviderEmbedder()
    vecs = emb.embed(["maize harvest outlook in Kenya",
                      "corn production forecast for Kenyan farms",
                      "submarine navigation protocols"])
    log("MODEL", f"{emb.provider}:{emb.model} dim={vecs.shape[1]}")
    assert abs(float(vecs[0] @ vecs[0]) - 1.0) < 1e-3          # unit-normalized
    related, unrelated = float(vecs[0] @ vecs[1]), float(vecs[0] @ vecs[2])
    log("SIMS", f"related={related:.3f} unrelated={unrelated:.3f}")
    assert related > unrelated


def test_live_pdf_to_retrieval_round_trip(monkeypatch, tmp_path, log):
    """A3.1 acceptance, for real: a doc ingests -> chunks -> embeds -> retrieves."""
    import requests
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)   # never touch cache/

    pdf = requests.get(FAO_KENYA_PDF, timeout=(5, 60))
    pdf.raise_for_status()
    text = docloader.extract_text(pdf.content, "KEN.pdf")
    log("EXTRACT", f"{len(pdf.content)} bytes -> {len(text)} chars")
    assert "Kenya" in text

    corpus = Corpus("live-test")
    res = corpus.ingest(text, {"source": "FAO GIEWS", "title": "Kenya country brief",
                               "countries": ["Kenya"], "url": FAO_KENYA_PDF,
                               "validation": "single-agency"})
    log("INGEST", res)
    assert res["chunks"] >= 1

    hits = corpus.search("cereal production and rainfall in Kenya", k=3)
    log("TOP-HIT", f"score={hits[0]['score']} text={hits[0]['text'][:120]!r}" if hits
        else "NO HITS")
    assert hits, "a Kenya cereal query must retrieve from the Kenya country brief"
    assert hits[0]["metadata"]["source"] == "FAO GIEWS"
