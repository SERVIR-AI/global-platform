---
name: trace-emit
description: Add execution tracing and observability to a backend, so every API response from it carries a structured record of what the program actually did, step by step - which functions/major components ran, how long each took, which external services, API's and/or local data stores and caches were hit, what was requested and returned from those external services, the query, cost and usage details for LLM interactions, and what each step decided.  Use this skill when a user wants to implement observability for a service or add onto it, emit a trace envelope on an API response, make an agent or pipeline auditable, expose why an answer came out the way it did, or port this pattern from a reference implementation to their own stack.
compatibility: Any backend language or framework. Examples are given in Python and TypeScript; the pattern needs only per-request state and a way to wrap a unit of work. Reference implementation is FastAPI + LangGraph.
allowed-tools: Read, Grep, Glob, Edit, Write, Bash
metadata:
  origin: https://github.com/SERVIR-AI/global-platform
  layer: backend
  pairs-with: trace-visualize
---

# Emitting an execution trace

Instrument a backend so each API response can carry a structured, per-step record of how the relevant output was produced. This teaches a **pattern**, not a schema. Field names are the host project's to choose; what matters is what each step captures and when it is captured.

**This is the backend half.** Rendering the trace is a separate job with a separate skill, `trace-visualize` - and it cannot start until this one has produced a real trace. Do not build a UI in the same pass.

---

## What this skill assumes it has

Only itself: this file, `references/`, and `assets/`. Everything needed to implement the pattern is here, including two real captured trace envelopes and the code shapes that produced them.

It does **not** assume the codebase this pattern came from is available, and must never require it. That reference implementation - a FastAPI + LangGraph geospatial risk agent - is described in `references/worked-example.md` in enough detail to learn from without reading a line of it. If the user happens to have it checked out and asks you to look at it, do. Otherwise never ask for it and never block on it.

The **host project** is the thing to survey. Assume it looks nothing like the reference.

---

## Working with the user

This is a design task with real tradeoffs, done *with* someone. Two habits carry the whole skill.

**Start high level, then offer more.** Lead with a short, plain-language version: what you propose to record, roughly what it costs, and what it will not tell them.

Then ask them the following details explicitly: 
- Ask them in the beginning how technical they are, how familiar they are with the codebase, and then how involved they want to be in the design process. Accordingly change how frequently you go back to the user for input and how much of the process you show them. Also modulate how high or low level your explanations are accordingly.
- *"say **more detail** for the field-by-field breakdown, or **show me the code** for the shape I'd write."* 
  - Do not bury someone in mechanism if they asked for a summary, and do not withhold it from someone who wants it. Anything you are deciding is an option the user gets to see: **show the choices and your recommendation, not a settled answer.**

**Stop at four checkpoints.** These are the four decisions that are expensive to reverse, so at each one, present the options with a recommendation and a reason, then **wait for a reply** before continuing:

| # | When | What to ask |
| --- | --- | --- |
| 1 | After the Step 0 survey | "Here is what I found - is my reading of your system right?" |
| 2 | Step 1a, capture scope | "Here is what I would record and what I would leave out - approve? **Always** include a table of which parameters will be included, could be included, and your recommendation." |
| 3 | Step 1b, delivery surface | "Here is where it would surface and who reads it - right?" |
| 4 | After the first instrumented step works | "Here is one real step, end to end - do the rest the same way?" |

Never widen scope silently between checkpoints.

---

## Rules that hold for the whole task

1. **The user decides; you recommend.** Granularity, field names, capture scope, and file locations are theirs. Build incrementally against the checkpoints above - lay out a roadmap, propose it, implement what they agreed to. Do not one-shot the whole feature unless they ask for it.
2. **Make minimal changes to code while implementing tracing.** Implement traceability using the least amount of changes in general for readability and user awareness. If user code from elsewhere is imported, there are API calls, or collecting traces requires changing several method signatures, consider wrapping it or collecting around it using ContextVars - see `references/io-capture.md`. If instrumenting something would require editing a dependency, stop and say so. Implementing tracing and observability will require changes to their code.
3. **Tracing is never load-bearing.** Assembly and persistence go inside a `try`/`except` that swallows everything. A tracing bug must never change or break a response. Build this in from the first commit, not at the end.
4. **Backend and frontend are separate changes**, separately proposed and separately approved.
5. **Ensure that any tracing code cannot run executable code based on the end-user's output in the application**. Implementing this should not compromise the security of the application.

---

## Step 0 - Survey, then report

Before proposing anything, find out what is actually there. Look for:

| Question | What to look for |
| --- | --- |
| Is this the correct branch? | Look at available branches and latest commits. Confirm with the user before surveying that they are on the correct branch. |
| What is a "turn" or "step"? | One HTTP request? One agent run? One queued job? This is the trace's unit. |
| What are the steps? | Route handlers, service functions, graph nodes, middleware, tool calls, pipeline stages, data retrieval/loading/writing. |
| What type of application is it? | Is it a chatbot? (If so, turn-based traces are needed). Is it one-shot? (If so, traces do not have to be grouped by turn) |
| Where can per-request state live? | A framework request object, a context variable, an explicit state dict threaded through, a graph state channel, or similar. |
| Is there existing observability? | Logging, OpenTelemetry, APM, a `verbose` flag, an existing `trace` field. **Reuse or extend it rather than adding a parallel system.** Only propose replacing it if it cannot be emitted as structured output for a consumer to read - and say why before you do. |
| What does the response model look like? | Where would a trace attach, and does the schema need a new field? |
| Are there LLM calls? | Which SDK, and does its response object expose token usage? If so, is there a way to calculate cost? |
| Are there external calls or caches? | HTTP clients, object storage, downloads, memoization, on-disk caches. |
| Could this application benefit from provenance and sourcing? | RAG systems, deriving answers from data sources, referencing real data, systems where source data is extensively used. |
| What is the response boundary? | The single place every response is assembled - where the envelope gets attached. |
| Where will the trace be exposed? | Look for details on how the user views data, whether it is visually using a GUI, using a CLI, a raw JSON response, etc. |
| Who will read the trace, and where? | End users in a product, developers debugging, an operator reading logs, or someone opening a JSON file after the fact. Different answers lead to different capture scope and a different surface - this feeds Step 1a and 1b. |

Report what you found in a few lines before moving on. For any suggestions or proposals, give a high-level explanation of why you recommended that. If the project already has OpenTelemetry and the user wants spans rather than a response-attached envelope, say so - that is a different and often better answer, and this skill's per-step design still applies to what each span records.

---

## Step 1 - Propose, and let the user choose

Three separate agreements. Get each one explicitly, and say what will change and what will not.

### 1a. What gets captured - checkpoint 2

Do not decide this alone, and do not present it as already settled. Show the menu with a recommendation per row and the reason. The defaults below are a starting point, not an answer:

| Capture | Suggested default | Why it matters |
| --- | --- | --- |
| Step order, timing, outcome | **Yes** | Cheap, safe, and the backbone of everything else. |
| What each step decided, and why | **Yes** | The whole point. A trace without decisions is a profiler. |
| Inputs each step received | **Yes** | The record of what it was actually asked to do. |
| Errors, as data on the failing step | **Yes** | Decide whether stack traces reach a client - they expose internals. |
| Cache hit vs. miss | **Yes** | Free to record, and invisible from timing alone once a cache is warm. |
| LLM provider, model, token counts, cost | **Yes**, where there are LLM calls | Cost detail is usually for developers, not end users. |
| External call: service, operation, outcome size, which endpoint answered, attempt count | **Yes** | Turns "why was that slow" from a guess into a fact. |
| **Full external request and response bodies** | **Ask** | Often the most useful thing to have when debugging, and the heaviest: size, and credentials in headers. |
| **Full LLM prompts and completions** | **Ask** | Same tradeoff, plus it exposes system prompts. Frequently worth it on a developer-only surface. |
| **Sources and Provenance** | **Ask** | For systems dealing with real data, sourcing and provenance is critical to verify where the answer came from. If sources used to derive answers are not forwarded from the backend, do so. |
| Database queries, with or without bound parameters | **Ask** | Statements are usually safe to show; parameters carry the actual data. |
| File paths, buckets, hostnames | **Ask** | Absolute paths leak infrastructure layout. Summarise rather than pass through. |
| Full message transcripts per turn | **Ask** | Large, but excellent for debugging conversational systems. |

For everything recorded, agree at the same time **who may see it**: tag each field for a *user* audience or a *developer* audience rather than forking the code, so one surface can filter on the tag.

**Ask once about personal data.** If any captured value could contain it - user text, transcripts, request bodies, query parameters - raise it here, once: what would be reduced, and how (truncate, hash, redact by key name, or summarise). If the user says it does not matter for their case, drop it and do not raise it again. If they do want it handled, **reduce in the builder**, not at the serialiser - the builder is the only place that knows what a value is.

Close this checkpoint by stating plainly **what will be captured, what will not, and what will be reduced.** Display the things that will be captured in a table format for readability, including your recommendation of whether to capture it or not. Repeat that summary at the end of the task, once it is actually true.

### 1b. Where the trace surfaces - checkpoint 3

The point of a trace is letting someone see what the backend did. Three surfaces, and they compose:

| Surface | Good when | What it needs |
| --- | --- | --- |
| **Attached to the response** | An app will render it, or a caller wants it inline | A response field, and a size budget |
| **Written as JSON** - one file per turn, or a store row | Someone will open it by hand, diff two runs, or attach one to a bug report | A path, a naming scheme, a retention answer |
| **Printed to the CLI or logs** | Local development; a batch job or worker with no response to attach to | A small renderer reading the same envelope, behind a verbosity flag |

Two things to get right here:

- **If there is no GUI, CLI and JSON together are the expected answer.** Do not stop at picking a surface - ask how they actually want to read it: a pretty-printed tree, one line per step, a verbosity flag, a `--trace` option on a command they already run. Their answer shapes the renderer.
- **Propose saving trace JSON even when a UI is planned.** It is what makes traces diffable, attachable to an issue, and usable before any frontend exists. Ask *whether* to save and *where* - directory, naming scheme, retention, and whether it is gitignored. Never choose a location unilaterally.

### 1c. Shape and scope

- **Granularity.** Which units of work become steps. The rule of thumb: *a step is something a user would name when describing what happened.* Too fine and it becomes a profiler; too coarse and it explains nothing.
- **Where the code goes.** A single tracing module beside the code it describes, or per-module builders. Name the files.
- **Per-step builders versus one generic builder.** Per-step builders let each outcome and its wording be specific, and are easy to unit-test; a generic builder is less code and drifts less. Recommend based on how genuinely different the steps are.
- **How much, first.** The smallest useful version is one step type on the most interesting function. Offer that as a first slice.

---

## Step 2 - Implement

Build **one unit of work end to end first** - one step, accumulated, attached or written, visible in a real call - before instrumenting the rest. A vertical slice proves the state plumbing and the response boundary, which are where the surprises live. Show the user that slice's real output and confirm the approach before continuing - that is checkpoint 4.

The design rules are in **`references/design-principles.md`** - read it before writing code.
The short version:

- One event per unit of work, **built immediately before that unit returns**, inside the function itself. Only the function knows what it just did.
- **Both paths emit.** A step that failed must still produce an event, or the trace goes silent exactly when it matters most.
- Every event carries: **timing** (monotonic duration + wall-clock start/end), a **plain-language summary of what it did (if necessary)** and why the step exists, its **inputs as actually received**, its **outcome**, and its **error or `null`**.
- **Distinguish `null` from `0`.** "No model ran" and "a model ran and used no tokens" are different facts, and a zeroed number cannot express the first one.
- **Cache checks are observable.** Anything that might have been served from cache records whether it was.
- **External calls are captured**, including which endpoint/mirror answered and after how many attempts. When a call happens inside a module whose return value cannot carry that information, collect it out of band - `references/io-capture.md`.
- **LLM calls record tokens and cost**, priced at the call site.
- Events accumulate into **per-turn state**, and the response boundary assembles them into one envelope with a computed header.
- **Builders are pure functions** taking already-computed values. Keep them dumb; keep the code small.
- **Keep it thread-safe**, in case it is multithreaded or multiple users may simultaneously use the service.

`references/architecture-mapping.md` translates "unit of work", "per-turn state", and "turn
boundary" onto LangGraph, plain FastAPI/Flask, Express/NestJS, Celery-style queues, and
agent loops.

---

## Step 3 - Verify with a real trace

Instrumentation is not done until a **real emitted trace** has been looked at. After confirming with the user, either instruct the user on how to do this, or call the endpoint (or run the job), capture the output, and check:

- Steps appear in execution order, with `step` indices matching their position.
- Durations are plausible and roughly sum to the turn's total.
- A step that did no LLM work has `null` provider/tokens, **not** zeros.
- A cached path reports it, and a fresh one reports that.
- Errors appear as data on the failing step, and the response is still correct.
- Nothing excluded at checkpoint 2 has leaked in.
- Deliberately break something (unresolvable input, a downed dependency) and confirm the answer still returns and the trace shows where it stopped.

Save one real trace into the host project as a fixture. It is what `trace-visualize` needs to start, and what a CLI renderer can be built against offline.

Close by restating in a few lines: **what is captured, what is deliberately not, where it surfaces, and what it cannot tell you.** The last of those matters most - a trace trusted beyond what it actually records is worse than no trace.

---

## Reference files

Load these as needed; **do not** read them all up front.

| File | Read it when |
| --- | --- |
| `references/design-principles.md` | Before writing any builder. The rules and the reasoning. |
| `references/architecture-mapping.md` | The host project is not a graph-based agent, or you are unsure what a "step" is there. |
| `references/io-capture.md` | External calls or cache hits happen inside functions whose return values cannot report them. |
| `references/worked-example.md` | Before step 3, and any time you want the whole pattern in one piece. A walkthrough of the two real envelopes in `assets/`, including the traps they contain. |
| `references/relevant-codebase-files.md` | **Only if the user asks to see the originating implementation** and has it checked out. A file map of it - nothing else here depends on it. |

## Assets

Two **real captured envelopes**, shipped with this skill - a genuine consecutive pair from one conversation, not fabricated. Use them to see what a finished trace looks like, and to build a renderer against before the host backend emits anything:

| File | What it shows |
| --- | --- |
| `assets/example-turn-1-paused.json` | A turn that stopped early to ask the user a question. Two steps. |
| `assets/example-turn-2-answered.json` | The next turn, resuming that answer. Four steps, a computed number, an LLM-phrased reply. |

They share a `thread_id` and are two separate envelopes, which is the point: **one envelope is one turn.** A paused question and its answer are never merged.

Both are unedited except for three redactions: absolute cache paths read `<cache>`, prompt text reads `<prompt>`, and model output reads `<response>`. Read them for an example of one possible architecture - what a step records, how a turn is bounded, where the header comes from. The field names and step kinds are one project's domain and do not transfer.