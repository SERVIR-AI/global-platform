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

UI_URI = "ui://grp/evidence"
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
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
// An interactive chart, not a picture. Inline SVG and hand-wired events because the
// sandbox CSP forbids loading a charting library from anywhere.
//
// Three affordances, all click-driven (hover alone is useless on touch and invisible
// in a screenshot): a RANGE picker to narrow the window, a POINT picker so any month
// can be read exactly rather than estimated off an axis, and a BANDS toggle for the
// El Nino / La Nina thresholds.
const _cs = {};                       // per-chart UI state, keyed by index
function chartState(i, n) {
  if (!_cs[i]) _cs[i] = { range: 0, sel: n - 1, bands: true };
  return _cs[i];
}
function chartSVG(sr, i, st) {
  const all = sr.points || [];
  const pts = st.range ? all.slice(-st.range) : all;
  const W = 560, H = 116, PAD = 24;
  const vs = pts.map(p => p.v);
  const lo = Math.min(-0.8, ...vs), hi = Math.max(0.8, ...vs);
  const x = k => PAD + (pts.length === 1 ? (W - PAD * 2) / 2
                                         : k * (W - PAD * 2) / (pts.length - 1));
  const y = v => H - PAD - (v - lo) * (H - PAD * 2) / (hi - lo);
  const line = pts.map((p, k) => (k ? "L" : "M") + x(k).toFixed(1) + "," + y(p.v).toFixed(1)).join("");
  const band = (v, label) => !st.bands ? "" : `
    <line x1="${PAD}" x2="${W - PAD}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"
      stroke="currentColor" stroke-dasharray="3 3" opacity=".35"/>
    <text x="${W - PAD + 2}" y="${(y(v) + 3).toFixed(1)}" font-size="8"
      fill="currentColor" opacity=".55">${label}</text>`;
  const sel = Math.min(Math.max(st.sel, 0), pts.length - 1);
  const sp = pts[sel];
  const dots = pts.map((p, k) => `
    <circle class="pt" data-chart="${i}" data-pt="${k}" cx="${x(k).toFixed(1)}"
      cy="${y(p.v).toFixed(1)}" r="7" fill="transparent" style="cursor:pointer"/>
    <circle cx="${x(k).toFixed(1)}" cy="${y(p.v).toFixed(1)}"
      r="${k === sel ? 3.5 : 1.8}" fill="currentColor"
      opacity="${k === sel ? 1 : .45}" pointer-events="none"/>`).join("");
  return `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
         aria-label="${esc(sr.title || sr.id)}">
      ${band(0.5, "+0.5 El Nino")}${band(-0.5, "-0.5 La Nina")}
      <line x1="${x(sel).toFixed(1)}" x2="${x(sel).toFixed(1)}" y1="${PAD - 8}"
        y2="${H - PAD}" stroke="currentColor" opacity=".28"/>
      <path d="${line}" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".85"/>
      ${dots}
      <text x="${PAD}" y="${H - 6}" font-size="8" fill="currentColor"
        opacity=".55">${esc(pts[0] ? pts[0].t : "")}</text>
      <text x="${W - PAD}" y="${H - 6}" font-size="8" text-anchor="end"
        fill="currentColor" opacity=".55">${esc(pts[pts.length - 1] ? pts[pts.length - 1].t : "")}</text>
    </svg>
    <div class="readout"><b>${esc(sp ? sp.t : "")}</b> &nbsp;${esc(sp ? sp.v : "")}
      ${esc(sp && sp.c ? sp.c : "")}</div>`;
}
function chartInner(sr, i) {
  const n = (sr.points || []).length, st = chartState(i, n);
  const ranges = [6, 12, 24].filter(r => r < n).concat([0]);
  const label = r => r ? "last " + r : "all " + n;
  return `
    <div class="meta"><b>${esc(sr.title || sr.id)}</b> · ${esc(sr.source || "")} ·
      as of ${esc(sr.as_of || "")}${sr.n ? " · [" + esc(sr.n) + "]" : ""}</div>
    <div class="ctl">
      ${ranges.map(r => `<button class="chip${st.range === r ? " on" : ""}"
         data-chart="${i}" data-range="${r}">${label(r)}</button>`).join("")}
      <button class="chip${st.bands ? " on" : ""}" data-chart="${i}"
        data-bands="1">thresholds</button>
    </div>
    ${chartSVG(sr, i, st)}`;
}
function chart(sr, i) {
  return `<div class="card chart" id="ch${i}">${chartInner(sr, i)}</div>`;
}
function redrawChart(i) {
  const sr = ((_data && _data.insight && _data.insight.series) || [])[i];
  const el = document.getElementById("ch" + i);
  if (!sr || !el) return;
  el.innerHTML = chartInner(sr, i);
  wireCharts();
  reportSize();
}
function wireCharts() {
  document.querySelectorAll("[data-range]").forEach(b => {
    b.onclick = () => { const i = +b.getAttribute("data-chart");
      const st = chartState(i, 0); st.range = +b.getAttribute("data-range");
      st.sel = 1e9; redrawChart(i); };          // clamped to the last point on redraw
  });
  document.querySelectorAll("[data-bands]").forEach(b => {
    b.onclick = () => { const i = +b.getAttribute("data-chart");
      const st = chartState(i, 0); st.bands = !st.bands; redrawChart(i); };
  });
  document.querySelectorAll("[data-pt]").forEach(c => {
    c.onclick = () => { const i = +c.getAttribute("data-chart");
      chartState(i, 0).sel = +c.getAttribute("data-pt"); redrawChart(i); };
  });
}
// The brief arrives as the markdown the model wrote and the gate checked. Headings
// and paragraphs only — it renders the gated text as written, never reformatting it.
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
    ${shown.map((sr, i) => chart(sr, i)).join("")}
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
        box ${esc(JSON.stringify(_ctx.containerDimensions||"unspecified"))}</div>
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
  appInfo: { name: "grp-evidence", version: "0.1.0" },
  // The spec's normative text names `appCapabilities`; its own inline sample uses
  // the MCP-style `capabilities`/`clientInfo`. Send both rather than bet on which
  // one a given host reads — extra JSON-RPC params are ignored, a missing one hangs.
  capabilities: {}, clientInfo: { name: "grp-evidence", version: "0.1.0" }
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
  if (m.id && _pending.has(m.id)) {
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
        + json.dumps(evidence_payload(data)) + "</script>")
