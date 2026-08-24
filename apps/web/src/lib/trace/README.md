# Trace layer

Reference for `types/trace.ts`, `lib/trace/`, and `components/Trace/` - the per-turn
execution trace, from wire to screen.

To build a **different** view of the same data (a printable report, a timeline, a
cross-turn comparison), read §2, §3 and §7. You should not need to open `tracing.py`.

For the portable pattern - deriving the shape from an unfamiliar trace, choosing a view,
the invariants any trace UI must keep - see the `trace-visualize` and `trace-emit` skills
in [`.claude/skills/`](../../../../../.claude/skills/).

---

## 1. What arrives

Every `POST /api/chat` response carries `trace_envelope`:

```
thread_id, trace_id, created_at, total_duration, total_tokens, steps[]
```

`trace_id` equals `ChatResponse.id`. It is the only trace on the wire, and is not gated by
`verbose`. It is also **best-effort**: assembly and persistence run inside a bare `except`
in `apps/api/src/app/api/routes/chat.py`, so it can be absent, and a missing trace is
normal rather than an error.

Assembled by `build_trace_envelope` in `apps/api/src/app/graph/tracing.py`; each step comes
from one `make_trace_event_*` function in that module, which is the contract. Also
persisted to `cache/traces/{trace_id}.envelope.json`.

There is no trace endpoint - `ChatStore` keeps the whole response per turn, so the panel
needs no fetch.

---

## 2. Layers

> **`lib/trace/` contains no JSX. `components/Trace/` contains no field knowledge.**

```
types/trace.ts     L1  Wire types. A discriminated union on `node`. No logic.
lib/trace/         L2  Pure functions: envelope -> presentation-neutral view models.
components/Trace/  L3  Renderers. Consume L2, decide only how it looks.
```

A second visualization replaces L3 and reuses L1+L2 verbatim. `lib/trace/` imports no
React, so a test or a plain Node script can use it too.

| Module             | Exports                                                       | Answers                                      |
| ------------------ | ------------------------------------------------------------- | -------------------------------------------- |
| `parse.ts`         | `parseEnvelope`                                               | Is this a usable envelope?                   |
| `selectors.ts`     | `summarizeEnvelope`, `toStepRows`, `stepUsedModel`, `format*` | Headline numbers, and the ordered steps.     |
| `fields.ts`        | `toStepFields`, `TraceFieldValue`                             | What is worth showing about a step, to whom? |
| `labels.ts`        | `stepTitle`, `NODE_LABEL`, `MISSING`, `AOI_HOW_LABEL`, …      | What do we call this in English?             |
| `graphTopology.ts` | `GRAPH_NODES`, `GRAPH_EDGES`, `VIEW_BOX`                      | What shape is the backend graph?             |
| `graphPath.ts`     | `toGraphPath`, `NODE_ID_BY_STEP`                              | Which parts of it did this turn touch?       |

Every field carries an `audience: 'user' | 'developer'` tag, set in `fields.ts`. That tag
*is* the split between the two views; no component decides it.

---

## 3. Field reference

`user` fields answer *"should I trust this answer?"*. `developer` fields answer *"what did
it cost, and what exactly happened?"*.

**Every step** carries `node`, `step`, `started_at`, `ended_at`, `duration_ms` (a float,
rounded to 0.1 ms), `error`, and two backend-authored strings: `summary` (what this step
did this time) and `why` (why the step exists at all). Render both verbatim.

### router - `node: "router"`, the `route()` node

| Field                                                     | Means                                                        | Audience  |
| --------------------------------------------------------- | ------------------------------------------------------------ | --------- |
| `kind`                                                    | `routed` / `declined` / `missing_place` / `apply_choice`     | user      |
| `derived_place`                                           | The place extracted from the question.                       | user      |
| `derived_countable_assets`                                | What it thinks you want counted (roads, hospitals…).         | user      |
| `derived_hazard_layers_used` / `derived_risk_layers_used` | Which hazard/risk layers it picked.                          | user      |
| `derived_tool_calls[0].function_name`                     | The calculation it chose.                                    | user      |
| `user_drawn_area`, `drawn_area_type`                      | Whether you drew the area rather than naming it.             | user      |
| `error`                                                   | Why it could not proceed.                                    | user      |
| `derived_tool_calls[0].function_args`                     | Its arguments.                                               | developer |
| `llm_provider`, `model_used`                              | `null` ⇒ no model ran. The authoritative signal.             | developer |
| `tokens`                                                  | `null` when no model ran. See §4.                            | developer |
| `llm_response`                                            | The model's text reply; `null` when it chose a tool instead. | developer |
| `messages`                                                | The prompt that was **sent**, never the reply.               | developer |
| `available_assets.available_tools`                        | What the model could choose from.                            | developer |

### resolve

| Field                 | Means                                                         | Audience  |
| --------------------- | ------------------------------------------------------------- | --------- |
| `decision`            | `passthrough_no_hazard` / `asked` / `auto_single` / `no_data` | user      |
| `hazard`              | Which hazard the question is about.                           | user      |
| `options[].label`     | The exposure-vs-risk choices offered.                         | user      |
| `question_asked`      | The clarifying question sent to the user.                     | user      |
| `awaiting_choice_set` | `true` ⇒ the graph paused here and the turn ended.            | user      |
| `byod_passthrough`    | An uploaded layer was used directly; no question needed.      | user      |
| `options[].layer`     | The GeoTIFF behind each option.                               | developer |

### fetch - the provenance step

| Field                             | Means                                                 | Audience  |
| --------------------------------- | ----------------------------------------------------- | --------- |
| `aoi.name` / `.area_km2` / `.how` | Which area, how big, how it was found.                | user      |
| `api_calls[]`                     | Third-party services consulted (Nominatim, Overpass). | user      |
| `cache[]`                         | Local artifacts reused or built. See below.           | user      |
| `downloads[]`                     | Bytes pulled from a remote store (Google Drive).      | user      |
| `rasters_clipped`, `l2_computed`  | Hazard layers cropped / risk layers recomputed.       | user      |
| `layers_fetched`                  | Feature layers requested; `null` means all defaults.  | user      |
| `mode`                            | `drawn_area` / `place_lookup`.                        | developer |
| `*.dest`, `downloads[].drive_id`  | Cache-relative paths and Drive ids.                   | developer |

`cache[]` entries carry a `what` of `aoi_boundary`, `osm_layer`, or `hazard_clip`, and are
emitted on **hit and miss alike** - `was_cached` answers "was this fresh?", and an absent
event means the check never ran, not that it missed. `was_cached: true` says the artifact
was reused; it says nothing about how old it is.

`dest` is relative to the cache directory (`bangkok/roads.geojson`), never a server path.

None of these carry timestamps, so they are an **ordered list, not a sub-timeline**.
Rendering them as one would invent precision the data does not have.

### operate - the only node that computes a number

| Field                     | Means                                                             | Audience  |
| ------------------------- | ----------------------------------------------------------------- | --------- |
| `result.value`            | The number.                                                       | user      |
| `result.method`           | The fixed calculation that produced it. **No model is involved.** | user      |
| `result.source`           | What data it was read from.                                       | user      |
| `result.by_severity`      | Breakdown by hazard severity class.                               | user      |
| `min_severity`            | The threshold applied; `null` ⇒ none was specified.               | user      |
| `operation`, raw `result` | The store op and its unreduced output.                            | developer |

### finalize

| Field                                  | Means                                                                      | Audience  |
| -------------------------------------- | -------------------------------------------------------------------------- | --------- |
| `grounded`                             | The computed number appears verbatim in the answer. A substring test - §4. | user      |
| `kind`                                 | `error_echo` ⇒ returned as-is. `llm_phrase` ⇒ a model worded it.           | user      |
| `error`                                | The failure echoed as the answer.                                          | user      |
| `llm_response`                         | The answer text.                                                           | developer |
| `messages`                             | The prompt that was sent, including the tool-result exchange.              | developer |
| `llm_provider`, `model_used`, `tokens` | Phrasing internals.                                                        | developer |

### envelope header

`total_duration` is user-facing. `total_tokens.total` and `.cost` are developer-only - a
token count answers no question an analyst has.

---

## 4. Missing values

**`null` is not `0`, and `0` is not blank.** Every absent value becomes
`{ kind: 'missing', reason }` from `labels.ts`'s `MISSING`, and renders as `-` with the
reason on hover. Never an empty cell, never a zero.

| Situation                                                       | Why it is null                                          |
| --------------------------------------------------------------- | ------------------------------------------------------- |
| `llm_provider` / `model_used` / `tokens`, on any no-model branch | No model was called.                                    |
| `tokens.cost`                                                   | No prices are configured - unknown, not free.           |
| `min_severity`                                                  | The model supplied none; not backfilled with a default. |
| `derived_tool_calls`                                            | The model answered in text instead of picking a tool.   |
| `result` on operate                                             | The calculation failed.                                 |
| `hazard` / `options` on `passthrough_no_hazard`                 | There was nothing to choose.                            |
| `grounded` on `error_echo`                                      | No number was computed, so nothing to check against.    |

**Use `stepUsedModel(step)` to decide whether a model ran** - it reads `llm_provider`.
`tokens.total === 0` is ambiguous: it could mean no call, or a call that used nothing.

**Label `grounded` for the test it performs.** It checks whether the number appears
verbatim in the answer text, with commas stripped. That has real false negatives (`241.0`
against "about 241 km") and real false positives (a count of `3` "confirmed" by an answer
mentioning `13`). Never render it as "Verified".

---

## 5. Failure behaviour

The trace is never load-bearing: no feature may read a field that exists only here.

| Case                                | Behaviour                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------- |
| No `trace_envelope`                 | Panel renders nothing. An untraced turn and a failed assembly look alike. |
| Header missing or non-numeric       | `parseEnvelope` recomputes the totals from `steps`.                       |
| `steps: []`                         | Empty state, not a crash.                                                 |
| A step has `error`                  | **Data, not a UI failure.** Node draws red, row shows the error.          |
| Malformed envelope                  | `parseEnvelope` returns `null`. It never throws.                          |
| Unknown `node` value                | `toStepFields`'s `default` branch renders a raw dump.                     |
| A render bug in `components/Trace/` | `TraceErrorBoundary` contains it; the chat bubble survives.               |

---

## 6. The flow diagram

`graphTopology.ts` mirrors `apps/api/src/app/graph/graph.py`: eight boxes - the five graph
nodes plus the `start`, `end` and `ask_end` terminals - and eleven edges, each with
`{cx, cy}` coordinates and a plain-English `when`. Only the five real nodes are in
`GRAPH_NODE_IDS`.

```
START -> route
route    -> resolve | fetch (resumed a choice) | finalize (error)
resolve  -> fetch   | ask_end (paused to ask)  | finalize (no data)
fetch    -> operate | finalize (error)
operate  -> finalize
finalize -> END
```

**Skipped nodes are drawn, not hidden.** "It never needed to ask you anything" is as
informative as "it did", and is only visible if the unused branches are on screen. Node
state is never encoded by colour alone.

**`router` vs `route`.** The trace tags `route()`'s step `node: "router"`, but the
LangGraph node is registered as `"route"`. Every other node matches 1:1. This is reconciled
in exactly one place - `NODE_ID_BY_STEP` in `graphPath.ts`. Do not compare these strings
anywhere else.

**Drift guard.** The topology is hardcoded here and lives in Python there.
`test_graph_topology_matches_frontend` (`apps/api/tests/test_tracing.py`) parses
`graphTopology.ts` and asserts its node and edge sets match the compiled graph. Keep the
`from:` / `to:` lines on their own lines - the test's regex depends on it.

---

## 7. Making changes

Run `npm run build` (`tsc -b`) after any of these; it points at whatever you missed.

| I want to…                                            | Edit                                                           |
| ----------------------------------------------------- | -------------------------------------------------------------- |
| Rename a step, node, or any word the frontend invents | `labels.ts` only                                               |
| Add, remove, or re-audience a field in a detail panel | `fields.ts` only                                               |
| Render a value a new way (a pill, a bar, a link)      | add to `TraceFieldValue` in `fields.ts`, then `FieldValue.tsx` |
| Re-lay-out the diagram                                | `graphTopology.ts` coordinates, and/or `TraceGraph.tsx`        |
| Restyle the diagram                                   | `TraceGraph.tsx` (`NODE_BOX`, `NODE_TEXT`) - semantic DaisyUI classes, not hex |
| Build a different view entirely                       | new components only; reuse L1 + L2 as-is                       |

### Adding a backend node

If the graph grows a sixth node (say `verify`):

| Step | Edit                                                              | Caught by                                        |
| ---- | ----------------------------------------------------------------- | ------------------------------------------------ |
| 1    | `types/trace.ts` - a `VerifyStep`, added to the `TraceStep` union | -                                                |
| 2    | `fields.ts` - a `case 'verify':` in `toStepFields`                | `tsc`: the exhaustive switch fails until step 1 has a case |
| 3    | `labels.ts` - `NODE_LABEL`, `GRAPH_NODE_LABEL`, `stepTitle`       | Nothing. The row falls back to "Ran a step".     |
| 4    | `graphTopology.ts` - the node's box, its edges, `GRAPH_NODE_IDS`  | `test_graph_topology_matches_frontend`           |
| 5    | `graphPath.ts` - `NODE_ID_BY_STEP`, unless the ids already match  | Nothing. The node is dropped from the diagram.   |

Until steps 4 and 5 are done, the step list and detail panel still work, but the diagram
omits the node - and if it sits between two nodes that already share an edge, that old edge
is drawn as though the new node never ran. The drift test is what turns a subtly wrong
picture into a red build.

---

## 8. Invariants

- **Never render `summary` or `why` yourself.** They are authored per branch in the
  backend. Lay them out; do not paraphrase, shorten, or regenerate them.
- **Steps are sequential and non-overlapping.** LangGraph runs the nodes in series. Use
  proportional bars; a waterfall would imply concurrency that did not happen.
- **One envelope = one turn.** A paused question and its answer are two envelopes sharing
  a `thread_id`.
- **Never let the trace break the answer.** Return `null` or a fallback rather than
  throwing. The error boundary is the last resort, not the plan.
- **Preserve `null`.** See §4.
