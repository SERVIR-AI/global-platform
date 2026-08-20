// Maize Situation Desk — frontend. Talks ONLY to this app's own backend
// (same origin, port 8080). All platform verdicts are resolved server-side
// or, for the groundedness strip, re-resolved at view time through our
// /api/resolve proxy — never a direct call from the browser to the platform.

import { renderTracePanel } from "./trace-render.js";

let META = null;

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fillTemplate(tpl, data) {
  return tpl.replace(/{{(\w+)}}/g, (_, key) => escapeHtml(data[key] ?? ""));
}

// Minimal markdown: "## Heading" lines and paragraphs. Citations like [3]
// are left as plain text — the source cards below carry the real evidence.
function renderBriefMarkdown(md) {
  const lines = md.split(/\n/);
  let html = "";
  let para = [];
  const flush = () => {
    if (para.length) {
      html += `<p>${escapeHtml(para.join(" "))}</p>`;
      para = [];
    }
  };
  for (const line of lines) {
    const heading = line.match(/^##\s+(.*)/);
    if (heading) {
      flush();
      html += `<h2>${escapeHtml(heading[1])}</h2>`;
    } else if (line.trim() === "") {
      flush();
    } else {
      para.push(line.trim());
    }
  }
  flush();
  return html;
}

async function loadMeta() {
  const res = await fetch("/api/meta");
  META = await res.json();
  document.getElementById("platform-header").innerHTML = META.header_html;
  const style = document.createElement("style");
  style.textContent = META.css;
  document.head.appendChild(style);
}

function renderDecline(note) {
  return fillTemplate(META.decline_card_template, { note });
}

function renderSourceCard(c) {
  return fillTemplate(META.source_card_template, {
    n: c.n,
    title: c.title || c.text || "(untitled)",
    source: c.source || c.role || "",
    pub_date: c.pub_date || c.date || "",
    url: c.url || "#",
    archived_copy: c.archived_copy || c.archived || "#",
  });
}

function renderGroundednessStrip(verify, pack, receipt) {
  return fillTemplate(META.groundedness_strip_template, {
    receipt_id: receipt.receipt_id,
    cited_count: (verify.cited || []).length,
    source_count: (pack.citations || []).length,
    evidence_tier: verify.evidence_tier || receipt.evidence_tier || "unknown",
  });
}

function renderProvenanceEmbed(receiptId) {
  const src = `http://localhost:5173/?embed=provenance_graph&receipt_id=${encodeURIComponent(receiptId)}`;
  return `<div class="msd-embed-wrap"><iframe src="${src}" title="provenance_graph"
    style="width:100%;height:520px;border:1px solid var(--grp-base-300);border-radius:var(--grp-radius-box,.5rem)"
    loading="lazy"></iframe></div>`;
}

function render(result) {
  const el = document.getElementById("result");
  el.hidden = false;

  if (result.status === "declined") {
    let html = `<div class="msd-stage">declined at: ${escapeHtml(result.stage)}</div>`;
    html += renderDecline(result.note || "No reason given.");
    if (result.draft) {
      html += `<h2>Draft (not published — did not pass the gate)</h2><div class="msd-brief">${renderBriefMarkdown(result.draft)}</div>`;
    }
    html += renderTracePanel(result.trace);
    el.innerHTML = html;
    return;
  }

  const { pack, draft, verify, receipt } = result;
  let html = "";
  html += `<h2>Brief</h2><div class="msd-brief">${renderBriefMarkdown(draft)}</div>`;

  html += `<h2>Groundedness</h2>`;
  html += renderGroundednessStrip(verify, pack, receipt);

  html += `<h2>Cited sources</h2><div class="msd-sources">`;
  for (const c of pack.citations || []) {
    html += renderSourceCard(c);
  }
  html += `</div>`;

  if ((pack.gaps || []).length) {
    html += `<h2>Declared gaps</h2><ul>` +
      pack.gaps.map((g) => `<li>${escapeHtml(g)}</li>`).join("") +
      `</ul>`;
  }

  html += `<h2>Provenance graph</h2>`;
  html += renderProvenanceEmbed(receipt.receipt_id);

  html += renderTracePanel(result.trace);

  el.innerHTML = html;
}

async function onSubmit(ev) {
  ev.preventDefault();
  const btn = document.getElementById("ask-btn");
  const country = document.getElementById("country").value;
  const question = document.getElementById("question").value.trim();
  if (!question) return;

  btn.disabled = true;
  btn.textContent = "Asking…";
  const el = document.getElementById("result");
  el.hidden = false;
  el.innerHTML = `<div class="msd-stage">Assembling evidence pack, drafting, verifying, minting receipt…</div>`;

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ country, question }),
    });
    const result = await res.json();
    render(result);
  } catch (err) {
    el.innerHTML = renderDecline(`Backend request failed: ${err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Ask";
  }
}

loadMeta();
document.getElementById("ask-form").addEventListener("submit", onSubmit);
