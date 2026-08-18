# Capturing I/O you cannot see from the call site

## The problem

As an example, the below is a step calls one helper that does several things worth recording:

```python
data = load_dataset(name, layers=["schools"])
```

Behind that one call: a lookup service was queried, one endpoint failed over twice, one file
came from cache and another was downloaded. The step needs all of it. The return value is
`data`. None of that information is in it.

Three bad answers:

1. **Change every return type** to `(value, events)`. Viral: it propagates up through every
   caller and every test, and it puts observability into the domain signature.
2. **Log it and scrape the logs.** Fragile, unordered, and unavailable to the trace.
3. **Edit the dependency.** Not an option - you do not own it, and you must not edit
   `.venv/`, `node_modules/`, or vendored code.

---

## The pattern: an ambient collector, installed for the duration of the step

Every runtime has a **request-scoped ambient store**: a place a value can live for the duration
of one logical operation, readable by code deep in the stack without anyone passing it down.

Put an optional collector there. Code deep in the stack calls a module-level `emit()`, which
appends to whatever collector is currently installed, or does nothing if there is none. The
step installs one around its work and drains it when building its event.

Four properties make this work. Keep all four whatever the language:

1. **`emit()` is a no-op when nothing is installed.** Instrumented modules stay usable - and
   testable - outside a traced request, with no setup and no import cycle.
2. **The collector is scoped to one logical operation**, so concurrent requests never mix.
3. **Domain signatures are untouched.** `load_dataset` still returns `data`.
4. **Installation is always undone**, on the error path too. A leaked collector attributes the
   next turn's I/O to this one.

### The mechanism, by language

| Runtime | Ambient store | Notes |
| --- | --- | --- |
| Python | `contextvars.ContextVar` | Copies correctly into `asyncio` tasks. Not a plain thread-local. |
| Node / TypeScript | `AsyncLocalStorage` (`node:async_hooks`) | `storage.run(store, fn)` scopes it to a callback - nothing to uninstall. |
| Go | A value on `context.Context` | Explicit, since `context` is already threaded. Guard the slice with a mutex if the step fans out. |
| JVM | A request-scoped bean, or the reactive context | **Not** a plain `ThreadLocal` if anything is reactive - it will not follow the scheduler. |
| Ruby / Rails | `ActiveSupport::CurrentAttributes`, or a fiber-local | Reset per request; the framework does not always do it for you. |
| .NET | `AsyncLocal<T>` | Flows across `await` the same way `ContextVar` does. |

The rest of this file is one implementation in full. Python, because it is the fiddliest - it
has an explicit install and uninstall to get wrong. Adapt the shape, not the API.

---

## Reference implementation

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
def _query(payload, attempts=3):
    for attempt in range(attempts):
        for url in ENDPOINTS:
            try:
                r = requests.post(url, data=payload, timeout=TIMEOUT)
                if r.status_code in (429, 504):
                    continue
                r.raise_for_status()
                results = r.json()["results"]
                emit({"kind": "api", "api": "Overpass", "endpoint_used": url,
                      "attempts": attempt + 1, "n_results": len(results)})
                return results
            except requests.RequestException:
                pass
        time.sleep(2 ** attempt)
    raise RuntimeError("all endpoints unavailable")
```

At the step:

```python
def fetch(state):
    collector = IOCollector()
    token = install(collector)
    try:
        data = load_dataset(name, layers=needed)
        event = make_fetch_event(..., drained_io_events=collector.drain(), error=None)
        return {"data": data, "events": [event]}
    except Exception as e:
        event = make_fetch_event(..., drained_io_events=collector.drain(), error=str(e))
        return {"error": ..., "events": [event]}
    finally:
        uninstall(token)                  # ALWAYS. A leak misattributes the next turn.
```

`uninstall` belongs in `finally`, and `drain()` must run on the error path too - the I/O that
happened before the failure is usually the most interesting part of it.

The Node version needs no `finally`, because the store is scoped to a callback:

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

When the callback may throw and you still want the events, capture `events` outside and read it
in a `finally` - the array is the same reference either way.

---

## What to put in an I/O event

Keep it flat and uniform enough to group after the fact. Splitting one drained list into
buckets at build time on `kind` means emitters never have to know which bucket they land in:

```python
api_calls = [e for e in drained if e.get("kind") == "api"]
downloads = [e for e in drained if e.get("kind") != "api"]
```

Useful fields:

| Field | For |
| --- | --- |
| `kind` | `api` / `download` / `cache` - how it gets grouped |
| `api` | Which third party |
| `op`, `query` | What was asked |
| `endpoint_used`, `attempts` | Which endpoint answered, after how many tries |
| `n_results`, `bytes`, `status` | Size and outcome of the response |
| `was_cached` | Reused or fetched - **emit on both branches** |
| `dest`, `layer`, `filename` | What was produced, and where |

### Reduce before you emit

This is where request and response bodies get captured, so this is where the capture scope
agreed in `SKILL.md` step 1a is enforced. Truncate, hash, redact by key name, or summarise
**at the `emit()` call** - not later. Once a full body is in the collector it will reach the
envelope, and the assembler has no idea what any of it is.

Headers are the usual leak: `Authorization`, `Cookie`, and API keys in query strings.

---

## Known limits of this pattern

Surface the limitations when implementing this pattern, so users can have a better idea of what to include within the io capture.

- **No timestamps unless you add them.** The list is *ordered*, but it is not a timeline.
  Anything rendering it as a waterfall is inventing precision. If you want sub-step timing, put
  a monotonic offset in the event at emission - retrofitting it later is a schema change.
- **Emission order, not causal order.** A cache-check event emitted before the download it
  triggers appears first. Fine, as long as it is expected.
- **Only what you emit exists.** If `emit` sits on the success path of a retry loop, failed
  attempts leave no record and the attempt count is the only evidence of a failover.
- **Anything not instrumented is invisible.** A library making its own HTTP calls will not
  appear. If completeness matters more than selectivity, instrument at the HTTP-client layer
  instead - a `requests` adapter, an axios interceptor, an `httpx` event hook - so *every* call
  is captured, then filter. That is more robust than relying on remembering to call `emit`.
- **Concurrency.** If a step fans out with `asyncio.gather` or `Promise.all`, each task
  inherits a copy of the context and appends to the same collector. Ordering is then completion
  order, not start order - record a sequence number at emission if that matters.

---

## Testing it

Three small tests, and they catch the failure modes that actually occur:

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
