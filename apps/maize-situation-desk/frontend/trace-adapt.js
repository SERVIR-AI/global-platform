// L1 — adapter. The only file that knows the backend's wire shape
// (apps/maize-situation-desk/backend/tracing.py::assemble_envelope /
// make_step). Everything above this (trace-select.js, trace-render.js)
// works only in terms of the normalised shape returned by parseEnvelope.
//
// Contract: parseEnvelope never throws. A malformed envelope returns null;
// callers render nothing rather than crash the result panel over it.

// The five step names the backend currently emits, in pipeline order, with
// the label this app invents for each (backend never sends a display title).
// A step name the backend emits that ISN'T in this list is not dropped —
// toGraphPath (trace-select.js) appends it at the end and flags the
// topology as out of date, rather than silently losing it (see
// trace-visualize's invariants §7: a hardcoded topology drifts).
export const KNOWN_TOPOLOGY = [
  { node: "resolve_input", label: "Validate request" },
  { node: "assemble_pack", label: "Assemble evidence pack" },
  { node: "draft_brief", label: "Draft brief" },
  { node: "verify_groundedness", label: "Verify groundedness" },
  { node: "record_receipt", label: "Mint receipt" },
];

function isRecord(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function parseStep(raw, fallbackIndex) {
  if (!isRecord(raw) || typeof raw.name !== "string") return null;
  return {
    index: typeof raw.step === "number" ? raw.step : fallbackIndex,
    name: raw.name,
    startedAt: raw.started_at ?? null,
    endedAt: raw.ended_at ?? null,
    durationMs: typeof raw.duration_ms === "number" ? raw.duration_ms : null,
    summary: typeof raw.summary === "string" ? raw.summary : null,
    why: typeof raw.why === "string" ? raw.why : null,
    inputs: isRecord(raw.inputs) ? raw.inputs : null,
    outcome: isRecord(raw.outcome) ? raw.outcome : null,
    error: typeof raw.error === "string" ? raw.error : null,
    llm: isRecord(raw.llm) ? raw.llm : null,
    external: isRecord(raw.external) ? raw.external : null,
    cache: isRecord(raw.cache) ? raw.cache : null,
  };
}

/** raw -> normalised envelope, or null. Never throws. */
export function parseEnvelope(raw) {
  if (!isRecord(raw) || !Array.isArray(raw.steps)) return null;

  const steps = raw.steps
    .map((s, i) => parseStep(s, i))
    .filter((s) => s !== null);

  if (steps.length === 0 && raw.steps.length > 0) {
    // every step was malformed — a broken trace, not an empty one
    return null;
  }

  // Recompute totals if the backend's header is missing/non-numeric rather
  // than dropping the whole trace over a cosmetic defect. Match the
  // backend's own rule: sum all durations, but SKIP (not zero) steps with
  // no llm field when totalling cost — a step that made no model call is
  // different from one that made a call costing $0.
  const totalDurationMs =
    typeof raw.total_duration_ms === "number"
      ? raw.total_duration_ms
      : steps.length
      ? round3(steps.reduce((sum, s) => sum + (s.durationMs ?? 0), 0))
      : null;

  let totalCostUsd = null;
  if (typeof raw.total_cost_usd === "number") {
    totalCostUsd = raw.total_cost_usd;
  } else {
    const costs = steps
      .map((s) => s.llm && s.llm.cost_usd)
      .filter((c) => typeof c === "number");
    totalCostUsd = costs.length ? round6(costs.reduce((a, b) => a + b, 0)) : null;
  }

  return {
    traceId: typeof raw.trace_id === "string" ? raw.trace_id : null,
    createdAt: raw.created_at ?? null,
    finalStatus: typeof raw.final_status === "string" ? raw.final_status : null,
    question: typeof raw.question === "string" ? raw.question : null,
    country: typeof raw.country === "string" ? raw.country : null,
    nSteps: steps.length,
    totalDurationMs,
    totalCostUsd,
    steps,
  };
}

function round3(n) {
  return Math.round(n * 1000) / 1000;
}
function round6(n) {
  return Math.round(n * 1e6) / 1e6;
}
