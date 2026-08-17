# The reference implementation, end to end

The pattern as actually built, in this repo. Read it to see the shape in working code, then adapt - not copy. The host project's units of work, field names, and state mechanism will differ.

Domain: a disaster-risk agent. A question goes in, a number computed from geospatial data comes out, and the trace exists so a user can tell whether to trust that number.

## The files

| File | Role |
| --- | --- |
| `apps/api/src/app/graph/tracing.py` | Every builder, the envelope assembler, the writer. All of it. ~500 lines. |
| `apps/api/src/app/graph/graph.py` | The five nodes. Each times itself and calls its builder before returning. |
| `apps/api/src/app/graph/geo/ingest.py` | The `IOCollector` / `install` / `emit` / `drain` collector, plus its emission sites. |
| `apps/api/src/app/api/routes/chat.py` | The response boundary: assemble, persist, attach - inside a bare `except`. |
| `apps/api/tests/test_tracing.py` | Builders unit-tested directly, no pipeline run. |
| `apps/api/tests/test_trace_recovery.py` | Recovery behaviours asserted end to end through the API. |

Note the ratio: one module of tracing code, two of tests. Builders being pure
(`design-principles.md` §12) is what makes that cheap.

## The flow

```
POST /api/chat
  -> graph.invoke()
       route()     -> builds a router event    -> returns {..., "events": [_RESET, event]}
       resolve()   -> builds a resolve event   -> returns {..., "events": [event]}
       fetch()     -> installs an IOCollector, builds a fetch event with the drained I/O
       operate()   -> builds an operate event
       finalize()  -> builds a finalize event
  -> chat.py: build_trace_envelope(result["events"], ...) inside try/except
             write_trace_envelope(envelope)
             ChatResponse(trace_envelope=..., trace_events=...)
```

## What each step captures, and why

| Step | The question it answers | Notable fields |
| --- | --- | --- |
| `router` | Did it understand what I asked? | `derived_place`, `derived_countable_assets`, `derived_tool_calls`, `kind` |
| `resolve` | Did it have to ask me something, and why? | `decision`, `options`, `question_asked`, `byod_passthrough` |
| `fetch` | Where did the data come from? | `aoi.how`, `api_calls[]`, `downloads[].was_cached` |
| `operate` | What produced the number? | `result.method`, `result.value`, `result.source`, `min_severity` |
| `finalize` | Did the model invent anything? | `grounded`, `kind`, `tokens` |

Each is a distinct user question, which is what makes the granularity right. The five steps
map 1:1 onto graph nodes here, but that is a consequence of the architecture, not the design
rule - the rule is §1 of `design-principles.md`.

## Four things worth stealing

**1. `kind` / `decision` are derived in the builder, not passed in.** The caller hands over
raw facts; the builder computes which outcome they represent, and writes the matching
`summary`/`why`:

```python
if derived_tool_calls is None:
    kind = "declined"
    summary = "Router received a text reply with no tool call"
    why = "The model didn't match the question to any available tool, so it answered directly."
elif error is not None:
    kind = "missing_place"
    ...
```

One place decides what an outcome is called, so the label and its explanation cannot drift
apart.

**2. Summarise, never leak internals.** `_summarize_aoi` reduces a bundle full of absolute
filesystem paths to `{name, area_km2, how}`. The raw bundle never reaches a client. Do this
at the builder, not at the serialiser - the builder is where you know what is safe.

**3. The header skips rather than coerces.**

```python
token_steps = [e["tokens"] for e in events if e.get("tokens")]
```

Steps with no tokens are omitted from the sum, not counted as zero
(`design-principles.md` §11).

**4. The response boundary cannot fail.**

```python
trace_envelope = None
try:
    trace_envelope = tracing.build_trace_envelope(
        events=result.get("events") or [], thread_id=thread_id, trace_id=response_id)
    tracing.write_trace_envelope(trace_envelope)
except Exception:   # envelope build/persist is best-effort; never break the answer
    trace_envelope = None
```

## Testing

**Builders, directly.** They are pure, so construct a fake SDK response with
`SimpleNamespace` and assert the returned dict - no graph, no network, no API key:

```python
def test_router_event_declined():
    resp = _llm_response(content="I can't answer that.", tool_calls=None)
    event = tracing.make_trace_event_router(..., llm_response=resp, error="I can't answer that.")
    assert event["kind"] == "declined"
    assert event["derived_tool_calls"] is None
```

**A required-field set**, so a dropped key breaks a test rather than a consumer:

```python
_REQUIRED_FIELDS = {"step", "started_at", "ended_at", "duration", "summary", "node", ...}
assert not (_REQUIRED_FIELDS - event.keys())
```

**Recovery paths end to end**, through the real API with stubs, asserting what the trace
shows: that a failover recorded `attempts: 2`, that a cache hit recorded `was_cached: true`,
that an error branch stopped at the failing node with the downstream steps absent. See
`apps/api/tests/test_trace_recovery.py`, and `docs/TRACE_RECOVERY.md` for the captured
output.

## Further reading in this repo

- `docs/TRACE_RECOVERY.md` - failure recovery visible in real captured traces.
- `docs/TRACE_USE_CASES.md` - what the trace is actually good for, and where it falls short.
  Read the `grounded` section before building any "verified" indicator on a heuristic.
- `apps/web/src/lib/trace/README.md` - how the reference frontend consumes it.
