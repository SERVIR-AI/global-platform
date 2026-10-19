"""What a retrieved passage must be before any pack cites it: a passage of its own,
not a reference list or a contents page, and — when the document declares the
countries it is about — about the country asked. The land-cover pack learned both
on the sprint corpus (a bibliography page outscored every finding; a Gulf of Nicoya
report was cited in Gulf of Guayaquil answers); they live here so `Corpus.search`,
`corpus_search` and every pack's retrieval apply the same rule.
"""
from __future__ import annotations

import re

# A reference list, a bibliography or a table of contents matches many queries and
# says nothing about the place. Headings in the languages hubs publish in.
_REF_HEADING = re.compile(
    r"^\W*(?:\d+(?:\.\d+)*\.?\s*)?(?:references?|bibliography|literature cited|works cited|"
    r"bibliograf[ií]a(?:\s+(?:citada|consultada))?|literatura\s+(?:citada|consultada)|"
    r"referencias(?:\s+bibliogr[aá]ficas)?|refer[eê]ncias(?:\s+bibliogr[aá]ficas)?|bibliographie|"
    r"r[ée]f[ée]rences(?:\s+bibliographiques)?|(?:table\s+of\s+)?contents|"
    r"[ií]ndice(?:\s+general)?|contenidos?|tabla\s+de\s+contenidos?|sum[aá]rio)\W*$", re.I | re.M)
# "Surname, A." / "Surname, A. B." — a surname and initials, in any Latin script.
_AUTHOR = re.compile(r"\b[A-ZÁÉÍÓÚÑÜÇ][a-záéíóúñüçãõ'’\-]+,\s(?:[A-ZÁÉÍÓÚÑ]\.\s?-?){1,3}")
_IMPRINT = re.compile(r"(?:^|\s)(?:In|En|Em|Dans):\s|\b[A-Z][a-zé]+(?:,\s?[A-Z]{2,})?:\s+[A-Z][\w&.\-]*")
_PAGES = re.compile(r"\b\d+\s?(?:p|pp|págs?|pages?)\.(?=\s|$)|\bpp?\.\s?\d+", re.I)
_LEADER = re.compile(r"(?:\.\s?){4,}\s*\d{1,4}\b|…\s*\d{1,4}\b")


def looks_like_references(text: str) -> bool:
    """A bibliography page matches many queries and says nothing about the place:
    many author-year citations, few sentences of its own — in any language
    ('Bibliografía', 'Literatura citada', 'Referencias'), and the fragments a
    reference list is cut into ('In: Paris: Food and Agriculture Organization'). A
    contents page is the same kind of noise: dotted leaders to page numbers."""
    years = len(re.findall(r"\b(?:19|20)\d\d[a-z]?\b", text))
    etal = text.lower().count("et al")
    dois = text.lower().count("doi")
    words = max(1, len(text.split()))
    authors = len(_AUTHOR.findall(text))
    imprint = len(_IMPRINT.findall(text))
    pages = len(_PAGES.findall(text))
    leaders = len(_LEADER.findall(text))
    return ((years >= 12 and years / words > 0.04) or etal >= 6 or dois >= 4
            or (authors >= 4 and authors / words > 0.02)
            or leaders >= 3
            or (bool(_REF_HEADING.search(text)) and authors + years + leaders + pages >= 3)
            or (words < 40 and imprint > 0 and (years + pages + authors + imprint) >= 2))


def declared_countries(metadata: dict) -> list[str]:
    return [str(c) for c in ((metadata or {}).get("countries") or []) if str(c).strip()]


def speaks_to(declared, countries) -> bool:
    """Does a document declaring `declared` countries speak to an area in one of
    `countries`? One that declares none is about anywhere; an area whose country is
    unknown rules nothing out. Names match forgivingly (Viet Nam / Vietnam)."""
    from ..graph.geo.vectors import country_matches
    declared = [str(c) for c in (declared or []) if str(c).strip()]
    countries = [str(c) for c in (countries or []) if str(c).strip()]
    if not declared or not countries:
        return True
    return any(country_matches(declared, c) for c in countries)


def sift(hits, where=(), skipped: dict | None = None, references: bool = False) -> list:
    """The hits a pack may cite, in their order: a reference-list or contents passage
    is passed over (unless `references`), and so is a document about another country
    than `where` (the countries of the area asked about). When `skipped` is a dict,
    what was passed over is recorded in it — {"references": [title], "elsewhere":
    {countries: [title]}} — so the caller can say so in its trace or its gaps."""
    kept = []
    for h in hits or []:
        m = h.get("metadata") or {}
        title = str(m.get("title") or h.get("doc_id") or "a document")
        if not references and looks_like_references(h.get("text") or ""):
            if skipped is not None:
                skipped.setdefault("references", []).append(title)
            continue
        declared = declared_countries(m)
        if not speaks_to(declared, where):
            if skipped is not None:
                skipped.setdefault("elsewhere", {}).setdefault(", ".join(declared), []).append(title)
            continue
        kept.append(h)
    return kept
