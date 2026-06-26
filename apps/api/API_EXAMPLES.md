# Frontend API — working examples (real, not mocked)

The frontend uses two endpoints: **`POST /api/chat`** (the question/answer flow, below) and
**`POST /api/tiffs`** (bring-your-own-data upload, at the end). Every request body below is a
**verified working request** (returned HTTP 200 with a real answer against the live backend).
Full sample responses are saved as `e2e_siemreap_landslide.json` and
`e2e_polygon_mode2.json` at the repo root.

> Everything spatial is **EPSG:4326** (lon/lat). The map is 3857 → reproject:
> vectors with `dataProjection:'EPSG:4326', featureProjection:'EPSG:3857'`; the raster
> with `ol/source/GeoTIFF`.

---

## Request schema

| Field | Type | Notes |
|---|---|---|
| `messages` | `[{role, content}]` | **required** — the chat turn(s). Existing field; unchanged. |
| `geometry` | GeoJSON Polygon **or** `[minLon,minLat,maxLon,maxLat]` **or** `null` | Mode 2: the drawn AOI. When set, used instead of resolving a place from text. |
| `hazard` | string or `null` | optional explicit hazard (`"flood"`, `"landslide"`, `"hazard_flood"`…), e.g. from a UI button. Else inferred from text. |
| `thread_id` | string or `null` | optional; continues a conversation (server-side memory). |
| `verbose` | bool (default `false`) | when `true`, response includes `trace` (step-by-step narration). |
| `provider` / `model` | string or `null` | optional overrides. |

---

## Mode 1 — text only

The place is resolved from the text (Nominatim). **Real working request:**

```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "how many km of road are at risk from landslides in Siem Reap?"}]
}'
```
→ `place: "Siem Reap"`, `aoi.source: "nominatim"`, `metric.value: 2199.4 km`, 10281 road `features`.
(Full response: `e2e_siemreap_landslide.json`.)

Other verified text requests:
```jsonc
{ "messages": [{"role":"user","content":"how many km of road are flooded in Battambang?"}] }
{ "messages": [{"role":"user","content":"how many buildings are exposed to an earthquake in Battambang?"}] }
{ "messages": [{"role":"user","content":"how many hospitals are in the flood zone in Battambang?"}] }
```

## Mode 2 — drawn AOI (geometry)

The drawn shape is the area; **no place needed**. `hazard` can come from a button or the text.

**Verified — bbox array form:**
```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "how many km of road are flooded here?"}],
  "geometry": [103.18, 13.08, 103.22, 13.12],
  "hazard": "flood"
}'
```
→ `place: "drawn area"`, `aoi.source: "drawn"`, `metric.value: 14.3 km`, 1802 road `features`.

**Verified — GeoJSON Polygon form:**
```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "how many buildings are at risk of flooding here?"}],
  "geometry": {"type":"Polygon","coordinates":[[[103.18,13.08],[103.22,13.08],[103.22,13.12],[103.18,13.12],[103.18,13.08]]]},
  "hazard": "flood"
}'
```
→ `place: "drawn area"`, `aoi.source: "drawn"`, 5 of 770 buildings flooded.
(Full response: `e2e_polygon_mode2.json`.)

> Transform the drawn shape to 4326 before sending:
> `JSON.parse(new GeoJSON().writeGeometry(geom.clone().transform('EPSG:3857','EPSG:4326')))`

---

## Response — every field and what to do with it

All existing fields (`message`, `usage`, `provider`, `model`, `thread_id`, `id`,
`created_at`) are unchanged. The geo fields below are **added** and present whenever the
answer is a spatial result (null otherwise).

| Field | Example (real) | Frontend use |
|---|---|---|
| `message.content` | "In Siem Reap, **2199.4 km** of road…" | the chat answer (markdown) |
| `place` / `hazard` / `layer` | `"Siem Reap"` / `"hazard_landslides"` / `"roads"` | labels |
| `metric` | `{value:2199.4, unit:"km", total:2199.5, min_severity:1, by_severity:{1:2196.1,…}}` | headline + per-class chart |
| `legend` | `{"3":{"label":"Moderate","color":"#fd8d3c"}, …}` | **color** the features, the raster ramp, and the legend box |
| `bounds` | `[103.696, 13.103, 103.924, 13.468]` | `map.getView().fit(bounds)` (after transform) |
| `aoi` | GeoJSON Feature, `properties.source: "nominatim"\|"drawn"\|"radius_box"` | draw the AOI outline |
| `features` | FeatureCollection — each `properties.severity` 0–5 | the assets, **colored by severity** |
| `hazard_layer.raster_url` | `"/api/raster/siem-reap/hazard_landslides.tif"` | `ol/source/GeoTIFF` (option A) |
| `hazard_layer.geojson` | FeatureCollection — polygons per class | a `VectorLayer` (option B) |
| `hazard_layer.crs` | `"EPSG:4326"` | source projection |
| `trace` | (only if `verbose:true`) step narration | optional "how it worked" panel |
| `choices` | (when the agent asks) `[{label, value}, …]` | render as buttons; send `value` back to resume (see below) |

### Fetching the raster (option A)
```bash
curl -s "localhost:8001/api/raster/siem-reap/hazard_landslides.tif" -o clip.tif
# -> HTTP 200, image/tiff (EPSG:4326). Returns 404 if that place/hazard wasn't queried yet.
```

---

## The exposure / risk choice (human-in-the-loop)

A hazard question doesn't compute immediately — the agent first asks **how** to answer it
and returns a `choices` array instead of a metric:

```jsonc
{ "messages": [{"role":"user","content":"schools at risk of flooding in Battambang"}] }
// -> message.content asks the question; choices: [
//      {"label":"Exposure — what sits in the hazard zone", "value":"1"},
//      {"label":"Risk — official precomputed",             "value":"2"},
//      {"label":"Risk — recompute from layers",            "value":"3"} ]
//    metric / features / legend = null (nothing computed yet)
```

Render `choices` as buttons. When the user picks one, send its `value` as the next message
on the **same `thread_id`** to resume and get the spatial result:

```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' -d '{
  "messages": [{"role": "user", "content": "2"}],
  "thread_id": "<thread_id from the previous response>"
}'
```

Only the paths whose data exists are offered (a hazard with no precomputed risk shows two
buttons, not three); a hazard with no data at all returns a plain refusal and no `choices`.

---

## Available values
- **Hazards** (`hazard` field, or inferred): flood · flashflood · drought · fire · landslide · cyclone · storm · tsunami · earthquake
- **Assets** (`layer`): roads (km) · hospitals · schools · buildings (counts)
- **Severity**: class 1 (low) – 5 (high); omit a severity → full `by_severity` breakdown.

## Refusals (graceful)
Things we don't have return a plain message, no geo fields:
```jsonc
{ "messages": [{"role":"user","content":"how many people are exposed to flooding in Battambang?"}] }
// -> message.content explains population/WorldPop isn't wired; metric/features = null
```

## Notes
- First query for a **new** place/hazard is slow (cold: Nominatim + Overpass + tif download
  + clip). Measured: a fresh place+hazard (Siem Reap + landslides) = **~84 s**. Cached after that.
- `verbose:true` adds `trace`; otherwise it's `null`.

---

## `POST /api/tiffs` — bring your own data (multipart)

Upload a hazard GeoTIFF (a single-band 0–5 or 1–5 severity raster). It's **verified** before
it can be used and registered for the given `thread_id` **only on a PASS**; then the user can
ask about it in chat on that same thread.

| Field (form-data) | Notes |
|---|---|
| `file` | the GeoTIFF (`.tif`/`.tiff`), ≤ 200 MB |
| `thread_id` | **required** — the conversation the layer is registered to (per-thread, in-memory) |
| `hazard_label` | **required** — what it represents, e.g. `flood` |
| `severity_scale` | `0-5` (default) or `1-5` |

```bash
curl -s -F file=@my_flood.tif -F thread_id=t1 -F hazard_label=flood -F severity_scale=0-5 \
  localhost:8001/api/tiffs
```

Response (HTTP 200 whether it passes or fails verification; a **rejection is `ok:false`**, not an error):
```jsonc
{
  "ok": true,
  "layer": "byod_flood_1a2b3c4d",        // the registered layer name (null on failure)
  "hazard_label": "flood",
  "mismatches": [],                       // why it failed (empty on pass)
  "warnings": ["CRS is EPSG:3857 …"],     // soft signals that didn't fail the gate
  "observed": { "dtype": "int16", "crs_epsg": 4326, "sampled_min": 1, "sampled_max": 5,
                "sampled_distinct": 5, "width": 160, "height": 160 }
}
```

A malformed request (bad extension, empty, or oversize) returns a **4xx** with `{ "detail": "…" }`.
After `ok:true`, a normal `/api/chat` call on the same `thread_id` (e.g. *"roads in my uploaded
flood layer in Battambang"*) routes to the new layer.
