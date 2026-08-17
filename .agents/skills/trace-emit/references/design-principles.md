# Design principles for a per-step trace

Each principle is stated generally, then shown as it appears in one possible
implementation. Adapt the pattern, only look at the code if the user has specifically requested it AND it is available.

---

## 1. One event per unit of work

A step should be **something a user would name when describing what happened** - "it wrote the tool calls for your query", "it fetched the map data", "it calculated the hazard layer for your area". Not every function call, and not one giant blob per request.

Too fine and you have a profiler nobody reads. Too coarse and the trace explains nothing, example: "handled request, 4.2s" answers no question.

*Example:* one system settled on five step kinds - route the question, resolve ambiguity, fetch data, compute, phrase the answer - one per graph node, each with its own builder function. Five to eight is typical; thirty means you instrumented too low a layer.

## 2. Build the event immediately before the unit returns, inside it

Not in a decorator, not in a wrapper, not at the response boundary.

A generic wrapper can only capture what is visible from outside: arguments in, value out, time elapsed. The interesting content is *inside* - which branch was taken, what the model was offered versus what it picked, why a question was skipped, which of three fallbacks produced the value. Only the function knows that, and only at the moment it returns.

This is the single decision that most determines whether a trace is useful. A decorator-based trace is cheap to add and says almost nothing.

```python
def fetch(state):
    t_start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        ...                                   # the real work
        event = make_fetch_event(             # built here, with everything known
            start_time=t_start, end_time=time.perf_counter(), started_at=started_at,
            ended_at=..., result=result, source=source, error=None, ...)
        return {"result": result, "events": [event]}
    except Exception as e:
        event = make_fetch_event(..., result=None, error=f"No data: {e}")
        return {"error": ..., "events": [event]}      # the failure is DATA, not a gap
```

Note both paths emit. **A step that failed must still produce an event** - otherwise the trace goes silent exactly when it matters most.

## 3. Time with a monotonic clock; timestamp with a wall clock

Two different jobs:

- **Duration** - `time.perf_counter()` / `performance.now()` / `process.hrtime`. Monotonic, unaffected by NTP adjustments. This is the number you display.
- **`started_at` / `ended_at`** - wall clock, ISO-8601 with a timezone. For correlating with logs and other systems. Never subtract these to get a duration.

Record the start *before* the work, including any setup, so the number reflects what the user waited for.

## 4. Every event says what it did, in plain language

Two authored strings per event:

- **`summary`** - what this step did, this time, with the specifics filled in.
  `"Router matched the question to 'count_in_hazard' for 'Bangkok'"`, as opposed to `"routing"`.
- **`why`** - a high level description of the purpose of this step. For example:
  `"This step extracts the tool call from the model; no computation happens here."`

Write them **in the backend, next to the branch that produced them**, assume consumers will render them verbatim. This is to ensure only one source of truth for tracing.

## 5. Capture inputs as actually received, not as declared

Record the arguments the step received - the parsed tool-call arguments, the resolved parameters, the request body fields it used. Derive them at runtime rather than hardcoding a list per function, so a new parameter shows up without a code change. Only expose what is considered appropriate considering PII - the user should have answered this already; if not, ask once, and drop it if they say it does not matter.

When a value is inferred instead or is a best-guess, indicate it, so the end-user is aware of it without needing to go through the code.

## 6. `null` is not `0`

The most common way a trace lies.

| Fact | Correct encoding |
| --- | --- |
| No model was called | `provider: null`, `tokens: null` |
| A model was called and used no tokens | `provider: "x"`, `tokens: {in: 0, ...}` |
| No threshold was supplied | `min_severity: null` |
| A threshold of zero was supplied | `min_severity: 0` |
| The calculation failed or didn't return | `result: null` |
| The calculation returned a zero | `result: {value: 0}` |

Use nulls and zeros where appropriate, they are not interchangeable.

## 7. Cache checks are observable

Anywhere a value *might* be served from cache, record that it was - with the same field, on both branches:

```python
was_cached = os.path.exists(path)
emit({"kind": "download", "layer": layer, "dest": path, "was_cached": was_cached})
if not was_cached:
    download(...)
```

"Was this computed fresh or reused?" is one high-value question a trace can answer, and it is invisible from timing alone once a cache is warm.

Emit on the hit path too. A cache hit that emits nothing is indistinguishable from no cache at all.

**Be honest about what the flag means.** `was_cached: true` says the value was reused. It does **not** say how old it is. If vintage matters, record a fetch timestamp or version alongside it - a boolean cannot carry that.

## 8. External calls are captured, including how they went

For every third-party call: which service, which operation, and enough of the outcome to
size it (`n_results`, status, bytes). Where there is failover or retry, record
**which endpoint actually answered and on which attempt**:

```python
emit({"kind": "api", "api": "Overpass", "endpoint_used": url,
      "attempts": attempt + 1, "n_results": len(elements)})
```

That single pair of fields turns "why was that slow" from a guess into a fact.

Know what you are *not* capturing, and write it down. If `emit` sits only on the success path
of a retry loop, failed attempts leave no record and the attempt count is the only evidence
anything went wrong. That is a defensible tradeoff - but a consumer will assume otherwise
unless it is documented.

When the call happens inside a module whose return value cannot carry this, see
`io-capture.md`.

## 9. LLM calls record tokens and cost, priced at the call site

Capture prompt and completion tokens from the SDK response, and compute cost **where the
model and provider are known** rather than at assembly time. Prices are usually quoted per
million tokens - divide before multiplying, and unit-test that.

**Capture the whole usage breakdown the SDK gives you, not just two numbers.** Providers bill cache reads and cache writes at different rates from ordinary input, and reasoning tokens are billed as output while never appearing in the completion. A trace recording only prompt and completion counts will misreport cost on any model doing either.

**Record the rate you priced at, alongside the computed cost.**

Also record which provider and model actually served the call. Get this at runtime if possible.

## 10. Accumulate into per-turn state, assemble at the boundary

Steps write events into a per-turn container; one place at the end assembles them:

- an envelope header (id, turn id, created-at, totals),
- the ordered steps,
- attached to the response and/or persisted.

**The header can be built either way.** Compute it at the end from the finished steps, or open
it at the start and append to it as the turn runs - whichever suits the architecture. Computing
at the end is simpler and keeps the totals consistent with the steps by construction; opening
early is what you want when a turn can be abandoned partway and you still need a header, or
when something must be recorded before the first step runs.

**Watch the reset.** If the state persists across turns - a checkpointed conversation, a
session object - the event list must be cleared per turn or it grows forever and every turn
reports its predecessors. One way: have the turn's first step emit a sentinel the accumulator
recognises as "discard everything before this".

**Index by position.** Derive a step's index **at assembly time from its position in the list**, not from a running count read inside the step.

## 11. Header totals skip, they do not coerce

When summing tokens across steps, **skip** steps that have none rather than treating them as
zero. Steps that never call a model legitimately have no token field; coercing them to zero
buries the distinction from principle 6 in the total.

Duration is different - every step has one, so sum them all.

## 12. Builders are pure functions of already-computed values

Each builder takes the values the caller already has and returns a plain dict. No I/O, no
re-deriving, no reaching into global state, no mutation of what it was passed.

That makes them trivially unit-testable without running the pipeline, keeps them small, and
means a tracing change cannot alter behaviour. A builder can be tested by constructing it
directly with a fake response object - no pipeline run, no network, no API key.

Keep the total code small. Tracing that is expensive to maintain gets deleted.

## 13. Tracing can never break the response

Wrap assembly and persistence so that any failure yields no trace rather than no answer:

```python
trace_envelope = None
try:
    trace_envelope = build_trace_envelope(events, thread_id, trace_id)
    write_trace_envelope(trace_envelope)
except Exception:            # observability is never load-bearing
    trace_envelope = None
```

Consumers must treat a missing trace as normal, not as an error.

The corollary: **never let a required field be sourced only from the trace.** The moment a
product feature reads a trace field, the `except` above becomes a silent outage.

---

## Checklist

Before calling it done:

- [ ] Every unit of work emits on **both** success and failure paths
- [ ] Events are built inside the unit, immediately before returning
- [ ] Monotonic duration; wall-clock timestamps; never subtract the timestamps
- [ ] `summary` and `why` authored in the backend, specific to the branch taken
- [ ] Absent values are `null`, never a zero or a default the caller did not choose
- [ ] One documented field is the authoritative "did X happen" signal
- [ ] Every cache check emits on both hit and miss
- [ ] External calls record service, outcome, and which endpoint/attempt succeeded
- [ ] LLM steps record provider, model, tokens, and cost
- [ ] Per-turn state resets per turn; indices assigned by position at assembly
- [ ] Header totals skip absent values rather than coercing them
- [ ] Assembly and persistence cannot raise into the response path
- [ ] A real emitted trace has been read by a human
- [ ] What was captured matches what the user agreed to - verify against a real trace, per `SKILL.md` steps 1a and 3
