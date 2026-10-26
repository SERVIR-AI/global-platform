"""start_here: which tool to call, with which arguments, and the rules, for a client that defers
tools and never reads the instructions. Deterministic (no LLM), read from the registries: each pack
row's routing words and `choices`, the resolver's boundary index, the countries and the feeds."""

from __future__ import annotations

import functools
import inspect
import re
from collections import Counter

from . import budget, instructions, packs

# Function words a question is written with; never a routing word (language, not data).
_STOP = frozenset("""a an the and or of in on at to for by with from into inside within about over
under near around across between since after before during until what which who whom whose when
where why how many much is are was were be been being has have had do does did will would can could
should may might must any all each every some more most less than then this that these those there
here it its their they them our your you we us me my not no but also using use used according become
became becomes show tell give get per as if so such very just only still now please like platform
servirplatform de del la las los el en y o que se por para con sin un una al lo le les des du et ou
dans pour avec sur au aux da do das dos em na nas nos um uma""".split())
_YEAR = r"(?<!\d)(?<!\d\.)((?:1[89]|20)\d\d)(?!\d)(?!\.\d)"
# A run of capitalised words after a preposition: a place the question names in its own words.
_RUN = re.compile(r"\b(?i:in|inside|within|at|near|around|across|for|of)\s+(?:the\s+)?"
                  r"((?:[A-ZÀ-Þ][\w'’.-]*)(?:\s+(?:(?:de|del|la|las|los|el|do|da|dos|das|of|the)\s+)*"
                  r"[A-ZÀ-Þ][\w'’.-]*)*)")
# The product a question says its answer must come from ('according to MapBiomas').
_SOURCE = re.compile(r"\b(?:according\s+to|seg[uú]n|selon|d['’]apr[eè]s)\s+(?:the\s+|el\s+|la\s+|o\s+|a\s+)?"
                     r"((?:[A-ZÀ-Þ][\w'’.-]*)(?:\s+(?:(?:of|de|del|la|the)\s+)*[A-ZÀ-Þ][\w'’.-]*)*)")
_SHOWN = 4                              # alternatives, related lookups and weak matches listed
_ECHO = 400                             # chars of the question a next call repeats; past it, a pointer
_QUOTED = 2000                          # chars of the question the reply quotes back


# --------------------------------------------------------------------------- words
def _key(text) -> str:
    from ..graph.geo import ingest
    return ingest._key(text)


def _stem(w: str) -> str:
    """A word as compared: plural and -ing/-ed/-ure/-e endings off ('floods', 'flooding' ->
    'flood'; 'exposed', 'exposure' -> 'expos'), so a question and a name meet."""
    if len(w) <= 3 or not w.isalpha():
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    for suf, keep in (("ing", 4), ("ed", 4), ("ure", 5), ("e", 4)):    # 'pasture' stays, not 'past'
        if w.endswith(suf) and len(w) - len(suf) >= keep:
            return w[:-len(suf)]
    return w


# Words that frame a question (when, how much, how it is measured), not what it is about: never
# routing evidence by themselves (language, not data).
_FRAME = frozenset(_stem(w) for w in """year years month months monthly week weekly day days daily
annual decade decades season seasons time times today current latest recent past last next future
trend trends history historical level levels rate rates amount number numbers total average mean
median share percent percentage proportion ratio value values index count size area areas region
regions place places zone zones map maps mapped mapping data dataset global world national local
change changes changed changing increase decrease high higher highest low lower lowest medium other
others background unknown class classes type types kind kinds set sets new old big small large least
best worst good bad worse better getting compare compared information report reports statistics
figure figures""".split())


def _stems(text) -> list[str]:
    return [_stem(w) for w in _key(text).split()]


def _content(stems) -> list[str]:
    return [s for s in stems if s not in _STOP and len(s) > 2 and not s.isdigit()]


def _has(seq: list, sub: list) -> bool:
    n = len(sub)
    return n > 0 and any(seq[i:i + n] == sub for i in range(len(seq) - n + 1))


def _coined(word: str) -> bool:
    """A word that names a product by itself: a capital inside a part of it or an underscore
    (MapBiomas, ESA, GWL_FCS30D, PM2.5), never a hyphenated place ('Trans-Nzoia') or digits."""
    w = str(word).strip(".,;:!?()[]'\"’")
    if "_" in w and any(ch.isalpha() for ch in w):
        return True
    return any(any(ch.isupper() for ch in part[1:]) for part in w.split("-"))


def _form(name: str) -> str:
    """How a choice's name names it: 'proper' (a coined word, or two or more capitalised words),
    'plain' (one word, or no capital: a hazard, a crop) or 'descriptive' ('Mangrove extent':
    words any map could carry, never a product's name by itself)."""
    words = str(name).split()
    if any(_coined(w) for w in words) or sum(1 for w in words if w[:1].isupper()) >= 2:
        return "proper"
    if len(words) <= 1 or not any(w[:1].isupper() for w in words):
        return "plain"
    return "descriptive"


def _initials(name: str) -> str:
    """'GMW' for 'Global Mangrove Watch': three or more capitalised words, no coined one."""
    words = [w for w in str(name).split() if w.lower() not in ("of", "the", "and", "de", "del", "la")]
    if len(words) >= 3 and all(w[:1].isupper() and w[1:].islower() and w.isalpha() for w in words):
        return "".join(w[0] for w in words)
    return ""


def _render(tool: str, args: dict) -> str:
    """tool(k=v, ...) as a call reads; a '<...>' placeholder is written bare."""
    def v(x):
        return x if isinstance(x, str) and x.startswith("<") else repr(x)
    return f"{tool}(" + ", ".join(f"{k}={v(x)}" for k, x in args.items()) + ")"


def _clauses(text) -> list[str]:
    """A routing text's clauses: split at , ; : / brackets, dashes and 'and' / 'or'; bracketed
    parts (where a pack's keywords say its places) left out."""
    t = str(text or "")
    for _ in range(2):
        t = re.sub(r"\([^()]*\)", " ", t)
    return [c.strip() for c in re.split(r"[,;:/\[\]]|\s[—–-]\s|\band\b|\bor\b", t) if c.strip()]


def _q(text: str) -> dict:
    """A text as matched: its words, stems, capitalised stems and coined words, raw."""
    plain = re.sub(r"(?<=\w)['’]s\b", "", text)          # "Kenya's" reads as "Kenya"
    words = _key(plain).split()
    raw = [w.strip(".,;:!?()[]'\"¿¡") for w in plain.split()]
    segments = re.split(r"[|;:!?()\[\]]|,\s|\.\s|\s[—–-]\s", plain)    # a run never crosses these
    return {"text": text, "raw": raw, "words": words, "stems": [_stem(w) for w in words],
            "caps": {_stem(t) for w in re.findall(r"[A-ZÀ-Þ][\w'’-]*", plain) for t in _key(w).split()},
            "runs": [r for s in segments for r in _cap_runs([w.strip(".,'\"¿¡") for w in s.split()])]}


def _cap_runs(raw: list[str]) -> list[list[str]]:
    """Maximal runs of capitalised or coined words, as written ('ESA CCI', 'Manglares El Salado')."""
    runs, cur = [], []
    for w in raw + [""]:
        if w and (w[:1].isupper() or _coined(w)):
            cur.append(w)
        else:
            if cur:
                runs.append(cur)
            cur = []
    return runs


# --------------------------------------------------------------------------- registries
def _choices(sp: dict) -> dict:
    """{key: [choice]} from a pack row's `choices` hooks; a failing hook gives none."""
    out = {}
    for k, fn in (sp.get("choices") or {}).items():
        try:
            rows = fn() if callable(fn) else fn
        except Exception:
            rows = []
        out[k] = [c for c in rows or [] if isinstance(c, dict) and c.get("value") is not None]
    return out


def _countries(choices_by_pack: dict) -> dict:
    """{country key: name} for every country the platform holds data for (registry.countries)
    and every pack's choices declare; Viet Nam / Vietnam are one key."""
    from ..graph.geo import vectors
    from . import registry
    names = list(registry.countries())
    for choices in choices_by_pack.values():
        names += [c for rows in choices.values() for row in rows for c in row.get("countries") or []]
    out = {}
    for n in names:
        if str(n).strip():
            out.setdefault(vectors.country_key(_key(n)), str(n))
    return out


def _countries_in(words: list[str], known: dict) -> list[str]:
    """The countries `words` name (up to four words each, aliases folded), in the order said."""
    from ..graph.geo import vectors
    found, i = [], 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            name = known.get(vectors.country_key(" ".join(words[i:i + n]))) if i + n <= len(words) else None
            if name:
                if name not in found:
                    found.append(name)
                i += n - 1
                break
        i += 1
    return found


@functools.lru_cache(maxsize=1)
def _tz_tables() -> tuple[dict, dict]:
    """({country key: name}, {country key: [(lon, lat)]}) from the system's IANA time-zone tables
    (iso3166.tab, zone.tab): where a named country lies, without a geocoder; empty when absent
    (a country is then known from the registries only, its coverage not known, never assumed)."""
    import zoneinfo
    from pathlib import Path
    from ..graph.geo import vectors
    for root in zoneinfo.TZPATH:
        zt, iso = Path(root) / "zone.tab", Path(root) / "iso3166.tab"
        if zt.is_file() and iso.is_file():
            break
    else:
        return {}, {}
    try:
        names, keyed, points = {}, {}, {}
        for line in iso.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "\t" in line:
                code, name = line.split("\t", 1)
                names[code.strip()] = name = re.sub(r"\s*\(.*\)\s*$", "", name.strip())
                keyed.setdefault(vectors.country_key(_key(name)), name)
        for line in zt.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if line.startswith("#") or len(parts) < 3:
                continue
            m = re.match(r"([+-])(\d\d)(\d\d)(\d\d)?([+-])(\d\d\d)(\d\d)(\d\d)?$", parts[1].strip())
            if not m:
                continue
            lat = (int(m[2]) + int(m[3]) / 60 + int(m[4] or 0) / 3600) * (-1 if m[1] == "-" else 1)
            lon = (int(m[6]) + int(m[7]) / 60 + int(m[8] or 0) / 3600) * (-1 if m[5] == "-" else 1)
            for code in parts[0].split(","):
                if names.get(code.strip()):
                    points.setdefault(vectors.country_key(_key(names[code.strip()])), []).append((lon, lat))
        return keyed, points
    except OSError:
        return {}, {}


def _tz_points() -> dict:
    return _tz_tables()[1]


def _named_countries(q: dict, known: dict) -> list[str]:
    """The countries the question names: those the platform holds data for, then any other
    country it capitalises (so a place no pack covers is said to be one)."""
    named = _countries_in(q["words"], known)
    other = {k: v for k, v in _tz_tables()[0].items() if k not in known}
    for c in _countries_in(q["words"], other):
        if c not in named and all(_stem(w) in q["caps"] for w in _key(c).split()):
            named.append(c)
    return named


def _feeds() -> dict:
    """{dataset: row} for every available feed and the caller's visible staged ones."""
    from . import registry
    rows = {k: v for k, v in list(registry.FEEDS.items()) if v.get("status") == "available"}
    try:
        from ..contrib import staging
        for ds in staging.staged_feeds_for_caller():
            row = staging.visible_staged_feed(ds)
            if row and ds not in rows:
                rows[ds] = row
    except Exception:
        pass
    return rows


# What a dataset says it is when its rows are a sample of the ground, not a census of it.
_SAMPLE = re.compile(r"\b(?:samples?|sampling|sampled|validation|validate|training|train|reference|"
                     r"ground[- ]truth|calibration|survey|surveyed|plots?|transects?|quadrats?)\b", re.I)


def _rows_are(spec: dict, records=None) -> str:
    """What a dataset's rows are, from its own fields and words: sample points when they carry a
    latitude and a longitude and the dataset says it is a sample (validation, training, survey
    plots), point records when they only carry coordinates, else its own records."""
    from . import feeds
    fetch = (spec or {}).get("fetch") or {}
    names = list(fetch.get("fields") or fetch.get("columns") or [])
    if not names and isinstance(records, list) and records and isinstance(records[0], dict):
        names = list(records[0])
    lat, lon = feeds._coord_columns(names)
    if not (lat and lon):
        return "the dataset's own records"
    said = " ".join(str((spec or {}).get(k) or "") for k in ("title", "description"))
    return "sample points (lat/lon)" if _SAMPLE.search(said) else "point records (lat/lon)"


# --------------------------------------------------------------------------- the place
def _overlaps(a, b) -> bool:
    return bool(a and b and len(a) == 4 and len(b) == 4 and a[0] <= b[2] and b[0] <= a[2]
                and a[1] <= b[3] and b[1] <= a[3])


def _place_ctx(countries: list, box=None) -> dict:
    """What a choice's reach is judged against: the countries named (keys), the place's own box
    (a contributed boundary) and, for choices that declare no country, where those countries lie."""
    from ..graph.geo import vectors
    ckeys = {vectors.country_key(_key(c)) for c in countries}
    pts = _tz_points()
    return {"ckeys": ckeys, "box": list(box) if box else None,
            "points": [p for k in ckeys for p in pts.get(k, [])]}


def _reach(choice: dict, pc: dict) -> bool | None:
    """Does the choice reach the place: a declared country decides (then the place's own box
    against its extent), else its extent against the place's box or its countries' places;
    None when neither side says."""
    from ..graph.geo import vectors
    cs = {vectors.country_key(_key(c)) for c in choice.get("countries") or []}
    box = choice.get("bbox")
    box = list(box) if isinstance(box, (list, tuple)) and len(box) == 4 else None
    if cs and pc["ckeys"]:
        if not cs & pc["ckeys"]:
            return False
        return _overlaps(box, pc["box"]) if box and pc["box"] else True
    if box and pc["box"]:
        return _overlaps(box, pc["box"])
    if box and pc["points"]:
        return any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in pc["points"])
    return None


def _fits(choice: dict, pc: dict) -> bool:
    return _reach(choice, pc) is not False


def _coverage(by_key: dict, pc: dict) -> bool | None:
    """Whether anything the pack holds reaches the place: True, False (every choice that can be
    placed lies elsewhere), None (nothing says)."""
    seen = [_reach(c, pc) for rows in by_key.values() for c in rows]
    return True if True in seen else False if False in seen else None


def _where(rows) -> str:
    """Where choices reach, in words: their declared countries, else their extents."""
    said = []
    for c in rows:
        b = c.get("bbox")
        part = (", ".join(c.get("countries") or []) or
                (f"lon {b[0]:.0f} to {b[2]:.0f}, lat {b[1]:.0f} to {b[3]:.0f}"
                 if isinstance(b, (list, tuple)) and len(b) == 4 else ""))
        for p in part.split(", ") if c.get("countries") else [part]:
            if p and p not in said:
                said.append(p)
    return budget.names(said, rank=budget.PLACE, more="more places") or "no declared place"


def _boundary(q: dict, ckeys: set, skip: set) -> dict | None:
    """{name, areas, layers, also}: the contributed boundary the question names, longest first (a
    one-word or all-subject-word name only capitalised; none outside a country it names), and in
    `also` the other boundaries it names."""
    from ..graph.geo import ingest, vectors
    try:
        layers = ingest._gazetteer_layers()
    except Exception:
        return None
    words, caps = q["words"], {t for w in q["raw"] if w[:1].isupper() for t in _key(w).split()}
    found: dict = {}
    for layer, entry in layers.items():
        try:
            index = ingest._gazetteer(layer, entry)
        except Exception:
            continue
        longest = max((len(k.split()) for k in index), default=0)
        for n in range(min(longest, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                gram = words[i:i + n]
                k = " ".join(gram)
                if len(k) < 4 or k not in index:
                    continue
                generic = all(w in _STOP or _stem(w) in skip for w in gram)
                if (n == 1 or generic) and not set(gram) <= caps:
                    continue
                found.setdefault((n, i, k), []).extend(index[k])

    def lies_in(a) -> set:
        entry = layers.get(a["layer"]) or {}
        vals = [*a["attrs"].values(), *(entry.get("countries") or [])]
        return {vectors.country_key(_key(v)) for v in vals}
    picked, used = [], set()
    for (n, i, k), areas in sorted(found.items(), key=lambda t: (-t[0][0], t[0][1])):
        span = set(range(i, i + n))
        if span & used:
            continue
        if ckeys:
            areas = [a for a in areas if lies_in(a) & ckeys]
            if not areas:
                continue
        used |= span
        picked.append(areas)
    if not picked:
        return None
    areas = picked[0]
    return {"name": areas[0]["label"], "areas": areas, "layers": layers,
            "also": [a[0]["label"] for a in picked[1:]]}


def _place_run(question: str, known: dict, skip: set) -> str:
    """A place the question names in its own words (a capitalised run after 'in', 'inside', ...;
    never a class or subject word), with the country it names after a comma or before an
    apostrophe; a run followed by a comma and a country first; '' when none."""
    from ..graph.geo import vectors
    first = ""
    for m in _RUN.finditer(question):
        run = m.group(1).strip(" .'’")
        country = ""
        if re.search(r"['’]s\s", run):
            before, _sep, run = re.split(r"(['’]s\s)", run, maxsplit=1)
            country = known.get(vectors.country_key(_key(before)), "")
        run = run.strip(" .'’")
        if not run or vectors.country_key(_key(run)) in known:
            continue
        if all(w in _STOP or _stem(w) in skip for w in _key(run).split()):
            continue                                     # 'Mangrove', 'People': not a place
        after = question[m.end():m.end() + 60]
        tail = re.match(r"\s*,\s*([A-ZÀ-Þ][\w'’.-]*(?:\s+[A-ZÀ-Þ][\w'’.-]*){0,3})", after)
        if tail and not country:
            t = tail.group(1).split()
            country = next((known[k] for k in [vectors.country_key(_key(" ".join(t[:n])))
                                                  for n in (3, 2, 1)] if k in known), "") or " ".join(t)
        said = f"{run}, {country}" if country else run
        if tail:
            return said
        first = first or said
    return first


def _area_of(q: dict, place, known: dict, skip: set) -> dict:
    """{place, place_how, box, countries, notes}: a passed place (its boundary's box when
    contributed), else the contributed boundary the question names, else a place it names in its
    own words, else the one country it names."""
    from ..graph.geo import ingest, vectors
    named = _named_countries(q, known)
    out = {"countries": list(named), "box": None, "notes": []}
    if place:
        out.update(place=str(place), place_how="as passed")
        for c in _named_countries(_q(str(place)), known):
            if c not in out["countries"]:
                out["countries"].append(c)
        try:
            layers = ingest._gazetteer_layers()
            hit = ingest._areas_named(_key(str(place).split(",")[0]), layers)
            out["box"] = hit[0].get("bbox") if len(hit) == 1 else None
        except Exception:
            pass
        return out
    ckeys = {vectors.country_key(_key(c)) for c in named}
    area = _boundary(q, ckeys, skip)
    if area:
        areas, layers = area["areas"], area["layers"]
        a = areas[0]
        title = (layers.get(a["layer"]) or {}).get("title") or a["layer"]
        out.update(place=a["label"] if len(areas) == 1 else ingest._forms(areas, layers)[id(a)][0],
                   box=a.get("bbox"),
                   place_how=f"a contributed boundary in {title}" + (
                       f"; {len(areas) - 1} more of the name: resolve_place_time lists them" if len(areas) > 1 else ""))
        for v in [*a["attrs"].values(), *((layers.get(a["layer"]) or {}).get("countries") or [])]:
            c = known.get(vectors.country_key(_key(v)))
            if c and c not in out["countries"]:
                out["countries"].append(c)
        if area["also"]:
            out["notes"].append(f"The question also names {', '.join(area['also'])}: these calls read "
                                f"{a['label']}; make the same calls once per place.")
        return out
    run = _place_run(q["text"], {**_tz_tables()[0], **known}, skip)
    if run:
        out.update(place=run, place_how="named in the question (resolve_place_time finds it)")
    elif len(named) == 1:
        out.update(place=named[0], place_how="the country the question names")
    return out


# --------------------------------------------------------------------------- what routes
def _evidence(sp: dict, choices: dict) -> dict:
    """{names, phrases} a pack routes on: whole names (keyword items without their bracketed places,
    a 2+-word display name, coined words, legend labels) and clauses of use_for and subjects (else
    usage notes) as word sets, two words of which must meet."""
    names, phrases = set(), set()

    def add(text, coined=False):
        st = tuple(_content(_stems(text)))
        if not st or (len(st) == 1 and (st[0] in _FRAME or (len(st[0]) < 4 and not coined))) \
                or all(s in _FRAME for s in st):
            return
        names.add(st)
    kw = packs.text(sp, "keywords")
    for c in _clauses(kw):
        add(c)
    disp = str(sp.get("display_name") or "")
    if len(_content(_stems(disp))) >= 2:
        add(disp)
    prose = [packs.text(sp, "use_for"), *[str(s) for s in sp.get("subjects") or []]]
    if not any(prose) and not kw and not choices:
        notes = packs.text(sp, "usage_notes")
        if notes and "todo" not in notes.casefold():
            prose.append(notes)
    for t in [kw, *prose]:
        for w in str(t).split():
            if _coined(w):
                add(w.strip(".,;:!?()[]'\"’"), coined=True)
    for t in prose:
        for c in _clauses(t):
            st = frozenset(s for s in _content(_stems(c)) if s not in _FRAME)
            if len(st) >= 2:
                phrases.add(st)
    for rows in choices.values():
        for ch in rows:
            for vs in (ch.get("values") or {}).values():
                for lab in vs or []:
                    if not (len(str(lab).split()) == 1 and str(lab).lower().endswith("ed")):
                        add(lab)                  # 'Protected', 'Proposed' qualify, never name
    return {"names": names, "phrases": phrases}


def _skip(evid: dict, choices_by_pack: dict) -> set:
    """Stems a place is never made of: every single-word routing name and legend label, and the
    names of the values a pack takes as plain words (a hazard, a crop)."""
    out = {n[0] for ev in evid.values() for n in ev["names"] if len(n) == 1}
    for by_key in choices_by_pack.values():
        for rows in by_key.values():
            for c in rows:
                out |= {s for vs in (c.get("values") or {}).values() for lab in vs or [] for s in _stems(lab)}
                out |= {s for n in c.get("names") or [] if _form(n) == "plain" for s in _stems(n)}
    return out - _STOP


def _vocab(sp: dict, choices: dict, feeds: dict, pid: str) -> set[str]:
    """Every word a pack's texts, choices and feed titles hold: what a weak match is read against."""
    texts = [packs.text(sp, k) for k in ("keywords", "use_for", "display_name")]
    texts += [str(s) for s in sp.get("subjects") or []]
    for rows in choices.values():
        for c in rows:
            texts += [str(n) for n in c.get("names") or []] + [str(c.get("words") or "")]
    texts += [str(f.get("title") or "") for f in feeds.values() if f.get("pack") == pid]
    return {s for t in texts for s in _content(_stems(t))}


def _tok(word: str) -> str:
    return " ".join(_stems(word))


def _families(rows: list) -> dict:
    """{coined word or initials: the product families (each choice's first name) that use it}."""
    out: dict = {}
    for c in rows:
        fam = str((c.get("names") or [c.get("value")])[0])
        for n in c.get("names") or []:
            for w in [*(w for w in str(n).split() if _coined(w)), *([_initials(n)] if _initials(n) else [])]:
                out.setdefault(_tok(w), set()).add(fam)
    return out


def _named_by(choice: dict, q: dict, shared=None, fams=None) -> tuple[int, str]:
    """(strength, words used): a plain or proper whole name (never a descriptive one); a coined word
    or initials ('GMW') one family uses, its run naming nothing else ('ESA CCI' is not WorldCover);
    a capitalised run of a proper name no other choice shares ('National Trust')."""
    best, said = 0, ""
    mine = {s for n in [*(choice.get("names") or []), str(choice.get("words") or "")] for s in _stems(n)}
    for name in choice.get("names") or []:
        form, st = _form(name), _stems(name)
        single_coined = len(str(name).split()) == 1 and _coined(name)
        if form != "descriptive" and st and not single_coined and _has(q["stems"], st):
            score = len(st) + (1 if any(_coined(w) for w in name.split()) else 0)
            if score > best:
                best, said = score, name
        if form != "proper" or best >= 2:
            continue
        tokens = [w for w in str(name).split() if _coined(w)] + ([_initials(name)] if _initials(name) else [])
        for w in tokens:
            k = _tok(w)
            if not k or not _has(q["stems"], k.split()) or len((fams or {}).get(k, ())) > 1:
                continue
            run = next((r for r in q["runs"] if any(_tok(x) == k for x in r)), [w])
            if q.get("guard", True) and any(_coined(x) and _tok(x) != k and not set(_stems(x)) <= mine
                                            and not set(_stems(x)) <= q.get("served", set()) for x in run):
                continue                     # 'ESA CCI' names another product than ESA WorldCover
            best, said = 2, w
            break
        if shared is None:
            continue
        for run in _runs(st):
            if len(run) <= best:
                break
            if shared.get(run, 0) <= 1 and set(run) & q["caps"] and _has(q["stems"], list(run)):
                best, said = len(run), " ".join(run)
    return best, said


def _runs(st: list[str]) -> list[tuple]:
    """Every run of two or more consecutive words of a name, longest first."""
    return sorted({tuple(st[i:j]) for i in range(len(st)) for j in range(i + 2, len(st) + 1)},
                  key=len, reverse=True)


def _pick(rows: list, q: dict, pc: dict, want: list) -> tuple[list, list]:
    """(named choices that may reach the place, best first; named ones that do not): ranked by
    how strongly named, a country named in both, the asked years held, the question's classes in
    its legend, the most classes, the most years, registration order."""
    from ..graph.geo import vectors
    shared = Counter(r for c in rows for r in {r for n in c.get("names") or [] if _form(n) == "proper"
                                               for r in _runs(_stems(n))})
    fams = _families(rows)
    named = [(c, *_named_by(c, q, shared, fams), i) for i, c in enumerate(rows)]
    named = [t for t in named if t[1] > 0]
    fit = [t for t in named if _fits(t[0], pc)]
    fitting = {t[3] for t in fit}

    def rank(t):
        c, sc, _said, i = t
        cs = {vectors.country_key(_key(x)) for x in c.get("countries") or []}
        return (-sc, -len(cs & pc["ckeys"]), -sum(1 for y in want if y in (c.get("years") or [])),
                -_hits(c, q), -_n_classes(c), -len(c.get("years") or []), i)
    return sorted(fit, key=rank), [t for t in named if t[3] not in fitting]


def _hits(c: dict, q: dict) -> int:
    return sum(1 for vs in (c.get("values") or {}).values() for lab in vs or []
               if _has(q["stems"], _stems(lab)))


def _n_classes(c: dict) -> int:
    return sum(len(vs or []) for vs in (c.get("values") or {}).values())


def _score_packs(q: dict, evid: dict, named: dict, skip: set) -> list[tuple]:
    """[(score, 0, pid, matched words)] best first, positive evidence only: a choice the question
    names (twice its strength), a routing name it holds whole, a clause it meets in two words;
    each weighted by how few packs share it (one every pack shares weighs nothing)."""
    qc = [s for s in _content(q["stems"]) if s not in skip]
    qs = set(qc)
    written = dict(zip(reversed(q["stems"]), reversed(q["words"])))
    hits = {}
    for pid, ev in evid.items():
        got = {}
        for name in ev["names"]:
            if (_has(qc, list(name)) if len(name) > 1 else name[0] in qs):
                got[("n", *name)] = len(name)
        for ph in ev["phrases"]:
            common = ph & qs
            if len(common) >= 2:
                got[("p", *sorted(common, key=qc.index))] = len(common)
        hits[pid] = got
    df = Counter(k for got in hits.values() for k in got)
    n, out = len(evid), []
    for pid, got in hits.items():
        score, words = 0.0, []
        for k, w in got.items():
            if n > 1 and df[k] == n:
                continue
            score += w / df[k]
            words.append(" ".join(written.get(s, s) for s in k[1:]))
        for _k, (_c, sc, said) in named.get(pid, {}).items():
            score += 2 * min(sc, 3)
            words.insert(0, said)
        once = {}
        for w in words:                                  # each word once, however it was written
            once.setdefault(_tok(w), w)
        out.append((round(score, 2), 0, pid, list(once.values())))
    return sorted([r for r in out if r[0] > 0], key=lambda r: (-r[0], r[2]))


def _unserved(q: dict, served: set) -> list[str]:
    """Products the question names that nothing here holds: a coined word no map, feed, pack or
    country carries, and the run after 'according to' when its words are not all held."""
    out = []
    for m in _SOURCE.finditer(q["text"]):
        run = re.sub(r"\s+(?:of|de|del|la|the)$", "", m.group(1).strip(" .,'’"))
        if run and not set(_content(_stems(run))) <= served:
            out.append(run)
    for w in dict.fromkeys(q["raw"]):
        if _coined(w) and not set(_stems(w)) <= served and not any(w in r for r in out):
            out.append(w)
    return out


def _served_words(choices_by_pack: dict, feeds: dict, known: dict) -> set:
    texts = []
    for pid, sp in packs.PACKS.items():
        texts += [packs.text(sp, k) for k in ("keywords", "use_for", "display_name")]
        texts += [str(s) for s in sp.get("subjects") or []]
        for rows in choices_by_pack[pid].values():
            for c in rows:
                texts += [*map(str, c.get("names") or []), str(c.get("words") or "")]
                texts += [str(v) for vs in (c.get("values") or {}).values() for v in vs or []]
    texts += [str(f.get(k) or "") for f in feeds.values() for k in ("title", "source", "description")]
    texts += list(known.values())
    out = {s for t in texts for s in _stems(t)}
    for by_key in choices_by_pack.values():
        out |= {_tok(_initials(n)) for rows in by_key.values() for c in rows
                for n in c.get("names") or [] if _initials(n)}
    return out


# --------------------------------------------------------------------------- years
def _years(text: str) -> dict:
    """The years a question asks about: two -> year and year_to; 'since/from Y' -> year=Y and
    year_to the latest mapped; one -> year; 'the 2010s' -> its first and last year; 'the past N
    years' -> N back from the latest. `said` says how each was read."""
    low = text.lower()
    dec = re.search(r"\b(?:the\s+)?((?:19|20)\d0)['’]?s\b", low)
    if dec:
        y = int(dec.group(1))
        return {"year": y, "year_to": y + 9, "said": {"year": f"the {y}s", "year_to": f"the {y}s"}}
    ys = [int(y) for y in re.findall(_YEAR, text)]
    if len(ys) >= 2:
        a, b = min(ys), max(ys)
        return {"year": a, "year_to": b, "said": {"year": f"{a} and {b}", "year_to": f"{a} and {b}"}}
    if ys:
        if re.search(rf"\b(?:since|from|after|starting(?:\s+in)?|desde|a\s+partir\s+de|depuis)\s+"
                     rf"(?:the\s+(?:end|start|beginning)\s+of\s+)?(?:el\s+|o\s+)?{ys[0]}", low):
            return {"year": ys[0], "year_to": "latest", "said": {"year": f"since {ys[0]}"}}
        return {"year": ys[0], "said": {"year": str(ys[0])}}
    m = re.search(r"\b(?:last|past)\s+(\d{1,2}|decade)\b", low)
    if m:
        back = 10 if m.group(1) == "decade" else int(m.group(1))
        said = "the past decade" if not m.group(1).isdigit() else f"the past {back} years"
        return {"back": back, "year_to": "latest", "said": {"year": said}}
    return {}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return v


def _year_source(picked, choices: dict, pc: dict, q: dict, want, pool=None) -> tuple:
    """(the choice whose years fill year and year_to, why): the named one; else, of those reaching
    the place (the named product's own), legend holding the asked classes, then holding the asked
    year, then most classes and years — the map the pack would read, never all maps' years."""
    if picked and picked.get("years"):
        return picked, "the map the question names"
    cands = [c for c in (pool or [c for rows in choices.values() for c in rows])
             if c.get("years") and _fits(c, pc)]
    cands = [c for c in cands if _hits(c, q)] or cands
    if want is not None:
        cands = [c for c in cands if want in c["years"]] or cands
    if not cands:
        return None, ""
    order = {id(c): i for i, c in enumerate(cands)}
    best = sorted(cands, key=lambda c: (-_hits(c, q), -_n_classes(c), -len(c["years"]), order[id(c)]))[0]
    why = ("the map whose legend holds the classes the question names" if _hits(best, q)
           else "the map with the most classes and years")
    return best, why + (" covering the place" if _reach(best, pc) else ", where nothing is known to cover the place")


def _years_into(args, how, gaps, yrs, source, why, optional, takes_to) -> None:
    """year and year_to into `args` from `source`'s years: 'latest' is its last; a year it does
    not map moves to the nearest one, said in `gaps`; a range given backwards is turned round."""
    years = sorted(source.get("years") or []) if source else []
    of = f"{source['value']} ({why})" if source else ""
    said = dict(yrs.get("said") or {})
    y, y2 = yrs.get("year"), yrs.get("year_to")
    latest = years[-1] if years else None
    if yrs.get("back") and latest is not None and not isinstance(y, int):
        y = min((v for v in years if v >= latest - yrs["back"]), default=None)
    if y2 == "latest":
        y2, said["year_to"] = latest, (f"the latest year {source['value']} maps" if source else "")
    if y is None and isinstance(y2, int) and years:
        early = [v for v in years if v < y2]
        if early:
            y, said["year"] = early[0], f"none asked: the earliest year of {of} before year_to"
    if y is None and latest is not None and not optional:
        y, said["year"] = latest, f"none asked: the latest year of {of}"
    for k in ("year", "year_to"):
        v = y if k == "year" else y2
        if isinstance(v, int) and years and v not in years:
            near = min(years, key=lambda x: (abs(x - v), x))
            gaps.append(f"{k}={v} is not a mapped year of {source['value']} ({packs.span(years)}): "
                        f"{near} is the nearest")
            y, y2 = (near, y2) if k == "year" else (y, near)
    if isinstance(y, int) and isinstance(y2, int) and y2 < y:
        gaps.append(f"the years were given latest first ({y}, {y2}): read as year={y2}, year_to={y}")
        y, y2 = y2, y
        said["year"], said["year_to"] = said.get("year_to", ""), said.get("year", "")
    if y is not None:
        args["year"], how["year"] = y, said.get("year") or ""
    if takes_to and y2 is not None and y2 != y:
        args["year_to"], how["year_to"] = y2, said.get("year_to") or said.get("year") or ""


# --------------------------------------------------------------------------- the calls
@functools.lru_cache(maxsize=1)
def _accepted() -> frozenset:
    """The parameters assemble_pack takes (assemble.assemble's own signature)."""
    from . import assemble
    return frozenset(inspect.signature(assemble.assemble).parameters)


def _what(c: dict) -> str:
    parts = [p for p in (str(c.get("product") or ""), ", ".join(c.get("countries") or []),
                         packs.span(c.get("years"))) if p and p != str(c["value"])]
    return f"{c['value']} ({'; '.join(parts)})" if parts else str(c["value"])


def _fill(pid: str, sp: dict, choices: dict, q: dict, ctx: dict, given: dict) -> dict:
    """The pack's target from the question: each key with `choices` from the choice the question
    names (reaching the place), the place, country and years from their own readers; what is
    left is a placeholder, said in `missing`."""
    keys = list(sp.get("target_keys") or ())
    optional = set(sp.get("optional_keys") or ())
    pc = ctx["pc"]
    args, how, other, missing, gaps = {"pack": pid}, {}, {}, [], []
    yrs = dict(q["years"], said=dict((q["years"] or {}).get("said") or {}))
    for k in ("year", "year_to"):
        v = given.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, int):
            gaps.append(f"{k}={v!r} is not a year: the question's own years are read instead")
            continue
        yrs[k], yrs["said"][k] = v, "as passed"
        if k == "year":
            yrs.pop("back", None)
    want = yrs.get("year") if isinstance(yrs.get("year"), int) else None
    chosen, pool = [], []
    for k, rows in choices.items():
        if k not in keys:
            continue
        fit, off = _pick(rows, q, pc, [y for y in (want, yrs.get("year_to")) if isinstance(y, int)])
        spread = {x for t in fit for x in t[0].get("countries") or []}
        if fit and not pc["ckeys"] and not pc["box"] and len(spread) > 1:
            pool += [t[0] for t in fit]
            other[k] = [_what(t[0]) for t in fit[:_SHOWN]] + (
                [f"and {len(fit) - _SHOWN} more"] if len(fit) > _SHOWN else [])
            how[k] = (f"the question names {fit[0][2]}, mapped for {', '.join(sorted(spread))}, and no "
                      f"place or country that says which: pass {k}= one of other_choices")
        elif fit:
            c, _sc, said, _i = fit[0]
            args[k] = c["value"]
            chosen.append(c)
            how[k] = f"the question names {said}: {_what(c)}"
            if len(fit) > 1:
                other[k] = [_what(t[0]) for t in fit[1:1 + _SHOWN]] + (
                    [f"and {len(fit) - 1 - _SHOWN} more"] if len(fit) - 1 > _SHOWN else [])
        elif off:
            said = off[0][2]
            where = sorted({x for t in off for x in t[0].get("countries") or []})
            gaps.append(f"{said} is mapped here for {', '.join(where) or 'other areas'}, not for "
                        f"{ctx.get('place') or ', '.join(ctx['countries']) or 'this place'}: no {k} "
                        "of it covers the place, so none is named")
    if "year" in keys:
        picked = next((c for c in chosen if c.get("years")), None)
        source, why = _year_source(picked, choices, pc, q, want, pool)
        _years_into(args, how, gaps, yrs, source, why, "year" in optional, "year_to" in keys)
    for k in ("place", "country"):
        if k in keys and k not in args and ctx.get(k):
            args[k], how[k] = ctx[k], ctx[f"{k}_how"]
    year = args.get("year") if isinstance(args.get("year"), int) else None
    for c in chosen or [c for rows in choices.values() for c in rows
                        if c.get("values") and _reach(c, pc) is True
                        and (year is None or year in (c.get("years") or []))]:
        for k, vs in (c.get("values") or {}).items():
            hit = [lab for lab in vs or [] if _has(q["stems"], _stems(lab))]
            if k in keys and hit:
                seen = {str(x).casefold() for x in args.get(k) or []}
                args[k] = list(args.get(k) or []) + [h for h in hit if h.casefold() not in seen]
                how[k] = (f"named in the question and in {c['value']}'s legend" if chosen else
                          "named in the question and in the legend of a map covering the place")
    for k in keys:
        if k not in _accepted():
            args.pop(k, None)
            missing.append(f"{k}: the pack's target takes it, but assemble_pack has no such "
                           "parameter, so this pack cannot be reached through assemble_pack yet")
        elif k not in args and k not in optional:
            doc = (sp.get("target_doc") or {}).get(k) or k
            vals = [str(c["value"]) for c in choices.get(k) or []]
            args[k] = f"<the {k} asked about: {doc}>"
            missing.append(f"{k}: the question names none" + (
                f" — one of {budget.names(vals, more='more')}" if vals else ""))
    args = {"pack": pid, **{k: args[k] for k in keys if k in args}}       # as the pack declares them
    return {"args": args, "how": how, "other": other, "missing": missing, "gaps": gaps}


def _related(q: dict, ctx: dict, pid: str, said: list[str], feeds: dict) -> list[dict]:
    """Approved methods computing from a related feed first, then the feeds the question's coined
    words, products, subjects or countries meet (none of another country), each with what its rows
    are; the pack's document library when it holds any."""
    from ..graph.geo import vectors
    from . import loop
    coined = list(dict.fromkeys([w for w in q["raw"] if _coined(w)][:20]
                                + [s for s in said if _form(s) == "proper" or len(_stems(s)) >= 2]))
    subj = set(_content(q["stems"])) - _FRAME - set(ctx["country_stems"])
    ckeys, known = ctx["pc"]["ckeys"], ctx["known"]
    scored = []
    for ds, spec in feeds.items():
        text = " ".join(str(spec.get(k) or "") for k in ("title", "source", "description"))
        fst = _stems(text)
        fc = {vectors.country_key(_key(c)) for c in spec.get("countries") or []} | {
            vectors.country_key(_key(c)) for c in _countries_in(_key(text).split(), known)}
        if ckeys and fc and not fc & ckeys:
            continue
        score = 3 * sum(1 for w in coined if _has(fst, _stems(w)))
        score += min(2, len(subj & set(_stems(f"{spec.get('title') or ''} {spec.get('source') or ''}"))))
        score += 1 if ckeys & fc else 0
        if score >= 3:
            scored.append((-(score + (1 if spec.get("pack") == pid else 0)), ds, spec))
    feeds_out, methods = [], []
    for _s, ds, spec in sorted(scored, key=lambda t: (t[0], t[1]))[:_SHOWN]:
        for m in loop.methods_reading(ds):
            call = (m.get("get_its_result") or [{}])[0]
            methods.append({"tool": "assemble_pack", "args": call.get("args") or {}, "method": m["method"],
                            "title": m.get("title"),
                            "what": (f"an approved method computing {', '.join(m.get('computes') or {})} "
                                     f"from {ds}: assemble_pack returns its cited result — use it before "
                                     "reading the rows")})
        feeds_out.append({"tool": "feeds_query", "args": {"dataset": ds}, "title": spec.get("title") or ds,
                          "what": (f"{_rows_are(spec)}, not maps: not for an area's class areas, change "
                                   "or exposure — assemble_pack measures those; no receipt of its own")})
    out = methods[:_SHOWN] + feeds_out
    sp = packs.PACKS.get(pid) or {}
    if sp.get("corpus") and packs.corpus_state(str(sp["corpus"])).get("status") == "available":
        out.append({"tool": "corpus_search", "args": {"query": _echo(q["text"]), "pack": pid},
                    "what": "what the reports in its library say, each passage with its provenance: "
                            "context the pack cites, not figures for the area"})
    return out


def _unrouted() -> list[str]:
    """Packs that declare no routing words: named, never skipped."""
    out = []
    for pid, sp in sorted(packs.PACKS.items()):
        if any(packs.text(sp, k) for k in ("keywords", "use_for")) or sp.get("subjects") \
                or sp.get("choices") or len(_content(_stems(str(sp.get("display_name") or "")))) >= 2:
            continue
        notes = packs.text(sp, "usage_notes")            # read last: a hook may be costly
        if not notes or "todo" in notes.casefold():
            out.append(f"{pid}: declares no routing words (keywords, use_for, choices or usage_notes), "
                       f"so no question routes to it by itself; name it with pack='{pid}' and its "
                       f"target ({', '.join(sp.get('target_keys') or ())})")
    return out


def rules() -> list[str]:
    """The core rules, short, in the instructions' priority order."""
    return [instructions.SHORT[s["key"]] for s in instructions.SECTIONS if s["key"] in instructions.SHORT]


def _echo(question: str) -> str:
    """The question as a next call repeats it: whole when short, else a pointer to `question`."""
    return question if len(question) <= _ECHO else "<the person's question, verbatim (`question` above)>"


def _quoted(question: str) -> str:
    return question if len(question) <= _QUOTED else (
        question[:_QUOTED] + f" …[{len(question) - _QUOTED:,} more chars: pass the whole question]")


def route(question: str, place: str | None = None, year=None, year_to=None,
          pack: str | None = None) -> dict:
    """start_here's reply: the route, the next calls with their arguments, related lookups with
    what they are, what is not served, and the rules."""
    with budget.snapshot():
        return _route(str(question or "").strip(), place, year, year_to, pack)


def _not_covered(question: str, note: str, gaps: list, extra: dict) -> dict:
    """A reply that sends no call: what is missing, the contribute pointer, the coverage check."""
    from . import loop
    out = {"status": "not_covered", "question": _quoted(question), "note": note}
    if gaps:
        out["gaps"] = gaps
    out["not_served"] = loop.not_served(gaps or question)
    out["next_calls"] = [{"tool": "platform_capabilities", "args": {"section": "coverage"},
                          "why": "every pack, feed, publisher and layer by name: check the subject "
                                 "is not there under another name before asking"}]
    out.update({k: v for k, v in extra.items() if v})
    out["rules"] = rules()
    return out


def _route(question: str, place, year, year_to, pack) -> dict:
    from ..graph.geo import vectors
    from . import loop
    if not question:
        return {"status": "declined", "note": "start_here needs the question, in the person's words"}
    if pack and pack not in packs.PACKS:
        return {"status": "declined", "note": f"unknown pack {pack!r} — one of: {', '.join(sorted(packs.PACKS))}"}
    q = _q(question)
    q["years"] = _years(question)
    feeds = _feeds()
    choices = {pid: _choices(sp) for pid, sp in packs.PACKS.items()}
    evid = {pid: _evidence(sp, choices[pid]) for pid, sp in packs.PACKS.items()}
    known = _countries(choices)
    served = _served_words(choices, feeds, known)
    q["served"] = _served_words(choices, {}, {})          # what the maps and packs carry
    skip = _skip(evid, choices)
    ctx = {"known": known, **_area_of(q, place, known, skip)}
    ctx["country_stems"] = [s for c in ctx["countries"] for s in _stems(c)]
    if ctx["countries"]:
        ctx["country"], ctx["country_how"] = ctx["countries"][0], "named in the question"
    ctx["pc"] = pc = _place_ctx(ctx["countries"], ctx["box"])
    named = {}
    for pid in packs.PACKS:
        for k, rows in choices[pid].items():
            fit, off = _pick(rows, q, pc, [])
            best = (fit or off or [None])[0]
            # a choice named only by a country is a place, not a subject
            if best and not all(vectors.country_key(_key(n)) in known for n in best[0].get("names") or []):
                named.setdefault(pid, {})[k] = (best[0], best[1], best[2])
    ranked = _score_packs(q, evid, named, set(ctx["country_stems"]))
    if pack:
        ranked = [r for r in ranked if r[2] == pack] or [(0, 0, pack, [])]
    unrouted = _unrouted()
    unserved = _unserved(q, served)
    if not ranked:
        held = sorted(pid for pid in packs.PACKS if _coverage(choices[pid], pc) is True)
        weak = {}
        for pid, sp in packs.PACKS.items():
            vocab = _vocab(sp, choices[pid], feeds, pid)
            hit = [w for w in dict.fromkeys(q["words"]) if _stem(w) in vocab
                   and _stem(w) not in ctx["country_stems"] and w not in _STOP]
            if hit:
                weak[pid] = hit[:_SHOWN]
        note = ("Nothing this platform serves is what the question asks about: no pack's subject, map "
                "product, hazard or crop is named in it."
                + (f" It holds data for {', '.join(ctx['countries'])} in: {', '.join(held)}." if held else "")
                + (" Words it shares with a pack (weak_matches) are not its subject: no route."
                   if weak else ""))
        return _not_covered(question, note, [f"{u} is not served here" for u in unserved[:_SHOWN]],
                            {"weak_matches": weak, "packs_without_routing_words": unrouted})
    top = ranked[0][2]
    sp = packs.PACKS[top]
    route_ = [{"pack": pid, "matched": words_[:8], "answers": packs.text(packs.PACKS[pid], "use_for")
               or packs.text(packs.PACKS[pid], "summary") or f"servirplatform://pack/{pid}"}
              for s_, _p, pid, words_ in ranked[:3] if pid == top or s_ >= max(2.0, ranked[0][0] / 3)]
    covered = _coverage(choices[top], pc)
    where = ctx.get("place") or ", ".join(ctx["countries"])
    if covered is False:
        reach = _where([c for rows in choices[top].values() for c in rows])
        gap = f"nothing the {top} pack holds covers {where}: what it has reaches {reach}"
        return _not_covered(
            question, (f"The {top} pack answers questions like this, but none of its maps or records "
                       f"reaches {where}. Say so; do not answer from another place or the web."),
            [gap] + [f"{u} is not served here" for u in unserved[:_SHOWN]],
            {"route": route_, "packs_without_routing_words": unrouted})
    given = {"year": _int(year), "year_to": _int(year_to)}
    filled = _fill(top, sp, choices[top], q, ctx, given)
    gaps = [f"{u} is not served here: no map, feed or document library of it is registered; the "
            "calls below read what is, so say that first" for u in unserved[:_SHOWN]] + filled["gaps"]
    geo = [c for rows in choices[top].values() for c in rows if c.get("countries") or c.get("bbox")]
    if covered is None and where and geo:
        gaps.append(f"whether the {top} pack covers {where} is not known here (what it has reaches "
                    f"{_where(geo)}): resolve_place_time places it, and assemble_pack declines a place "
                    "outside it")
    args = dict(filled["args"])
    args["focus"] = _echo(question)
    calls = []
    if args.get("place") and not str(args["place"]).startswith("<"):
        calls.append({"tool": "resolve_place_time", "args": {"place": args["place"]},
                      "why": "optional: the area the platform will analyse (boundary or box, km²)"})
    calls.append({"tool": "assemble_pack", "args": args,
                  "why": "computes and cites the evidence; returns the pack_id"})
    calls.append({"tool": "publish_answer",
                  "args": {"pack_id": "<the pack_id assemble_pack returns>",
                           "draft": "<your brief from the pack, citing [n]>", "question": _echo(question)},
                  "why": "gates the draft and mints the receipt: show the brief only with it"})
    said = [v[2] for v in named.get(top, {}).values()]
    lead = ("The question names something this platform does not serve (gaps): say so first. "
            if unserved or any("not for" in g for g in filled["gaps"]) else "")
    out = {"status": "ok", "question": _quoted(question),
           "note": (f"Route: {top}. {lead}Make the calls in next_calls in order (arguments filled "
                    "from the question; check `filled_from`), draft only from the pack, and show the "
                    "brief with its receipt."),
           "route": route_, "next_calls": calls, "filled_from": filled["how"]}
    if filled["other"]:
        out["other_choices"] = filled["other"]
    if filled["missing"]:
        out["missing"] = filled["missing"]
    if gaps:
        real = [g for g in gaps if not g.startswith("whether ")]      # not knowing is not a gap to fill
        out["gaps"] = gaps + ([loop.not_served(real)] if real else [])
    if ctx["notes"]:
        out["also"] = ctx["notes"]
    related = _related(q, ctx, top, said, feeds)
    if related:
        out["related_lookups"] = related
    if unrouted:
        out["packs_without_routing_words"] = unrouted
    out["rules"] = rules()
    out["your_next_output"] = (f"Call {_render('assemble_pack', {k: v for k, v in args.items() if k != 'focus'})}"
                               " (with focus= the question); draft from the pack; finish with publish_answer.")
    return out


# --------------------------------------------------------------------------- lookups
def _lookup_text(result: dict) -> tuple[str, list[str], str]:
    """(the words a lookup's rows come with, the countries they declare, what the rows are): a
    feed's own title, source and description; a search's passports, never the caller's query."""
    ds = result.get("dataset")
    if ds:
        from . import registry
        spec = registry.FEEDS.get(ds)
        if spec is None:
            try:
                from ..contrib import staging
                spec = staging.visible_staged_feed(ds)
            except Exception:
                spec = None
        if not spec:
            return "", [], ""
        text = " | ".join(str(spec.get(k) or "") for k in ("title", "source", "description"))
        return text, list(spec.get("countries") or []), _rows_are(spec, result.get("records"))
    hits = result.get("hits")
    if isinstance(hits, list) and hits:
        ps = [h.get("passport") or {} for h in hits if isinstance(h, dict)]
        said = [f"{p.get('title') or ''} | {p.get('source') or ''}" for p in ps]
        cs = [c for p in ps for c in (p.get("countries") or []) if isinstance(p.get("countries"), list)]
        return " | ".join(said), cs, "document passages"
    return "", [], ""


def steer(result: dict) -> str | None:
    """One line for a lookup whose rows are of a product a pack measures from its maps, for the
    country they are of: the pack, the assemble_pack call to make first and what the rows are,
    within budget.NOTE_CHARS; None when nothing matches."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    with budget.snapshot():
        text, declared, rows_are = _lookup_text(result)
        if not text.strip():
            return None
        return _steer(text, declared, rows_are)


def _steer(text: str, declared: list, rows_are: str) -> str | None:
    q = _q(text)
    choices = {pid: _choices(sp) for pid, sp in packs.PACKS.items()}
    known = _countries(choices)
    q["guard"] = False             # a dataset's title is not a question naming another product
    said_c = list(dict.fromkeys([*declared, *_countries_in(q["words"], known)]))
    pc = _place_ctx(said_c)
    groups: dict = {}
    for pid, by_key in choices.items():
        for k, rows in by_key.items():
            fams = _families(rows)
            for i, c in enumerate(rows):
                sc, name = _named_by(c, q, None, fams)
                if sc and _form(name) == "proper" and not (pc["ckeys"] and _reach(c, pc) is not True):
                    groups.setdefault((pid, k, name), []).append((i, c))
    if not groups:
        return None
    (pid, k, name), members = max(groups.items(), key=lambda g: (len(g[1]), -min(i for i, _c in g[1])))
    sp = packs.PACKS[pid]
    optional = set(sp.get("optional_keys") or ())
    where = sorted({x for _i, m in members for x in m.get("countries") or []})
    one = (sorted(members, key=lambda t: (-_n_classes(t[1]), -len(t[1].get("years") or []), t[0]))[0][1]
           if pc["ckeys"] or len(where) <= 1 else None)
    years = sorted({y for _i, m in members for y in m.get("years") or []}) if one is None else one.get("years") or []
    args = {"pack": pid}
    for key in sp.get("target_keys") or ():
        if key == k:
            continue
        if key == "year" and years:
            args[key] = f"<{packs.span(years)}>"
        elif key == "year_to" and len(years) > 1:
            args[key] = "<a later year>"
        elif key not in optional:
            args[key] = f"<the {key} asked about>"
    args[k] = one["value"] if one else f"<the {name} {k} for the place>"
    if pc["ckeys"]:
        where = [x for x in where if x in said_c] or where
    point = "sample" in rows_are or "point" in rows_are

    def line() -> str:
        places = budget.names(where, rank=budget.PLACE, more="more countries")
        return (f"{name} maps{' for ' + places if places else ''} are measured by the {pid} pack: for an "
                f"area's figures from these maps, call {_render('assemble_pack', args)} first. These rows "
                f"are {rows_are}: no receipt, never cited in a brief"
                + ("; a sample showing none is not the maps' answer." if point else "."))
    return budget.fit("the measured-by line", line, budget.NOTE_CHARS)


# --------------------------------------------------------------------------- the tool's texts
def describe() -> str:
    """start_here's description: what it answers and what not first, then every routing word the
    packs declare (registry.routing_words), the feeds and publishers ranked first to go."""
    from . import registry
    sources = registry.sources_line()
    return (
        "START HERE — WHICH TOOL TO CALL, WITH WHICH ARGUMENTS, AND THE RULES. Not for the answer "
        "itself (assemble_pack computes it, publish_answer receipts it), a dataset's rows "
        "(feeds_query) or documents (corpus_search). Call this first for any question about "
        + registry.routing_words() + " — even when asked 'according to' a map product named here; "
        "it returns which tool to call with which arguments and the rules: the domain pack the "
        "question's words route to, the exact next calls with their arguments filled "
        "(resolve_place_time, assemble_pack, publish_answer), related lookups and what they are (a "
        "dataset's sample points are not its maps), and what is not served (ask the person for the "
        "source). Use it INSTEAD OF A WEB SEARCH for these subjects.\n\n"
        + (f"{sources}\n\n" if sources else "")
        + "Deterministic (no LLM runs here), read from the live registries: a stack, feed, "
        "boundary or pack contributed a minute ago routes with no code change. Returns: {status, "
        "question, note, route, next_calls, filled_from, other_choices, missing, gaps, "
        "related_lookups, rules, your_next_output}; status \"not_covered\" (no subject it serves, "
        "or no map of it reaching the place) -> `not_served` says what to ask the person for.")


def describe_params() -> dict:
    """start_here's parameters as its input schema describes them."""
    from . import registry
    pids = ", ".join(f"`{pid}`" for pid in sorted(packs.PACKS))
    return {
        "question": ("The person's question, verbatim: its words pick the domain pack, a named map "
                     "product's stack or a named hazard or crop, the place (a contributed "
                     "boundary's feature name, else a place or country it names) and the years "
                     "('since 2020': year=2020, year_to=the latest mapped)."),
        "place": ("The place the question is about, when you can tell (a district, park, reserve "
                  "or country); a contributed boundary's name in the question is found without "
                  "it. Not geocoded here: resolve_place_time does that."),
        "year": "A year the question asks about, when its words do not say it.",
        "year_to": "A later year the question compares with, when its words do not say it.",
        "pack": (f"Route to this pack only: {pids} (their targets: assemble_pack's DOMAIN PACKS). Omit "
                 f"it and the question's words pick; {registry.COVERAGE_CALL} names every subject each "
                 "pack covers."),
    }
