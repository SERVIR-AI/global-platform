# Mapping the pattern onto a real architecture

The pattern needs three things from the host system. Find these first; everything else
follows.

| Concept | The question it answers |
| --- | --- |
| **Unit of work** | What becomes one step? |
| **Per-turn state** | Where do events accumulate while the turn runs? |
| **Turn boundary** | Where is the envelope assembled and attached? |

---

## The mapping table

| Architecture | Unit of work | Per-turn state | Turn boundary |
| --- | --- | --- | --- |
| **Graph agent** (LangGraph, Burr) | A node | A state channel with an append reducer | After `invoke()`, in the caller |
| **Plain web handler** (FastAPI, Flask, Express) | A service/domain function called by the handler | A `ContextVar` / `AsyncLocalStorage` set by middleware, or an explicit context object passed down | Response middleware, or the handler's return |
| **Layered service** (controller → service → repo) | Each service method; repos usually too fine | Request-scoped DI container, or an explicit context argument | Controller, before serialising |
| **Queue worker** (Celery, Sidekiq, SQS) | Each stage of the task | Task-local storage, or a dict on the job payload | Task completion - persist rather than return; there is no response |
| **Agent loop** (tool-calling while-loop) | One iteration: the model call plus the tool it ran | A list on the loop's own scope | After the loop exits |
| **Streaming / SSE** | Same as the non-streaming case | Same | Trailing event after the content stream - see below |
| **Batch / ETL** | Each transform stage | The pipeline context object | End of run - write beside the output artifact |

---

## Choosing the unit of work

Apply the test from `design-principles.md` §1: *a step is something a user would name.*

Practical heuristics:

- **Start at the layer where branching decisions happen.** If a function's interesting output
  is "which of three paths did I take", it is a step. If it is "I transformed A into B", it
  probably is not.
- **Do not make repositories or DAOs steps.** Their calls belong as captured I/O inside the
  step that made them (`io-capture.md`), not as steps of their own.
- **A retry loop is not N steps.** It is one step that records how many attempts it took.
- **If you cannot write a one-line `summary` for it, it is not a step.** That is the test,
  not a formality.

Five to eight step types is typical. If you have thirty, you have chosen too fine a layer.

---

## Per-turn state, by mechanism

### An explicit state object passed through
Cleanest and most testable. Each unit returns its event; the orchestrator collects them. Use
this whenever the architecture already threads a context.

### A framework request-scoped store
`request.state` in FastAPI/Starlette, `res.locals` in Express, `RequestContextHolder` in
Spring. Fine, but couples domain code to the framework - the domain function now needs the
request. Prefer passing a small context object it does not have to know the shape of.

### A context variable
`contextvars.ContextVar` (Python), `AsyncLocalStorage` (Node), thread-locals (JVM/Ruby).
The right tool when a value must reach code you cannot change the signature of - see
`io-capture.md`. Two cautions:

- **Async safety.** `ContextVar` and `AsyncLocalStorage` are per-task and copy correctly
  across `await`. Plain thread-locals do not, in an async runtime. Check which you have.
- **Always uninstall in a `finally`.** A leaked collector attributes the next turn's I/O to
  this one.

### A graph state channel
LangGraph-style: declare a channel with an append reducer, have each node return its event
in a delta.

The trap: on a checkpointed thread the channel **persists across turns**, so it grows forever and every turn reports its predecessors. Reset it per turn. The reference does this with a sentinel value its custom reducer strips:

```python
def _add_reset(left, right):
    left, right = left or [], right or []
    if right and right[0] == _RESET:
        return right[1:]        # discard everything accumulated so far
    return left + right
```

Only the turn's first node may emit the sentinel. Assign indices at assembly.

---

## Turn boundaries that are not a response

**Queue workers and batch jobs** have no response to attach to. Persist the envelope keyed
by the job id and expose it through whatever already surfaces job status. The trace becomes
more valuable here, not less - there is no user watching.

**Streaming responses** cannot attach a trace to a header that has already been sent. Options,
in order of preference:

1. A final SSE event (`event: trace`) after the content stream completes. The client already
   has a message loop; this is one more case.
2. Persist it and return its id in an early metadata frame; the client fetches it if the user
   asks.
3. A trailing chunk in the response body, if the protocol allows.

Do not buffer the whole stream just to attach a trace.

**Multiple backend services.** If a turn spans services, either propagate a turn id and have
each service persist its own envelope under it (assemble on read), or have the edge service
collect sub-envelopes as nested steps. If you are already running distributed tracing,
propagate its trace id as your turn id so the two systems line up - do not invent a second
correlation id.

---

## When you already have OpenTelemetry

If the project runs OTel, say so during the survey and offer the choice explicitly, because
these solve overlapping problems for different audiences:

| | Spans (OTel) | Response-attached envelope |
| --- | --- | --- |
| Audience | Operators, in a backend | The end user, in the product |
| Lifetime | Sampled, retained days | Lives with the answer |
| Content | Timing, status, attributes | Decisions, inputs, provenance, narration |
| Reaches the client? | No | Yes |

They compose well: emit spans as you already do, and build the envelope alongside from the
same captured values. What this skill adds to a span is principles 4 (`summary`/`why`), 5
(inputs as received), and 6 (`null` ≠ `0`) - none of which a generic span carries by default.

If the user only needs operator-side visibility, **spans alone are the better answer** and
this skill should say so rather than adding a parallel system.

---

## Language notes

**Python.** `time.perf_counter()`, `contextvars.ContextVar`, `datetime.now(timezone.utc)`.
Builders as module-level functions returning dicts; `TypedDict` if you want the shape checked.

**TypeScript/Node.** `performance.now()`, `AsyncLocalStorage` from `node:async_hooks`.
Builders returning a discriminated union on the step kind - the union is what lets a consumer
switch exhaustively, and it is what `trace-visualize` expects as its L1.

**Go.** `time.Now()` with a monotonic reading, `context.Context` for the collector. Builders
returning structs with `*T` for genuinely-absent values, so `null` survives JSON encoding -
zero values will not.

**JVM.** `System.nanoTime()`, and a request-scoped bean or explicit context. Beware
thread-locals across reactive schedulers; use the reactive context instead.

Whatever the language: **an absent value must serialise as `null`, not as a type's zero
value.** Languages without nullable primitives need boxed or pointer types for this. It is
the most common way principle 6 gets silently lost at the serialisation boundary.
