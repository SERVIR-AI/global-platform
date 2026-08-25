"""MCP Apps surface: an interactive HTML view the HOST renders, not prose.

Why this exists. Everything the platform is proud of — the evidence chain, how old
each source is, which were pulled live, what is MISSING — arrives today as text. In
a chat client that is a wall nobody reads, and the value is invisible in the ten
minutes a demo gets.

MCP Apps (Jan 2026) fixes that at the protocol level: a server predeclares an HTML
template as a `ui://` resource with mime type `text/html;profile=mcp-app`, and a
host that advertises `io.modelcontextprotocol/ui` renders it in a sandboxed iframe
beside the tool result. Claude Desktop advertises exactly that in its initialize
handshake, so this does NOT depend on the model choosing to draw something — which
is what went wrong when we only asked nicely in the instructions.

Two rules this file exists to keep:

  1. The app renders EVIDENCE, never a verdict. A rendered surface freezes what is
     in it, and a frozen verdict attests nothing (rule 5). The verdict stays a link
     to the resolver, which re-resolves at view time.
  2. It is styled from the platform's own tokens, so it cannot drift from the
     design language a builder is told to use.
"""

from __future__ import annotations

import json

from . import ui

UI_URI = "ui://servirplatform/evidence"
UI_MIME = "text/html;profile=mcp-app"


def _tokens_css() -> str:
    """The platform's palette as custom properties, so the app inherits the design
    language instead of inventing one."""
    try:
        return ui.css_vars()
    except Exception:                      # never let a theme problem break the app
        return ":root{--grp-base-100:#11151a;--grp-base-content:#eef2f5;}"


def template() -> str:
    """The app shell. Data arrives from the tool result, not baked in here, so one
    registered template serves every pack."""
    return """<!doctype html>
<meta charset="utf-8">
<style>
__TOKENS_CSS__
*{box-sizing:border-box}
body{margin:0;padding:12px;
  font-family:var(--font-sans,ui-sans-serif,system-ui,sans-serif);
  font-size:var(--font-text-sm-size,.875rem);
  line-height:var(--font-text-sm-line-height,1.5);
  color:var(--color-text-primary,var(--grp-base-content,#1c212a));
  background:var(--color-background-primary,var(--grp-base-100,#fbfcfd))}
h2{margin:0 0 .15rem;font-size:var(--font-heading-sm-size,1rem);
  line-height:var(--font-heading-sm-line-height,1.3);
  font-weight:var(--font-weight-semibold,600)}
.sub{color:var(--color-text-secondary,var(--grp-neutral,#5a6472));
  font-size:var(--font-text-xs-size,.75rem);margin-bottom:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.5rem}
.card{border:var(--border-width-regular,1px) solid
    var(--color-border-primary,var(--grp-base-300,#e0e6ec));
  border-radius:var(--border-radius-md,8px);padding:.55rem .65rem;
  background:var(--color-background-secondary,var(--grp-base-200,#f1f4f7))}
.card b{display:block;font-size:var(--font-text-sm-size,.82rem);
  font-weight:var(--font-weight-semibold,600)}
.meta{color:var(--color-text-secondary,var(--grp-neutral,#5a6472));
  font-size:var(--font-text-xs-size,.72rem);margin-top:.15rem}
.pill{display:inline-block;font-size:var(--font-text-xs-size,.68rem);
  padding:.05rem .4rem;border-radius:var(--border-radius-sm,4px);
  border:var(--border-width-regular,1px) solid currentColor;margin-right:.25rem}
.live{color:var(--color-text-info,var(--grp-info,#2380b0))}
.arch{color:var(--color-text-tertiary,var(--grp-neutral,#5a6472))}
.gap{border-left:3px solid var(--color-border-warning,var(--grp-warning,#a06a08));
  background:var(--color-background-warning,transparent);
  color:var(--color-text-warning,inherit);
  padding:.5rem .7rem;margin:.3rem 0;border-radius:var(--border-radius-sm,4px);
  font-size:var(--font-text-sm-size,.82rem)}
.foot{margin-top:1rem;font-size:var(--font-text-xs-size,.76rem);
  color:var(--color-text-secondary,var(--grp-neutral,#5a6472));
  border-top:var(--border-width-regular,1px) solid
    var(--color-border-primary,var(--grp-base-300,#e0e6ec));padding-top:.6rem}
a{color:var(--color-text-info,var(--grp-primary,#2380b0))}
.chart{margin:0 0 .5rem;color:var(--color-text-info,var(--grp-info,#2380b0));
  background:var(--color-background-secondary,var(--grp-base-200,#f1f4f7))}
.sec{margin-top:1rem}
.oneline{padding:.3rem .1rem}
.links{margin-top:.35rem;font-size:var(--font-text-xs-size,.72rem)}
.note{margin-top:.5rem;padding:.4rem .6rem;border-radius:var(--border-radius-sm,6px);
  font-size:var(--font-text-xs-size,.72rem);word-break:break-all;
  background:var(--color-background-warning,var(--grp-base-200,#f1f4f7));
  color:var(--color-text-warning,var(--grp-base-content,#1c212a))}
.lnk{cursor:pointer;text-decoration:underline;
  color:var(--color-text-info,var(--grp-primary,#2380b0))}
.more{margin-top:.5rem;font:inherit;font-size:var(--font-text-xs-size,.75rem);
  cursor:pointer;padding:.3rem .7rem;
  color:var(--color-text-primary,var(--grp-base-content,#1c212a));
  background:var(--color-background-secondary,var(--grp-base-200,#f1f4f7));
  border:var(--border-width-regular,1px) solid
    var(--color-border-primary,var(--grp-base-300,#e0e6ec));
  border-radius:var(--border-radius-sm,6px)}
.chart .meta{color:var(--color-text-secondary,var(--grp-neutral,#5a6472));margin-bottom:.1rem}
.ctl{display:flex;flex-wrap:wrap;gap:.3rem;margin:.35rem 0 .1rem}
.chip{font:inherit;font-size:var(--font-text-xs-size,.7rem);cursor:pointer;
  padding:.12rem .5rem;border-radius:var(--border-radius-full,999px);
  color:var(--color-text-secondary,var(--grp-neutral,#5a6472));
  background:transparent;border:var(--border-width-regular,1px) solid
    var(--color-border-primary,var(--grp-base-300,#e0e6ec))}
.chip.on{color:var(--color-text-primary,var(--grp-base-content,#1c212a));
  border-color:currentColor}
.livewarn{margin:.25rem 0;padding:.3rem .55rem;font-size:var(--font-text-xs-size,.7rem);
  border-radius:var(--border-radius-sm,6px);
  background:var(--color-background-warning,var(--grp-base-200,#f1f4f7));
  color:var(--color-text-warning,var(--grp-base-content,#1c212a))}
.stats{font-size:var(--font-text-xs-size,.7rem);
  color:var(--color-text-tertiary,var(--grp-neutral,#5a6472));margin-top:.1rem}
.tblwrap{overflow-x:auto;margin-top:.4rem}
.tbl{border-collapse:collapse;font-size:var(--font-text-xs-size,.72rem);width:100%}
.tbl th{text-align:left;font-weight:var(--font-weight-semibold,600);
  color:var(--color-text-secondary,var(--grp-neutral,#5a6472))}
.tbl th,.tbl td{padding:.15rem .5rem .15rem 0;border-bottom:var(--border-width-regular,1px)
  solid var(--color-border-primary,var(--grp-base-300,#e0e6ec))}
.s0{color:var(--color-text-info,var(--grp-info,#2380b0))}
.s1{color:var(--color-text-warning,var(--grp-warning,#a06a08))}
.readout{font-size:var(--font-text-xs-size,.72rem);
  color:var(--color-text-secondary,var(--grp-neutral,#5a6472));margin-top:.1rem}
.readout b{color:var(--color-text-primary,var(--grp-base-content,#1c212a))}
.brief h3{margin:.9rem 0 .2rem;font-size:var(--font-text-md-size,.9rem);
  font-weight:var(--font-weight-semibold,600);
  color:var(--color-text-primary,var(--grp-base-content,#1c212a))}
.brief p{margin:.2rem 0 .5rem;color:var(--color-text-secondary,var(--grp-base-content,#1c212a))}
</style>
<div id="root">loading…</div>
<div id="note" class="note" style="display:none"></div>
<script>
// GENERATED from _VERDICT_FIELDS — mirrored, never re-typed, so the two cannot drift.
const VERDICT_FIELDS = __VERDICT_FIELDS__;
// Rule 5 enforced at the RENDERER, which is the only place that matters: the tool
// result legitimately carries the gate outcome (the MODEL must see it), and
// the host hands that whole result to this iframe. Stripping server-side would
// blind the model; stripping here keeps the verdict out of the frozen surface
// without taking it away from the caller.
const strip = o => { const c = {...o}; for (const k of VERDICT_FIELDS) delete c[k]; return c; };
// Quotes INCLUDED: esc'd values land inside double-quoted attributes
// (href/data-url/aria-label), and titles/URLs come from external feeds and corpus
// documents. Without &quot; a title like x" onpointerover="... closes the
// attribute and injects a handler — script in the panel can then reach tools/call.
const esc = s => String(s ?? "").replace(/[<>&"']/g, c => (
  {"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c]));
// ---- ANALYTICS -------------------------------------------------------------
// The panel is an analytics surface, not a picture. Inline SVG and hand-wired
// listeners because the sandbox CSP forbids loading any charting library; every
// affordance is CLICK-driven because hover is useless on touch and invisible in
// a screenshot.
//
// Data comes in two grades and the panel never blurs them:
//   pack — points sealed in this receipt's evidence pack (the default view);
//   live — pulled on demand THROUGH THE HOST via tools/call (spec: a View may
//          call server tools when hostCapabilities.serverTools is set). Live
//          points are NOT attested by the receipt, and every live view says so.
const _cs = {};
function chartState(i) {
  if (!_cs[i]) _cs[i] = { src: "pack", range: 0, sel: -1, bands: true,
                          table: false, hist: null, loading: false };
  return _cs[i];
}
let _ov = false;                        // overlay: both indices on one axis
let _ana = null;                        // analogue events cache
let _anaOpen = false;
const seriesList = () => (_data && _data.insight && _data.insight.series) || [];
const serverToolsOK = () => !!(_caps && _caps.serverTools);

const _calls = new Map();               // JSON-RPC id -> promise handlers
function callTool(name, args) {
  return new Promise((resolve, reject) => {
    if (!serverToolsOK()) { reject(new Error("host does not proxy tool calls")); return; }
    const id = ++_id;
    _calls.set(id, { resolve: resolve, reject: reject });
    post({ jsonrpc: "2.0", id: id, method: "tools/call",
           params: { name: name, arguments: args } });
  });
}
function toolJSON(res) {                // CallToolResult -> the tool's JSON payload
  for (const b of (res && res.content) || [])
    if (b.type === "text") { try { return JSON.parse(b.text); } catch (e) { return null; } }
  return null;
}
function toPoints(records) {            // client-side mirror of synthesis._series
  const out = [];
  for (const r of records || []) {
    if (!r || r.value === undefined || r.value === null) continue;
    const t = r.season ? (r.season + " " + r.year)
            : r.month ? (r.year + "-" + String(r.month).padStart(2, "0"))
            : String(r.year || "");
    out.push({ t: t, v: r.value, c: r.classification });
  }
  return out;
}
function loadHistory(i) {
  const sr = seriesList()[i], st = chartState(i);
  if (!sr || st.loading) return;
  st.loading = true; redrawChart(i);
  callTool("feeds_query", { dataset: sr.id, params: { limit: 1200 } }).then(res => {
    const d = toolJSON(res) || {};
    st.loading = false;
    if (d.status !== "ok") st.hist = { err: d.note || "feed declined" };
    else st.hist = { points: toPoints(d.records), as_of: d.as_of,
                     stale: !!(((d.passport || {}).stale_data || {}).served_stale) };
    redrawChart(i);
  }).catch(e => {
    st.loading = false; st.hist = { err: String(e && e.message || e) }; redrawChart(i);
  });
}
function activePoints(sr, st) {
  const base = (st.src === "live" && st.hist && st.hist.points) ? st.hist.points
                                                                : (sr.points || []);
  return st.range ? base.slice(-st.range) : base;
}
const f2 = v => (Math.round(v * 100) / 100).toFixed(2);
function stats(pts) {
  if (!pts.length) return null;
  let lo = pts[0].v, hi = pts[0].v, sum = 0;
  for (const p of pts) { if (p.v < lo) lo = p.v; if (p.v > hi) hi = p.v; sum += p.v; }
  return { n: pts.length, min: lo, max: hi, mean: sum / pts.length };
}
function chartSVG(sr, i, st) {
  const pts = activePoints(sr, st);
  if (pts.length < 2) return '<div class="meta">not enough data in this window</div>';
  const W = 560, H = 116, PAD = 24;
  const vs = pts.map(p => p.v);
  const lo = Math.min(-0.8, ...vs), hi = Math.max(0.8, ...vs);
  const x = k => PAD + k * (W - PAD * 2) / (pts.length - 1);
  const y = v => H - PAD - (v - lo) * (H - PAD * 2) / (hi - lo);
  const line = pts.map((p, k) => (k ? "L" : "M") + x(k).toFixed(1) + "," + y(p.v).toFixed(1)).join("");
  const band = (v, label) => !st.bands ? "" : `
    <line x1="${PAD}" x2="${W - PAD}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"
      stroke="currentColor" stroke-dasharray="3 3" opacity=".35"/>
    <text x="${W - PAD + 2}" y="${(y(v) + 3).toFixed(1)}" font-size="8"
      fill="currentColor" opacity=".55">${label}</text>`;
  const sel = st.sel < 0 ? pts.length - 1 : Math.min(st.sel, pts.length - 1);
  const sp = pts[sel];
  const dots = pts.length > 60 ? "" : pts.map((p, k) => `
    <circle cx="${x(k).toFixed(1)}" cy="${y(p.v).toFixed(1)}"
      r="${k === sel ? 3.5 : 1.8}" fill="currentColor"
      opacity="${k === sel ? 1 : .45}" pointer-events="none"/>`).join("");
  const s = stats(pts);
  // Delta follows the SELECTION, not the window end: showing MJJ 2026's move next
  // to a clicked NDJ 2017 readout misreports data — caught in browser verification.
  const dv = sel > 0 ? pts[sel].v - pts[sel - 1].v : null;
  const dtxt = dv === null ? "" : (dv >= 0 ? " · +" : " · ") + f2(dv) + " vs prev";
  return `
    <svg class="cv" data-chart="${i}" viewBox="0 0 ${W} ${H}" width="100%" height="${H}"
         role="img" aria-label="${esc(sr.title || sr.id)}" style="cursor:crosshair">
      ${band(0.5, "+0.5 El Nino")}${band(-0.5, "-0.5 La Nina")}
      <line x1="${x(sel).toFixed(1)}" x2="${x(sel).toFixed(1)}" y1="${PAD - 8}"
        y2="${H - PAD}" stroke="currentColor" opacity=".28"/>
      <path d="${line}" fill="none" stroke="currentColor" stroke-width="1.4" opacity=".85"/>
      ${dots}
      <circle cx="${x(sel).toFixed(1)}" cy="${y(sp.v).toFixed(1)}" r="3.5" fill="currentColor"/>
      <text x="${PAD}" y="${H - 6}" font-size="8" fill="currentColor"
        opacity=".55">${esc(pts[0].t)}</text>
      <text x="${W - PAD}" y="${H - 6}" font-size="8" text-anchor="end"
        fill="currentColor" opacity=".55">${esc(pts[pts.length - 1].t)}</text>
    </svg>
    <div class="readout"><b>${esc(sp.t)}</b> &nbsp;${esc(sp.v)} ${esc(sp.c || "")}${dtxt}
      <span class="meta">— click anywhere on the chart to read a month</span></div>
    <div class="stats">${s.n} pts · min ${f2(s.min)} · max ${f2(s.max)} · mean ${f2(s.mean)}</div>`;
}
function tableHTML(pts) {
  const rows = pts.slice(-60);
  const trimmed = pts.length - rows.length;
  return `<div class="tblwrap"><table class="tbl">
    <tr><th>period</th><th>value</th><th>classification</th></tr>
    ${rows.map(p => `<tr><td>${esc(p.t)}</td><td>${esc(p.v)}</td><td>${esc(p.c || "")}</td></tr>`).join("")}
    </table>${trimmed > 0 ? `<div class="meta">${trimmed} earlier rows in the chart only — narrow the window to see them here</div>` : ""}</div>`;
}
function chip(i, act, label, on) {
  return `<button class="chip${on ? " on" : ""}" data-chart="${i}" data-act="${act}">${label}</button>`;
}
function chartInner(sr, i) {
  const st = chartState(i);
  const live = st.src === "live" && st.hist && st.hist.points;
  const histErr = st.hist && st.hist.err;
  const chips = [
    chip(i, "src-pack", "pack (" + (sr.points || []).length + ")", st.src === "pack"),
    serverToolsOK() ? chip(i, "src-live", st.loading ? "loading…" : "full history",
                           st.src === "live") : "",
    chip(i, "range-12", "1y", st.range === 12),
    chip(i, "range-60", "5y", st.range === 60),
    chip(i, "range-0", "all", st.range === 0),
    chip(i, "table", "table", st.table),
    chip(i, "bands", "thresholds", st.bands),
  ];
  return `
    <div class="meta"><b>${esc(sr.title || sr.id)}</b> · ${esc(sr.source || "")} ·
      as of ${esc(sr.as_of || "")}${sr.n ? " · [" + esc(sr.n) + "]" : ""}</div>
    <div class="ctl">${chips.join("")}</div>
    ${live ? `<div class="livewarn">live pull to ${esc(st.hist.as_of || "")} — beyond this
      receipt's evidence pack, not attested by it${st.hist.stale ? " · SERVED FROM CACHE" : ""}</div>` : ""}
    ${histErr ? `<div class="livewarn">history unavailable: ${esc(histErr)}</div>` : ""}
    ${chartSVG(sr, i, st)}
    ${st.table ? tableHTML(activePoints(sr, st)) : ""}`;
}
function chart(sr, i) {
  return `<div class="card chart" id="ch${i}">${chartInner(sr, i)}</div>`;
}
function redrawChart(i) {
  const sr = seriesList()[i];
  const el = document.getElementById("ch" + i);
  if (!sr || !el) return;
  el.innerHTML = chartInner(sr, i);
  wireCharts();
  reportSize();
}
function overlayCard(series) {
  const a = series[0], b = series[1];
  const pa = a.points || [], pb = b.points || [];
  const K = Math.min(pa.length, pb.length);
  if (K < 2) return "";
  const A = pa.slice(-K), Bp = pb.slice(-K);
  const W = 560, H = 130, PAD = 24;
  const vs = A.concat(Bp).map(p => p.v);
  const lo = Math.min(-0.8, ...vs), hi = Math.max(0.8, ...vs);
  const x = k => PAD + k * (W - PAD * 2) / (K - 1);
  const y = v => H - PAD - (v - lo) * (H - PAD * 2) / (hi - lo);
  const path = P => P.map((p, k) => (k ? "L" : "M") + x(k).toFixed(1) + "," + y(p.v).toFixed(1)).join("");
  return `<div class="card chart">
    <div class="meta"><b>Overlay</b> · <span class="s0">■ ${esc(a.title || a.id)}</span>
      · <span class="s1">■ ${esc(b.title || b.id)}</span></div>
    <div class="stats">both in °C anomaly · aligned by recency, last ${K} points — the
      cadences differ, so read co-movement, not exact dates</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="overlay">
      <line x1="${PAD}" x2="${W - PAD}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"
        stroke="currentColor" stroke-dasharray="3 3" opacity=".3"/>
      <g class="s0"><path d="${path(A)}" fill="none" stroke="currentColor"
        stroke-width="1.5" opacity=".9"/></g>
      <g class="s1"><path d="${path(Bp)}" fill="none" stroke="currentColor"
        stroke-width="1.5" opacity=".9"/></g>
      <text x="${PAD}" y="${H - 6}" font-size="8" fill="currentColor"
        opacity=".55">${esc(A[0].t)} / ${esc(Bp[0].t)}</text>
      <text x="${W - PAD}" y="${H - 6}" font-size="8" text-anchor="end"
        fill="currentColor" opacity=".55">${esc(A[K - 1].t)} / ${esc(Bp[K - 1].t)}</text>
    </svg></div>`;
}
function globalBar(series) {
  const items = [];
  if (series.length > 1)
    items.push(`<button class="chip${_ov ? " on" : ""}" data-act="overlay">overlay indices</button>`);
  if (serverToolsOK())
    items.push(`<button class="chip${_anaOpen ? " on" : ""}" data-act="ana">El Niño analogues</button>`);
  return items.length ? `<div class="ctl">${items.join("")}</div>` : "";
}
function anaHTML() {
  if (!_anaOpen) return "";
  if (_ana === "loading")
    return '<div class="card"><div class="meta">loading analogue events…</div></div>';
  if (_ana && _ana.err)
    return `<div class="card"><div class="livewarn">analogues unavailable: ${esc(_ana.err)}</div></div>`;
  const rows = (_ana && _ana.rows) || [];
  return `<div class="card">
    <div class="meta"><b>El Niño analogue events</b> · derived from NOAA CPC ONI ·
      through ${esc((_ana && _ana.asof) || "")}</div>
    <div class="livewarn">live pull — beyond this receipt's evidence pack, not attested by it</div>
    <div class="tblwrap"><table class="tbl">
      <tr><th>start</th><th>end</th><th>seasons</th><th>peak ONI</th><th>at</th><th>strength</th></tr>
      ${rows.map(r => `<tr><td>${esc(r.start)}</td><td>${esc(r.end)}</td>
        <td>${esc(r.seasons)}</td><td>${esc(r.peak_oni)}</td>
        <td>${esc(r.peak_season)}</td><td>${esc(r.strength)}</td></tr>`).join("")}
    </table></div></div>`;
}
function toggleAnalogues() {
  _anaOpen = !_anaOpen;
  if (_anaOpen && !_ana) {
    _ana = "loading"; paintAna();
    callTool("feeds_query", { dataset: "enso_event_history", params: {} }).then(res => {
      const d = toolJSON(res) || {};
      if (d.status !== "ok") _ana = { err: d.note || "feed declined" };
      else _ana = { rows: (d.records || []).filter(r => r.phase === "El Nino").reverse(),
                    asof: d.as_of };
      paintAna();
    }).catch(e => { _ana = { err: String(e && e.message || e) }; paintAna(); });
  } else paintAna();
}
function paintAna() {
  const el = document.getElementById("ana");
  if (el) { el.innerHTML = anaHTML(); reportSize(); }
  const b = document.querySelector('[data-act="ana"]');
  if (b) b.classList.toggle("on", _anaOpen);
}
function rerender() { if (_data) { render(_data); reportSize(); } }
function wireCharts() {
  document.querySelectorAll("[data-act]").forEach(b => {
    b.onclick = () => {
      const act = b.getAttribute("data-act");
      if (act === "overlay") { _ov = !_ov; rerender(); return; }
      if (act === "ana") { toggleAnalogues(); return; }
      const i = +b.getAttribute("data-chart");
      const st = chartState(i);
      if (act === "src-pack") { st.src = "pack"; st.sel = -1; }
      else if (act === "src-live") {
        st.src = "live"; st.sel = -1;
        if (!st.hist || st.hist.err) { st.hist = null; loadHistory(i); }
      }
      else if (act === "table") st.table = !st.table;
      else if (act === "bands") st.bands = !st.bands;
      else if (act.indexOf("range-") === 0) { st.range = +act.slice(6); st.sel = -1; }
      redrawChart(i);
    };
  });
  document.querySelectorAll("svg.cv").forEach(sv => {
    sv.onclick = e => {
      const i = +sv.getAttribute("data-chart");
      const sr = seriesList()[i]; if (!sr) return;
      const st = chartState(i), pts = activePoints(sr, st);
      if (pts.length < 2) return;
      // getScreenCTM maps client px -> viewBox units exactly, INCLUDING the
      // letterbox margins preserveAspectRatio adds once the element is wider
      // than 560px — which Desktop's 736px box always is. The naive
      // width-proportional map read ~10% off near the edges there: a click on
      // MJJ 2026 reported an earlier month in an EVIDENCE panel.
      const W = 560, PAD = 24;
      let xv;
      try {
        xv = new DOMPoint(e.clientX, e.clientY)
               .matrixTransform(sv.getScreenCTM().inverse()).x;
      } catch (err) {
        const r = sv.getBoundingClientRect();
        xv = (e.clientX - r.left) / r.width * W;
      }
      const k = Math.round((xv - PAD) / (W - 2 * PAD) * (pts.length - 1));
      st.sel = Math.max(0, Math.min(pts.length - 1, k));
      redrawChart(i);
    };
  });
}
function brief(text){
  if (!text) return "";
  // Line-aware, not block-aware: the gate does not require a blank line after
  // a heading, and a real draft put its heading and first sentence on adjacent
  // lines — which a paragraph-splitter renders as one run-on wall.
  let out = "", para = [];
  const flush = () => { if (para.length) { out += `<p>${esc(para.join(" "))}</p>`; para = []; } };
  for (const line of text.split(/\\r?\\n/)) {
    const h = line.match(/^#{1,4}\\s+(.*)$/);
    if (h) { flush(); out += `<h3>${esc(h[1])}</h3>`; }
    else if (!line.trim()) flush();
    else para.push(line.trim());
  }
  flush();
  return out;
}
// Inline is a GLANCE, not a report. Seventeen cards inline is a wall in any host,
// and in one with a fixed short box it is a wall nobody can scroll. So the default
// is charts + a count, with the evidence one click away — and the click also asks
// the host for fullscreen, which it may decline without breaking anything.
// How much of this fits in the box the host gave us. Guessing a fixed layout and
// hoping is what produced a thin strip with everything crammed into it; instead ask
// the container how tall it is and show what actually fits, with the rest one click
// away. Unbounded height -> show everything.
function chartBudget(){
  const cd = _ctx.containerDimensions || {};
  const h = ("height" in cd) ? cd.height : (cd.maxHeight || 0);
  if (!h || _expanded || _ctx.displayMode === "fullscreen") return Infinity;
  return Math.max(1, Math.floor((h - 110) / 190));   // header + one chart card each
}
// Evidence inline when the box can hold it, behind a control when it cannot.
// Desktop reports maxHeight 5000 — hiding seventeen cards behind a click there is
// as wrong as cramming them into 320px was.
function roomForEvidence(){
  if (_expanded || _ctx.displayMode === "fullscreen") return true;
  const cd = _ctx.containerDimensions || {};
  const h = ("height" in cd) ? cd.height : (cd.maxHeight || 0);
  return !h || h >= 1200;
}
function expand(){
  _expanded = true;
  request("ui/request-display-mode", { mode: "fullscreen" });
  if (_data) { render(_data); reportSize(); }
}
function render(d){
  _data = d;
  const srcs = d.sources || [], gaps = d.gaps || [];
  const ins = d.insight || {};
  const series = ins.series || [], budget = chartBudget();
  const shown = series.slice(0, budget === Infinity ? series.length : budget);
  const hidden = series.slice(shown.length);
  const pulled = new Set((d.evidence_freshness||{}).pulled_sources || []);
  document.getElementById("root").innerHTML = `
    <h2>${esc(d.question || "Evidence")}</h2>
    <div class="sub">${srcs.length} sources · ${pulled.size} pulled live · ${gaps.length} declared gap(s)</div>
    ${globalBar(series)}
    ${_ov && series.length > 1 ? overlayCard(series) : ""}
    ${shown.map((sr, i) => chart(sr, i)).join("")}
    <div id="ana">${anaHTML()}</div>
    ${hidden.map(sr => `<div class="meta oneline"><b>${esc(sr.title||sr.id)}</b> ·
        ${esc((sr.points[sr.points.length-1]||{}).v)}
        ${esc((sr.points[sr.points.length-1]||{}).c||"")} ·
        as of ${esc(sr.as_of||"")}</div>`).join("")}
    ${srcs.length ? (roomForEvidence()
      ? `<h2 class="sec">Evidence</h2>`
      : `<button class="more" id="more">Show ${hidden.length?`${hidden.length} more chart(s) and `:""}all ${srcs.length} sources</button>`) : ""}
    <div class="grid" style="display:${roomForEvidence()?"grid":"none"}">${srcs.map(s => `
      <div class="card">
        <b>[${esc(s.n)}] ${esc(s.source)}</b>
        <div class="meta">${esc(s.title||"")}</div>
        <div class="meta">
          <span class="pill ${pulled.has(s.n)?"live":"arch"}">${pulled.has(s.n)?"pulled live":"archived"}</span>
          ${esc(s.pub_date||"undated")} · ${esc(s.validation||"unvalidated")}
        </div>
        ${s.caveat?`<div class="meta">⚠ ${esc(s.caveat)}</div>`:""}
        <div class="links">
          ${s.archived_url?`<a class="lnk" data-url="${esc(s.archived_url)}"
             href="${esc(s.archived_url)}">archived copy</a>`:""}
          ${s.archived_url && s.url?" · ":""}
          ${s.url?`<a class="lnk" data-url="${esc(s.url)}" href="${esc(s.url)}">source</a>`:""}
        </div>
      </div>`).join("")}</div>
    ${gaps.length?`<h2 style="margin-top:1rem">What is missing</h2>
      ${gaps.map(g=>`<div class="gap">${esc(g)}</div>`).join("")}`:""}
    <div class="foot">
      This shows EVIDENCE only. The verdict is deliberately not drawn here: a rendered
      surface freezes what is in it, and a frozen verdict attests nothing.
      ${d.public_resolver?`Resolve it live: <a class="lnk" data-url="${esc(d.public_resolver)}" href="${esc(d.public_resolver)}">${esc(d.receipt_id||"receipt")}</a>`:""}
      <div class="meta">${esc((_ctx.hostInfo||{}).name||"host")} ·
        ${esc(_ctx.displayMode||"inline")} ·
        box ${esc(JSON.stringify(_ctx.containerDimensions||"unspecified"))} ·
        ${serverToolsOK() ? "tool calls proxied — live history available"
                          : "host does not proxy tool calls — pack snapshot only"}</div>
    </div>`;
  const more = document.getElementById("more");
  if (more) more.onclick = expand;
  wireLinks();
  wireCharts();
}
// MCP Apps delivers the tool result by postMessage as `ui/notifications/tool-result`.
// Accept a couple of shapes: the spec is young and hosts differ, and a template that
// only understands one of them silently renders nothing.
// --- MCP Apps lifecycle, spec 2026-01-26 --------------------------------
// The View MUST OPEN with a `ui/initialize` REQUEST. Only once the Host answers
// it, and the View replies with `ui/notifications/initialized`, may the Host send
// anything at all — the spec is explicit: "The Host MUST NOT send any request or
// notification to the View before it receives an `initialized` notification."
//
// Our first version skipped the request and posted the notification on its own.
// Claude Desktop therefore rendered the iframe and correctly sent nothing back,
// and the panel sat on "loading..." forever. The widget was never broken; the
// handshake was never opened.
let _id = 0, _ready = false, _ctx = {}, _caps = {}, _data = null, _expanded = false;
// id -> METHOD. It was a bare Set, so every response was handled as if it were the
// initialize response — and `ui/open-link` acks with an empty result, so
// `_caps = m.result.hostCapabilities || {}` wiped openLinks after the first click.
// Links then fell through to window.open, which a sandboxed iframe blocks. That is
// the "works once or twice then stops" symptom, and request-display-mode did it too.
const _pending = new Map();
const post = m => { try { parent.postMessage(m, "*"); } catch (_) {} };
const request = (method, params) => {
  const id = ++_id; _pending.set(id, method);
  post({ jsonrpc: "2.0", id, method, params });
  return id;
};
const INIT_PARAMS = {
  protocolVersion: "2026-01-26",
  appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  appInfo: { name: "servirplatform-evidence", version: "0.1.0" },
  // The spec's normative text names `appCapabilities`; its own inline sample uses
  // the MCP-style `capabilities`/`clientInfo`. Send both rather than bet on which
  // one a given host reads — extra JSON-RPC params are ignored, a missing one hangs.
  capabilities: {}, clientInfo: { name: "servirplatform-evidence", version: "0.1.0" }
};
// The host hands the View its OWN design tokens, fonts, theme and container size
// in `hostContext`. Adopting them is the difference between a panel that belongs in
// the conversation and one that arrives in someone else's colours. Our palette
// stays underneath as the fallback layer, for hosts that send nothing and for the
// standalone browser view.
function applyHostContext(hc) {
  if (!hc) return;
  _ctx = Object.assign({}, _ctx, hc);
  const root = document.documentElement;
  const vars = (hc.styles && hc.styles.variables) || {};
  for (const k in vars) if (vars[k]) root.style.setProperty(k, vars[k]);
  const fonts = hc.styles && hc.styles.css && hc.styles.css.fonts;
  if (fonts) { const st = document.createElement("style"); st.textContent = fonts;
               document.head.appendChild(st); }
  if (hc.theme) root.style.colorScheme = hc.theme;
  const cd = hc.containerDimensions;
  if (cd) {                                  // sizing rules per the spec
    // Fixed height means the HOST owns the box and ignores size-changed. Filling it
    // without allowing scroll is what crammed everything into a thin strip.
    if ("height" in cd) { root.style.height = "100vh"; root.style.overflow = "auto"; }
    else if (cd.maxHeight) root.style.maxHeight = cd.maxHeight + "px";
    if ("width" in cd) root.style.width = "100vw";
    else if (cd.maxWidth) root.style.maxWidth = cd.maxWidth + "px";
  }
}
// A sandboxed iframe cannot navigate the parent, so an <a href target="_blank">
// silently does nothing — which is why the receipt link and the sources were dead
// on Desktop. The spec provides `ui/open-link`, gated by hostCapabilities.openLinks.
// Fall back to window.open for the standalone browser view, where there is no host.
let _lastLink = "";
function note(msg) {
  const el = document.getElementById("note");
  if (el) { el.textContent = msg; el.style.display = "block"; }
}
function openLink(url) {
  if (!url) return;
  _lastLink = url;
  if (_caps.openLinks) request("ui/open-link", { url: url });
  else {
    // No host capability: try the browser, and if the sandbox blocks it say so
    // rather than leaving a click that appears to do nothing.
    let w = null;
    try { w = window.open(url, "_blank", "noopener"); } catch (_) {}
    if (!w) note("This host will not open links from the panel. Copy it: " + url);
  }
}
function wireLinks() {
  document.querySelectorAll("[data-url]").forEach(el => {
    el.onclick = e => { e.preventDefault(); openLink(el.getAttribute("data-url")); };
  });
}
function ready() {
  if (_ready) return;
  _ready = true;
  post({ jsonrpc: "2.0", method: "ui/notifications/initialized", params: {} });
}
// Flexible height: with no fixed `height` in containerDimensions the host sizes the
// iframe from what the View REPORTS, and a View that reports nothing gets the
// host's default box. Ours did not report, so two charts and seventeen cards were
// clipped into a ~300px window. The SDK does this with a ResizeObserver; we are not
// using the SDK, so we do it by hand.
let _lastH = 0;
function reportSize(){
  const h = Math.ceil(document.documentElement.scrollHeight);
  if (h && Math.abs(h - _lastH) > 2) {
    _lastH = h;
    post({ jsonrpc: "2.0", method: "ui/notifications/size-changed",
           params: { height: h } });
  }
}
try { new ResizeObserver(reportSize).observe(document.documentElement); } catch (_) {}
window.addEventListener("message", e => {
  const m = e.data || {};
  // A REQUEST or NOTIFICATION carries `method`; a RESPONSE does not. Classify on
  // that, never on id alone: the host numbers its own requests from its own
  // counter, and an id collision with ours previously swallowed the host's
  // request as if it were an answer to us. JSON-RPC also obliges us to answer
  // every id-bearing request — `ping` is exactly that, and an unanswered ping
  // reads as a dead View.
  if (m.method && m.id !== undefined && m.id !== null) {
    if (m.method === "ping") post({ jsonrpc: "2.0", id: m.id, result: {} });
    else post({ jsonrpc: "2.0", id: m.id,
                error: { code: -32601, message: "not implemented: " + m.method } });
    return;
  }
  if (!m.method && m.id && _calls.has(m.id)) {   // a server tool answered
    const h = _calls.get(m.id); _calls.delete(m.id);
    if (m.error) h.reject(new Error((m.error && m.error.message) || "tool call failed"));
    else h.resolve(m.result);
    return;
  }
  if (!m.method && m.id && _pending.has(m.id)) {
    const method = _pending.get(m.id);
    _pending.delete(m.id);
    if (method === "ui/initialize" || method === "initialize") {
      if (m.result) {                      // on error, the fallback below retries
        _caps = m.result.hostCapabilities || {};
        applyHostContext(m.result.hostContext); ready();
      }
    } else if (method === "ui/open-link" && m.error) {
      // A denied link must not look like a dead one.
      note("Link blocked by the host. Copy it instead: " + (_lastLink || ""));
    }
    return;
  }
  // Theme or size can change while we are on screen (light/dark toggle, expand).
  if (m.method === "ui/notifications/host-context-changed") {
    applyHostContext(m.params); return;
  }
  const isResult = m.method === "ui/notifications/tool-result";
  const d = (isResult && (m.params?.structuredContent || m.params))
         || m.structuredContent || m.evidence;
  if (d && (d.sources || d.question)) { render(strip(d)); reportSize(); }
});
request("ui/initialize", INIT_PARAMS);
// Same spec document names the method `ui/initialize` in its normative text and
// `initialize` in its sample code. Try the documented one, then fall back, so a
// host that implemented from the sample still reaches us instead of hanging.
setTimeout(() => { if (!_ready) request("initialize", INIT_PARAMS); }, 700);
const inline = document.getElementById("payload");
if (inline) render(strip(JSON.parse(inline.textContent)));
</script>
""".replace("__TOKENS_CSS__", _tokens_css()) \
       .replace("__VERDICT_FIELDS__", json.dumps(list(_VERDICT_FIELDS)))


# Fields that assert a verdict. The renderer strips these before drawing anything —
# see VERDICT_FIELDS in the template, generated from this tuple.
#
# Why not strip them server-side: `structuredContent` is ONE payload serving two
# readers. The model needs `passed` to know whether it may show the brief at all;
# the iframe must never receive it, because a rendered surface freezes what is in
# it and a frozen verdict attests nothing (rule 5). Removing the fields from the
# tool result would satisfy the iframe by lying to the model. So the split happens
# at the renderer. `evidence_payload` remains the server-side filter for the
# standalone/browser path, where there is no second reader.
_VERDICT_FIELDS = ("passed", "evidence_tier", "verified_text", "draft_sha256", "report_id")


def evidence_payload(receipt: dict) -> dict:
    """What the app is allowed to see: evidence, provenance, gaps and a live
    resolver link. Never the verdict."""
    return {k: v for k, v in (receipt or {}).items() if k not in _VERDICT_FIELDS}


def standalone(data: dict) -> str:
    """The same template with a payload baked in, for opening in a browser. Used to
    verify the app renders without needing a host that supports MCP Apps."""
    marker = '<div id="root">loading…</div>'
    return template().replace(
        marker,
        marker + '\n<script type="application/json" id="payload">'
        # "</" escaped: a source title containing "</script>" would otherwise
        # terminate the block at HTML-parse time and execute what follows.
        # "<\/" is legal JSON and identical after JSON.parse.
        + json.dumps(evidence_payload(data)).replace("</", "<\\/") + "</script>")
