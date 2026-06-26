# Risk, computed intelligently — what we're building

We're building an agent that, when asked something like *"how many buildings are at flood risk in
Battambang?"*, works out the **cheapest trustworthy way to answer from the data it actually has** —
and shows you its plan before it does any work.

Risk always means the same thing: **how dangerous the hazard is here × how vulnerable the people and
assets here are.** What changes between requests is *how much of that we have to compute ourselves.*
That's the ladder.

## The four layers — how much we recompute

- **Layer 1 — read the answer.** ADPC already published a finished risk map; we sample it where the
  user's assets are. *(This is all we have working today.)*
- **Layer 2 — combine ready-made scores.** No finished risk map, but the hazard and vulnerability maps
  are already graded 1–5. We multiply and add them with our chosen weights.
- **Layer 3 — grade the raw maps first.** Only the raw measurements exist (flood depth, population,
  built-up %). We grade each to 1–5 ourselves, then combine. This is where we control the cutoffs.
- **Layer 4 — go to the original sources.** Nothing usable locally; we fetch from the upstream
  providers (the flood model, population, buildings, maps) — a fixed, known list — then grade and combine.

Each layer is the fallback when the one above isn't available. **Lower = cheaper and more validated;
higher = more control but more work.**

## How the agent decides

For each piece of data a request needs, it works out:

1. **What exists vs. what must be fetched** — and picks the lowest usable layer.
2. **What the user is asking about** — roads/buildings/hospitals come from the map; population from a
   population dataset; and so on.
3. **Trust nothing blindly** — every file is opened and checked that it really is what it claims (right
   units, right scale). *(We already caught a map labelled "metres" that was actually millimetres — a
   1000× error avoided only by looking.)*
4. **Ask before deciding** — where there's a real choice (e.g. raw exposure vs precomputed vs
   recomputed risk), it surfaces the options and waits for you to pick rather than guessing. A fuller
   "here is the whole plan and its cost, approve before I fetch" gate is the direction this is heading.

## How we build it

Strictly one small, **demoable** step at a time, each tested on real data. We have Layer 1. The order:

**Foundation** (the file-checking gate) → **Layer 2** (the combine engine — flood first, then every
hazard) → **the resolver** (the brain that picks the layer) → **Layer 3** (grading raw maps) →
**Layer 4** (upstream sources) → **the approval step** (plan-before-run).

Layers 1–2 and the resolver are implemented today; Layers 3–4 and the full plan-approval gate are the
remaining work.
