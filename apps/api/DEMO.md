# Demo queries (curl)

Exhaustive manual tests against the running API. Start it first:

```bash
uv run uvicorn app.main:app --reload --app-dir apps/api/src   # from repo root
```

All requests are `POST /api/chat` with `{"messages":[{"role":"user","content":"…"}]}`.
The examples pipe through `jq -r '.message.content'` to show just the answer; drop the
`jq` to see the full envelope (`provider`, `model`, `usage`, `thread_id`). A `provider`
field is optional — without it the server uses `DEFAULT_PROVIDER` from `.env`.

> First query for a new place fetches OSM (~30–60 s) and downloads the hazard tif once;
> after that it's instant. `✓` = verified end-to-end during the build.

A tiny helper to keep the lines short:

```bash
ask() { curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
        -d "{\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}" | jq -r '.message.content'; }
```

---

## 0. Health + smoke

```bash
curl -s localhost:8000/api/health | jq
ask "how many km of road are flooded in Battambang?"            # ✓
```

## 1. The four assets (all from OSM)

```bash
ask "how many km of road are flooded in Battambang?"            # ✓ roads = length
ask "how many hospitals are in the flood zone in Battambang?"   # count
ask "how many schools are flooded in Battambang?"               # count
ask "how many buildings are exposed to flooding in Battambang?" # ✓ count (98)
```

## 2. The nine hazards (each pulls its own ADPC raster)

```bash
ask "km of road flooded in Battambang"                          # ✓
ask "km of road affected by a flashflood in Battambang"
ask "km of road affected by drought in Battambang"
ask "km of road affected by wildfire in Battambang"
ask "km of road affected by landslides in Battambang"
ask "km of road at risk from a cyclone in Battambang"           # ✓ (class 2, km/h legend)
ask "km of road affected by a storm in Battambang"
ask "km of road exposed to a tsunami in Sihanoukville"          # coastal
ask "how many buildings are exposed to an earthquake in Battambang"  # ✓ (all class 1, PGA legend)
```

## 3. Severity control

```bash
ask "km of road flooded in Battambang"                          # ✓ no severity -> full per-class breakdown
ask "km of road in flood severity class 4 or above in Battambang"   # explicit threshold
ask "how many buildings are in severe flooding in Battambang"   # qualitative -> high class
```

## 4. Place resolution — boundary, box fallback, typo, errors

```bash
ask "km of road flooded in Phnom Penh"                          # admin polygon (685 km²)
ask "km of road flooded in Sihanoukville"                       # ✓ radius-box fallback (no small boundary)
ask "km of road flooded in Batambang"                           # ✓ typo -> recovers Battambang
ask "km of road flooded in London"                              # outside SE Asia -> honest error
ask "km of road flooded in Asdfqwertyville"                     # ✓ unknown place -> honest error
```

## 5. "It's real, not mocked" — the coastal contrast

```bash
ask "km of road flooded in Sihanoukville"                       # ✓ 0.0 km — no river basin
ask "km of road exposed to a tsunami in Sihanoukville"          # non-zero — the real coastal threat
```

## 6. Honest refusals — layers we knowingly don't have

```bash
ask "how many people are exposed to flooding in Battambang?"    # population (WorldPop) not wired
ask "how many homeless people are in the flood zone in Battambang?"  # no such layer
ask "how many km of road are affected by extreme heat in Battambang?"  # no heat hazard
```

## 7. Multi-turn (memory by thread_id)

The checkpointer remembers history for a `thread_id`; send only the new turn each time.

```bash
TID=demo-$(date +%s)
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"km of road flooded in Battambang?\"}],\"thread_id\":\"$TID\"}" \
  | jq -r '.message.content'
# follow-up on the same thread — "there" resolves to Battambang from history
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d "{\"messages\":[{\"role\":\"user\",\"content\":\"and how many schools are flooded there?\"}],\"thread_id\":\"$TID\"}" \
  | jq -r '.message.content'
```

## 8. Full envelope + provider override

```bash
# see provider / model / token usage / thread_id, not just the text
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"km of road flooded in Battambang?"}],"provider":"claude"}' | jq
```

## 9. Verbose — the step-by-step trace (how the answer was produced)

Add `"verbose": true` to get a `trace` array narrating route → boundary → exposure →
overlay (the CLI's `-v` output). Omit it and `trace` is null.

```bash
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"how many buildings are exposed to flooding in Battambang?"}],"verbose":true}' \
  | jq '{answer: .message.content, trace}'
```

The `boundary` line shows the resolution decision — `admin boundary ~115 km²`,
`… (corrected 'Batambang' -> 'Battambang')`, or `12 km radius box (no admin boundary under cap)`.
