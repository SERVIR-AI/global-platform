# Reading a real trace

A walkthrough of the two envelopes in `assets/`. They are real captured output from one
conversation with a disaster-risk agent - a question goes in, a number computed from
geospatial data comes out - not fabricated, and not cleaned up.

Three things are redacted: absolute cache paths read `<cache>`, prompt text reads `<prompt>`,
and model output reads `<response>`. Everything else is exactly as emitted.

Read them for the **architecture**: what a step records, how a turn is bounded, where the
header comes from, and how absence is encoded. The field names and step kinds are one
project's domain and do not transfer.

Open both files alongside this.

---

## The shape

Both envelopes have the same five header keys and a `steps` list:

```
thread_id        the conversation - shared by both files
trace_id         this turn - different in each file
created_at       when the envelope was assembled
total_duration   milliseconds
total_tokens     {in, out, total, cost}
steps            ordered, each with a `step` index and a `node` kind
```

**One envelope is one turn.** Turn 1 asked the user a question and stopped. Turn 2 resumed
with their answer. They are two envelopes sharing a `thread_id`, never merged - which is what
lets a consumer show "this turn did two things" honestly instead of inventing a six-step turn
that never happened.

---

## Turn 1 - the turn that paused

Two steps, 1621 ms.

**Step 0, `router`** - the model read the question and produced a tool call. Note what it
captures beyond the obvious:

- `derived_tool_calls` - the parsed call, with its arguments as actually received
  (`min_severity: 4`, `layer: "schools"`). Not the arguments as declared anywhere.
- `available_assets` - what the model was *offered*. Together with `derived_tool_calls`, that
  is the pair that answers "did it pick well?" - which you cannot ask with only one of them.
- `derived_place`, `derived_countable_assets` - inferred from the tool call, not stated by the
  user. A consumer needs to know these are best-effort guesses; the trace does not say so, and
  that is a gap.
- `summary`: `"Router matched the question to `count_in_hazard` for 'drawn area'"` - the
  specifics filled in. `why` explains what the step is *not*: "no computation happens here."

**Step 1, `resolve`** - 12 ms, and it decided to stop and ask. `decision: "asked"`,
`question_asked` carrying the exact text the user saw, `options` carrying what they chose
between.

The turn ends here. **It is complete and successful, not truncated** - a consumer that renders
a two-step turn as a failure is reading it wrong.

---

## Turn 2 - the turn that answered

Four steps, 2920 ms.

**Step 0, `router`** - 0.014 ms, and `kind: "apply_choice"`. No model ran; the user's reply
("1") was applied deterministically. The `why` says exactly that.

**Step 1, `fetch`** - 1582 ms, the slowest step, and the only one that touched the network.
The I/O it hides is captured out of band (`io-capture.md`) and split into two lists:

```json
"api_calls": [{"kind": "api", "api": "Overpass", "attempts": 1, "n_elements": 154}]
"downloads": [{"kind": "clip", "layer": "hazard_flood", "was_cached": false}, ...]
```

`attempts: 1` says the first endpoint answered. `was_cached: false` says this was computed
fresh - and the same field appears on the hit path too, which is what makes it meaningful.

Note also `aoi: {name, area_km2, how}`. The real object here is a bundle full of absolute
filesystem paths; the builder reduced it to three fields before it ever reached the envelope.
**Reduce at the builder** - it is the only place that knows what a value is.

**Step 2, `operate`** - 16 ms, and the only step that produces a number:

```json
"result": {"value": 0, "by_severity": {"1": 2, "2": 1, "3": 4, "4": 0, "5": 0}}
```

`value: 0` is a real computed zero - two schools at severity 1, one at 2, four at 3, none at 4
or above, and the question asked for severity ≥ 4. Compare with a *failed* calculation, which
would be `result: null`. Those are different facts and the encoding keeps them apart.

**Step 3, `finalize`** - 1323 ms, the model phrased the answer. `grounded: true` means the
computed number appeared verbatim in the answer text - the check ran against the real output,
which reads `<response>` here, truncated as the response is not relevant. The trace must, if relevant, indicate whether the answer is grounded or not.

---

## Six things to check in your own trace

**1. The durations sum.** 0.014 + 1582 + 16 + 1323 = 2920 = `total_duration`. That means the
steps ran in series, and a consumer may draw proportional bars but not a waterfall.

**2. The header is derived, and it skips.** Turn 2's `total_tokens` is 876/128 - the finalize
step's numbers alone, because that is the only step that called a model.

**3. Absence is encoded, but not consistently.** Turn 2's router has
`available_assets: {available_tools: null, countable: null, ...}` - the keys are present and
null. The `resolve` step, by contrast, simply has no `llm_provider` key at all. Both mean "not
applicable", in two different ways, in the same trace. **Pick one and hold to it**; a consumer
otherwise needs to handle both.

**4. There is a live trap in this data.** Turn 2's router step reports:

```json
"llm_provider": null,
"tokens": {"in": 0, "out": 0, "total": 0, "cost": 0}
```

No model ran - but the tokens object is fully zeroed rather than null, because the builder
reused a usage helper with no response to read. So `tokens.total == 0` is **ambiguous
forever** ("no call" or "a call that used nothing"?) while `llm_provider == null` stays
unambiguous.

Either avoid emitting zeroed objects for absent things, or publish which field is
authoritative. A consumer reading token counts to decide whether a model ran will tell the
user the opposite of what happened.

**5. `cost: 0.0` on a real call.** The finalize step used 1004 tokens and reports zero cost -
pricing was not configured for that model. A zero that means "not priced" is
indistinguishable from a zero that means "free", which is the same trap as (4) in a different
field.

**6. Both files capture the full message transcript.** The `messages` array on the router and
finalize steps held the entire system prompt and the model's output. It is redacted here to
`<prompt>` and `<response>`. When tracing, this is relevant for
so the end user can see the runtime prompt. Ask the user if they want the whole thing emitted or not.

The redaction preserved: the roles, the message count, the `tool_call` type on the
router's third message.

---

## What the builders did that you should copy

**The outcome label is derived in the builder, not passed in.** The caller hands over raw
facts; the builder decides which outcome they represent and writes the matching `summary` and
`why`:

```python
if derived_tool_calls is None:
    kind = "declined"
    summary = "Router received a text reply with no tool call"
    why = "The model didn't match the question to any available tool, so it answered directly."
elif error is not None:
    kind = "missing_place"
    ...
```

One place decides what an outcome is called, so the label and its explanation cannot drift
apart.

**Builders are pure, so they are tested directly.** Construct a fake SDK response, call the
builder, assert the dict - no pipeline, no network, no API key:

```python
def test_router_event_declined():
    resp = _llm_response(content="I can't answer that.", tool_calls=None)
    event = make_router_event(..., llm_response=resp, error="I can't answer that.")
    assert event["kind"] == "declined"
    assert event["derived_tool_calls"] is None
```

**Assert a required-field set**, so a dropped key breaks a test rather than a consumer:

```python
_REQUIRED_FIELDS = {"step", "started_at", "ended_at", "duration", "summary", "node", ...}
assert not (_REQUIRED_FIELDS - event.keys())
```
