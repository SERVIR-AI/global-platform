# Invariants - rules a trace UI must keep

Each of these is a place where the obvious implementation states something **false to the
user**. They are not style preferences. Read before writing render code, and again before
shipping.

---

## 1. `null` is not `0`, and `0` is not blank

A well-built backend distinguishes "this did not happen", "this was not supplied", and "this
was zero". Collapsing them in the UI destroys that at the last step. Some examples:

| The backend says | It means | Render |
| --- | --- | --- |
| `provider: null` | No model was called | "no model ran in this step" |
| `tokens: null` | The model probably did not run, or the call failed. | `-` |
| `result: null` | The computation failed | "the calculation failed" |
| `result: {value: 0}` | It computed zero | `0` |

**Never backfill an absent value with a default.**

Give absence a dedicated representation in the view model - `{ kind: 'missing', <reason> }` -
so it cannot decay into an empty string on the way to the screen. Carry the *reason*: "why is
this blank?" is a question users ask, and you already have the answer.
Ask the backend which field is authoritative for each claim; do not infer. If
nothing can answer, do not guess, let the user know.

## 2. Backend-authored text is content, not labels

If a step carries a summary or explanation written server-side, **render it verbatim**. Do not
paraphrase, shorten, translate, regenerate, or write your own narration beside it.
A UI-side rewrite is a second source of truth that begins drifting immediately.

Everything the *frontend* invents - node labels, step titles, the word used for an absent
value - is yours. The line between the two must stay sharp.

## 3. Do not imply timing that did not happen

If the backend ran its steps in series, use proportional bars. A waterfall implies overlap
and concurrency that did not occur. Verify before designing: if per-step durations roughly
sum to the total, it was serial.

If nested I/O events carry no timestamps, they are an **ordered list**. Not a sub-timeline,
not a nested waterfall. Ordering may also be emission order rather than causal order - a
cache-check emitted before the download it triggers appears first.

## 4. Know what one trace covers

Find out whether one trace is one request, one turn, or a whole conversation - and design to
that.

The usual answer is **one envelope is one turn**: a question that pauses for input and the
answer that follows are two separate envelopes sharing a conversation id. Consequences:

- A turn that ends mid-flow because it paused for input is **complete and successful**, not
  truncated. Do not render it as a failure.
- Any cross-turn view is a join you perform. Nothing in the trace does it for you.

## 5. The trace can never break the product

| Case | Required behaviour |
| --- | --- |
| No trace on the response | Render nothing. Not an error state. |
| Zero steps | Empty state |
| A step carries an error | **Data.** Show it; downstream steps are legitimately absent |
| Malformed trace | Return `null` from L1 and indicate the malformed trace. Never throw. |
| An unknown step kind | Generic renderer - a raw dump is fine. A newer backend must not crash an older client. |

An error boundary is the last resort, not the plan. And **never let a product feature depend
on a trace field** - the moment it does, best-effort tracing becomes a silent outage.

## 6. Do not overstate a heuristic

The general rule: **a UI must not make a stronger claim than the field supports.** If you
cannot tell what a field means, ask, and read the code.

If the backend ships a verification or confidence flag, **read its implementation before
labelling it**.

A typical example is a `grounded` flag: true if the computed number appears verbatim in the
answer text. That is a substring test. It has real false negatives (a value of `241.0` against
an answer saying "about 241 km") and real false positives (a count of `3` "confirmed" by an
answer mentioning `13`), as well as honest `false` results.

Label it for what it does - "the computed number appears in the answer" - not for what it
implies - "Verified".

## 7. A hardcoded topology drifts

If the flow diagram hardcodes the backend's graph, it goes silently wrong the first time the
backend gains a node: the unknown step is dropped from the picture, and if it sits between two
nodes that already have an edge, that old edge is drawn as though the new node never ran.

Everything else degrades safely - the step list still shows the unknown step with its
backend-authored summary, and a generic detail renderer handles it. The diagram is the one
part that becomes *quietly inaccurate* rather than obviously broken.

Two acceptable answers:

- **Guard it.** A backend test that parses the diagram's topology and asserts its nodes and
  edges match the backend emission turns "the diagram is subtly wrong forever" into "the build is red
  until you fix it".
- **Derive it** from the steps actually received - always accurate, but it cannot show what
  was skipped, which is often the most informative part.

Choose deliberately. An unguarded hardcoded diagram is the worst of both.
