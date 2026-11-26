"""The groundedness check: what a draft must satisfy against its pack's evidence before it is an
answer. Every pack's draft goes through `check_grounded`."""

from __future__ import annotations

import bisect
import functools
import re
from types import SimpleNamespace

from ..config import get_settings

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
    """The code lists the gate reads (conf/gate_codes.yml): ISO 3166 and ISO 4217, as data.
    Missing or unreadable, it is logged loudly and only the country-domain and currency checks go."""
    import logging
    import yaml
    path = get_settings().gate_codes_path
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logging.getLogger(__name__).error("gate code lists unreadable (%s: %s): country-domain and "
                                          "currency checks are off until it is installed", path, exc)
        return {}


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
        from ..ui import ui
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
    from ..packs.food_security.synthesis import SECTIONS      # the default: that pack's contract
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
