"""What a retrieved passage must be before a pack cites it: about the country asked
when the document declares its countries, and — for a caller that opts in
(`skip_references`) — a passage of its own, not a reference list or a contents page.
The land-cover pack learned both on the sprint corpus (a bibliography page outscored
every finding; a Gulf of Nicoya report was cited in Gulf of Guayaquil answers). The
reference test is opt-in because a heuristic that misreads one dated table drops
evidence silently; land cover and `corpus_search` (which says what it passed over)
opt in.
"""
from __future__ import annotations

import re

# A reference list, a bibliography or a table of contents matches many queries and
# says nothing about the place. Headings in the languages hubs publish in; one counts
# only at the top of a passage (a 'Contents' link in a page footer does not).
_REFS_WORDS = (r"references?(?:\s+and\s+data\s+sources)?|bibliography|literature cited|works cited|"
               r"bibliograf[ií]a(?:\s+(?:citada|consultada))?|literatura\s+(?:citada|consultada)|"
               r"referencias(?:\s+bibliogr[aá]ficas)?|refer[eê]ncias(?:\s+bibliogr[aá]ficas)?|"
               r"bibliographie|r[ée]f[ée]rences(?:\s+bibliographiques)?")
_TOC_WORDS = (r"(?:table\s+of\s+)?contents|[ií]ndice(?:\s+general)?|contenidos?|"
              r"tabla\s+de\s+contenidos?|sum[aá]rio|table\s+des\s+mati[eè]res")
_HEAD = r"^\W*(?:\d+(?:\.\d+)*\.?\s*)?(?:{})\W*$"
_REF_HEADING = re.compile(_HEAD.format(_REFS_WORDS), re.I)
_TOC_HEADING = re.compile(_HEAD.format(_TOC_WORDS), re.I)
# "Surname, A." / "Surname, A. B." — a surname and initials, in any Latin script.
_AUTHOR = re.compile(r"\b[A-ZÁÉÍÓÚÑÜÇ][a-záéíóúñüçãõ'’\-]+,\s(?:[A-ZÁÉÍÓÚÑ]\.\s?-?){1,3}")
_ETAL = re.compile(r"\bet\s+al\b", re.I)
_LINK = re.compile(r"(?:https?://|\bwww\.)\S+|\bdoi\s*:\s*\S+", re.I)     # one per link
_YEAR = re.compile(r"\b(?:19|20)\d\d[a-z]?\b")
_PAREN = re.compile(r"\([^()]*\)")
# The fragments a reference list is cut into: 'In: …', 'Paris: Food and Agriculture …'.
_IN = re.compile(r"(?:^|\s)(?:In|En|Em|Dans):\s")
_IMPRINT = re.compile(r"\b[A-Z][a-zé]+(?:,\s?[A-Z]{2,})?:\s+[A-Z][\w&.\-]*")
_PAGES = re.compile(r"\b\d+\s?(?:p|pp|págs?|pages?)\.(?=\s|$)|\bpp?\.\s?\d+", re.I)
# Dotted leaders to ONE page number: a table row's '…… 1,234', '…… 5.8' or '…… 12 34'
# is not a contents line.
_LEADER = re.compile(r"(?:(?:\.\s?){4,}|…+)\s*\d{1,4}(?![\d.,]|[ \t]+\d)")
# A contents line: a title then its page number (after dotted leaders, or alone).
_TOC_LINE = re.compile(r"(?:(?:\.\s?){4,}|…+|\s)\s*(?:\d{1,4}|[ivxlc]{1,6})\s*$", re.I)
# A table: a caption opening a line ('TABLE 4.3', 'Cuadro 2'), or rows mostly of numbers.
_CAPTION = re.compile(r"^\W*(?:table|tabla|tableau|tabela|cuadro|quadro)\s+[a-z]?\d+[a-z]?(?:\.\d+)*\b",
                      re.I | re.M)
_NUM = re.compile(r"[(\[]?[-+−–]?\d[\d.,\-–−/]*%?[)\]]?[,;:]?")
_PLAIN = re.compile(r"[(\[]?[-+−]?\d[\d.,]*%?[)\]]?[,;:]?")


def _is_table(text: str) -> bool:
    """A captioned table, or three rows of three or more plain numbers (a year, a
    page range or a line with a link is not one: those are what reference lines hold)."""
    if _CAPTION.search(text):
        return True
    rows = 0
    for ln in text.splitlines():
        toks = ln.split()
        nums = [t for t in toks if _NUM.fullmatch(t)]
        plain = [t for t in nums if _PLAIN.fullmatch(t) and not _YEAR.search(t)]
        rows += len(plain) >= 3 and 2 * len(nums) >= len(toks) and not _LINK.search(ln)
    return rows >= 3


def looks_like_references(text: str) -> bool:
    """A bibliography page or a contents page — never a passage with findings of its
    own. Years alone prove nothing (a dated report, a statistical table); it takes
    citations dense enough to BE the text (authors with initials, 'et al.', DOIs,
    links), a references heading opening the passage, or contents entries making
    up most of it. A short imprint fragment ('In: Paris: FAO') counts too."""
    words = max(1, sum(1 for w in text.split() if any(ch.isalnum() for ch in w)))  # not leaders
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:3]                                  # a running page header may come first
    years = len(_YEAR.findall(text))
    # '(Hoang et al., 2019; Lauri et al., 2012)' is prose citing its sources; only
    # citations outside parentheses are entries of a list
    bare = _PAREN.sub(" ", text)
    links = len(_LINK.findall(bare))
    cites = len(_AUTHOR.findall(bare)) + len(_ETAL.findall(bare)) + links
    pages = len(_PAGES.findall(text))
    leaders = len(_LEADER.findall(text))
    toc_lines = sum(1 for ln in lines if _TOC_LINE.search(ln))
    if (cites >= 5 and years >= 3 and (cites * 20 >= words or links * 30 >= words)
            and not _is_table(text)):                 # a table's 'Sources: …' footnote is not a list
        return True                                   # citations (or links) are the text
    if any(_REF_HEADING.match(ln) for ln in head) and cites + years + pages >= 3:
        return True                                   # 'References' heads the passage
    if leaders >= 3 and leaders * 20 >= words:
        return True                                   # dotted leaders to page numbers, little else
    if any(_TOC_HEADING.match(ln) for ln in head) and toc_lines >= 3 and 2 * toc_lines >= len(lines) - 1:
        return True                                   # 'Contents' heads a list of page numbers
    # a short tail of a reference list; 'Synopsis: El Niño …' or 'Source: FAO, 2024'
    # is a label, not an imprint, unless pages or a citation come with it
    return words < 40 and (bool(_IN.search(text))
                           or (bool(_IMPRINT.search(text)) and pages + cites >= 1))


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


def sift(hits, where=(), skipped: dict | None = None, skip_references: bool = False) -> list:
    """The hits a pack may cite, in their order: a document about another country than
    `where` (the countries of the area asked about) is passed over, and so — only when
    `skip_references` — is a reference-list or contents passage. When `skipped` is a
    dict, what was passed over is recorded in it — {"references": [title],
    "elsewhere": {countries: [title]}} — so the caller can say so in its trace or gaps."""
    kept = []
    for h in hits or []:
        m = h.get("metadata") or {}
        title = str(m.get("title") or h.get("doc_id") or "a document")
        if skip_references and looks_like_references(h.get("text") or ""):
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
