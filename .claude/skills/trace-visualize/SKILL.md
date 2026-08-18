---
name: trace-visualize
description: Build a view of an execution trace a backend already emits - a step list, timeline, printable report, flow diagram, terminal renderer, or trust badge - so someone can see what the server actually did and judge whether to trust the answer. Requires a backend that already emits a trace; pairs with trace-emit, which produces one. Use when a response already carries trace or observability data and someone wants to render it, restyle an existing trace panel, add an execution-flow diagram, print a trace in the CLI, or show provenance in a UI.
metadata:
  origin: https://github.com/SERVIR-AI/global-platform
---

# Visualising an execution trace

Turn a trace the backend already produces into something a person can read.

## What this skill assumes it has

Only itself: this file, `references/`, and `assets/`. No particular codebase is required. The **host project** is the thing to survey - its frontend, or its CLI if it
has no frontend - and the trace it consumes is the host backend's, in whatever shape that
backend chose.

## Stop: this skill has a precondition

**You cannot build a consumer for a response that does not exist.** This half depends on the
backend half. Exactly one of these must already be true before you start:

1. **The backend already emits structured tracing or observability data** that a consumer can
   read - and you have a real sample of it.
2. **The companion skill `trace-emit` has been run first**, and the backend now emits a trace.
3. **The user has a trace .json depicting a backend trace**. In this case, the frontend, in CLI or UI form, must be able to consume it from the backend before running this skill.

**If none are true, stop here.** Say so plainly and point at `trace-emit`. Do not design
against an imagined shape, and do not build both halves in one pass - a consumer written
against a guessed schema is rework. If the user supplies their own trace schema, ask them if it is implemented, and suggest using the `trace-emit` skill to implement it.

Existing logs or spans do not automatically satisfy (1). The test is whether something
structured and per-step comes out of the system today; unstructured log lines and operator-only
spans do not, and those cases go to `trace-emit` too.

Get the sample by calling the endpoint, reading a persisted trace the backend writes, asking the
user to paste one, or reading a committed fixture you can confirm still matches what the backend
emits. Actual output from an actual call - not a schema, not a type definition, not an example
from another project.

If a trace exists but is thin - one blob per request, timings only - say what it can and cannot
support before proposing a shape.

---

## Working with the user

**Start high level, then offer more.** Lead with what you propose to build and what it will
show. Then offer detail explicitly - *"say **more detail** for the field-by-field breakdown, or
**show me the code** for the structure I'd write."* Show the choices and your recommendation,
not a settled answer.

**Stop at three checkpoints.** Present options with a recommendation and a reason, then wait:

| # | When | What to ask |
| --- | --- | --- |
| 1 | After deriving the trace's shape | "Here is what your trace can and cannot support - is that right?" |
| 2 | Choosing a shape | "Here is what I would build and what I would leave out - including building nothing." |
| 3 | After the first view renders real data | "Here is one view against your real trace - continue?" |

---

## Step 0 - Read the real trace and write down its shape

Derive the shape **from the data**, not from assumptions:

- What is the top-level container? Is there a header (totals, ids, timestamps) or only a list?
- What identifies a step's kind? A `node`/`type`/`name` field is the discriminant that
  everything else keys off.
- Which fields appear on every step, and which only on some kinds?
- Which values can be absent, and how is absence encoded - `null`, a missing key, or a zero?
  **This matters more than it looks**; see `references/invariants.md` §1.
- Is there anything nested (I/O calls, sub-results, message transcripts)?
- Is one trace one turn, one request, or a whole conversation?

Get more than one trace if you can: a success, a failure, and an unusual path. One sample makes
optional fields look required and absent values look impossible.

Report the shape you derived and confirm it before building on it.

---

## Step 1 - Offer the choice of whether, and what

Do not assume a step list is wanted. Present the options with a recommendation and let the user
pick. `references/view-shapes.md` covers each in detail.

| Shape | Good when | Needs from the trace |
| --- | --- | --- |
| **Step list** with expandable detail | Users are technical, or it is a support tool | Everything |
| **Timeline / proportional bars** | Latency is the question | Per-step durations + a total |
| **Flow diagram** of the backend's nodes | The *path taken* is the story, including what was skipped | Step kinds + the graph topology |
| **Printable report** | Answers get forwarded to people who never see the app | Selectors only; no framework needed |
| **Terminal output** | There is no frontend, or developers are the audience | Whatever you choose to print |
| **Trust badge** | One fact matters, not the whole story | One or two fields |
| **Nothing** | Nobody will act on it | - |

**"Nothing" is a real answer.** A trace is pure addition: ignoring it breaks nothing and the
backend keeps recording regardless. If the users are non-technical, the UI is embedded or
kiosk, or nobody will act on it, a panel is maintenance without payoff.

These compose - a badge that opens a panel is a common and good answer.

Also settle: **who reads this**, which fields are for them versus for developers, where it
lives, and whether it ships behind a flag.

**A frontend is not the only target.** The same L2 output feeds a terminal renderer, a JSON
export, and a static report. Where the backend has no frontend, the CLI is the primary target
rather than a fallback.

---

## Step 2 - Build in three layers

The rule that makes a second view cheap:

> **The middle layer contains no markup. The render layer contains no field knowledge.**

```
L1  types / adapter    The wire shape, and a function normalising it into that shape.
L2  selectors          Pure functions: trace -> presentation-neutral view models. No framework.
L3  renderers          Consume L2 output. Decide only how it looks.
```

**L1 is where the host backend's shape is absorbed**, and it is what makes this portable. If
the backend emits `{step_name, description, elapsed_ms}`, L1 maps that onto the view models and
L2/L3 never learn it. Write the adapter against the real trace from step 0.

**L2 must not import your framework.** That single constraint is what lets a test, a script, a
terminal renderer, or a static report generator reuse it.

Full breakdown and exported surface: `references/layer-architecture.md`.

---

## Step 3 - Build it, and do not build the default

**Read `references/invariants.md` first.** Every rule in it is a case where the obvious
implementation states something *false to the user* - `null` rendered as `0`, a fact inferred
from an ambiguous count, backend-authored text paraphrased, serial steps drawn as a waterfall, a
malformed trace taken down the product. Those are correctness rules, not style preferences, and
they are the reason this is not just a table-rendering job.

Then know what you would otherwise produce. **Left alone, a trace UI converges on the same
thing every time**, and it is worth naming so you can refuse it:

- A raw JSON dump behind a collapse toggle, which is the trace with none of the work done.
- Every step expanded on open, so there is no verdict-first tier and nothing is scannable.
- Coloured status pills as the only signal for errored, skipped, and paused - unreadable to
  anyone not distinguishing those hues, and unreadable in print.
- Durations as raw milliseconds, unnormalised, so nobody can see which step dominated.
- Absent values as empty cells, which silently destroys the distinction the backend went to
  trouble to encode.
- A "✓ Verified" badge over whatever boolean happened to be available, claiming more than the
  field supports.

Each is the path of least resistance, and each makes the trace less true than the data it came
from. Where the host project's design system pins a decision, follow it; where it does not,
don't spend the freedom on one of the above.

What to do instead: **progressive disclosure in three tiers** - a one-line verdict, then the
ordered steps with their summaries, then one step's full detail on demand, never open at full
detail. Long values collapse rather than truncate, and anything cut stays recoverable. Absent
values render as `-` with the reason, because "why is this blank?" is a question users ask and
the answer is data you already have. Audience is a tag in L2 filtered by one toggle, not two
components that drift apart.

`references/view-shapes.md` has the per-shape detail, including readability and collapsible
sections.

### The flow diagram, if you build one

The most explanatory view, and the one with a maintenance cost worth stating out loud.

`assets/execution-graph.svg` is one backend's graph as a standalone, self-contained SVG. **It is
a starting point to redraw, not a diagram to ship.** Replace the node boxes and edge paths with
the host topology, keep the `id` and `data-state` attributes, and keep the CSS keyed off
`data-state` - rendering a turn is then setting one attribute per element (`visited`, `skipped`,
`errored`, `paused`) with no geometry changes.

**Draw skipped nodes, do not hide them.** "It never needed to ask you anything" is as
informative as "it did", and only visible if the unused branches are on screen.

**A hardcoded topology drifts.** Guard it with a test comparing the diagram's nodes and edges
against the real backend, or derive it from the steps actually received. Without one of those,
the picture goes quietly wrong the first time the backend gains a node (`invariants.md` §8).

---

## Reference files and assets

Load as needed; do not read them all up front.

- **`references/layer-architecture.md`** - before writing code. The L1/L2/L3 split, the adapter,
  the exported surface.
- **`references/view-shapes.md`** - choosing or designing a shape; readability and
  collapsible-detail specifics.
- **`references/invariants.md`** - before writing render code, and again before shipping.
- **`references/relevant-codebase-files.md`** - **only if the user asks to see the originating
  implementation** and has it checked out. A file map of it; nothing else here depends on it.
- **`assets/execution-graph.svg`** - one backend's flow graph, standalone and theme-aware, with
  `data-state` styling hooks. Redraw for the host topology.

No example trace ships here, because the only trace that matters is the host backend's. If
`trace-emit` is installed alongside, `../trace-emit/assets/` holds two real captured envelopes -
a paused turn and the turn that answered it - worth a look to see what a finished trace carries.
