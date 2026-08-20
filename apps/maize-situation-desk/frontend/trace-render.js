// L3 — renderer. Consumes trace-select.js view models and decides only how
// they look. No field knowledge lives here beyond "how to draw a field of
// kind X" — if you find yourself reading step.llm.tokens.input directly in
// this file, that logic belongs in trace-select.js instead.
import { parseEnvelope } from "./trace-adapt.js";
import {
  summarizeEnvelope,
  toStepRows,
  toStepFields,
  toGraphPath,
  formatDuration,
  formatCost,
} from "./trace-select.js";

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/** rawTrace -> HTML string, or "" if there is nothing to show. Never throws
 * — a missing or malformed trace is normal, not an error (the pipeline's
 * actual answer already rendered without it). */
export function renderTracePanel(rawTrace) {
  try {
    if (!rawTrace) return "";
    const envelope = parseEnvelope(rawTrace);
    if (!envelope) {
      return `<div class="msd-trace-note">A trace was attached to this response but could not be parsed as a valid execution trace.</div>`;
    }
    if (envelope.nSteps === 0) {
      return `<details class="msd-trace"><summary>Execution trace — no steps recorded</summary></details>`;
    }

    const summary = summarizeEnvelope(envelope);
    const rows = toStepRows(envelope);
    const graph = toGraphPath(envelope);

    return `
      <details class="msd-trace">
        <summary class="msd-trace__summary">${renderVerdictLine(summary)}</summary>
        <div class="msd-trace-body">
          ${renderGraph(graph)}
          <div class="msd-trace-steps">
            ${rows.map(renderStepRow).join("")}
          </div>
        </div>
      </details>`;
  } catch {
    // Tracing is never load-bearing — if rendering it breaks for any
    // reason, show nothing rather than take the result panel down with it.
    return "";
  }
}

function renderVerdictLine(s) {
  const parts = [
    s.status === "ok" ? "OK" : `Declined${s.erroredStep ? ` at ${escapeHtml(s.erroredStep.name)}` : ""}`,
    `${s.nSteps} step${s.nSteps === 1 ? "" : "s"}`,
    formatDuration(s.totalDurationMs) ?? "duration unknown",
  ];
  if (s.totalCostUsd != null) parts.push(formatCost(s.totalCostUsd));
  const statusClass = s.status === "ok" ? "ok" : "error";
  return `Execution trace — <span class="msd-trace__verdict msd-trace__verdict--${statusClass}">${parts.join(" · ")}</span>`;
}

function renderGraph(graph) {
  const nodes = graph.nodes
    .map(
      (n, i) => `
      ${i > 0 ? '<span class="msd-trace-arrow" aria-hidden="true">&rarr;</span>' : ""}
      <span class="msd-trace-node" data-state="${n.state}" title="${escapeHtml(n.label)} — ${n.state}">
        <span class="msd-trace-node__icon" aria-hidden="true">${nodeIcon(n.state)}</span>
        <span class="msd-trace-node__label">${escapeHtml(n.label)}</span>
      </span>`
    )
    .join("");
  const note = graph.topologyOutOfDate
    ? `<p class="msd-trace-note">This trace includes a step this diagram's fixed layout doesn't know about — it was appended rather than dropped, but the diagram may not reflect the true topology.</p>`
    : "";
  return `<div class="msd-trace-graph" role="img" aria-label="Pipeline steps and their status">${nodes}</div>${note}`;
}

function nodeIcon(state) {
  if (state === "visited") return "✓"; // check
  if (state === "errored") return "✕"; // cross
  return "–"; // dash — skipped
}

function renderStepRow(row) {
  const pct = row.durationFraction != null ? Math.round(row.durationFraction * 100) : 0;
  const durationText = formatDuration(row.durationMs) ?? "—";
  const statusLabel = row.status === "error" ? "Error" : "OK";
  return `
    <details class="msd-trace-step" data-status="${row.status}">
      <summary>
        <span class="msd-trace-step__idx">${row.index + 1}</span>
        <span class="msd-trace-step__title">${escapeHtml(row.title)}</span>
        <span class="msd-trace-step__bar-wrap" title="${pct}% of total duration">
          <span class="msd-trace-step__bar" style="width:${pct}%"></span>
        </span>
        <span class="msd-trace-step__duration">${escapeHtml(durationText)}</span>
        <span class="msd-trace-step__status msd-trace-step__status--${row.status}">${statusLabel}</span>
      </summary>
      <div class="msd-trace-step__detail">
        ${row.summary ? `<p class="msd-trace-step__summary">${escapeHtml(row.summary)}</p>` : ""}
        ${row.why ? `<p class="msd-trace-step__why">${escapeHtml(row.why)}</p>` : ""}
        ${renderFieldGroups(toStepFields(row.raw))}
      </div>
    </details>`;
}

function renderFieldGroups(fields) {
  let html = "";
  let lastSection = null;
  for (const f of fields) {
    if (f.section !== lastSection) {
      if (lastSection !== null) html += `</dl>`;
      html += `<div class="msd-trace-section"><h4>${escapeHtml(f.sectionLabel)}</h4><dl class="msd-trace-fields">`;
      lastSection = f.section;
    }
    html += renderField(f);
  }
  if (lastSection !== null) html += `</dl></div>`;
  return html;
}

function renderField(f) {
  if (f.kind === "section-missing") {
    return `<div class="msd-trace-field msd-trace-field--missing">— <span class="msd-trace-field__reason">${escapeHtml(f.reason)}</span></div>`;
  }
  const label = `<dt>${escapeHtml(f.label)}</dt>`;
  let value;
  switch (f.kind) {
    case "missing":
      value = `<dd class="msd-trace-field--missing" title="${escapeHtml(f.reason)}">— <span class="msd-trace-field__reason">${escapeHtml(f.reason)}</span></dd>`;
      break;
    case "flag":
      value = `<dd>${f.value ? "✓ yes" : "✕ no"}</dd>`;
      break;
    case "list": {
      const shown = f.items.slice(0, 8);
      const more = f.items.length > 8 ? ` <span class="msd-trace-more">+${f.items.length - 8} more</span>` : "";
      value = `<dd>${shown.map((i) => escapeHtml(i)).join(", ")}${more}</dd>`;
      break;
    }
    case "json":
      value = `<dd><details class="msd-trace-json"><summary>view (${Array.isArray(f.value) ? f.value.length + " items" : "object"})</summary><pre>${escapeHtml(JSON.stringify(f.value, null, 2))}</pre></details></dd>`;
      break;
    case "text":
    default: {
      const text = f.text ?? "";
      if (text.length > 240) {
        value = `<dd><details class="msd-trace-json"><summary>${escapeHtml(text.slice(0, 120))}&hellip; (show all ${text.length} chars)</summary><pre class="msd-trace-pre">${escapeHtml(text)}</pre></details></dd>`;
      } else {
        value = `<dd>${escapeHtml(text)}</dd>`;
      }
    }
  }
  return `<div class="msd-trace-field-row">${label}${value}</div>`;
}
