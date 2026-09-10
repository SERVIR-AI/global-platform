# Choosing and building a shape

Six answers. Suggest one and present appropriate ones; do not assume the first one.

---

## A. Step list with expandable detail

Ordered rows, one per step: a title, the backend-authored summary, a proportional duration
bar, a status. Clicking one opens its fields, split by audience.

**Needs:** everything. **Good when** users are technical, or this is a support tool.
**Cost:** the largest surface - one render branch per field kind, plus expansion state.

The default choice, and worth defending against rather than defaulting into.

## B. Timeline / proportional bars

The same steps as bars sized by duration.

**Needs:** per-step durations and a total. **Good when** latency is the question - a slow turn
should show *which part* was slow.

**Constraint:** if the backend ran its steps in series, bars must be proportional, not a
waterfall. A waterfall implies concurrency and overlap that did not happen. Check before
designing: `sum(step durations) ≈ total` means serial.

If nested I/O (external calls, downloads) carries no timestamps, it is an **ordered list**,
not a sub-timeline. Rendering it as one invents precision that is not in the data.

## C. Flow diagram

If the backend's sequential steps can be modeled as a graph of sequential and branching nodes, with nodes, external calls and methods represented as boxes, with the path this turn took highlighted and the branches it
did not take drawn dimmed or dashed.

**Needs:** step kinds, plus the graph topology from somewhere. **Good when** the *shape* of
what happened is the story - especially when a skipped branch is meaningful ("it never had to
ask you anything", "it stopped before it computed anything"). Also good when the backend takes different branches or 'decisions'.

**Draw skipped nodes.** Hiding them removes the most informative part.

**The cost:** the topology has to come from somewhere. Either hardcode it - and then guard
against drift with a test that compares it to the real backend, because otherwise the picture
goes quietly wrong the first time a node is added - or derive a simpler diagram from the steps
actually present in the trace, which is always accurate but cannot show what was skipped.

That tradeoff is real and should be stated to the user, not decided silently.

See SKILL.md step 3 and `assets/execution-graph.svg`.

## D. Printable / exportable report

One static document per answer: what was asked, what was extracted, what data was read, what
came out, whether it was verified.

**Needs:** L2 only. Because L2 imports no framework, a Node script or a server-side renderer
produces this with no browser. **Good when** answers get forwarded to people who never see
the app - regulators, colleagues, a report.

Cheap once the step list exists, and often the highest-value shape for non-technical users.

## E. Terminal output

The same steps printed to a console - one line per step, or a tree, behind a verbosity flag.

**Needs:** whatever you choose to print. **Good when** there is no frontend at all, when the
audience is developers, or when the trace is being read next to logs during an incident.

It is an L3 like any other: the same selectors, a different renderer. Nothing about it belongs
in L1 or L2, and if a project builds this first, a UI later costs only its own renderer.

## F. Compact trust badge

One inline indicator beside the answer, expanding to a sentence or two.

**Needs:** one or two fields. **Good when** a single fact matters and a panel is overkill.

**Say what the field means, not what it implies.** If the backend's verification is a
heuristic, the badge must not read "Verified". Read what the flag actually does first. A
typical one: "grounded" meaning the computed number appears verbatim in the answer text - a
substring test, with real false negatives (`241.0` against "about 241 km") and real false
positives (a count of `3` "confirmed" by an answer mentioning `13`). Label it for the test it
performs. A badge overstating a heuristic is worse than no badge, because it manufactures
confidence.

## Composition

These are not exclusive. Badge → opens panel → panel contains list + diagram, with a report
export, is one coherent product built from four of them.

---

# Readability

A trace is dense. The UI's job is to get someone from "was this fine?" to "what happened in
step 3?" without reading everything.

## Three tiers, always

1. **Verdict** - one line. Outcome, duration, and the single most trust-relevant fact.
2. **Steps** - the ordered list with summaries. Scannable.
3. **Detail** - one step's fields, on demand.

Never open at tier 3. The tiers are the feature.

## Collapsible sections

Use real disclosure semantics - `<details>/<summary>`, or a framework equivalent with correct
`aria-expanded` and a focusable trigger. Not a `div` with a click handler.

Why it matters concretely: `<details>` content is findable by in-page search, reachable by
keyboard, exposed to screen readers, and printable. A hand-rolled toggle usually loses all
four.

- **Independent state per step.** Opening one must not close another.
- **Persist expansion across re-renders** - key it by step index, not by array position in a
  list that can be replaced.
- **Expand-all / collapse-all** once there are more than a handful.
- **Print styles open everything.** Nobody wants a printed page of collapsed triangles.

## Markdown

If the backend authors summaries containing emphasis, code spans, or lists, they were written
to be read as markdown. Render them.

- Render **safely** - sanitise, or use a renderer that does not emit raw HTML. Backend-authored
  text can still contain user-supplied fragments (a place name, a question, a filename).
- Keep code spans monospace. Field values and layer names are unreadable in prose type.
- Render the markdown; do not rewrite the text (`invariants.md` §2).

## Long values

Collapse, never silently truncate:

- **Transcripts** - collapsed by default, one line per message with role and a preview.
- **Argument blobs / raw JSON** - pretty-printed in a collapsed, scrollable block. Never let
  one force horizontal scrolling on the page.
- **Long lists** - show the first few and "+N more".
- **Absolute paths and ids** - middle-truncate with the full value available on hover or copy.
- For long pieces of text, they must be wrapped and scrollable.

Anything cut must be recoverable. A truncation with no way to see the rest is a lie about the
data.

## Absent values

`-` with the reason on hover, from the descriptor's `reason`. Never an empty cell, never a
zero. "Why is this blank?" is a question users ask, and the answer is data you already have.

## Audience toggle

One control, filtering on the `audience` tag from L2. Default to the user view. Do not build
two components - the tag is the split (`layer-architecture.md`).

## Accessibility and theming

- The trace is supplementary: it must not steal focus or trap it. Escape closes.
- **Never encode status by colour alone.** Errored, skipped, and paused need a shape, icon, or
  label too - the flow diagram is where this is most often got wrong.
- Duration bars need a text value beside them; a bar alone is unreadable to a screen reader.
- Use semantic theme tokens rather than hardcoded colours, so the panel follows light/dark.
- The shipped SVG carries `role="img"` and a `<title>`; keep those when you redraw it.
