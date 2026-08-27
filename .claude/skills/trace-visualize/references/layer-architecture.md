# The three-layer split

```
L1  types / adapter    The wire shape, and a function normalising the host backend into it.
L2  selectors          Pure functions: trace -> presentation-neutral view models.
L3  renderers          Consume L2 output. Decide only how it looks.
```

> **L2 contains no markup. L3 contains no field knowledge.**

Samples below are TypeScript for concreteness. The split is language-independent; only L3 is
framework-specific.

## Why the split pays for itself

A second visualisation is always wanted eventually - a printable report, a compact badge, a
redesign. If renderers read the trace directly, every rewrite re-derives "which field means
what", scattered across whichever one happened to need it. With the split, a rewrite replaces
L3 and leaves L1 and L2 untouched.

---

## L1 - the adapter, and why it is the portable part

L1 does two things: describe the shape L2 consumes, and map the host backend's actual output
onto it.

**Nothing prescribes a wire format.** A backend chooses its own field names; this layer absorbs
whatever it chose. The tradeoff is deliberate and worth knowing: two teams' traces are not
interchangeable without a small adapter, which is the price of fitting real architectures
rather than bending them. L1 is where that price is paid, once.

**If the backend was built with `trace-emit`**, the mapping is close to 1:1 and L1 is mostly
type declarations.

**If the backend emits something else**, L1 is where that is absorbed - and everything above
stays unchanged:

```ts
// The backend emits {step_name, description, elapsed_ms, failed}. Nothing above knows.
const adaptStep = (raw: RawStep): TraceStep => ({
  node: raw.step_name,
  summary: raw.description,
  why: raw.rationale ?? '',
  duration: raw.elapsed_ms,
  error: raw.failed ? raw.error_text : null,
  ...raw,
});
```

Write this against the **real trace** captured in SKILL.md step 0. An adapter written against
an imagined shape is a failure.

### Parsing must never throw

L1 owns validation, and its contract is *return null, never raise*:

```ts
export const parseEnvelope = (raw: unknown): TraceEnvelope | null => {
  if (!isRecord(raw) || !Array.isArray(raw.steps)) return null;
  const steps = raw.steps.filter(isTraceStep);
  if (steps.length === 0 && raw.steps.length > 0) return null;   // all malformed
  return { ...header(raw), steps };
};
```

Two details worth getting right:

- **Recompute a derivable header rather than rejecting.** If totals are missing or
  non-numeric but the steps are fine, sum them yourself. A missing header is a cosmetic
  defect, not a reason to drop the trace. **Match the backend's own summing rules** when you
  do - if the backend skips steps with no token data rather than counting them as zero, so
  must you, or the two disagree. For any additional derivation done in the frontend, notify the user.
- **Distinguish "no steps" from "all steps malformed".** A trace returning zero steps is an empty
  state; a list that parsed to nothing is a broken trace. They deserve different treatment.

### Type the step union on its discriminant

If your language has them, make the step kind a discriminated union rather than one wide
optional-everything type:

```ts
export type TraceStep = ParseStep | FetchStep | ComputeStep | RespondStep;
```

The payoff is in L2: an exhaustive `switch` over the union means the compiler tells you
exactly where to add a case when the backend grows a step kind. You cannot forget one. Keep a
`default` branch anyway, for data from a newer backend than the client.

---

## L2 - selectors

Pure functions from a parsed trace to view models. No framework imports, no formatting
decisions that belong to a designer, no DOM.

A surface that works, as a shape to copy:

| Function | Returns | Answers |
| --- | --- | --- |
| `summarizeEnvelope` | `{stepCount, totalDurationMs, tokensTotal, costUsd, outcome, usedModel}` | "how did this turn go, in one line?" |
| `toStepRows` | ordered rows: `{index, node, title, summary, why, durationMs, durationFraction, status, error, step}` | "what do I lay out?" |
| `toStepFields` | field groups, each field tagged `user` or `developer` | "what is worth showing about this step, to this audience?" |
| `toGraphPath` | `{nodeStates, edgesTaken, paused}` | "which parts of the graph did this turn touch?" |
| `stepUsedModel` | boolean | "did a model actually run here?" - the authoritative test |
| formatters | strings | duration, tokens, cost, text |

Three things to steal:

**`durationFraction`, not raw milliseconds, for bars.** L2 computes the 0..1 share of the
turn; L3 multiplies by a width. The renderer never divides.

**`status` as a small enum** (`ok` / `error` / `paused`), computed once from whatever
combination of fields implies it. Every renderer then agrees on what "this step failed"
means.

**One function per ambiguous question.** Where the trace has a trap - a field that looks like
it answers a question but does not - encode the correct test as a named L2 function and use it
everywhere. A common one is `stepUsedModel`: it must read an explicit provider field rather
than a token count, because a step that never called a model may still emit zeroed tokens. A
single named function is what stops that trap being rediscovered per component.

### Field descriptors as data

`toStepFields` returns descriptors, not markup:

```ts
type TraceFieldValue =
  | { kind: 'text';       text: string }
  | { kind: 'code';       text: string }
  | { kind: 'list';       items: string[] }
  | { kind: 'flag';       value: boolean; label: string }
  | { kind: 'missing';    reason: string }        // renders as an em dash + tooltip
  | { kind: 'transcript'; messages: Message[] }
  | { kind: 'json';       value: unknown };

interface TraceField {
  key: string; label: string;
  audience: 'user' | 'developer';
  hint?: string;                                  // tooltip; on `missing`, the explanation
  value: TraceFieldValue;
}
```

Implement one render branch per `kind` and every field of every step kind renders. **Keep the
union small** - each member is a branch every future renderer must reimplement. Adding one
should feel expensive.

The `audience` tag *is* the user/developer split. No component decides it, so the two views
cannot drift, and moving a field between them is a one-line data change.

`{ kind: 'missing', reason }` is how `null` survives to the screen. Without a dedicated
member, absent values quietly become empty strings and the distinction is lost at the last
step - see `invariants.md` §1.

---

## L3 - renderers, plural

The only framework-specific layer, and it should be boring: take view models, decide
appearance. If a renderer reaches into a raw step to work out what a field means, that logic
belongs in L2.

**Plural is the point.** Because L2 imports no framework, a terminal renderer, a static HTML
report, and a framework component are all L3s over the same selectors - each deciding only how
things look. That is the return on keeping L2 clean, and it is why the printable and CLI shapes
are nearly free once the step list exists.

Wrap a UI trace panel in an error boundary. It is the last resort, not the plan - the layers
below should already return `null` rather than throw.

---

## A file layout that works

One module per layer, whatever the language:

| Module | Layer | Contents |
| --- | --- | --- |
| `types` | L1 | The discriminated union and per-step interfaces |
| `parse` | L1 | `parseEnvelope`, and a builder for envelopes assembled from loose steps |
| `selectors` | L2 | `summarizeEnvelope`, `toStepRows`, `stepUsedModel`, formatters |
| `fields` | L2 | `toStepFields` and the field descriptor types |
| `labels` | L2 | Every user-facing string the *consumer* invents |
| `graphTopology` | L2 | Node and edge geometry as data |
| `graphPath` | L2 | `toGraphPath` |
| `components/` or `render/` | L3 | Panel, summary, step list, step detail, field value, graph |

Splitting L2 across several small modules rather than one file is what keeps the graph code out
of the way of projects that never draw one.

### Keep invented strings in one place

`labels` holds every user-facing string the consumer makes up - node labels, step titles, the
reason shown for each kind of absent value. Renaming anything is then one file, and it stays
obvious which wording is yours and which is the backend's.

The line to hold: **strings the backend authored are not labels.** They are content, rendered
verbatim (`invariants.md` §2). Never move one into `labels` "to keep strings together".
