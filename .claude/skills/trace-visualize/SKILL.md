---
name: trace-visualize
description: Build a view of an execution trace a backend already emits - a step list, timeline, printable report, flow diagram, terminal renderer, or trust badge - so someone can see what the server actually did and judge whether to trust the answer. Requires a backend that already emits a trace; pairs with trace-emit, which produces one. Use when a response already carries trace or observability data and someone wants to render it, extend existing frontend tracing, restyle an existing trace panel, add an execution-flow diagram, print a trace in the CLI, or show provenance in a UI.
metadata:
  origin: https://github.com/SERVIR-AI/global-platform
---

# Visualising an execution trace

Turn a trace the backend already produces into something a person can read.

See `references/layer-architecture.md` for more information on how to design the viz.

## What this skill assumes it has

Only itself: this file, `references/`, and `assets/`. No particular codebase is required. The **host project** is the thing to survey - its frontend, or its CLI if it
has no frontend - and the trace it consumes is the host backend's, in whatever shape that
backend chose.

## Stop: this skill has a precondition

**You cannot build a consumer for a response that does not exist.** This half depends on the
backend half. At least one of these must already be true before you start:

1. **The backend already emits structured tracing or observability data** that a consumer can
   read - and you have a real sample of it.
2. **The companion skill `trace-emit` has been run first**, and the backend now emits a trace.
3. **The user has a trace .json depicting a backend trace**. In this case, the frontend, in CLI or UI form, must be able to consume it from the backend before running this skill.

**If none are true, stop here.** Say so plainly and point at `trace-emit`. Do not design
a frontend against an imagined trace envelope, and do not build both the backend and frontend halves in one pass.
If the user supplies their own trace schema, ask them if it is already consumable, and suggest using the `trace-emit` skill to implement it.

Existing logs or spans do not automatically satisfy (1). Unstructured log lines and operator-only
spans also do not satisfy this constraint.

Get the emitted sample by either: calling the endpoint, reading a persisted trace the backend writes, asking the
user to paste one, or reading a committed fixture you can confirm still matches what the backend
emits. Use actual output from an actual call.

If a trace exists but is thin - one blob per request, timings only - say so before asking the user if they want to expand it. If they want to expand it, use `trace-emit`.

---

## Working with the user

**Start high level, then offer more.** Lead with what you propose to build and what it will
show.
Then ask them the following details explicitly: 
- Ask them in the beginning how technical they are, how familiar they are with the codebase, and then how involved they want to be in the design process. Accordingly change how frequently you go back to the user for input and how much of the process you show them. Also modulate how high or low level your explanations are accordingly. If the user wants to be heavily involved, ask them frequently once you come up with designs. If not, use more autonomy.
- Then offer detail explicitly - *"say **more detail** for the field-by-field breakdown, or **show me the code** for the shape I'd write."* 
  - Do not bury someone in mechanism if they asked for a summary, and do not withhold it from someone who wants it. Anything you are deciding is an option the user gets to see: **show the choices and your recommendation, not a settled answer.**

**Stop at three checkpoints.** Present options with a recommendation and a reason, then wait:

| # | When | What to ask |
| --- | --- | --- |
| 1 | After deriving the trace's shape | "Here is what your trace can and cannot support - is that right?" |
| 2 | Choosing a shape | "Here is what I would build and what I would leave out - including building nothing. Get the green light from the user regarding the architecture and visualization." |
| 3 | After the first view renders real data | "Here is one view against your real trace - continue?" |

---

## Step 0 - Read the real trace and write down its shape

Derive the shape **from the response**, not from assumptions:

- What is the top-level container? Is there a header (totals, ids, timestamps) or only a list?
- What identifies a step's kind? A `node`/`type`/`name` field is the discriminant that
  everything else keys off.
- Which fields appear on every step, and which only on some kinds?
- Which values can be absent, and how is absence encoded - `null`, a missing key, or a zero?
  **This matters more than it looks**; see `references/invariants.md` §1.
- Is there anything nested (I/O calls, sub-results, message transcripts)?
- Is one trace one turn, one request, or a whole conversation?
- When do they want the visualization available? At the end or per-turn?

Get more than one trace if you can: a success, a failure, and an unusual path. One sample makes
optional fields look required and absent values look impossible.

Report the shape you derived and confirm it before building on it.

---

## Step 1 - Design in three layers

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

## Step 2 - Offer the choice of whether, and what

Do not assume any of the below options are wanted. Present the options with a recommendation and let the user
pick. `references/view-shapes.md` covers each in detail. Below is a list of available shapes.
- **Step list** with expandable detail
- **Timeline / proportional bars**
- **Flow diagram** of the backend's nodes
- **Printable report**
- **Terminal output**
- **Trust badge**

Also settle: **who reads this**, which fields are for them versus for developers, where it
lives, and whether it ships behind a flag, or if all of it should always be visible.

**A frontend is not the only target.** An L2 output can feed a terminal renderer, a JSON
export, and a static report. Where the backend has no frontend, the CLI is the primary target
rather than a fallback.

---

## Step 3 - Build it

**Read `references/invariants.md` before writing render code, and again before shipping.**
Every rule in it is a case where the obvious implementation - the one you would reach for
without thinking - states something *false to the user*. They are correctness rules, not style
preferences, and they are why this is not just a table-rendering job.

The one default worth naming here: a raw JSON dump behind a collapse toggle is the trace with
none of the work done.

Instead: **progressive disclosure in three tiers** - a one-line verdict, then the
ordered steps with their summaries, then one step's full detail on demand, never open at full
detail. Long values collapse rather than truncate, and anything cut stays recoverable. Absent
values render as `-` with the reason, because "why is this blank?" is a question users ask and
the answer is data you already have. Audience is a tag in L2 filtered by one toggle, not two
components that drift apart. Large blocks of text must be rendered in a readable format and wrap text so the user does not have to scroll a lot.

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
informative as "it did", and only visible if the unused branches are on screen. If the user does not want skipped nodes drawn, dynamically render the chosen route in L3.

**A hardcoded topology drifts.** Guard it with a test comparing the diagram's nodes and edges
against the real backend, or derive it from the steps actually received. Without one of those,
the picture goes quietly wrong the first time the backend gains a node (`invariants.md` §7).

**Provenance and showing sources.** For RAG systems or systems where verifying the original source of data is important. Look at how the backend forwards sources used to cite data, and collaborate with the user on how they can display this in a digestible way. Are the sources grouped by category? Are they easily differentiable? Is it too cluttered to be useful? All of this requires user feedback.

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
