// L2 — selectors. Pure functions: normalised envelope (trace-adapt.js) ->
// presentation-neutral view models. No DOM, no framework, no formatting
// decisions beyond turning a value into a string/number. trace-render.js
// is the only thing that decides how these look.
import { KNOWN_TOPOLOGY } from "./trace-adapt.js";

// The authoritative test for "did a model run in this step" — an explicit
// field, never a token count (a step can legitimately record 0 tokens for
// a real call, which must not read the same as "no call happened").
export function stepUsedModel(step) {
  return step.llm != null;
}

export function stepStatus(step) {
  return step.error != null ? "error" : "ok";
}

export function summarizeEnvelope(envelope) {
  const erroredStep = envelope.steps.find((s) => s.error != null) ?? null;
  return {
    status: envelope.finalStatus ?? (erroredStep ? "declined" : "ok"),
    nSteps: envelope.nSteps,
    totalDurationMs: envelope.totalDurationMs,
    totalCostUsd: envelope.totalCostUsd,
    usedModel: envelope.steps.some(stepUsedModel),
    erroredStep: erroredStep
      ? { name: erroredStep.name, index: erroredStep.index, error: erroredStep.error }
      : null,
    lastStep: envelope.steps.length
      ? envelope.steps[envelope.steps.length - 1]
      : null,
  };
}

export function toStepRows(envelope) {
  const total = envelope.totalDurationMs;
  return envelope.steps.map((s) => ({
    index: s.index,
    node: s.name,
    title: labelFor(s.name),
    summary: s.summary,
    why: s.why,
    durationMs: s.durationMs,
    durationFraction:
      total && s.durationMs != null ? Math.min(1, s.durationMs / total) : null,
    status: stepStatus(s),
    usedModel: stepUsedModel(s),
    raw: s,
  }));
}

// Diagram path over the KNOWN topology, plus any step the backend emitted
// that isn't in it — appended rather than dropped, per trace-visualize's
// invariant that a hardcoded topology must not silently lose new nodes.
export function toGraphPath(envelope) {
  const seen = new Set();
  const nodes = KNOWN_TOPOLOGY.map(({ node, label }) => {
    const step = envelope.steps.find((s) => s.name === node);
    if (step) seen.add(step.name);
    return {
      node,
      label,
      state: step ? (step.error != null ? "errored" : "visited") : "skipped",
    };
  });

  const extras = envelope.steps.filter((s) => !seen.has(s.name));
  const extraNodes = extras.map((s) => ({
    node: s.name,
    label: labelFor(s.name),
    state: s.error != null ? "errored" : "visited",
    isUnknownToTopology: true,
  }));

  return {
    nodes: [...nodes, ...extraNodes],
    topologyOutOfDate: extraNodes.length > 0,
  };
}

// --- field groups (Inputs / Outcome / LLM call / External call) ----------

const SECTION_ORDER = ["inputs", "outcome", "llm", "external", "cache"];
const SECTION_LABEL = {
  inputs: "Inputs",
  outcome: "Outcome",
  llm: "LLM call",
  external: "External / MCP call",
  cache: "Cache",
};
const SECTION_ABSENT_REASON = {
  llm: "this step made no model call",
  external: "this step made no external/MCP call",
  cache: "caching does not apply to this step",
};

/** step -> ordered list of {section, sectionLabel, key, label, kind, ...} */
export function toStepFields(step) {
  const groups = [];
  for (const section of SECTION_ORDER) {
    const raw = step[section];
    const sectionLabel = SECTION_LABEL[section];
    if (raw == null) {
      if (section === "inputs" || section === "outcome") continue; // always present in practice
      groups.push({
        section,
        sectionLabel,
        key: null,
        label: null,
        kind: "section-missing",
        reason: SECTION_ABSENT_REASON[section] ?? "not recorded",
      });
      continue;
    }
    for (const [key, value] of Object.entries(raw)) {
      groups.push({
        section,
        sectionLabel,
        key,
        label: humanize(key),
        ...describeValue(value),
      });
    }
  }
  if (step.error != null) {
    groups.push({
      section: "error",
      sectionLabel: "Error",
      key: "error",
      label: "Error",
      kind: "text",
      text: step.error,
    });
  }
  return groups;
}

function describeValue(value) {
  if (value === null || value === undefined) {
    return { kind: "missing", reason: "not recorded" };
  }
  if (typeof value === "boolean") {
    return { kind: "flag", value };
  }
  if (typeof value === "number") {
    return { kind: "text", text: String(value) };
  }
  if (typeof value === "string") {
    return { kind: "text", text: value };
  }
  if (Array.isArray(value)) {
    if (value.every((v) => typeof v === "string" || typeof v === "number")) {
      return { kind: "list", items: value.map(String) };
    }
    return { kind: "json", value };
  }
  return { kind: "json", value };
}

function humanize(key) {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function labelFor(node) {
  const known = KNOWN_TOPOLOGY.find((t) => t.node === node);
  return known ? known.label : node.replace(/_/g, " ");
}

// --- formatters ------------------------------------------------------------

export function formatDuration(ms) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 2 : 0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function formatCost(usd) {
  if (usd == null) return null;
  return `$${usd.toFixed(4)}`;
}

export function formatTimestamp(iso) {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}
