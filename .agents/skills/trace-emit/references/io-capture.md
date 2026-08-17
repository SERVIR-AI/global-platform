# Capturing I/O you cannot see from the call site

## The problem

A step calls a helper that does several things worth recording:

```python
aoi = ingest.ensure_aoi(place, layers=["schools"])
```

Behind that one call: a geocoder was queried, an OSM mirror failed over twice, one raster
came from cache and another was downloaded. The step needs all of it. The return value is
`aoi` - a bundle of file paths. None of that information is in it.

Three bad answers:

1. **Change every return type** to `(value, events)`. Viral: it propagates up through every
   caller and every test, and it puts observability into the domain signature.
2. **Log it and scrape the logs.** Fragile, unordered, and unavailable in the response.
3. **Edit the dependency.** Not an option - you do not own it, and you must not edit
   `.venv/`, `node_modules/`, or vendored code.

## The pattern: an ambient collector, installed for the duration of the step

A context variable holds an optional collector. Code deep in the stack calls a module-level
`emit()`, which appends to whatever collector is currently installed, or does **nothing** if
there is none. The step installs one around its work and drains it when building its event.

Three properties make this work:

- **`emit()` is a no-op when nothing is installed.** Instrumented modules stay usable -
  and testable - outside a traced request, with no setup and no import cycle.
- **The collector is per-context**, so concurrent requests never mix. `ContextVar` and
  `AsyncLocalStorage` both copy correctly into async tasks.
- **The domain signature is untouched.** `ensure_aoi` still returns `aoi`.

---

## Python

```python
import contextvars

_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("io_collector", default=None)


class IOCollector:
    """Accumulates {kind, ...} io events for one unit of work."""
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        self.events.append(event)

    def drain(self) -> list[dict]:
        events, self.events = self.events, []
        return events


def install(collector):
    "Make `collector` active for this context; returns a token for uninstall()."
    return _ACTIVE.set(collector)

def uninstall(token) -> None:
    "Undo install(), restoring whatever collector (if any) was active before."
    _ACTIVE.reset(token)

def emit(event: dict) -> None:
    "Record one io event on the active collector, if any (else a no-op)."
    collector = _ACTIVE.get()
    if collector is not None:
        collector.record(event)
```

At the emission sites, deep in the module:

```python
def _overpass(query, attempts=3):
    for attempt in range(attempts):
        for url in MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, timeout=TIMEOUT)
                if r.status_code in (429, 504):
                    continue
                r.raise_for_status()
                elements = r.json()["elements"]
                emit({"kind": "api", "api": "Overpass", "mirror_used": url,
                      "attempts": attempt + 1, "n_elements": len(elements)})
                return elements
            except requests.RequestException:
                pass
        time.sleep(2 ** attempt)
    raise RuntimeError("Overpass unavailable")
```

At the step:

```python
def fetch(state):
    collector = IOCollector()
    token = install(collector)
    try:
        aoi = ingest.ensure_aoi(place, layers=needed)
        event = make_trace_event_fetch(..., drained_io_events=collector.drain(), error=None)
        return {"aoi": aoi, "events": [event]}
    except Exception as e:
        event = make_trace_event_fetch(..., drained_io_events=collector.drain(), error=str(e))
        return {"error": ..., "events": [event]}
    finally:
        uninstall(token)                  # ALWAYS. A leak misattributes the next turn.
```

`uninstall` belongs in `finally`, and `drain()` must run on the error path too - the I/O that
happened before the failure is usually the most interesting part of it.

## TypeScript / Node

```ts
import { AsyncLocalStorage } from 'node:async_hooks';

type IOEvent = { kind: string; [k: string]: unknown };
const storage = new AsyncLocalStorage<IOEvent[]>();

export const emit = (event: IOEvent): void => { storage.getStore()?.push(event); };

export const collectIO = async <T>(fn: () => Promise<T>): Promise<[T, IOEvent[]]> => {
  const events: IOEvent[] = [];
  const value = await storage.run(events, fn);
  return [value, events];
};
```

`storage.run` scopes the store to the callback, so there is no uninstall to forget. When the
callback may throw and you still want the events, capture `events` outside and read it in a
`finally` - the array is the same reference either way.

**Go:** put the collector on the `context.Context` and pass it down; a mutex-guarded slice or
a buffered channel both work. **JVM:** a request-scoped bean, or the reactive context - not a
plain `ThreadLocal` if anything is reactive.

---

## What to put in an I/O event

Keep it flat and uniform enough to group after the fact. The reference splits one drained
list into `api_calls` and `downloads` at build time on `kind`, which means emitters never
have to know which bucket they land in:

```python
api_calls = [e for e in drained if e.get("kind") == "api"]
downloads = [e for e in drained if e.get("kind") != "api"]
```

Useful fields:

| Field | For |
| --- | --- |
| `kind` | `api` / `download` / `clip` / `cache` - how it gets grouped |
| `api` | Which third party |
| `op`, `query` | What was asked |
| `mirror_used`, `attempts` | Which endpoint answered, after how many tries |
| `n_results`, `n_elements` | Size of the response |
| `was_cached` | Reused or fetched - **emit on both branches** |
| `dest`, `layer`, `filename` | What was produced, and where |

---

## Known limits of this pattern

Write these down wherever the events are documented; consumers will otherwise assume
otherwise.

- **No timestamps unless you add them.** The reference records none, so the list is *ordered*
  but is not a timeline. Anything rendering it as a waterfall is inventing precision. If you
  want sub-step timing, put a monotonic offset in the event at emission - retrofitting it
  later is a schema change.
- **Emission order, not causal order.** A cache-check event emitted before the download it
  triggers appears first. Fine, as long as it is expected.
- **Only what you emit exists.** In the reference, `emit` sits on the success path of the
  retry loop, so failed mirrors leave no record and `attempts > 1` is the only evidence of a
  failover. Defensible; just not obvious to a reader.
- **Anything not instrumented is invisible.** A library making its own HTTP calls will not
  appear. If completeness matters more than selectivity, instrument at the HTTP-client layer
  (a `requests` adapter, an axios interceptor, an `httpx` event hook) so *every* call is
  captured - then filter, rather than relying on remembering to call `emit`.
- **Concurrency.** If a step fans out with `asyncio.gather` or `Promise.all`, each task
  inherits a copy of the context and appends to the same collector. Ordering is then
  completion order, not start order - record a sequence number at emission if that matters.

## Testing it

The pattern is worth three small tests, and they catch the failure modes that actually occur:

```python
def test_emit_is_a_noop_when_nothing_is_installed():
    emit({"kind": "api"})                     # must not raise

def test_collector_records_and_drains():
    c = IOCollector(); token = install(c)
    emit({"kind": "api"}); uninstall(token)
    assert len(c.drain()) == 1
    assert c.drain() == []                    # drain empties

def test_collectors_do_not_leak_across_installs():
    c1 = IOCollector(); t1 = install(c1); emit({"kind": "a"}); uninstall(t1)
    c2 = IOCollector(); t2 = install(c2); emit({"kind": "b"}); uninstall(t2)
    assert c1.drain() == [{"kind": "a"}] and c2.drain() == [{"kind": "b"}]
```

The third is the one that matters: a leaked collector silently attributes one turn's I/O to
another, and nothing else will catch it.
