"""Sprint fix round 2, review findings (2026-10-19): retrieval keeps every passage unless
the caller opts in, and the reference test reads only real bibliographies and contents
pages; a clip the host cannot honour is declined in words; a torn zip is declined; an
anonymous caller owns nothing; GDAL reads contributed bytes with one driver chosen from
their magic bytes and no network; a recipe marker anywhere in the header is refused; a
take-down removes only its own landing. All on synthetic data, never presented as real."""
import hashlib
import re

import numpy as np
import pytest

from app.config import get_settings
from app.mcp import fetch
from app.rag import filters as rag_filters
from app.rag.store import Corpus


# ------------------------------------------------------------ 1. retrieval, opt-in skip
class WordEmbedder:
    """A bag of words on 1,024 hashed dimensions — no network."""
    provider, model = "test", "words-v1"

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(1024, np.float32)
            for w in re.findall(r"[a-záéíóúñ]+", t.lower()):
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % 1024] += 1
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return np.stack(out)


# Shapes measured on the real libraries, with made-up figures and sources: each is a
# passage with findings that the old heuristic passed over.
DATED_REPORT = (
    "Thematic Report | 16 January 2025\nCRISIS IMPACTS\nFood security\nThe drought has aggravated "
    "food insecurity. Between October 2024 and March 2025 an estimated 5.1 million people (31% of "
    "the analysed population) will face high levels of food insecurity, including 210,000 people at "
    "Emergency (IPC Phase 4) levels (IPC 02/10/2024; StatsOffice accessed 10/12/2024). Food prices "
    "rose 17.9% between November 2023 and November 2024 (StatsOffice 10/12/2024; Inst 21/11/2024). "
    "The 2023-2024 season was the poorest since 2019, after 2015-2016 and 1991-1992, and grain "
    "output fell by half against 2022-2023 (Agency 16/12/2024; Agency 07/05/2024; Govt 16/04/2024).")
STATS_TABLE = (
    "Table 3.7: Production and Value of Millet, 2020-2024\nYear Units 2020 2021 2022 2023 2024*\n"
    "Area Hectares 116,000 126,000 108,000 129,000 131,000\nProduction Tonnes 81,000 92,000 70,000 "
    "99,000 101,000\nValue KSh million 2019 2020 2021 2022 2023\nSource: State Department, 2024")
CONTENTS_TAIL = (
    "Purpose 3\nMethodology 3\nKey Messages 4\nSouthern Africa 5\nEastern Africa 6\nContents\n\n"
    "Methodology\nThe study examines the main impacts of the 2015/16 and 2023/24 El Niño events on "
    "drought, flooding, displacement and the agriculture sector, to show what a future El Niño "
    "could bring to each region. Key messages: about 60 million people were affected in 2015/16 "
    "and around 50 million in 16 countries by April 2024; the worst-affected countries were chosen "
    "by severity of impacts, people affected and the level of food insecurity.")
LEADER_TABLE = ("Maize ........ 1,234\nSorghum ........ 5.8\nBeans ........ 12 34\nCassava ........ 2,001\n"
                "Rice ........ 0.75")
CITED_PROSE = (
    "There is an urgent need to evaluate the combined impacts of climate change and infrastructure "
    "basin-wide (Alpha et al., 2019; Beta et al., 2010; Gamma et al., 2012). Existing studies focus "
    "on basin flows (Delta et al., 2018; Alpha et al., 2016; Epsilon et al., 2018) or on the lake "
    "(Zeta et al., 2012; Eta et al., 2021; Theta et al., 2018; Iota et al., 2019); very little is "
    "known of the floodplain itself.")
LABEL_SHORT = ("ENSO Alert System Status: El Niño Advisory\nSynopsis: El Niño conditions are present and "
               "expected to strengthen into the Northern Hemisphere winter 2026-27.")
URL_LIST = "\n".join(
    f"AGENCY ({d} 20{y}). Report {y} on the season. URL: https://example.org/reports/{y}/season-{y}"
    for d, y in (("March", 16), ("18 Mar", 16), ("13 Aug", 24), ("31 Jan", 16), ("11 Dec", 23),
                 ("May", 24), ("30 Aug", 24)))
AUTHOR_LIST = ("Apellido, A. B., Autora, C., et al. (2012). Carbon stocks. Estuaries 35, 1-10. doi:10.1/x\n"
               "Nombre, D. (2010) Wetlands. Tercero, E. et al. 2008 Global Biogeochem. doi:10.1/y\n"
               "Cuarto, F. G. et al. (2011) Nat. Geosci. 4, 293. Quinto, H. et al. 2011 Wetlands 31, 343.")
REFS_HEADED = ("Report of the season 14\nReferences\nAGENCY (2019). Informe nacional. San José.\n"
               "OFFICE (2021). Mapa de ecosistemas. San José.\nFAO (2007). The world's mangroves. Roma.")
TOC_LEADERS = ("CHAPTER 2: APPROACH ............................ 13\n2.0. OVERVIEW .................... 14\n"
               "2.1. APPROACH ...................... 14\n2.1.1. Roadmap ..................... 14\n"
               "2.1.2. Funding ...................... 15")
TOC_HEADED = "Table of Contents\nPurpose 3\nMethodology 3\nKey Messages 4\nSouthern Africa 5\nEastern Africa 6"


def test_the_reference_test_reads_real_bibliographies_and_contents_pages_only(log):
    """Years alone, a contents tail before findings, table rows with leaders, prose citing
    in parentheses and a 'Synopsis:' label are passages; author or link lists, a references
    heading at the top and contents pages are not."""
    for name, text in (("dated report", DATED_REPORT), ("statistics table", STATS_TABLE),
                       ("contents tail then findings", CONTENTS_TAIL), ("leader table", LEADER_TABLE),
                       ("prose citing in parentheses", CITED_PROSE), ("short labelled lines", LABEL_SHORT)):
        log("CHECK", f"kept: {name}")
        assert not rag_filters.looks_like_references(text), name
    for name, text in (("link list", URL_LIST), ("author list", AUTHOR_LIST), ("references heading", REFS_HEADED),
                       ("dotted contents", TOC_LEADERS), ("headed contents", TOC_HEADED),
                       ("imprint fragment", "In: Paris: Food and Agriculture Organization")):
        log("CHECK", f"passed over: {name}")
        assert rag_filters.looks_like_references(text), name


@pytest.fixture
def library(tmp_path, monkeypatch):
    import app.rag.embed as rag_embed
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "rag_min_relevance", 0.01)
    monkeypatch.setattr(rag_embed, "get_embedder", lambda: WordEmbedder())

    def fill(name, docs):
        c = Corpus(name)
        for text, meta in docs:
            c.ingest(text, {"source": "test", "temporal": "retrospective", **meta})
        return Corpus(name)
    return fill


DOCS = ((DATED_REPORT, {"title": "Drought impact update"}), (STATS_TABLE, {"title": "Production report"}),
        (CONTENTS_TAIL, {"title": "Lessons for preparedness"}), (AUTHOR_LIST, {"title": "Carbon paper, references"}))
Q = "drought food insecurity millet production impacts season carbon"


@pytest.mark.parametrize("pack", ["food-security", "risk", "land-cover"])
def test_the_library_skips_nothing_unless_the_caller_opts_in(library, pack, log):
    """Every pack's library: the default keeps every passage; opting in passes over the
    bibliography only — the dated report, the table and the contents tail stay."""
    c = library(pack, DOCS)
    every = {h["metadata"]["title"] for h in c.search(Q, k=10)}
    skipped = {}
    sifted = {h["metadata"]["title"] for h in c.search(Q, k=10, skip_references=True, skipped=skipped)}
    log("OUTPUT", f"{pack}: default {sorted(every)}; opted in {sorted(sifted)}")
    assert every == {t for _, m in DOCS for t in [m["title"]]}
    assert sifted == every - {"Carbon paper, references"}
    assert skipped["references"] == ["Carbon paper, references"]


def test_corpus_search_opts_in_and_keeps_a_freshly_contributed_dated_report(library, log):
    """corpus_search passes over the bibliography and says so; a contributed report full of
    dates (not a bibliography) is returned, not passed over."""
    library("food-security", DOCS + ((DATED_REPORT.replace("Thematic", "Contributed"),
                                      {"title": "Contributed update", "contribution_id": "c0ffee01"}),))
    out = fetch.search(Q, k=10)
    titles = [h["passport"]["title"] for h in out["hits"]]
    log("OUTPUT", titles)
    assert "Contributed update" in titles and "Drought impact update" in titles
    assert "Carbon paper, references" not in titles
    assert "1 reference-list or contents passage(s)" in out["passed_over"]
