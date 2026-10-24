"""A6 — the synthesis engine: question -> evidence pack -> grounded, cited brief.

The LLM is called exactly twice: parse the question (tool-call extraction, no
data access) and write the brief FROM THE PACK ONLY. Everything between is
deterministic; a blocking groundedness check runs after, with one retry — a
brief whose citations don't resolve does not ship.
"""

from __future__ import annotations

import bisect
import functools
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from ..config import get_settings
from ..llm import build_client, default_model
from ..rag.store import Corpus, source_block
from . import calendar as crop_calendar
from . import cropmonitor

CORPUS = "food-security"

_PARSE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "plan_brief",
        "description": "Extract the target of an in-scope food-security question.",
        "parameters": {
            "type": "object",
            "properties": {
                "crop": {"type": "string", "description": "crop asked about (e.g. maize); '' if none"},
                "country": {"type": "string", "description": "country/region asked about; '' if none"},
                "focus": {"type": "string", "description": "one line: what the asker wants to know"},
            },
            "required": ["focus"],
        },
    },
}]

def _parse_system() -> str:
    """The routing prompt, with the library's actual coverage in it. It used to
    freeze "El Nino impacts, food security in East/Southern Africa": a hub's own
    Cambodia rice bulletins would have been declined out of scope by the router
    before the library was read."""
    from ..mcp import registry
    try:
        c = registry.coverage()
    except Exception:
        c = {}
    crops = ", ".join(c.get("crops") or []) or "crops"
    where = ", ".join(c.get("countries") or []) or "the countries hubs contribute documents for"
    return (
        "You route questions for a food-security briefing service: crop conditions, seasonal "
        "forecasts, climate-driver (ENSO, IOD) impacts and food security. The document library "
        f"currently holds {crops} in {where}; any crop or country a hub has contributed "
        "documents for is in scope, and a question about crop conditions or food security "
        "anywhere is in scope — the evidence pack says what the library lacks. If the question "
        "is in scope, call plan_brief with its crop/country/focus. If it is not about food "
        "security or crop conditions at all, do not call the tool — reply with one sentence "
        "saying why you cannot brief on it.")

SECTIONS = ("## What history says", "## The current signal",
            "## What's missing and how to weigh it", "## Season timing caveat")

_SYNTH_SYSTEM = """You write food-security briefs for hub analysts and ministry advisors.

Non-negotiable rules:
- Use ONLY the numbered evidence provided. Every paragraph must carry at least one citation marker like [3]. Never use a citation number that is not in the evidence list.
- Never state a number, rating, or projection that does not appear in the evidence.
- Attribute every claim to its authority by name ("The GEOGLAM Crop Monitor rates...", "FEWS NET reported...", "ICPAC's outlook projects..."). Never say "we computed" or "our model predicts"; never present a projection as fact.
- Forecast/outlook statements may only cite evidence marked temporal=forecast, presented as dated projections of the issuing authority.
- The Season timing caveat MUST be based on the crop-calendar evidence entry (when present) and the asked-in month; if that entry is marked ADJUSTED, the caveat must say the calendar was adjusted by the requester. If there is NO crop-calendar entry — for example the question names no crop — still write the section, and say plainly that no crop calendar applies so timing cannot be pinned to a season. Never omit a required section.
- Evidence marked DRIVER SIGNAL ONLY (ENSO/ONI, IOD/DMI, the model plume, the IRI outlook) describes the ocean/atmosphere state. You may report its status, strength, timing and historical analogues, and you MUST attribute it. You may NOT use it on its own to claim any local rainfall, crop or food-security outcome — for a local claim, cite evidence that is itself about that place. If the only evidence for a local effect is a driver index, say the link is not established by this pack.
- Model counts in the plume are COUNTS, not probabilities. Never convert "N of M models" into a percentage or a chance.
- The "Known gaps" in the request MUST be reflected in the What's missing section.
- If the evidence cannot support an answer, write no sections; reply with one paragraph starting "DECLINE:" naming exactly what is missing.

Write EXACTLY these markdown sections and nothing else:
""" + "\n".join(SECTIONS) + """

Do not write a Sources section — the system appends it."""

_CITE_GROUP = re.compile(r"\[([\d\s,–\-]+)\]")   # [3], [1, 9], [1-3], [2016]

# Thousands separators: a comma OR space between a digit and a 3-digit group
# (chained) — "800 000"/"800,000" are one number. UN/African docs use spaces,
# so a comma-only strip false-flagged legitimate figures as unverified.
# Thousands separators, as they ACTUALLY occur in extracted source text — not just
# the ASCII two. UN and WMO house style uses U+00A0 NO-BREAK SPACE, and PDF
# extraction emits it and bare newlines; measured across stored citations: comma
# 1,083, space 298, NBSP 160, newline 21. Missing the last two made the gate read
# WMO's "more than 30\xa0000 livestock deaths and destroyed 170\xa0000\xa0ha of
# cropland" as the numbers 30, 000, 170, 000 — so a brief quoting it faithfully was
# scored as two fabrications and blocked. Roughly two thirds of the gate's blocks
# were correctly-sourced figures until this class was widened.
_SEP = "[ ,\u00a0\u202f\u2009]"
_THOUSANDS = re.compile(rf"(?<=\d){_SEP}(?=\d{{3}}(?:{_SEP}\d{{3}})*(?!\d))")


def _norm_nums(text: str) -> str:
    return _THOUSANDS.sub("", text)


def _number_set(text: str) -> set[str]:
    """Every number in `text`, keyed by VALUE rather than spelling.

    "0.40" and "0.4" are the same weight; "242.0" and "242" are the same
    kilometres. Comparing the strings made honest drafts look unsourced and, worse,
    buried the numbers that really were unsourced in that noise — which is how an
    invented figure rode through a gate that had actually noticed it.
    """
    out: set[str] = set()
    for tok in re.findall(r"\d+(?:\.\d+)?", _norm_nums(text)):
        if "." not in tok:
            # An integer keeps its exact digits. Float-normalising it turned a
            # 14-digit identifier into 2.02305e+13 and made two copies of the same
            # number stop matching — noise in exactly the signal that has to be
            # trustworthy before it can block anything.
            out.add(tok.lstrip("0") or "0")
            continue
        try:
            v = float(tok)
        except ValueError:                                  # pragma: no cover
            out.add(tok)
            continue
        out.add(f"{v:.10g}")
        out.add(str(int(v)) if v == int(v) else f"{v:.10g}")
        out.add(str(round(v)))              # a rounded quote of the same figure
    return out


def _cited_numbers(text: str) -> set[int]:
    """All citation numbers in bracket groups, ranges expanded; 4-digit values
    are years, not citations."""
    nums: set[int] = set()
    for group in _CITE_GROUP.findall(text):
        for part in re.split(r"[,\s]+", group.replace("–", "-")):
            if not part:
                continue
            a, dash, b = part.partition("-")
            if dash and a.isdigit() and b.isdigit() and int(a) <= int(b):
                nums.update(range(int(a), min(int(b), int(a) + 50) + 1))
            elif part.isdigit():
                nums.add(int(part))
    return {n for n in nums if n < 1000}


# --------------------------------------------------------------------------- G13: figures in words
# "3.2 million" was read as 3.2: under the >= 10 rule, so a fabricated magnitude was never
# checked. Magnitudes and numbers in words become their values before any number check.
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"hundred": 10 ** 2, "thousand": 10 ** 3, "lakh": 10 ** 5, "million": 10 ** 6,
           "crore": 10 ** 7, "billion": 10 ** 9, "trillion": 10 ** 12,
           "mn": 10 ** 6, "mln": 10 ** 6, "bn": 10 ** 9, "tn": 10 ** 12}
_LETTER = {"k": 10 ** 3, "m": 10 ** 6, "b": 10 ** 9}
# A magnitude that names its unit: its value in that unit's base, then the base unit.
_UNIT_MAG = {"Gt": (10 ** 9, "t"), "Mt": (10 ** 6, "t"), "kt": (10 ** 3, "t"),
             "Mha": (10 ** 6, "ha"), "kha": (10 ** 3, "ha")}
_FRACTIONS = {"half": 0.5, "halves": 0.5, "third": 1 / 3, "thirds": 1 / 3,
              "quarter": 0.25, "quarters": 0.25}
_HAS_FRACTION = re.compile("half|halves|third|quarter", re.I)
_NUM = r"\d+(?:\.\d+)?"
_SCALE = r"(?:hundred|thousand|lakh|million|crore|billion|trillion)s?"
_RANGE = rf"(?:({_NUM})\s*(?:-|–|to)\s*)?"
_MAG_WORD = re.compile(rf"(?<![\d.]){_RANGE}({_NUM})[\s\u00a0-]*((?:{_SCALE}|mln|mn|bn|tn)"
                       rf"(?:[\s-]+{_SCALE})*)\b", re.I)
# '7.4m', '45K', '2.8 Mt': a single letter only when glued to the number
_MAG_ABBR = re.compile(rf"(?<![\d.]){_RANGE}({_NUM})(?:\s?(Gt|Mt|kt|Mha|kha)|([mMbBkK]))(?![\w²³])")
_FRACTION = re.compile(r"\b(?:(\d+|one|two|three|four|five|six|seven|eight|nine)\s+and\s+)?"
                       r"(?:(an?|one|two|three)\s+)?(half|halves|thirds?|quarters?)\s+(?:of\s+)?"
                       rf"(?:an?\s+)?({_SCALE})\b", re.I)
_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
_W = "|".join(sorted([*_UNITS, *_TENS], key=len, reverse=True))
_NUMBER_WORD = (rf"(?:(?:{'|'.join(_TENS)})-(?:{'|'.join(w for w, v in _UNITS.items() if v < 10)})"
                rf"|{_W}|{_SCALE})")
_NUMBER_WORDS = rf"(?:an?\s+)?{_NUMBER_WORD}(?:\s+(?:and\s+)?{_NUMBER_WORD})*"
_NUMBER_RUN = re.compile(rf"(?<![A-Za-z-]){_NUMBER_WORDS}(?![A-Za-z-])", re.I)
# 'two point five million': read as 2 and 5 million, so it is refused, never guessed
_POINT = re.compile(rf"\b(?:{_W})(?:[\s-]+(?:{_W}|hundred|and))*\s+point(?:\s+(?:{_W}))+"
                    rf"[\s-]+{_SCALE}\b", re.I)


def _gate_codes() -> dict:
    """The code lists the gate reads (conf/gate_codes.yml): ISO 3166 and ISO 4217, as data."""
    import yaml
    with open(get_settings().gate_codes_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _plain(word: str) -> str:
    """A word without its accents: 'colón' -> 'colon'."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


_CODES = _gate_codes()
_ISO_4217 = {str(k): v for k, v in (_CODES.get("iso_4217") or {}).items()}
# after a figure, a currency's unit name in lower case ('kwacha', 'pesos', 'colones'; 'a 2019 RAND
# study' and 'Afghani families' are names) or its ISO code says money
_MONEY = (r"(?-i:" + "|".join(rf"{re.escape(u)}(?:e?s)?" for u in sorted(
    {w for u in _ISO_4217.values() if u for w in (str(u).lower(), _plain(str(u).lower()))},
    key=len, reverse=True)) + "|" + "|".join(sorted(_ISO_4217, key=len, reverse=True)) + ")")


@functools.cache
def _currency() -> re.Pattern:
    """A currency mark right before a figure: any Unicode currency symbol (category Sc, read
    from the Unicode database once), an ISO 4217 code or a written form ('KSh', 'Rs.')."""
    import sys
    import unicodedata
    sc = "".join(c for c in map(chr, range(sys.maxunicode + 1)) if unicodedata.category(c) == "Sc")
    marks = sorted({*_ISO_4217, *str(_CODES.get("written_forms") or "").split()}, key=len, reverse=True)
    return re.compile(rf"(?:[{re.escape(sc)}]|\b(?:{'|'.join(map(re.escape, marks))})\.?)\s?$")


# 'm' after a number is metres here, not millions
_LENGTH_AFTER = re.compile(
    r"\s*-?\s*(?:resolution|res\b|pixels?|px\b|cells?|grids?|gridded|elevation|altitude|deep|"
    r"depths?|high|height|tall|wide|width|long|length|above|below|asl\b|a\.s\.l|amsl|msl\b|"
    r"contours?|buffers?|radius|spacing|dem\b|srtm|thick|x\b|×|/|of\s+(?:water|depth|elevation|"
    r"sea)|sea[\s-]level|(?:storm\s+)?surges?|waves?|tides?|inundation|(?:flood|water)\s+depth|"
    r"rise\b)", re.I)
_LENGTH_BEFORE = re.compile(
    r"\b(?:depths?|deep|elevations?|altitudes?|heights?|resolution|surges?|waves?|rise|rose|risen|"
    r"inundat\w*|buffers?|radius|tides?|spacing|pixels?|cells?|grids?|dem|thick)\b\W+(?:\w+\W+){0,2}$",
    re.I)
# '7.4m people' is millions whatever else is near it
_COUNTED = re.compile(
    r"\s+(?:people|persons?|residents|inhabitants|households|families|farmers|children|"
    r"individuals|beneficiaries|refugees|migrants|workers|jobs|homes|houses|buildings|hectares|"
    r"ha|acres|tonnes|tons|t|head|cattle|livestock|animals|birds|trees|visitors|tourists|users|"
    rf"deaths|cases|{_MONEY}|in\s+(?:damage|losses|aid|funding|"
    r"assistance|revenue|exports|imports))\b", re.I)
# 'winds of 65 kt': knots, not kilotonnes
_WIND_BEFORE = re.compile(r"\b(?:winds?|gusts?|speeds?|sustained|knots?)\b[^.]{0,40}$", re.I)
# 'Figure 2b', 'Table 3B', 'Sentinel-2B': a label, not a billion
_LABEL_BEFORE = re.compile(
    r"\b(?:fig(?:ure)?s?|tables?|tab|sections?|annex(?:es)?|appendix|zones?|phases?|tiers?|"
    r"categor(?:y|ies)|cat|class(?:es)?|levels?|grades?|steps?|box(?:es)?|maps?|panels?|"
    r"chapters?|bands?|versions?|types?|models?|scenarios?|stages?|items?|parts?|rows?|"
    r"columns?|plots?|sites?|stations?|groups?|units?|blocks?|wards?|lots?|sectors?|ssp|rcp)"
    r"\.?\s*$", re.I)


def _base(w: str) -> str:
    """A scale word without its plural: 'millions' -> 'million'."""
    w = w.lower()
    return w[:-1] if w.endswith("s") and w[:-1] in _SCALES else w


def _scaled(num: str, scale: int, written: str, meta: dict, alt=None) -> str:
    """`num` x `scale` as digits, recorded in `meta` as (tolerance: half a unit of the last
    digit written, the figure as written, the number as written when it may be metres)."""
    from decimal import Decimal
    v = Decimal(num) * scale
    key = str(int(v)) if v == v.to_integral_value() else format(v.normalize(), "f")
    decimals = len(num.split(".")[1]) if "." in num else 0
    meta[key] = (0.5 * 10 ** (-decimals) * scale, written.strip(), alt)
    return key


def _word_num(w: str) -> int:
    w = w.lower()
    return 1 if w in ("a", "an") else _UNITS.get(w, 0) if not w.isdigit() else int(w)


def _word_kind(w: str) -> str | None:
    if w in _UNITS:
        return "unit"
    if w in _TENS:
        return "tens"
    if _base(w) in _SCALES and len(w) > 3:
        return "scale"
    parts = w.split("-")
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _UNITS and _UNITS[parts[1]] < 10:
        return "tens"                                  # twenty-five
    return None


def _word_runs(text: str):
    """(start, end, value, last scale word) for each run of number words, by their grammar:
    'twelve thousand five hundred', 'one hundred and twenty', 'a million'."""
    for run in _NUMBER_RUN.finditer(text):
        yield from _run_values(text, [(m.start() + run.start(), m.end() + run.start(),
                                       m.group().lower()) for m in _WORD.finditer(run.group())])


def _run_values(text: str, toks: list):
    i = 0
    while i < len(toks):
        s, e, w = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        lead_a = (w == "a" and nxt and _word_kind(nxt[2]) == "scale"
                  and not text[e:nxt[0]].strip())
        if not (_word_kind(w) in ("unit", "tens") or lead_a):
            i += 1                                     # a figure starts with a number
            continue
        total, cur, last, j, end, scale_word = 0, 0, None, i + (1 if lead_a else 0), e, None
        if lead_a:
            cur = 1
        while j < len(toks):
            js, je, jw = toks[j]
            if j > i and text[toks[j - 1][1]:js].strip():
                break                                  # punctuation ends a figure
            kind = _word_kind(jw)
            if jw == "and" and last == "scale" and j + 1 < len(toks) and \
                    _word_kind(toks[j + 1][2]) in ("unit", "tens"):
                j += 1
                continue
            if kind == "unit":
                v = _UNITS[jw]
                if last == "unit" or (last == "tens" and (v >= 10 or cur % 10)):
                    break
                cur += v
            elif kind == "tens":
                if last in ("unit", "tens"):
                    break
                cur += _TENS[jw.split("-")[0]] + (_UNITS[jw.split("-")[1]] if "-" in jw else 0)
            elif kind == "scale":
                sc = _SCALES[_base(jw)]
                if sc == 100:
                    if cur >= 100 or last == "scale" and not cur:
                        break
                    cur = (cur or 1) * 100
                else:
                    if last == "scale" and not cur:
                        break
                    total += (cur or 1) * sc
                    cur = 0
                scale_word = _base(jw)
            else:
                break
            last = "scale" if kind == "scale" else kind
            end = je
            j += 1
        if j > i + (1 if lead_a else 0):
            yield s, end, total + cur, (scale_word if last == "scale" else None)
        i = max(j, i + 1)


def _spelled(text: str, ambiguous: bool = True) -> tuple[str, dict]:
    """`text` with magnitudes ('3.2 million', '7.4m', 'USD 45k', '2.8 Mt', 'half a million') and
    numbers in words as values, and {value: (tolerance, as written, the number if '7.4m' may be
    metres)}. `ambiguous=False` (evidence) leaves a bare '7.4m' as written."""
    meta: dict = {}
    text = _norm_nums(str(text or ""))

    def fraction(m):
        count = _word_num(m.group(2)) if m.group(2) else 1
        whole = _word_num(m.group(1)) if m.group(1) else 0
        v = (whole + count * _FRACTIONS[m.group(3).lower()]) * _SCALES[_base(m.group(4))]
        key = str(int(round(v)))
        meta[key] = (0.1 * v, m.group().strip(), None)
        return key

    def word(m):
        scale = 1
        for w in re.findall(r"[A-Za-z]+", m.group(3)):
            scale *= _SCALES[_base(w)]
        hi = _scaled(m.group(2), scale, m.group(), meta)
        if m.group(1) and float(m.group(1)) <= float(m.group(2)):   # '95 to 1.3 thousand': 95
            return _scaled(m.group(1), scale, m.group(), meta) + "-" + hi
        return m.group()[:m.start(2) - m.start()] + hi

    def abbr(m):
        s, num, letter = m.string, m.group(2), m.group(4)
        before, after = s[max(0, m.start() - 48):m.start()], s[m.end():m.end() + 48]
        cur = bool(_currency().search(before))
        if (before[-1:].isalnum() or before[-1:] in ("_", "-", "/")) and not cur:
            return m.group()                           # an id: N09, Sentinel-2B, r3_2b
        alt, unit = None, ""
        if m.group(3):
            if m.group(3) == "kt" and _WIND_BEFORE.search(before):
                return m.group()
            scale, unit = _UNIT_MAG[m.group(3)]
        else:
            scale = _LETTER[letter.lower()]
            if letter in "bB" and not cur and _LABEL_BEFORE.search(before):
                return m.group()
            if letter in "mM" and not cur and (_LENGTH_AFTER.match(after)
                                               or _LENGTH_BEFORE.search(before)):
                return m.group()
            if letter == "m" and not cur and not _COUNTED.match(after):
                if not ambiguous:
                    return m.group()                   # evidence: as written
                alt = num                              # millions, or metres as written
        hi = _scaled(num, scale, m.group(), meta, alt)
        if m.group(1) and float(m.group(1)) <= float(num):
            hi = _scaled(m.group(1), scale, m.group(), meta, m.group(1) if alt else None) + "-" + hi
        else:
            hi = m.group()[:m.start(2) - m.start()] + hi
        return hi + (f" {unit}" if unit else "")

    if _HAS_FRACTION.search(text):
        text = _FRACTION.sub(fraction, text)
    text = _MAG_WORD.sub(word, text)
    text = _MAG_ABBR.sub(abbr, text)
    out, at = [], 0
    for s, e, value, scale_word in _word_runs(text):
        if value < 10:
            continue
        out.append(text[at:s] + str(value))
        meta[str(value)] = (0.5 * _SCALES[scale_word] if scale_word else None, text[s:e], None)
        at = e
    return "".join(out) + text[at:], meta


# --------------------------------------------------------------------------- G13: years, spans
_YEAR = re.compile(r"(?<![\d.,\u00a0\u202f\u2009])(?:1[89]\d\d|20\d\d|2100)(?![\d]|[.,\u00a0]\d)")
_NOT_A_YEAR = re.compile(r"\s*-?\s*(?:hundred|thousand|lakh|million|crore|billion|trillion|mln|mn|bn|"
                         r"tn|gt|mt|kt|mha|kha|ha|hectares?|km²|km2|sq\.?\s?km|tonnes?|t|kg|k|m|b|%|"
                         r"per\s?cent|percent)(?![A-Za-z0-9])", re.I)
_SEPARATED = re.compile(rf"(?<![\d.])\d{{1,3}}(?:{_SEP}\d{{3}})+(?!\d)")
# 'over the past twenty years', 'ten to fifteen years ago': a span of time written in words is
# not a figure; in digits ('over the past 20 years') it is checked like any other number
_SPAN_UNIT = r"[\s-]+(?:years?|months?|weeks?|days?|decades?|hours?)\b"
_SPAN = re.compile(
    rf"\b((?:past|next|last|previous|coming|recent|over|for|within|during|after|in)\s+(?:the\s+)?"
    rf"(?:(?:past|next|last|previous|coming)\s+)?)({_NUMBER_WORDS}(?:\s*(?:-|–|to|or)\s*"
    rf"{_NUMBER_WORDS})?)(?={_SPAN_UNIT})|\b{_NUMBER_WORDS}(?:\s*(?:-|–|to|or)\s*{_NUMBER_WORDS})?"
    rf"(?={_SPAN_UNIT}\s+ago\b)", re.I)
# a 4-digit figure before a counted noun is a count ('2000 people'), after a time word a year ('in
# 2020 farmers planted'): read the same way in a draft and in its evidence
_YEAR_CUE = re.compile(r"\b(?:in|since|during|until|till|january|february|march|april|may|june|"
                       r"july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|"
                       r"aug|sept?|oct|nov|dec)\.?\s+$", re.I)


def _year_forms(raw: str) -> tuple[set, set]:
    """(values written as bare years, values written as counts: '2,000', '2000 ha', '2000 people')."""
    years, counted = set(), {_norm_nums(m.group()) for m in _SEPARATED.finditer(raw)}
    for m in _YEAR.finditer(raw):
        count = _NOT_A_YEAR.match(raw, m.end()) or (_COUNTED.match(raw, m.end()) and not
                                                     _YEAR_CUE.search(raw, max(0, m.start() - 12),
                                                                      m.start()))
        (counted if count else years).add(m.group())
    return years, counted


def _claims(text: str) -> SimpleNamespace:
    """A draft's claims as the gate reads them: no citation markers, links or spans of time in
    words, figures in words or magnitudes as values; with each value's reading, the bare years, the
    values written as counts, the unit and word after each, and what it cannot read."""
    raw = _without_links(_CITE_GROUP.sub("", str(text or "")))
    unreadable = [m.group().strip() for m in _POINT.finditer(raw)]
    raw = _SPAN.sub(lambda m: m.group(1) or "", _POINT.sub(" ", raw))   # a span in words
    spelled, meta = _spelled(raw)
    years, counted = _year_forms(raw)
    counted |= set(meta)
    context: dict = {}
    for m in re.finditer(_NUM, spelled):
        before, after = spelled[max(0, m.start() - 12):m.start()], spelled[m.end():m.end() + 40]
        unit, word, kind = _unit(after), re.match(r"\s*([A-Za-z]+)", after), _kind(before, after)
        for k in _number_set(m.group()):
            units, nouns, kinds = context.setdefault(k, (set(), set(), set()))
            units.update([unit] if unit else [])
            nouns.update([word.group(1).lower()] if word else [])
            kinds.update(k for k in (kind, _UNIT_KIND.get(unit)) if k)
    return SimpleNamespace(text=spelled, meta=meta, years=years - counted, counted=counted,
                           context=context, unreadable=unreadable)


# --------------------------------------------------------------------------- G13: what vouches
_UNIT_WORDS = (("km2", r"km²|km2|km\^2|sq\.?\s?km|square\s+kilomet(?:re|er)s?"),
               ("ha", r"hectares?|ha"), ("t", r"tonnes?|tons?|t"), ("kg", r"kilograms?|kg"),
               ("km", r"kilomet(?:re|er)s?|km"), ("m", r"met(?:re|er)s?|m"),
               ("%", r"%|per\s?cent|percent"))
_UNIT = re.compile(r"\s*(?:" + "|".join(f"(?P<u{i}>{p})" for i, (_, p) in enumerate(_UNIT_WORDS))
                   + r")(?![A-Za-z0-9])", re.I)
# (draft unit, pack unit) -> the pack figure's factor: 408.4 km² is 40,840 ha
_CONVERT = {("ha", "km2"): 100, ("km2", "ha"): 0.01, ("kg", "t"): 1000, ("t", "kg"): 0.001,
            ("m", "km"): 1000, ("km", "m"): 0.001}
_DOI = re.compile(r"\b(?:doi:\s*)?10\.\d{4,9}/\S+", re.I)
_CODE_AFTER = re.compile(r"\s*(?:=|\(\s*[A-Z]|×|[xX]\s*\d)")
_CODE_BEFORE = re.compile(r"\b(?:class(?:es)?|codes?|min|max|range|band|ids?|epsg|tile|version|"
                          r"isbn|issn)\s*[:=#]?\s*$", re.I)
_ID_BEFORE = re.compile(r"\b[A-Z][A-Za-z]+[ \t]+$")             # 'Zenodo 6894273': a record id
_COUNT_AFTER = re.compile(r"\s*(pixel|px|cell|patch|tile)", re.I)
_COUNT_BEFORE = re.compile(r"\b(pixel|cell|patch)(?:e?s)?(?:\s+count)?\s*[:=]?\s*$", re.I)
_CELL_BEFORE = re.compile(r"(?:\b[A-Z][A-Za-z]*|\d)[ \t]+$")
_CELL_AFTER = re.compile(r"[ \t]+(?:[A-Z][a-z]|\d)")
_RUN_BEFORE = re.compile(r"(?:^|[ \t])\d[\d.]*[ \t]+$")       # a number beside a number: a table row
_RUN_AFTER = re.compile(r"[ \t]+\d")
_ROW_UNIT = re.compile(r"\(([^()\n]{1,24})\)[^\n\d]{0,12}$")   # 'Production (Tons) ' labels its cells
_DOT_THOUSANDS = re.compile(r"[1-9]\d{0,2}(?:\.\d{3})+")        # '263.261 ha' in a Spanish source


# what a figure counts, read from the word after it (or a currency before it): a rounding never
# vouches across kinds, '3 million hectares' for 3,215,433 people. Money is also read from what
# follows it ('in damage'), so a currency the code lists lack still says money.
_KIND_WORDS = (("area", r"km²|km2|sq\.?\s?km|square\s+kilomet\w*|hectares?|ha|acres?"),
               ("mass", r"tonnes?|tons?|t|kg|kilograms?"),
               ("people", r"people|persons?|residents|inhabitants|individuals|children|farmers|"
                          r"refugees|migrants|workers|beneficiaries"),
               ("households", r"households?|families|homes|houses|dwellings"),
               ("money", rf"{_MONEY}|in\s+(?:damage|losses|aid|funding|assistance|revenue|exports|"
                         r"imports)"),
               ("percent", r"%|per\s?cent|percent"))
_UNIT_KIND = {"km2": "area", "ha": "area", "t": "mass", "kg": "mass", "%": "percent"}
_KIND = re.compile(r"\s*(?:" + "|".join(f"(?P<k{i}>{p})" for i, (_, p) in enumerate(_KIND_WORDS))
                   + r")(?![A-Za-z0-9])", re.I)


def _kind(before: str, after: str) -> str | None:
    """What the figure between `before` and `after` counts: area, mass, people, money..."""
    if _currency().search(before[-12:]):
        return "money"
    m = _KIND.match(after)
    return next((k for i, (k, _) in enumerate(_KIND_WORDS) if m.group(f"k{i}")), None) if m else None


def _unit(after: str) -> str | None:
    """The unit named right after a figure, canonical ('km2', 'ha', 't', ...), or None."""
    m = _UNIT.match(after)
    return next((u for i, (u, _) in enumerate(_UNIT_WORDS) if m.group(f"u{i}")), None) if m else None


def _sig(tok: str) -> int:
    """Significant digits as written: 12034 -> 5, 1200 -> 2, 408.4 -> 4."""
    d = tok.replace(".", "").lstrip("0")
    return len(d if "." in tok else d.rstrip("0"))


def _row_unit(t: str, start: int, breaks: list, labels: dict) -> str | None:
    """The unit a table row's label names for its cells ('Production (Tons) 36873 34512'): each
    line's label read once (`breaks`: the offsets of t's newlines)."""
    i = bisect.bisect_left(breaks, start)
    at = breaks[i - 1] + 1 if i else 0
    if at not in labels:
        line = t[at:breaks[i] if i < len(breaks) else len(t)]
        m = _ROW_UNIT.search(re.split(r"[ \t]\d", line, maxsplit=1)[0] + " ")
        labels[at] = _unit(" " + m.group(1)) if m else None
    return labels[at]


def _evidence(text) -> SimpleNamespace:
    """One piece of EVIDENCE read once: its figures (as written, magnitudes read), the rows that
    may vouch by rounding, the values that are codes or ids, what each count is of, its years."""
    raw = str(text or "")
    blank = _DOI.sub(" ", _without_links(raw))
    spelled, meta = _spelled(blank, ambiguous=False)
    rows, codes, counts = _voucher_rows(blank, spelled, meta)
    years, counted = _year_forms(raw)
    return SimpleNamespace(figures=_number_set(raw) | _number_set(spelled), rows=rows, codes=codes,
                           counts=counts, years=years, counted=counted | set(meta))


def _merged(reads) -> SimpleNamespace:
    """Several pieces of evidence as one: figures pooled; a value that is a code anywhere never
    vouches; a value stated only as a year never vouches for a count."""
    reads = list(reads)
    codes, counts = set().union(*(r.codes for r in reads)), {}
    for r in reads:
        for v, of in r.counts.items():
            counts.setdefault(v, of)
    return SimpleNamespace(
        figures=set().union(*(r.figures for r in reads)),
        vouchers=[(*row[:5], counts.get(row[0]), row[5], row[6])
                  for r in reads for row in r.rows if row[0] not in codes],
        years=set().union(*(r.years for r in reads)) - set().union(*(r.counted for r in reads)))


def _figures(text: str) -> set[str]:
    """Every number EVIDENCE states, as written and with its magnitudes read."""
    return _evidence(text).figures


def _vouchers(text: str) -> list[tuple]:
    """EVIDENCE figures that may vouch by rounding or conversion: (value, as written, unit, sig.
    digits, written in digits, count of, unit required, kind)."""
    return _merged([_evidence(text)]).vouchers


def _voucher_rows(text: str, spelled: str, meta: dict) -> tuple[list, set, dict]:
    """(rows, codes, counts) of `text` (links and DOIs blanked) and its magnitudes read: never a
    code, an id or a link's digits; a pixel count only for pixels; a cell only for its row's unit."""
    rows, out_, count_of = [], set(), {}
    for written, t in ((True, _norm_nums(text)), (False, spelled)):
        breaks, labels = [m.start() for m in re.finditer("\n", t)], {}
        for m in re.finditer(r"(?<![\d.])" + _NUM, t):
            tok = m.group()
            if not written and tok not in meta:
                continue                               # the spelled pass adds magnitudes only
            before, after = t[max(0, m.start() - 48):m.start()], t[m.end():m.end() + 32]
            if (before[-1:].isalpha() or before[-1:] == "_") and not _currency().search(before):
                continue                               # glued to an id: v200, N09, r3_2021
            unit = _unit(after)
            if after[:1].isalpha() and unit is None:
                continue                               # 2021v, 09W
            v, kind = float(tok), _kind(before, after)
            if _CODE_AFTER.match(after) or _CODE_BEFORE.search(before) or (
                    len(tok) >= 5 and tok.isdigit() and not (unit or kind) and _ID_BEFORE.search(before)):
                out_.add(v)                            # a code, a dimension or an id
                continue
            cell = unit is None and (bool(_RUN_BEFORE.search(before) or _RUN_AFTER.match(after)) or bool(
                _CELL_BEFORE.search(before) and _CELL_AFTER.match(after)))
            if cell:
                unit = _row_unit(t, m.start(), breaks, labels)
                if unit is None:
                    out_.add(v)                        # a table cell no label gives a unit
                    continue
            c = _COUNT_AFTER.match(after) or _COUNT_BEFORE.search(before)
            if c:
                count_of.setdefault(v, c.group(1).lower())
            kind = kind or _UNIT_KIND.get(unit)        # a row label's tonnes are a mass
            rows.append((v, tok, unit, _sig(tok), written, cell, kind))
            if written and unit and _DOT_THOUSANDS.fullmatch(tok):   # only for the same unit
                rows.append((float(tok.replace(".", "")), f"{tok} (with '.' as thousands)", unit,
                             _sig(tok.replace(".", "")), written, True, kind))
    return rows, out_, count_of


# --------------------------------------------------------------------------- G13: links
# A link the pack does not carry is the web in a governed answer. A link ends at whitespace, ASCII
# delimiters, Unicode dashes, quotes, guillemets, ellipsis or full-width punctuation.
_STOP = ("\\s<>\"'`{}|\\\\^\u2013\u2014\u2018\u2019\u201c\u201d\u00ab\u00bb\u2039\u203a\u2026"
         "\u3000-\u303f\uff00-\uffef")
_URL = re.compile(rf"(?:\b[a-z][a-z0-9+.\-]*://|\bmailto:|(?<![\w:/])//(?=[a-z0-9-]+\.[a-z])|"
                  rf"\bwww\.)[^{_STOP}]+", re.I)
_DOMAIN = re.compile(rf"(?<![\w@.\-/])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+)([a-z]{{2,6}})"
                     rf"(?![\w-])((?:/[^{_STOP}]*)?)", re.I)
# generic domains, and country codes used as generic ones (co, io, ai): a bare host is a link
_GENERIC = {"com", "org", "net", "gov", "edu", "int", "mil", "eu", "africa", "asia", "co",
            "info", "biz", "io", "ai", "app", "dev", "news", "online", "site", "xyz"}
_RISKY = {"info", "biz", "io", "ai", "app", "dev", "news", "online", "site", "xyz"}
_SLD = {"gov", "go", "co", "ac", "or", "org", "com", "net", "edu", "ne", "nic", "mil"}
# country domains: every ISO 3166 alpha-2 code (conf/gate_codes.yml) but the English words ('data.in')
_CC = set(str(_CODES.get("iso_3166_alpha2") or "").lower().split()) - set(
    str(_CODES.get("english_word_codes") or "").lower().split())
_SHORT = {"bit.ly", "t.co", "youtu.be", "goo.gl", "ow.ly", "buff.ly", "rb.gy", "is.gd",
          "lnkd.in", "tiny.cc", "cutt.ly", "shorturl.at"}


def _trim(u: str) -> str:
    """A link without trailing punctuation or a closing bracket it does not open."""
    pair = {")": "(", "]": "[", "}": "{"}
    while u:
        if u[-1] in ".,;:!?*_'\"":
            u = u[:-1]
        elif u[-1] in pair and u.count(pair[u[-1]]) < u.count(u[-1]):
            u = u[:-1]
        else:
            break
    return u


def _is_domain(m) -> bool:
    """A bare host that is a web address, not code, a file or a typo: 'QGIS.app', '2020.net',
    'feature.id', 'era5.nc', 'rainfall.Net change' are not; 'FEWS.NET', 'go.ke' hosts, 'ipcc.ch/x',
    'data.ipcc.ch' and 'bit.ly/x' are (a country domain needs a path or a subdomain)."""
    labels, tld, path = m.group(1), m.group(2), m.group(3)
    host = labels + tld
    if not host.isupper() and tld != tld.lower():
        return False                                   # 'rainfall.Net change': a missing space
    parts = host.lower().split(".")
    if any(p.isdigit() for p in parts):
        return False                                   # '2020.net', 'chirps-v2.0.2020.nc'
    if parts[-1] in _GENERIC:
        return not (parts[-1] in _RISKY and len(parts) == 2 and not path)
    if len(parts) >= 3 and parts[-2] in _SLD and len(parts[-1]) == 2:
        return True                                    # gov.uk, co.ke
    if ".".join(parts[-2:]) in _SHORT:
        return bool(path)
    return (parts[-1] in _CC and len(parts[-2]) >= 3 and (bool(path) or len(parts) >= 3)
            and (host == host.lower() or host.isupper() or bool(path)))


def _deobfuscate(text: str) -> str:
    return re.sub(r"\[\.\]|\(\.\)|\[dot\]|\(dot\)", ".", str(text or ""), flags=re.I)


def _links(text: str) -> list[tuple[int, int, str]]:
    """(start, end, as written) for each URL and bare web domain in `text`; a domain inside a
    URL is the URL's (both come in order, so one pass)."""
    urls = []
    for m in _URL.finditer(text):
        u = _trim(m.group())
        urls.append((m.start(), m.start() + len(u), u))
    out, i = list(urls), 0
    for m in _DOMAIN.finditer(text):
        while i < len(urls) and urls[i][1] <= m.start():
            i += 1
        if (i < len(urls) and urls[i][0] <= m.start()) or not _is_domain(m):
            continue
        u = _trim(m.group())
        out.append((m.start(), m.start() + len(u), u))
    return sorted(out)


def _without_links(text: str) -> str:
    """`text` with its URLs and domains blanked: a link's digits are not figures (an unserved
    link blocks on its own)."""
    text, out, at = _deobfuscate(text), [], 0
    for s, e, _ in _links(text):
        if s >= at:
            out.append(text[at:s] + " ")
            at = e
    return "".join(out) + text[at:]


def _link_key(s: str) -> tuple[str, str]:
    """A link compared as host + path (no scheme, www., #fragment or trailing punctuation):
    with its query string, and without it."""
    s = _trim(str(s or "").strip()).lower()
    s = re.sub(r"^(?:[a-z][a-z0-9+.\-]*://|mailto:|//)", "", s)
    s = (s[4:] if s.startswith("www.") else s).split("#", 1)[0].rstrip("/")
    return s, s.split("?", 1)[0].rstrip("/")


def _platform_bases() -> list[str]:
    """The platform's own addresses: the receipt resolver and embed bases (ui tokens) and its
    public URL. A link under one of them is the platform's, never the web's."""
    bases = [get_settings().grp_public_url]
    try:
        from ..mcp import ui
        product = ui.tokens()["product"]
        bases += [product["resolver"]["base"], product["embed_base"]["url"]]
    except Exception:
        pass
    return [k for k in (_link_key(b)[1] for b in bases if b) if k]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _links_not_served(draft: str, *evidence) -> list[str]:
    """Each URL or bare domain in `draft` that appears nowhere in the pack's own evidence
    (every string of its citations, figures and gaps), matched from a host boundary (a parent
    domain is not its subdomain), and is not the platform's own."""
    from urllib.parse import urlsplit
    from ..mcp import shape
    own = urlsplit(shape.PLACES).scheme + ":"          # the server's resources: servirplatform://...
    blob = " ".join(_strings(list(evidence))).lower()
    blob = re.sub(r"(?<![\w.-])www\.", "", re.sub(r"[a-z][a-z0-9+.\-]*://", " ", blob))
    bases = _platform_bases()
    out = []
    for _, _, written in _links(_deobfuscate(draft)):
        full, base = _link_key(written)
        if not base or written.lower().startswith(own) or any(
                base == b or base.startswith(b + "/") for b in bases):
            continue
        if any(re.search(rf"(?<![a-z0-9.\-]){re.escape(k)}(?![a-z0-9])", blob) for k in (full, base)):
            continue
        if written not in out:
            out.append(written)
    return out


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0} if u else {"in": 0, "out": 0}


def _parse(client, model, question, trace, usage):
    resp = client.chat.completions.create(
        model=model, max_tokens=300, tools=_PARSE_TOOLS,
        messages=[{"role": "system", "content": _parse_system()},
                  {"role": "user", "content": question}])
    usage.append(_usage(resp))
    msg = resp.choices[0].message
    if not msg.tool_calls:
        trace.append("parse -> out of scope (no tool call)")
        return None, msg.content or "This question is outside the food-security brief's scope."
    try:
        args = json.loads(msg.tool_calls[0].function.arguments)
        if not isinstance(args, dict):
            raise ValueError("tool arguments are not an object")
    except (ValueError, TypeError) as exc:      # truncated/malformed model output
        trace.append(f"parse -> malformed tool arguments ({exc})")
        return None, ("The question could not be parsed reliably (malformed model "
                      "response) — please try rephrasing it.")
    parsed = {"crop": (args.get("crop") or "").strip(),
              "country": (args.get("country") or "").strip(),
              "focus": (args.get("focus") or question).strip()}
    trace.append(f"parse -> {parsed}  [the model extracts the target; it fetches nothing]")
    return parsed, None


def _conditions_citation(crop, country, trace):
    """The GEOGLAM feed as one evidence entry, or an honest gap line."""
    try:
        res = cropmonitor.conditions(crop=crop or None, place=country or None)
    except cropmonitor.CropMonitorError as exc:
        trace.append(f"conditions -> unavailable: {exc}")
        return None, f"current Crop Monitor conditions unavailable ({exc})"
    if not res["records"]:
        trace.append("conditions -> no assessment rows")
        return None, res.get("note", "no current Crop Monitor assessment for the target")
    regions = "; ".join(f"{r['region'] or r['country']}: {r['conditions'] or 'out of season'}"
                        for r in res["records"][:12])
    if res["count"] > 12:                       # truncation must be visible evidence
        regions += f" (first 12 of {res['count']} rows)"
    drivers = sorted({r["drivers"] for r in res["records"] if r["drivers"]})
    text = (f"GEOGLAM Crop Monitor synthesis as of {res['as_of']} for "
            f"{(country or 'all monitored countries')} {(crop or 'all crops')}: "
            f"summary {res['summary']}; regions: {regions}; "
            f"drivers: {', '.join(drivers) if drivers else 'none stated'}.")
    trace.append(f"conditions -> {res['count']} rows as of {res['as_of']} "
                 f"(receipt: {res['query']['where']})")
    # Staleness must travel with the evidence: served-from-cache data presented as
    # live is exactly the dishonesty the wrapper exists to prevent (rule 1).
    stale = res.get("stale_data")
    if stale:
        text += (f" NOTE: this feed was UNAVAILABLE and these values are served from "
                 f"the last-good cache (last successful fetch {stale.get('last_good_fetch')}) "
                 f"— treat as possibly out of date.")
        trace.append(f"conditions -> STALE (last good {stale.get('last_good_fetch')})")
    return {"kind": "conditions", "retrieval": "pulled-at-pack-time",
            "source": "GEOGLAM Crop Monitor (CMET)",
            "title": f"{country or 'Global'} {crop or 'crop'} conditions".strip(),
            "pub_date": res["as_of"], "validation": "multi-agency-consensus",
            "url": res["query"]["url"], "query": res["query"]["where"],
            "stale_data": stale, "text": text}, None


def _driver_citations(trace):
    """The Pillar-1 climate DRIVER signal (ENSO/IOD) as evidence entries.

    Registry-driven, not hardcoded: any FEEDS row tagged `brief_role: driver` lands
    here, so a sixth driver feed is a config row. Goes through `feeds.query` rather
    than the fetchers so the passport, declines and staleness are the same ones the
    tool surface reports — one contract, not two.

    Each entry carries the use-case doc's hard constraint in its own text: Phase 1
    describes the SIGNAL and must not infer local agricultural impact. Putting that
    on the evidence itself (not only in the system prompt) means it survives into
    the pack and the receipt, where a reader can check it.
    """
    from ..mcp import feeds, registry     # local: mirrors registry's own cycle guard

    def render(records, budget=1800):
        """Feed rows -> evidence prose, shape-driven rather than feed-name-driven.

        A summary line alone is not evidence: `enso_outlook`'s whole purpose is the
        official probability wording, and a citation that said "11 narrative
        sections" carried none of it — the model could not quote what it could not
        see. Budgeted, because an unbounded narrative would crowd the pack.
        """
        parts = []
        for r in records or []:
            if isinstance(r, dict) and "paragraphs" in r:          # narrative sections
                parts.append(f"{r.get('heading')}: " + " ".join(r["paragraphs"]))
            elif isinstance(r, dict) and "forecast" in r:          # per-model plume rows
                continue                                           # summarised, not quoted
            elif isinstance(r, dict):
                parts.append("; ".join(f"{k}={v}" for k, v in r.items()))
            else:
                parts.append(str(r))
            if sum(len(p) for p in parts) > budget:
                parts.append(f"[... {len(records) - len(parts)} further rows not shown]")
                break
        return " | ".join(parts)[:budget]

    out, gaps = [], []
    specs = {n: s for n, s in registry.FEEDS.items()
             if s.get("status") == "available" and s.get("brief_role") == "driver"}
    try:          # a contributor's own STAGED driver previews AS a driver, not as a table
        from ..contrib import staging
        specs.update({n: s for n, s in staging.visible_staged_feeds_for_pack("food-security").items()
                      if s.get("brief_role") == "driver" and n not in specs})
    except Exception:
        pass
    rows = sorted(specs.items())
    for name, spec in rows:
        res = feeds.query(name)
        if res.get("status") != "ok":
            trace.append(f"driver[{name}] -> {res.get('status')}: {res.get('note')}")
            gaps.append(f"{name}: {res.get('note') or 'no data'}")
            continue
        p = res.get("passport") or {}
        body = render(res.get("records"))
        basis = (f" [SST basis: {p['sst_basis']}]" if p.get("sst_basis") else "")
        text = (f"{spec.get('source')} {name} as of {res.get('as_of')}{basis}: "
                f"{res.get('summary')}."
                + (f" {body}" if body else "") +
                " DRIVER SIGNAL ONLY — this describes the ocean/atmosphere state and says "
                "nothing about rainfall, crops or food security at any particular place.")
        if res.get("note"):
            text += f" NOTE: {res['note']}"
        stale = p.get("stale_data") or {}
        if stale.get("served_stale"):
            text += (f" NOTE: served from last-good cache, not a live read "
                     f"({stale.get('reason')}) — treat as possibly out of date.")
            trace.append(f"driver[{name}] -> STALE ({stale.get('reason')})")
        else:
            trace.append(f"driver[{name}] -> {res.get('count')} rows as of {res.get('as_of')}")
        # Keep the STRUCTURED rows alongside the flattened text. `render` exists to
        # give the LLM prose it can cite, and it truncates at a character budget —
        # so until now the actual series was thrown away the moment it was
        # stringified, and nothing downstream could plot a number. The text stays
        # exactly as it was; this is an addition, not a change to what is cited.
        series = _series(name, res.get("records"))
        out.append({"kind": "index", "brief_role": "driver", "retrieval": "pulled-at-pack-time",
                    **({"review_status": spec["review"]} if spec.get("review") else {}),
                    **({"staged_by": spec["staged_by"], "contribution_id": spec.get("contribution_id")}
                       if spec.get("staged_by") else {}),
                    **({"series": series} if series else {}),
                    "source": spec.get("source"), "title": spec.get("title") or spec.get("description", name),
                    "pub_date": res.get("as_of"), "validation": spec.get("validation"),
                    "residency": spec.get("residency"), "url": p.get("url"),
                    "query": p.get("query"), "stale_data": stale or None,
                    "temporal": "forecast" if "forecast" in name or "plume" in name
                                or "outlook" in name else "observation",
                    "text": text})
    return out, gaps


def _series(name: str, records) -> dict | None:
    """A plottable view of a monthly index, or None if these rows are not a series.

    Deliberately narrow: {season|year|month, value, classification} is the shape the
    climate-index adapter returns, and anything else (verbatim narrative sections,
    the derived event catalogue) has no y-axis and is left alone. Capped, because
    this rides in a tool result that a host renders.
    """
    pts = []
    for r in records or []:
        if not isinstance(r, dict) or r.get("value") is None:
            continue
        label = (f"{r['season']} {r['year']}" if r.get("season")
                 else f"{r.get('year')}-{r.get('month'):02d}" if r.get("month")
                 else str(r.get("year") or ""))
        pts.append({"t": label, "v": r["value"], "c": r.get("classification")})
    if len(pts) < 2:
        return None
    # Threshold bands are INDEX knowledge, carried on the series so the renderer
    # never invents them: the panel used to hardcode +/-0.5 "El Nino/La Nina" on
    # EVERY chart — approximately right for ONI, wrong for DMI (+/-0.4 is the IOD
    # convention), and nonsense for any other pack's series.
    bands = {"oni": [{"v": 0.5, "label": "+0.5 El Nino"},
                     {"v": -0.5, "label": "-0.5 La Nina"}],
             "enso_oni": [{"v": 0.5, "label": "+0.5 El Nino"},
                          {"v": -0.5, "label": "-0.5 La Nina"}],
             "dmi": [{"v": 0.4, "label": "+0.4 positive IOD"},
                     {"v": -0.4, "label": "-0.4 negative IOD"}],
             "iod_dmi": [{"v": 0.4, "label": "+0.4 positive IOD"},
                         {"v": -0.4, "label": "-0.4 negative IOD"}]}
    out = {"id": name, "points": pts[-24:], "unit": "degrees C anomaly"}
    if name in bands:
        out["bands"] = bands[name]
    return out


_CONTEXT_ONLY = ("calendar", "index", "gaps")


def _has_evidence(citations: list, country: str | None = None) -> bool:
    """Can these citations carry a brief? Context alone cannot: a calendar, the
    global driver indices, the gaps entry. A source BOUND to this pack can when
    it is ABOUT the place asked — a hub's Malawi rainfall table declaring
    countries [Malawi] is evidence about Malawi — but it shares the `index` kind
    with the drivers and used to be barred with them, so a fresh library declined
    "No evidence available" over a country-matching table. A pack source that
    declares no countries is global as far as the platform knows, and a global
    table can no more carry a brief about one country than an ocean index can."""
    from ..graph.geo import vectors as _vectors
    for c in citations:
        if c.get("kind") not in _CONTEXT_ONLY:
            return True
        if (c.get("brief_role") == "pack-source" and country and c.get("countries")
                and _vectors.country_matches(c["countries"], country)):
            return True
    return False


def _pack_bound_sources(trace, gaps, country=None):
    """Feeds and tables bound to this pack that are NOT seasonal drivers.

    The brief read only `brief_role: driver` rows, so a contributed table bound to
    food-security — the DEFAULT pack for a contribution — staged, answered
    feeds_query, and could never be cited in an answer. The risk pack already
    reads its own pack-bound sources; this is the same path, which food-security
    simply never had.
    """
    from ..contrib import staging
    from ..mcp import feeds, registry
    rows = {n: sp for n, sp in registry.FEEDS.items()
            if sp.get("status") == "available"
            and sp.get("pack", "food-security") == "food-security"
            and sp.get("brief_role") != "driver"}
    try:
        rows.update({n: sp for n, sp
                     in staging.visible_staged_feeds_for_pack("food-security").items()
                     if n not in rows})
    except Exception:
        pass
    out, elsewhere = [], {}
    for ds in sorted(rows):
        # Is this source about the country asked about? A Battambang rainfall table was
        # cited into a KENYA maize brief. Countries and a box, the rule every pack
        # applies (mcp.feeds.scope_miss); this pack answers for a whole country, so a
        # box is placed through the countries the source also declares, never guessed.
        miss = feeds.scope_miss(rows[ds], [country] if country else [], None)
        if miss:
            elsewhere[ds] = miss
            continue
        res = feeds.query(ds, {"limit": 4})
        if res.get("status") != "ok":
            gaps.append(f"source {ds} bound to this pack did not answer: "
                        f"{res.get('note', 'no reason given')}")
            continue
        spec, records = rows[ds], (res.get("records") or [])
        from ..risk.synthesis import _is_time_series
        per_row_time, _af = _is_time_series(spec, records)
        if per_row_time and records:
            last = records[-1]
            body = (f"{spec.get('title', ds)} ({ds}), latest reading as of "
                    f"{res.get('as_of')}: "
                    + ", ".join(f"{k} {v}" for k, v in last.items() if v is not None))
        else:
            shown = records[:4]
            body = (f"{spec.get('title', ds)} ({ds}) is a LOOKUP TABLE, not a time "
                    f"series — no row is 'the latest'. {len(records)} row(s); showing "
                    f"{len(shown)}: "
                    + ("; ".join(", ".join(f"{k} {v}" for k, v in r.items()
                                           if v is not None) for r in shown)
                       or "no values returned"))
        pp = res.get("passport") or {}
        out.append({
            "kind": "index", "retrieval": "pulled-at-pack-time",
            # A standing seasonal DRIVER and a table bound to this pack are
            # different kinds of evidence, and a reader should be able to tell
            # which one they are looking at.
            "brief_role": "pack-source",
            "countries": spec.get("countries"),
            "source": pp.get("source") or spec.get("source"),
            "title": spec.get("title", ds),
            "validation": pp.get("validation") or spec.get("validation", "unvalidated"),
            "url": pp.get("url"),
            **({"staged_by": spec["staged_by"],
                "contribution_id": spec.get("contribution_id")}
               if spec.get("staged_by") else {}),
            **({"review_status": spec["review"]} if spec.get("review") else {}),
            "text": body + ". " + (res.get("summary") or "")
                    + (f" Review: {spec['review']}." if spec.get("review") else "")
                    + (f" Contributor guidance: {spec['usage_notes']}"
                       if spec.get("usage_notes") else ""),
        })
        trace.append(f"pack_source[{ds}] {res.get('count')} rows")
    if elsewhere:
        trace.append(f"pack_sources[skipped:{','.join(sorted(elsewhere))}] scoped elsewhere")
        gaps.append(feeds.scope_gap(elsewhere))
    return out


def gather_evidence(parsed, trace, calendar_override=None, calendar_target=(None, None)):
    """Deterministic evidence assembly: two retrieval slices + the conditions feed
    + the Pillar-1 climate drivers + the crop calendar. Returns (citations, gaps,
    stats), citations numbered. A calendar override made for one country/crop is
    never silently applied to another — a mismatch drops it and declares the drop
    as a gap."""
    corpus = Corpus(CORPUS)
    crop, country, focus = parsed["crop"], parsed["country"], parsed["focus"]
    gaps = []
    # BOTH slices have to be about what was ASKED. The retrospective query used to
    # be a fixed sentence — "impact of past El Nino events on <crop> production and
    # food security in <country>" — with no trace of the question in it, so half of
    # every brief's document evidence was retrieved blind. Measured: for Kenya it
    # returned the SAME three documents whether the analyst asked about post-harvest
    # storage losses or about early signs of crop failure. A platform whose promise
    # is that an answer traces to its evidence cannot retrieve that evidence against
    # a question nobody asked.
    q_now = f"El Nino seasonal rainfall forecast outlook {country} {crop} {focus}".strip()
    q_past = (f"{focus} — impact of past events on {crop or 'crop'} production and "
              f"food security in {country}").strip()
    # A document about another country is passed over and the next one down takes its
    # place, so k stays filled with this country's (or undeclared) documents.
    where, skipped = ([country] if country else []), {}
    forecast_hits = corpus.search(q_now, k=5, temporal="forecast", where=where, skipped=skipped)
    retro_hits = corpus.search(q_past, k=5, temporal="retrospective", where=where, skipped=skipped)
    trace.append(f"retrieve[forecast] {q_now!r} -> {len(forecast_hits)} hits")
    trace.append(f"retrieve[retrospective] {q_past!r} -> {len(retro_hits)} hits")
    if not retro_hits:
        # A narrow question can put every retrospective document below the floor.
        # Losing the whole historical slice is worse than answering the general
        # question, so fall back — and SAY which query produced the evidence, or
        # the citation list quietly stops matching the question it is filed under.
        # The corpus was assembled around El Nino, so this is the question it was
        # built to answer — the right thing to fall back TO, and it was the only
        # query the platform ever ran until now.
        q_general = (f"impact of past El Nino events on {crop or 'crop'} production "
                     f"and food security in {country}").strip()
        retro_hits = corpus.search(q_general, k=5, temporal="retrospective", where=where,
                                   skipped=skipped)
        trace.append(f"retrieve[retrospective:fallback] {q_general!r} -> "
                     f"{len(retro_hits)} hits")
        if retro_hits:
            gaps.append(
                f"no retrospective document matched the specific question ({focus!r}); "
                "the historical evidence below answers the general question about "
                f"{crop or 'this crop'} in {country} instead")
    # THE CURRENT SEASON, which neither query above could reach. `temporal` marks a
    # document observational-vs-forecast; `q_past` reads that same tag as meaning
    # HISTORICAL. A country brief published during this season is observational but
    # not historical, so the only query that could see it asked about past events.
    # Measured on the flagship question: the FAO GIEWS Kenya brief (04-May-2026)
    # scores 0.716 and 0.679 on a current-season query for the two chunks describing
    # THIS season's rains, and 0.611 for its price table — and the price table was
    # the one that reached the pack, so the brief reported no current-season
    # evidence while the library held it. Additive: this can only ADD observational
    # evidence already tagged the same way, never displace what was retrieved.
    q_current = (f"{country} {crop or 'crop'} current season to date — rainfall "
                 f"received, crop and rangeland condition, harvest prospects "
                 f"{focus}").strip()
    current_hits = corpus.search(q_current, k=3, temporal="retrospective", where=where,
                                 skipped=skipped)
    already = {h["id"] for h in retro_hits}
    added = [h for h in current_hits if h["id"] not in already]
    retro_hits = retro_hits + added
    trace.append(f"retrieve[current-season] {q_current!r} -> {len(current_hits)} hits, "
                 f"{len(added)} not already retrieved")
    if not current_hits:
        gaps.append("no document in the library describes the CURRENT season's "
                    "observed conditions for this country and crop")

    if not forecast_hits:
        gaps.append("no forecast/outlook document in the library matched the question")
    if not retro_hits:
        gaps.append("no analog-year/retrospective document in the library matched the question")

    # WHICH COUNTRY ARE THESE DOCUMENTS ACTUALLY ABOUT? Retrieval is by meaning,
    # so a question about a country the library does not cover comes back full of
    # confident, well-scored documents about a different one. Asked about Ethiopia,
    # the pack returned status ok with ten document citations, every one tagged
    # Kenya or Zambia, and declared no gap at all. A brief drafted from that reads
    # as authoritative and is about the wrong country — the failure that moves
    # emergency resources to the wrong place.
    # Documents about other countries are now passed over at retrieval (above), so
    # the library's silence about this country is read from what was skipped.
    if country:
        about = [h for h in forecast_hits + retro_hits if h["metadata"].get("countries")]
        others = sorted({str(c).strip() for w in (skipped.get("elsewhere") or {})
                         for c in w.split(", ") if str(c).strip()})
        if others and not about:
            n_cited = len(forecast_hits) + len(retro_hits)
            gaps.insert(0, (
                f"NO DOCUMENT IN THE LIBRARY IS ABOUT {country.upper()}. Its closest "
                f"matches are about {', '.join(others)} and were NOT cited"
                + (f"; the {n_cited} document(s) cited below declare no country. They may "
                   "describe a shared driver, but nothing here observes this country. Do "
                   f"not read any of it as evidence about {country}." if n_cited else ".")))

    # The same check for CROP, which was never written. Country was varied all
    # through testing and crop never was — every test asked about maize — so an
    # uncovered crop declared no gap at all: asking for Kenya RICE returned maize
    # documents with the word rice around them. Silence is worse than the country
    # case, which at least announced itself.
    if crop:
        covered_crops = {str(x).strip().lower()
                         for h in forecast_hits + retro_hits
                         for x in (h["metadata"].get("crops") or [])}
        if covered_crops and crop.strip().lower() not in covered_crops:
            gaps.insert(0, (
                f"NO DOCUMENT IN THE LIBRARY IS ABOUT {crop.upper()}. The "
                f"{len(forecast_hits) + len(retro_hits)} document(s) cited below are "
                f"about {', '.join(sorted(x.title() for x in covered_crops))}. Their "
                f"findings do not transfer to {crop}: sowing windows, water demand and "
                f"failure modes differ by crop. Do not read any of it as evidence "
                f"about {crop}."))

    from ..contrib import staging
    citations = []
    for h in forecast_hits + retro_hits:
        m = h["metadata"]
        citations.append({
            "kind": "document", "retrieval": "archived-document",
            "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h["score"],
            # The countries this document is actually about. Without it the drafter
            # cannot see a geography mismatch it is about to write over.
            "countries": m.get("countries"),
            "doc_id": h["doc_id"], "chunk_id": h["id"],
            **({"review_status": staging.cited_review(m)} if staging.cited_review(m) else {}),
            "archived_copy": (f"/api/food-security/rag/document/{h['doc_id']}"
                              if corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            # a preview citation says so, so the pack, report and receipt built
            # on it inherit the contributor's visibility (contrib/staging.py)
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": h["text"] + (f" Review: {staging.cited_review(m)}."
                                 if staging.cited_review(m) else "")})
    cond, gap = _conditions_citation(crop, country, trace)
    if cond:
        citations.append(cond)
    else:
        gaps.append(gap)
    drivers, driver_gaps = _driver_citations(trace)
    citations.extend(drivers)
    gaps.extend(driver_gaps)
    citations.extend(_pack_bound_sources(trace, gaps, country=country))
    asked_month = datetime.now(timezone.utc).month
    t_country, t_crop = calendar_target
    if calendar_override and (
            (t_country and t_country.lower() != (country or "").lower())
            or (t_crop and t_crop.lower() != (crop or "").lower())):
        gaps.append(f"a calendar adjustment made for {t_country or '?'} {t_crop or '?'} "
                    f"was NOT applied — the question is about {country or '?'} "
                    f"{crop or '?'}; the hub-default calendar was used instead")
        trace.append("calendar -> override target mismatch; ignored")
        calendar_override = None
    cal = crop_calendar.citation(country, crop, asked_month, override=calendar_override)
    if cal:
        citations.append(cal)
        trace.append("calendar -> " + ("ADJUSTED by requester" if cal["adjusted"]
                                       else "hub default") + f" ({country} {crop})")
    elif calendar_override:
        gaps.append("a calendar adjustment was sent but no country/crop could be "
                    "resolved to apply it to")
    for n, c in enumerate(citations, 1):
        c["n"] = n
    stats = {"forecast_hits": len(forecast_hits), "retrospective_hits": len(retro_hits),
             "conditions": cond is not None, "drivers": len(drivers),
             "calendar": (cal or {}).get("adjusted") is not None and (
                 "adjusted" if (cal or {}).get("adjusted") else "default"),
             # the literal retrieval queries — provenance for the retrieval step itself
             "queries": {"forecast": q_now, "retrospective": q_past}}
    return citations, gaps, stats


def _render_pack(citations):
    """The numbered evidence block: documents via the source_block seam (E3),
    the conditions feed in the same shape."""
    def _text(c):
        t = (f"[temporal={c['temporal']}] {c['text']}"
             if c.get("temporal") else c["text"])
        # contributor guidance rides WITH the evidence, where the drafter reads it
        if c.get("usage_notes"):
            t = f"[contributor guidance: {c['usage_notes']}] {t}"
        return t
    doc_like = [{"metadata": {k: c.get(k) for k in
                              ("source", "title", "pub_date", "validation", "url")},
                 "text": _text(c)}
                for c in citations]
    return source_block(doc_like)


def _citation_values(c: dict) -> list[float]:
    """Every number inside ONE citation's text."""
    out = []
    for tok in re.findall(r"\d+(?:\.\d+)?", _norm_nums(str(c.get("text") or ""))):
        try:
            out.append(float(tok))
        except ValueError:                                  # pragma: no cover
            pass
    return sorted(set(out))


_DRIVER_ONLY = re.compile(r"DRIVER SIGNAL ONLY|says nothing about rainfall", re.I)
_LOCAL_CLAIM = re.compile(
    r"\b(rainfall|rains|precipitation|harvest|crop|yield|planting|sowing|"
    r"food security|famine|hunger|waterlogg|flooding|drought)\w*\b", re.I)


def _driver_only_claims(draft: str, citations: list) -> list[dict]:
    """Paragraphs that make a LOCAL claim resting only on driver-signal evidence.

    The drafting rules already say it: evidence marked DRIVER SIGNAL ONLY describes
    the ocean and atmosphere, and "You may NOT use it on its own to claim any local
    rainfall, crop or food-security outcome". The citations say it too — they carry
    the sentence "says nothing about rainfall, crops or food security at any
    particular place". Nothing enforced it, and a brief told a ministry advisor that
    "the forecast rainfall peak lands immediately after" the harvest window, citing
    a NOAA CPC ENSO discussion that contains no rainfall forecast and explicitly
    disclaims that use. That sentence was the answer's only action item.

    It WARNS rather than blocks. Measured across 1,348 passing briefs it fires on
    0.8%, and reading them, some are a legitimate description of what a driver
    modulates rather than a claim about a place. A blocking check has to be right
    about every one of those, and this one is not yet.
    """
    by_n = {}
    for c in citations or []:
        try:
            by_n[int(c.get("n"))] = c
        except (TypeError, ValueError):
            continue
    out = []
    # SENTENCE level, not paragraph. The real case was
    # "September 2026 is inside that same harvest window [16], and the forecast
    # rainfall peak falls immediately after it [11]." — [16] is the crop calendar,
    # so a paragraph-level "are all of these driver-only?" test passes it. The
    # claim that matters is pinned to [11] alone, in its own clause.
    for para in re.split(r"\n\s*\n", draft.split("\n## Sources")[0]):
        for sentence in re.split(r"(?<=[.!?])\s+|(?<=\])\s*,\s+and\s+", para):
            cited = [n for n in _cited_numbers(sentence) if n in by_n]
            if not cited:
                continue
            body = _CITE_GROUP.sub("", sentence)
            if not _LOCAL_CLAIM.search(body):
                continue
            if all(_DRIVER_ONLY.search(str(by_n[n].get("text") or "")) for n in cited):
                out.append({"cites": sorted(cited), "claim": sentence.strip()[:220],
                            "why": ("every source cited for this claim is DRIVER "
                                    "SIGNAL ONLY and says nothing about rainfall, "
                                    "crops or food security at any particular place")})
    return out


def _index_scale(num: str) -> bool:
    """A small DECIMAL — the shape every climate index takes.

    `_load_bearing` skips everything under 10 so that counts ("3 districts") do
    not bury the figures that matter. That silently exempted exactly the numbers
    a driver claim turns on: an ONI of +1.8, an SOI of -0.5, a Nino-3.4 anomaly.
    A fabricated index value was never checked at all. An integer under 10 is
    still noise; a decimal under 10 is not.
    """
    try:
        v = float(num)
    except ValueError:                                      # pragma: no cover
        return False
    return abs(v) < 10 and v != int(v)


def _load_bearing(num: str, years=None) -> bool:
    """Is this the kind of number a decision would turn on? Not a year or a count under 10.
    With `years` (a draft's bare 4-digit tokens) only those are years: '2,000', '2 thousand'
    and 'two thousand' are counts."""
    try:
        v = float(num)
    except ValueError:                                      # pragma: no cover
        return False
    if 1900 <= v <= 2100 and v == int(v) and (years is None or num in years):
        return False                                        # a year
    return v >= 10


def _attribution_warnings(draft: str, citations: list, read=None, paras=None) -> list[dict]:
    """Numbers attributed to a citation that does not contain them.

    The blocking check asks whether a number exists ANYWHERE in the pack, so a
    figure lifted from citation [7] and attributed to [2] passes it. Existence is
    not attribution, and for a reader following a claim back to its source the
    difference is the whole point of the receipt.

    This WARNS rather than blocks: paragraph-level scoping mis-reads legitimately
    (a paragraph citing [2][3] and quoting a figure whose supporting citation is
    named in the next sentence), and a check that blocks honest work gets switched
    off. It is surfaced so a reader can see it, not used to refuse the brief.
    """
    # G13 (2026-10-23), re-measured after the fix pass: 43 alarms on 31 of 2,127 stored passing
    # drafts, 21+ false (pixel sizes '30 m cells', season labels '2026/27', dates), 0 on the 61
    # passing suite drafts; a check that blocks honest work gets switched off, so it warns.
    out = []
    by_n = {}
    for c in citations or []:
        try:
            by_n[int(c.get("n"))] = c
        except (TypeError, ValueError):
            continue
    # each citation's figures (text; all its fields) and each paragraph's claims, as check_grounded
    # read them once
    read = read if read is not None else {n: _cited_figures(c)[2] for n, c in by_n.items()}
    in_text, in_all = {n: f[0] for n, f in read.items()}, {n: f[1] for n, f in read.items()}
    for para, claims in (paras if paras is not None else _paragraphs(draft)):
        cited = _cited_numbers(para)
        if not cited:
            continue
        local = set().union(*(in_all[n] for n in cited if n in in_all))
        if not local:
            continue
        for num in sorted(_number_set(claims.text) - local):
            if not _load_bearing(num, claims.years):
                continue
            if _cited_share(num, [by_n[n] for n in cited if n in by_n]):
                continue
            tol = (claims.meta.get(num) or (None,))[0]
            if tol and any(float(num) - tol <= float(k) < float(num) + tol for k in local):
                continue                    # '3.2 million' for the cited 3,215,433
            elsewhere = sorted(n for n in by_n if num in in_text[n])
            if elsewhere:
                out.append({"number": num, "attributed_to": sorted(cited),
                            "actually_in": elsewhere,
                            "paragraph": para.strip()[:120]})
    return out


def _cited_figures(c: dict) -> tuple:
    """One citation read once: (its text's reading, its other fields' reading, (the figures in its
    text, the figures in all of it))."""
    text = _evidence(c.get("text"))
    rest = _evidence(" ".join(str(c.get(k) or "") for k in ("pub_date", "title", "source")))
    return text, rest, (text.figures, text.figures | rest.figures)


def _paragraphs(draft: str) -> list[tuple]:
    """(paragraph, its claims) for each paragraph of a draft above its Sources."""
    return [(p, _claims(p)) for p in re.split(r"\n\s*\n", draft.split("\n## Sources")[0])]


def _claims_of(parts: list) -> SimpleNamespace:
    """Several paragraphs' claims as one draft's (a value a count anywhere is not a year)."""
    meta, context = {}, {}
    for p in parts:
        meta.update(p.meta)
        for k, sets in p.context.items():
            for have, more in zip(context.setdefault(k, (set(), set(), set())), sets):
                have |= more
    counted = set().union(*(p.counted for p in parts))
    return SimpleNamespace(text="\n\n".join(p.text for p in parts), meta=meta, counted=counted,
                           years=set().union(*(p.years for p in parts)) - counted, context=context,
                           unreadable=[u for p in parts for u in p.unreadable])


def _rounded_or_converted(num: str, vouchers: list, tol: float | None = None,
                          units=(), nouns=(), kinds=()) -> str | None:
    """`num` as a pack figure rounded to its written digits (957 for 957.5 ha) or magnitude
    (half-open: 3,500 is '4 thousand'), or converted between units both sides name (40,840 ha
    for 408.4 km²) from a figure of 3+ significant digits. Never a sum, a difference or a ratio."""
    try:
        v = float(num)
    except ValueError:                                      # pragma: no cover
        return None
    decimals = len(num.split(".")[1]) if "." in num else 0
    if tol is None:
        how, lo, hi = f"{decimals} decimal(s)", v - 0.5 * 10 ** (-decimals), v + 0.5 * 10 ** (-decimals)
        near = lambda g: lo - 1e-9 <= g <= hi + 1e-9       # noqa: E731
    else:
        how = "the magnitude written"
        near = lambda g: v - tol - 1e-9 <= g < v + tol - 1e-9   # noqa: E731  (3,500 is 4k, not 3k)
    nouns = {str(n).lower() for n in nouns}
    for f, e, unit, sig, written, count_of, cell, kind in vouchers:
        if (count_of and not any(n.startswith(count_of) for n in nouns)) or (cell and unit not in units):
            continue
        # never across named units or kinds; a magnitude naming its kind needs a figure of that kind
        same = not (unit and units and unit not in units) and not (
            kinds and (kind not in kinds if tol is not None else kind and kind not in kinds))
        if same and _load_bearing(f"{f:.10g}") and near(f):
            return e if abs(v - f) <= 1e-9 else f"{e} rounded to {how}"
        if not (written and unit and sig >= 3) or (1900 <= f <= 2100 and f == int(f)) or (
                tol is not None and tol > 0.05 * v):          # '3 thousand ha' is too coarse to convert
            continue
        for u in units:
            scale = _CONVERT.get((u, unit))
            if scale and near(f * scale):
                exact = abs(v - f * scale) <= 1e-9
                return (f"{e} {unit} × {scale:g} = {u} (unit conversion"
                        + (")" if exact else f", rounded to {how})"))
    return None


def _cited_share(target: str, citations: list, draft_nums: set | None = None) -> str | None:
    """Is `target` a share of a total stated in the SAME citation?

    The one piece of arithmetic a drafter legitimately does is "X of Y, which is
    Z%". Everything else it computes is the drafter doing analysis the platform did
    not do, and in a brief that allocates disaster response that has to be refused
    rather than reasoned about.

    The constraints are what make this safe, and they were all found by measurement:
    the target must actually BE a percentage (0-100), the two figures must come from
    the SAME citation, and the numerator cannot exceed the denominator. Without the
    percentage bound a fabricated "US$167.13 million" was "explained" as a share of
    two unrelated figures; without the single-citation bound, searching every number
    in the pack explained ALL 46 flagged numbers including the known fabrications,
    because a few hundred values combined pairwise can hit any target by chance.
    """
    try:
        t = float(target)
    except ValueError:                                      # pragma: no cover
        return None
    if not 0.0 <= t <= 100.0:
        return None
    for c in citations or []:
        vals = _citation_values(c)
        for a in vals:
            for b in vals:
                if not b or a > b or abs(a / b * 100 - t) > 0.6:
                    continue
                # BOTH figures must be on the page. "2 of 27 schools, 7.4%" is
                # checkable by a reader; a bare 7.4 that happens to equal some
                # ratio buried in a citation is not, and allowing it waved
                # through 88% of arbitrary percentages in a real pack.
                if draft_nums is not None and not (
                        _fmt(a) & draft_nums and _fmt(b) & draft_nums):
                    continue
                return f"{a} of {b} in [{c.get('n')}]"
    return None


def _fmt(v: float) -> set:
    """How a figure might be written, so "2" matches "2.0"."""
    out = {f"{v:.10g}", str(round(v))}
    if v == int(v):
        out.add(str(int(v)))
    return out



_GAP_SUBJECT = re.compile(r"NO DOCUMENT IN THE LIBRARY IS ABOUT ([A-Z][A-Z \-\'']+?)\.")
_ACK_PHRASES = ("no document", "no evidence", "nothing here observes", "not covered",
                "no source", "does not transfer", "no data", "outside the library",
                "not in the library", "library holds nothing", "cannot be answered")


def _unacknowledged_gaps(draft: str, gaps) -> list[str]:
    """Declared 'we hold nothing about X' gaps that the draft writes straight past.

    The pack already announces when it has no document about the country or crop
    asked for. Nothing made the draft repeat it, so a confident brief about an
    uncovered target passed the gate and minted a receipt. WARNING for now, not a
    block: an honest brief that DOES own the gap must never be punished, and the
    false-alarm rate has to be measured before this is made binding.
    """
    low = draft.lower()
    owned = any(p in low for p in _ACK_PHRASES)
    out = []
    for g in gaps or []:
        m = _GAP_SUBJECT.search(str(g))
        if not m:
            continue
        subject = m.group(1).strip()
        if subject.lower() not in low:
            continue        # the draft never claims anything about it at all
        if not owned:
            out.append(f"the pack declares it holds no document about {subject.title()}, "
                       f"and the draft makes claims about {subject.title()} without "
                       "stating that gap")
    return out


def check_grounded(draft, citations, sections=None, extra_evidence=None,
                   gaps=None):
    """Blocking: sections present, no model-written Sources, citations resolve, every paragraph
    cites, every load-bearing figure (digits, words, a magnitude) traces to the evidence, every
    link is one the pack carries (G13). `sections`: another pack's own contract."""
    valid = {c["n"] for c in citations}
    used = _cited_numbers(draft)
    phantom = sorted(used - valid)
    missing_sections = [s for s in (SECTIONS if sections is None else sections) if s not in draft]
    wrote_sources = "## Sources" in draft
    paragraphs = []                    # header lines don't shield the prose under them
    for block in draft.split("\n\n"):
        body = "\n".join(l for l in block.splitlines()
                         if not l.strip().startswith("#")).strip()
        if body:
            paragraphs.append(body)
    uncited = [p[:70] for p in paragraphs if not _CITE_GROUP.search(p)]
    # The evidence read ONCE per check: each citation's text and fields (the attribution check
    # reuses them) and the numbers the PLATFORM computed (area, asset totals, weights), which
    # are evidence as much as a citation is.
    reads, read = [], {}
    for c in citations:
        text, rest, figures = _cited_figures(c)
        reads += [text, rest]
        try:
            read[int(c.get("n"))] = figures
        except (TypeError, ValueError):
            pass
    if extra_evidence:
        reads.append(_evidence(str(extra_evidence)))
    evidence = _merged(reads)
    evid_nums, vouchers, evid_years = evidence.figures, evidence.vouchers, evidence.years
    # The bibliography is not a claim: a trailing "## Sources" block is the platform's own
    # rendering of the citations (URLs, document ids, dates). Each paragraph is read once.
    paras = _paragraphs(draft)
    claims = _claims_of([c for _, c in paras])
    draft_nums = _number_set(claims.text)
    # a count ('2,000', 'two thousand') is not vouched for by a year the pack states
    flagged = sorted((draft_nums - evid_nums) | (draft_nums & claims.counted & evid_years))
    # Separate the drafter's one legitimate calculation from figures that trace to
    # nothing at all. Measured across 1,288 previously-passing briefs: 1.3% newly
    # blocked, and the real fabrication ("US$167.13 million", "47281 displaced",
    # absent from every field of every citation in its pack) is among them.
    derived, unverified, index_scale = {}, [], []
    for num in flagged:
        if not _load_bearing(num, claims.years):
            # Sub-threshold. A decimal down here is a climate-index value, not a
            # count, and was exempt from every check — WARN on it while the
            # false-alarm rate is measured, rather than block today.
            if _index_scale(num) and not _cited_share(num, citations, draft_nums):
                index_scale.append(num)
            continue          # a year or a count under 10 blocks nothing
        share = _cited_share(num, citations, draft_nums)
        if share:
            derived[num] = share
            continue
        tol, written, alt = claims.meta.get(num) or (None, None, None)
        units, nouns, kinds = claims.context.get(num) or ((), (), ())
        traced = _rounded_or_converted(num, vouchers, tol, units, nouns, kinds)
        if not traced and alt is not None and alt in evid_nums:
            traced = f"{alt} as the pack writes it ({written!r} read as a length)"
        if traced:
            derived[num] = traced
        else:
            unverified.append(num)
    links = _links_not_served(draft, citations, extra_evidence, gaps)
    misattributed = _attribution_warnings(draft, citations, read, paras)
    driver_only = _driver_only_claims(draft, citations)
    unowned_gaps = _unacknowledged_gaps(draft, gaps)
    failures = []
    if missing_sections:
        failures.append(f"missing required sections: {missing_sections}")
    if wrote_sources:
        failures.append("draft wrote its own Sources section (the system appends it)")
    if not used:
        failures.append("no citations at all")
    if phantom:
        failures.append(f"citation numbers not in the evidence list: {phantom}")
    if uncited:
        failures.append(f"paragraphs without citations: {uncited}")
    if unverified:
        # BLOCKING. A number that traces to no citation and no platform-computed
        # figure is the failure this platform exists to prevent: it reads as
        # authoritative, it is precise, it carries a citation marker, and someone
        # allocates flood response with it. Recording it and passing anyway put a
        # PASSED badge above two fabricated figures in a real brief.
        shown = [f"{n} ({claims.meta[n][1]!r})" if n in claims.meta else n for n in unverified]
        failures.append(
            "numbers that appear in no citation and in no platform-computed figure: "
            f"{shown} — quote the platform's own figure, cite a source that "
            "states it, or remove the number. If it is a share of a cited total, "
            "state both figures so it can be checked."
            + (" An abbreviation like '7.4m' may be millions or metres: write the figure "
               "in full ('7.4 million', '7.4 metres')."
               if any((claims.meta.get(n) or (0, 0, None))[2] is not None for n in unverified)
               else ""))
    if claims.unreadable:
        failures.append(f"figures in words the gate cannot read: {claims.unreadable} — write "
                        "each in digits ('2.5 million').")
    if links:
        # BLOCKING (G13): an answer rests only on what the platform serves.
        failures.append(
            f"links to sources this platform does not serve: {links} — remove the link, or "
            "contribute the source so the platform serves it (ask the person for its link; "
            "contribute_submit).")
    return {"passed": not failures, "failures": failures, "cited": sorted(used),
            "phantom_citations": phantom, "missing_sections": missing_sections,
            "uncited_paragraphs": uncited, "numbers_unverified": unverified,
            "numbers_derived": derived, "links_not_served": links,
            "numbers_unreadable": claims.unreadable,
            # WARNING, not a failure: the figure is in the pack but not in the
            # citation the paragraph points at.
            "numbers_attributed_elsewhere": misattributed,
            "local_claims_on_driver_evidence": driver_only,
            "numbers_index_scale_unverified": index_scale,
            "gaps_not_acknowledged": unowned_gaps,
            # One place a caller can look for everything that did NOT block. These
            # are the checks queued for promotion once their false-alarm rate is
            # measured; keeping them in one list makes that promotion one edit.
            "warnings": ([f"claim about an uncovered target: {w}" for w in unowned_gaps]
                         + [f"climate-index figure in no citation: {n}" for n in index_scale]
                         + [f"local claim resting on driver evidence: {d}"
                            for d in (driver_only or [])]
                         + [f"number attributed to a citation that lacks it: {m}"
                            for m in (misattributed or [])])}


def _sources_md(citations):
    lines = []
    for c in citations:
        bits = [str(c.get("source") or "unknown source")]
        bits += [str(c[k]) for k in ("title", "pub_date") if c.get(k)]
        if c.get("validation"):
            bits.append(f"validation: {c['validation']}")
        line = f"[{c['n']}] " + " — ".join(bits)
        if c.get("url"):
            line += f"\n    source: {c['url']}"
        if c.get("archived_copy"):
            line += f"\n    archived: {c['archived_copy']}"
        if c.get("query"):
            line += f"\n    query: {c['query']}"
        lines.append(line)
    return "\n".join(lines)


def _declined(reason, *, trace, usage, citations=None, check=None, stats=None):
    return {"declined": True, "brief": None, "decline_reason": reason,
            "citations": citations or [], "evidence": stats or {},
            "grounded": check, "trace": trace, "usage": usage}


def synthesize(question, provider=None, model=None, calendar=None,
               calendar_target=(None, None)):
    """The full pipeline. Returns a dict the route serves as-is."""
    settings = get_settings()
    provider = provider or settings.default_provider
    model = model or default_model(provider)
    client = build_client(provider)
    trace, usage = [f'question: "{question}"'], []

    parsed, decline = _parse(client, model, question, trace, usage)
    if decline:
        return _declined(decline, trace=trace, usage=usage) | {
            "provider": provider, "model": model}

    citations, gaps, stats = gather_evidence(parsed, trace, calendar_override=calendar,
                                             calendar_target=calendar_target)
    # A calendar is context, not evidence — it cannot carry a brief alone. Neither
    # can the driver indices: they are GLOBAL, so on a question about one country
    # they establish the ocean state and nothing about that place. Letting them
    # clear the bar would let the engine answer "maize in Kenya" out of an ENSO
    # index alone, which is exactly the inference the use case forbids in Phase 1.
    if not _has_evidence(citations, parsed.get("country")):
        trace.append("evidence -> empty; declining without a synthesis call")
        return _declined(
            "No evidence available: " + "; ".join(gaps), trace=trace, usage=usage,
            stats=stats) | {"provider": provider, "model": model}

    # Declared gaps (or their explicit absence) as a citable entry — appended
    # AFTER the evidence bar so it can never carry a brief alone. Observed: the
    # model honestly wrote "there are no identified gaps..." and the gate blocked
    # it as uncited, costing a retry.
    from ..mcp import packs as mcp_packs
    citations = [*citations, mcp_packs.gaps_citation(citations, gaps)]

    asked_on = datetime.now(timezone.utc).strftime("%B %Y")
    user_msg = (
        f"Question: {question}\n"
        f"Asked in: {asked_on} (state the season-timing caveat relative to this)\n"
        f"Parsed target: crop={parsed['crop'] or '?'}, country={parsed['country'] or '?'}, "
        f"focus={parsed['focus']}\n"
        "Known gaps (must appear in What's missing, citing the declared-gaps "
        "evidence entry): "
        + ("; ".join(gaps) if gaps else "none identified") + "\n\n"
        "Numbered evidence (the ONLY permissible sources):\n\n" + _render_pack(citations))

    messages = [{"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": user_msg}]
    check = None
    for attempt in (1, 2):
        # 1500 was set when a pack held ~4 citations. With the Pillar-1 drivers a
        # brief runs longer, and a TRUNCATED draft fails the gate as "missing
        # sections" — a length problem wearing a groundedness problem's clothes,
        # which cost real debugging time. Headroom, plus an explicit truncation
        # check below so the next occurrence names itself.
        resp = client.chat.completions.create(model=model, max_tokens=3000,
                                              messages=messages)
        usage.append(_usage(resp))
        draft = (resp.choices[0].message.content or "").strip()
        if getattr(resp.choices[0], "finish_reason", None) == "length":
            trace.append(f"synthesis attempt {attempt} -> TRUNCATED at max_tokens; "
                         "the draft is incomplete, not ungrounded")
        if draft.startswith("DECLINE:"):
            trace.append(f"synthesis attempt {attempt} -> model declined")
            return _declined(draft, trace=trace, usage=usage, citations=citations,
                             stats=stats) | {"provider": provider, "model": model}
        check = check_grounded(draft, citations)
        check["attempts"] = attempt
        trace.append(f"groundedness attempt {attempt} -> "
                     + ("PASS" if check["passed"] else "FAIL: " + "; ".join(check["failures"])))
        if check["passed"]:
            brief = draft + "\n\n## Sources\n" + _sources_md(citations)
            return {"declined": False, "brief": brief, "citations": citations,
                    "parsed": parsed, "evidence": stats, "gaps": gaps, "grounded": check,
                    "provider": provider, "model": model, "trace": trace, "usage": usage}
        messages += [{"role": "assistant", "content": draft},
                     {"role": "user", "content":
                      "Your draft failed the groundedness check — "
                      + "; ".join(check["failures"])
                      + ". Rewrite following the rules exactly."}]
    return _declined(
        "The draft could not be grounded in the evidence after a retry — refusing to "
        "ship an uncited brief.", trace=trace, usage=usage, citations=citations,
        check=check, stats=stats) | {"provider": provider, "model": model}
