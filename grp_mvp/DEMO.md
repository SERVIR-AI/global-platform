# Demo queries

A running list of queries to demo the agent. Copy-paste any line. Grouped by what each
one *shows* — append new ones under the right section as we go.

> First query for a new city fetches its OSM (~30–60 s) and downloads a hazard tif once
> (~50 MB), then caches both — everything after is instant. `✓` = verified working.
> `--check` needs no API key.

```
python -m grp_mvp.run "<your question>"
```

---

## Verbose mode — narrate how it works (`-v`)

Add `-v` to any query to print a plain-text trace of every step: question → routing →
**boundary decision** → OSM exposure → hazard raster → overlay → answer.

```
python -m grp_mvp.run -v "how many km of road are flooded in Battambang, all severities"   # ✓
```

The **boundary decision** is the highlight — it shows which of three paths was taken:
- **clean resolve** — found an admin boundary under 1,500 km² → use the polygon (e.g. Siem Reap)
- **box fallback** — no boundary small enough → 12 km box around the center (e.g. Sihanoukville)
- **typo recovery** — typed name has no boundary → re-query the corrected name (e.g. Batambang → Battambang)

A fresh (uncached) city shows the full live trail; a cached city shows the saved decision.
*Note: the LLM sometimes fixes a famous typo itself before the boundary code sees it, so
the typo-recovery path shows most reliably on lesser-known places — or run it directly:*

```
python -c "from grp_mvp import ingest, narrate; narrate.enable(); ingest._boundary('Batambang')"   # ✓ shows the re-query
```

---

## 0. The flagship — road exposure + independent validation

```
python -m grp_mvp.run "how many km of road are flooded in Battambang?"          # ✓
python -m grp_mvp.run --check "Battambang"                                      # ✓ cross-check, 1.5% apart
```
*Shows: the core overlay (OSM roads × flood raster), per-severity breakdown, grounding,
and that the number survives an independent vector-clip check.*

## 1. The four assets — all from OpenStreetMap

```
python -m grp_mvp.run "how many km of road are flooded in Battambang?"          # ✓ roads = length
python -m grp_mvp.run "how many hospitals are in the flood zone in Battambang?" # ✓ count
python -m grp_mvp.run "how many schools are flooded in Battambang?"             # ✓ count
python -m grp_mvp.run "how many buildings are exposed to flooding in Battambang?" # ✓ count
```
*Shows: roads are measured (km), hospitals/schools/buildings are counted — all live from OSM.*

## 2. The nine hazards — all from the ADPC tifs

```
python -m grp_mvp.run "km of road affected by flood in Battambang"              # ✓
python -m grp_mvp.run "km of road affected by a flashflood in Battambang"
python -m grp_mvp.run "km of road affected by drought in Battambang"
python -m grp_mvp.run "km of road affected by wildfire in Battambang"
python -m grp_mvp.run "km of road affected by landslides in Battambang"
python -m grp_mvp.run "km of road affected by a cyclone in Battambang"
python -m grp_mvp.run "km of road affected by a storm in Battambang"
python -m grp_mvp.run "km of road exposed to a tsunami in Sihanoukville"        # ✓ coastal
python -m grp_mvp.run "how many buildings are exposed to an earthquake in Battambang"  # ✓
```
*Shows: each hazard pulls its own severity raster by Drive id; flood/cyclone/earthquake
have physical legends, the rest a generic 1–5.*

## 3. Severity control

```
python -m grp_mvp.run "km of road under severe flooding in Battambang"          # qualitative -> high class
python -m grp_mvp.run "km of road in flood severity class 4 or above in Battambang"  # explicit class
python -m grp_mvp.run "km of road flooded in Battambang, all severities"        # ✓ full breakdown
python -m grp_mvp.run "how many km of road are flooded in Battambang?"          # ✓ no severity -> it ASKS and waits
```
*Shows: it reads "severe" → a class, takes an explicit class, gives the full per-class
table on "all", and — when severity is omitted — stops and asks rather than guessing.*

## 4. Place resolution — boundaries, fallbacks, typos

```
python -m grp_mvp.run "km of road flooded in Phnom Penh"                        # admin polygon (685 km²)
python -m grp_mvp.run "km of road exposed to tsunami in Sihanoukville"          # ✓ radius-box fallback (no small boundary)
python -m grp_mvp.run "km of road flooded in Batambang"                         # ✓ typo -> recovers Battambang's polygon
python -m grp_mvp.run "km of road flooded in Phnom Pen"                         # typo -> recovers Phnom Penh
python -m grp_mvp.run "km of road flooded in London"                            # outside SE Asia -> honest error at clip
python -m grp_mvp.run "km of road flooded in Asdfqwertyville"                   # ✓ unknown place -> honest error
```
*Shows: clean admin boundary when there is one; a 12 km box when a place only has an
oversized province; typo recovery via a canonical re-query; and honest errors otherwise.*

## 5. "It's real, not mocked" — the coastal contrast

```
python -m grp_mvp.run "km of road flooded in Sihanoukville"                     # ✓ 0.0 km — no river basin
python -m grp_mvp.run "km of road exposed to a tsunami in Sihanoukville"        # ✓ 1,122 km — whole coast
```
*Shows: same place, same box, two hazards → two genuinely different answers (flood 0 /
tsunami 1,122). The zero is real (riverine flood layer), not an empty clip.*

## 6. Honest refusals — things we knowingly don't have

```
python -m grp_mvp.run "how many people are exposed to flooding in Battambang?"  # population (WorldPop) not wired
python -m grp_mvp.run "how many homeless people are in the flood zone in Battambang?"  # no such layer
python -m grp_mvp.run "how many km of road are affected by extreme heat in Battambang?"  # no heat tif
```
*Shows: it refuses cleanly and says why, instead of inventing a number.*

## 7. Validation

```
python -m grp_mvp.run --check "Battambang"                                      # ✓ 1.5% apart
python -m grp_mvp.run --check "Phnom Penh"                                       # cross-check another city
```
*Shows: raster-sampling vs an independent vector clip agree — the overlay is self-consistent.*
