# Trace & observability layer

How the per-turn execution trace gets from the backend into the UI, what every field
means, and what you must preserve if you rebuild the visualization in a different shape.

If you are here to build a **different** view of the same data — a printable report, a
timeline, a cross-turn comparison — read §2 and §3, then §6. You should not need to open
`tracing.py`.

---

## 1. Where the data comes from

Every `POST /api/chat` response carries a complete trace of the turn that produced it:

| Field            | What it is                                                                                                                                    |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `trace_envelope` | The whole thing: `thread_id`, `trace_id`, `created_at`, `total_duration`, `total_tokens`, `steps[]`. Best-effort — absent if assembly failed. |
| `trace_events`   | The same `steps[]`, without the computed header. Present unconditionally.                                                                     |
| `trace`          | **Legacy.** A `string[]` narration, only when the request sets `verbose: true`. The UI no longer sends `verbose` or reads this.               |

Backend origin:

- Built in `apps/api/src/app/api/routes/chat.py:98-104`, inside a bare `except` — a tracing
  bug must never break the answer.
- Assembled by `apps/api/src/app/graph/tracing.py:build_trace_envelope`; each step is built
  by one `make_trace_event_*` function in the same module.
- Persisted to `cache/traces/{trace_id}.envelope.json`. `trace_id` equals `ChatResponse.id`.
- The contract is defined by the exports in graph/tracing.py

There is **no trace endpoint**. The envelope arrives inline and `ChatStore` already keeps
the whole response per turn, so the panel needs no fetch.

---

## 2. The layer contract

Three layers, and one rule:

> **`lib/trace/` contains no JSX. `components/Trace/` contains no field knowledge.**

```
types/trace.ts          L1  Wire types. A discriminated union on `node`. No logic.
lib/trace/              L2  Pure functions: envelope -> presentation-neutral view models.
components/Trace/       L3  Renderers. Consume L2 output, decide only how it looks.
```

| Module             | Exports                                                        | Answers                                                    |
| ------------------ | -------------------------------------------------------------- | ---------------------------------------------------------- |
| `parse.ts`         | `parseEnvelope`, `envelopeFromSteps`                           | "is this a usable envelope?"                               |
| `selectors.ts`     | `summarizeEnvelope`, `toStepRows`, `stepUsedModel`, formatters | "what are the headline numbers and the ordered steps?"     |
| `fields.ts`        | `toStepFields`                                                 | "what is worth showing about this step, to this audience?" |
| `labels.ts`        | `stepTitle`, `NODE_LABEL`, `MISSING`, …                        | "what do we call this in English?"                         |
| `graphTopology.ts` | `GRAPH_NODES`, `GRAPH_EDGES`, `VIEW_BOX`                       | "what shape is the backend graph?"                         |
| `graphPath.ts`     | `toGraphPath`, `NODE_ID_BY_STEP`                               | "which parts of it did this turn touch?"                   |

**Why the split exists.** A second visualization is expected. If components read the
envelope directly, every rewrite re-derives "which field means what", scattered across
whichever component happened to need it. With the split, a rewrite replaces L3 and reuses
L1+L2 verbatim. `lib/trace/` is also React-free, so a test, a Node script, or a static
report generator can use it — see §6.

---

## 3. Field mapping

`user` fields answer _"should I trust this answer?"_. `developer` fields answer _"what did
it cost and what exactly happened?"_. The audience is stored per field in `fields.ts`, as
data, so the two views cannot drift apart.

### router (`node: "router"` — the `route()` node)

| Field                                                     | Means                                                                                                        | Audience          |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ----------------- |
| `kind`                                                    | `routed` / `declined` / `missing_place` / `apply_choice` — which of the four outcomes. Drives the row title. | user (as a title) |
| `derived_place`                                           | The place it extracted from your question.                                                                   | user              |
| `derived_countable_assets`                                | What it thinks you want counted (roads, hospitals…).                                                         | user              |
| `derived_hazard_layers_used` / `derived_risk_layers_used` | Which hazard/risk layers it picked.                                                                          | user              |
| `derived_tool_calls[0].function_name`                     | The calculation it chose.                                                                                    | user              |
| `derived_tool_calls[0].function_args`                     | Its arguments.                                                                                               | developer         |
| `user_drawn_area`, `drawn_area_type`                      | Whether you drew the area rather than naming it.                                                             | user              |
| `error`                                                   | Why it couldn't proceed.                                                                                     | user              |
| `llm_provider`, `model_used`                              | **`null` ⇒ no model ran this turn.** The authoritative signal.                                               | developer         |
| `tokens`                                                  | See the zero-tokens trap below.                                                                              | developer         |
| `available_assets.available_tools`                        | What the model could choose from.                                                                            | developer         |
| `messages`                                                | System prompt + your message + the model's reply.                                                            | developer         |

### resolve

| Field                 | Means                                                          | Audience          |
| --------------------- | -------------------------------------------------------------- | ----------------- |
| `decision`            | `passthrough_no_hazard` / `asked` / `auto_single` / `no_data`. | user (as a title) |
| `hazard`              | Which hazard the question is about.                            | user              |
| `options[].label`     | The exposure-vs-risk choices you were offered.                 | user              |
| `question_asked`      | The clarifying question sent to you.                           | user              |
| `awaiting_choice_set` | `true` ⇒ the graph paused here and the turn ended.             | user              |
| `byod_passthrough`    | Your uploaded layer was used directly, no question needed.     | user              |
| `options[].layer`     | The GeoTIFF behind each option.                                | developer         |

### fetch — the provenance step

| Field                             | Means                                                                | Audience  |
| --------------------------------- | -------------------------------------------------------------------- | --------- |
| `aoi.name` / `.area_km2` / `.how` | Which area, how big, and how it was found.                           | user      |
| `api_calls[]`                     | **Which third-party services were consulted** (Nominatim, Overpass). | user      |
| `downloads[].was_cached`          | **Reused from cache vs fetched fresh.**                              | user      |
| `rasters_clipped`, `l2_computed`  | Hazard layers cropped / risk layers recomputed.                      | user      |
| `layers_fetched`                  | Feature layers pulled; `null` means all defaults.                    | user      |
| `mode`                            | `drawn_area` / `place_lookup`.                                       | developer |
| `downloads[].dest`, `.drive_id`   | Local paths and Drive ids.                                           | developer |

`api_calls` and `downloads` carry **no timestamps** — `ingest.py`'s `emit()` records only
`kind`/`api`/`layer`/`was_cached`. They are an ordered list, never a nested timeline.

### operate — the only node that computes a number

| Field                     | Means                                                                                                  | Audience  |
| ------------------------- | ------------------------------------------------------------------------------------------------------ | --------- |
| `result.value`            | The number.                                                                                            | user      |
| `result.method`           | The fixed calculation that produced it. **No model is involved in this step** — worth saying out loud. | user      |
| `result.source`           | What data it was read from.                                                                            | user      |
| `result.by_severity`      | Breakdown by hazard severity class.                                                                    | user      |
| `min_severity`            | The threshold applied; `null` ⇒ none was specified.                                                    | user      |
| `operation`, raw `result` | The store op and its unreduced output.                                                                 | developer |

### finalize

| Field                                                  | Means                                                                                                      | Audience  |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- | --------- |
| `grounded`                                             | **The single most trust-relevant field.** True if the computed number appears verbatim in the answer text. | user      |
| `kind`                                                 | `error_echo` ⇒ returned as-is, no model. `llm_phrase` ⇒ a model worded it.                                 | user      |
| `error`                                                | The failure echoed as the answer.                                                                          | user      |
| `llm_provider`, `model_used`, `tokens`, `llm_response` | Phrasing internals.                                                                                        | developer |

### envelope header

`total_duration` is user-facing. `total_tokens.total` and `.cost` are developer-only — a
token count answers no question an analyst has.

---

## 4. Missing data

**`null` is not `0`, and `0` is not blank.** This schema's nullability is deliberate and
was reasoned about commit by commit; erasing it in the UI undoes that work at the last
step. Every absent value becomes `{ kind: 'missing', reason }` carrying an explanation from
`labels.ts`'s `MISSING`, and renders as `—` with the reason as a tooltip.

| Situation                                                       | Why it's null                                                                        | Render |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------ |
| `llm_provider` / `model_used` on `apply_choice` or `error_echo` | No model was called.                                                                 | `—`    |
| `tokens` on finalize's `error_echo`                             | No model was called.                                                                 | `—`    |
| `min_severity`                                                  | The model never supplied one; **not** backfilled with `store.py`'s implicit default. | `—`    |
| `derived_tool_calls`                                            | The model answered in text instead of picking a tool.                                | `—`    |
| `result` on operate                                             | The calculation failed.                                                              | `—`    |
| `hazard` / `options` on `passthrough_no_hazard`                 | There was nothing to choose.                                                         | `—`    |
| `grounded` on `error_echo`                                      | No number was computed, so nothing to check against.                                 | `—`    |

### ⚠ The zero-tokens trap

The router's `apply_choice` branch emits a **fully zeroed** `tokens` object, not `null`
(`tracing.py:193` calls `_usage(None, ...)`, which returns all zeros). So:

```
tokens.total === 0   ->  AMBIGUOUS. Could be "no call" or "a call that used nothing".
llm_provider === null ->  UNAMBIGUOUS. No model ran.
```

Always use `stepUsedModel(step)` from `selectors.ts`. Reading the token counts to decide
whether a model ran will tell a user that the model was called and returned nothing, which
is the opposite of what happened.

---

## 5. Failure handling

Observability is never load-bearing. The rules:

| Case                                                | Behaviour                                                                                                                                                                   |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No `trace_envelope` and no `trace_events`           | Panel renders nothing.                                                                                                                                                      |
| `trace_envelope` missing but `trace_events` present | `envelopeFromSteps` rebuilds the header client-side, matching the backend's own summing rules exactly.                                                                      |
| `steps: []`                                         | Empty state, not a crash.                                                                                                                                                   |
| A step has `error`                                  | **Data, not a UI failure.** Node draws red, row shows the error, downstream nodes stay dashed.                                                                              |
| Malformed envelope                                  | `parseEnvelope` returns `null`. Never throws.                                                                                                                               |
| Unknown `node` value                                | `toStepFields`'s `default` branch renders a raw dump. The union catches this at compile time for our code; the default catches it at runtime for data from a newer backend. |
| A render bug in the trace components                | `TraceErrorBoundary` contains it. The bubble survives.                                                                                                                      |

---

## 6. The execution-flow graph

`graphTopology.ts` is a hand-transcribed copy of `apps/api/src/app/graph/graph.py:487-500`
plus its `_after_route` / `_after_resolve` / `_after_fetch` branch functions. Five nodes,
eleven edges, with `{cx, cy}` coordinates and a plain-English `when` per edge.

```
START -> route
route    -> resolve | fetch (resumed a choice) | finalize (error)
resolve  -> fetch   | ask_end (paused to ask)  | finalize (no data)
fetch    -> operate | finalize (error)
operate  -> finalize
finalize -> END
```

**Skipped nodes are drawn, not hidden.** "It never needed to ask you anything" is as
informative as "it did", and is only visible if the unused branches are on screen.

### ⚠ `router` vs `route`

The trace tags route()'s step `node: "router"` (`tracing.py:144,212`), but the LangGraph
node is registered as `"route"` (`graph.py:488`). Every other node matches 1:1. This is
reconciled in exactly one place — `NODE_ID_BY_STEP` in `graphPath.ts`. Do not compare these
strings anywhere else.

### Drift

The topology is hardcoded here and lives in Python there. The guard is
`test_graph_topology_matches_frontend` in `apps/api/tests/test_tracing.py`: it parses this
very file and asserts the node **and edge** sets match the compiled graph. If you re-lay-out
the diagram, keep the `from:` / `to:` lines on their own lines — the test's regex depends on
that formatting.

---

## 7. How to change things — a task-to-file map

Each row is one kind of change, the file(s) it lives in, and what it costs. Find your task,
edit those files, run `npm run build` (which is `tsc -b` — it will point at anything you
missed).

| I want to…                                                                                                            | Edit                                                                                  | Notes                                                                                                                                                                                                                                                                |
| --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Rename a step, node, or any invented word** (e.g. "Understand" → "Interpret")                                       | `labels.ts` only                                                                      | Every user-facing string the frontend makes up lives here. Do **not** touch `summary`/`why` on the step — those come from the backend and are rendered verbatim (see the rule below).                                                                                |
| **Change what a step's detail panel shows**, add/remove a field, or move a field between the user and developer views | `fields.ts` only                                                                      | Each field carries an `audience: 'user' \| 'developer'` tag. That tag _is_ the split — no component decides it.                                                                                                                                                      |
| **Change how a value looks** (e.g. render a new kind like a coloured pill, a bar, a link)                             | 1. add a member to `TraceFieldValue` in `fields.ts` 2. handle it in `FieldValue.tsx`  | `tsc` forces step 2: the moment you add the member, the build fails in `FieldValue.tsx` until you draw it. You cannot forget a case.                                                                                                                                 |
| **Re-lay-out the flow diagram** (horizontal, radial, different spacing)                                               | `graphTopology.ts` (the `{cx, cy}` coordinates) and/or `TraceGraph.tsx` (the drawing) | Coordinates are plain data. Moving a box is editing two numbers.                                                                                                                                                                                                     |
| **Restyle the diagram** (colours, which state looks how)                                                              | `TraceGraph.tsx` (`NODE_BOX`, `NODE_TEXT` maps)                                       | Uses semantic DaisyUI classes, not hex — keep it that way so themes work.                                                                                                                                                                                            |
| **Build a completely different view** (a printable report, a timeline, a per-turn comparison)                         | new components only                                                                   | Reuse L1 + L2 as-is: `parseEnvelope` → `summarizeEnvelope` / `toStepRows` / `toStepFields` / `toGraphPath` give you everything. Because `lib/trace/` imports no React, a plain Node script can do this too — that is how the diagram was proofed during development. |

### Adding a new backend node — the full checklist

If the backend graph grows a sixth node (say `verify`), do these in order. Skipping any one
is caught by either the compiler or the drift test — see "What breaks if you skip this".

1. **`types/trace.ts`** — write a `VerifyStep` interface (`node: 'verify'` plus its fields)
   and add it to the `TraceStep` union.
   _What breaks if you skip this:_ `tsc` fails in `fields.ts`'s `toStepFields` switch —
   `Type 'VerifyStep' is not assignable to type 'never'` — because the switch is exhaustive.
   This is the compiler pointing at the exact place that needs the node.
2. **`fields.ts`** — add a `case 'verify':` to `toStepFields` returning its field groups.
   Clears the `tsc` error from step 1.
3. **`labels.ts`** — add `verify` to `NODE_LABEL` (the short badge) and `GRAPH_NODE_LABEL`
   (the diagram label), and a `case 'verify':` to `stepTitle`.
   _What breaks if you skip this:_ nothing crashes — the row falls back to the raw node name
   and the title "Ran a step". Just looks unpolished.
4. **`graphTopology.ts`** — add the node's box (`{id, cx, cy, …}`) and its edges, and add
   `'verify'` to `GRAPH_NODE_IDS`.
   _What breaks if you skip this:_ the backend test
   `test_graph_topology_matches_frontend` fails — it compares this file against the real
   graph. That failure is the reminder to do this step.
5. **`graphPath.ts`** — add `verify: 'verify'` to `NODE_ID_BY_STEP` (unless the backend's
   step tag already equals the graph node id — see the `router`/`route` note in §6).
   _What breaks if you skip this:_ the node is silently dropped from the diagram — see the
   next section.

The order is deliberate: do step 1 and `tsc` walks you to step 2. Do step 4 and the Python
test walks you to the rest. You are never guessing what to change.

### Will an un-updated frontend break if the backend adds a node?

**No — it will not crash, and the trace is still not load-bearing.** But the flow diagram
degrades _silently_, and that one part is worth understanding. Tested by injecting an
unknown `verify` node into a real envelope and running the actual selector layer:

| Part of the UI                | What happens to an unknown node                                                                                                                                                                                     | Why                                                                                                    |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| The answer itself             | Unaffected                                                                                                                                                                                                          | The trace never touches the answer.                                                                    |
| Tier 2 — the **step list**    | **Shows the new step**, titled "Ran a step", with the backend-written `summary` rendered and a working duration bar                                                                                                 | `parse.ts` keeps any step with a valid `node`/`summary`/`duration`; `stepTitle` has a generic default. |
| Tier 3 — the **detail panel** | **Works** — shows a raw JSON dump of the step                                                                                                                                                                       | `toStepFields`'s `default` branch handles any unrecognized node.                                       |
| The **flow diagram**          | **Silently omits the node** — and worse, if the new node sits between two nodes that already have an edge (e.g. between `operate` and `finalize`), the diagram draws that old edge as though the new node never ran | `toGraphPath` skips any step it can't map (`if (!id) continue`), and the topology it draws is static.  |

So: **runtime is safe** — nothing throws, the panel renders, the step is visible in the
list. The **diagram becomes quietly inaccurate** until someone does step 4/5 above. Nothing
tells the _end user_ the picture is incomplete; the thing that catches it is the backend
drift test, which fails loudly in CI and tells the _developer_ to update the two files.
That test is the whole reason the silent degradation is acceptable: it converts "the diagram
is subtly wrong forever" into "the build is red until you fix it."

### Rules that must stay true whatever you build

- **Never render `summary` or `why` yourself.** They are authored per node in the backend's
  `tracing.py`. Lay them out; do not paraphrase, shorten, or regenerate them.
- **Steps are sequential and non-overlapping** — LangGraph runs the nodes in series. A
  waterfall implying concurrency would be false precision; use proportional bars.
- **One envelope = one turn.** A paused question and its answer are two separate envelopes
  that share a `thread_id`.
- **Never let the trace break the answer.** Return `null`/a fallback, don't throw; the error
  boundary is the last resort, not the plan.
- **`null` is not `0`** — see §4. Preserve it.
