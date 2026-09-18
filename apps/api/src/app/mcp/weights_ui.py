"""MCP App: a picker for the Layer-2 vulnerability weights.

Why an app and not a form in prose. The weights are the one thing the hub most wants
to change and the hardest thing to state in a sentence: they are shares of one score
and must add to 1, so every edit moves the others. Typing that as JSON in a chat
message is how people submit weights summing to 0.97 and never find out. A slider
that renormalises as you drag makes the constraint the interface rather than a rule
someone has to remember.

It is NOT a control panel. Nothing here writes: the button composes a CONTRIBUTION
and hands it to the same gate every other contribution goes through, so the change
is staged, previewable by its author alone, and live only when a reviewer approves.
The app's job is to get the numbers and the reason right before that.

The lifecycle below mirrors app_ui.py, which paid for it: the View must OPEN with a
`ui/initialize` request and wait for the host's answer before anything is sent.
"""

from __future__ import annotations

import json

from . import ui

UI_URI = "ui://servirplatform/risk-weights"
UI_MIME = "text/html;profile=mcp-app"


def _tokens_css() -> str:
    try:
        return ui.css_vars()
    except Exception:
        return ":root{--grp-base-100:#fbfcfd;--grp-base-content:#1c212a;--grp-primary:#2380b0}"


def template() -> str:
    return """<!doctype html>
<meta charset="utf-8">
<style>
__TOKENS_CSS__
*{box-sizing:border-box}
body{margin:0;padding:14px;font-family:var(--font-sans,ui-sans-serif,system-ui,sans-serif);
  font-size:.875rem;line-height:1.5;
  color:var(--color-text-primary,var(--grp-base-content,#1c212a));
  background:var(--color-background-primary,var(--grp-base-100,#fbfcfd))}
h2{margin:0 0 .1rem;font-size:1rem;font-weight:600}
.sub{color:var(--color-text-secondary,#5a6472);margin-bottom:.9rem}
.row{display:grid;grid-template-columns:1fr 62px;gap:10px;align-items:center;
  padding:7px 0;border-top:1px solid var(--color-border-primary,var(--grp-base-300,#e0e6ec))}
.row:first-of-type{border-top:0}
.lab{display:flex;flex-direction:column;gap:2px;min-width:0}
.nm{font-weight:500;overflow-wrap:anywhere}
.hint{font-size:.75rem;color:var(--color-text-secondary,#5a6472)}
input[type=range]{width:100%;accent-color:var(--grp-primary,#2380b0)}
.val{font-variant-numeric:tabular-nums;text-align:right;font-weight:600}
.delta{font-size:.72rem;color:var(--color-text-secondary,#5a6472);
  font-variant-numeric:tabular-nums;text-align:right}
.sum{display:flex;justify-content:space-between;align-items:center;margin:.7rem 0 .2rem;
  padding:.45rem .6rem;border-radius:6px;
  background:var(--color-background-secondary,var(--grp-base-200,#f1f4f7))}
.sum.bad{background:#f6e4e4;color:#9b2c2c}
label.blk{display:block;margin:.8rem 0 .25rem;font-weight:500}
textarea{width:100%;min-height:58px;padding:.45rem .55rem;border-radius:6px;
  border:1px solid var(--color-border-primary,#e0e6ec);font:inherit;resize:vertical;
  background:var(--color-background-primary,#fff);color:inherit}
select{font:inherit;padding:.3rem .4rem;border-radius:6px;
  border:1px solid var(--color-border-primary,#e0e6ec);background:inherit;color:inherit}
button{font:inherit;font-weight:600;padding:.5rem .9rem;border-radius:6px;border:0;
  background:var(--grp-primary,#2380b0);color:#fff;cursor:pointer}
button[disabled]{opacity:.45;cursor:not-allowed}
button.ghost{background:transparent;color:inherit;
  border:1px solid var(--color-border-primary,#e0e6ec);font-weight:500}
.bar{display:flex;gap:8px;align-items:center;margin-top:.9rem}
.msg{margin-top:.7rem;padding:.5rem .6rem;border-radius:6px;font-size:.82rem;
  background:var(--color-background-secondary,#f1f4f7);white-space:pre-wrap}
.msg.err{background:#f6e4e4;color:#9b2c2c}
.msg.ok{background:#e6f3ec;color:#1f7a4d}
.foot{margin-top:.8rem;font-size:.75rem;color:var(--color-text-secondary,#5a6472)}
</style>
<h2>Vulnerability weights</h2>
<div class="sub" id="sub">loading…</div>
<div id="haz" style="margin-bottom:.6rem"></div>
<div id="rows"></div>
<div class="sum" id="sum"></div>
<label class="blk" for="why">Why these weights <span class="hint">(a reviewer judges the
reason, not the numbers)</span></label>
<textarea id="why" placeholder="e.g. population matters more than road access for riverine
flood in the Lower Mekong"></textarea>
<label class="blk" for="notes">Guidance for anyone reading a risk level from these
<span class="hint">(optional, 500 characters)</span></label>
<textarea id="notes" placeholder="e.g. Hub-adjusted for riverine flood; not calibrated
against observed loss."></textarea>
<div class="bar">
  <button id="go" disabled>Propose these weights</button>
  <button id="reset" class="ghost">Reset</button>
</div>
<div class="msg" id="msg" hidden></div>
<div class="foot">Nothing here changes a number on its own. Proposing sends a contribution
through the same gate every source goes through: yours to preview, live for everyone only
once a reviewer approves.</div>
<script>
let _id = 0, _ready = false, _caps = {}, _data = null;
const _pending = new Map(), _calls = new Map();
const post = m => { try { parent.postMessage(m, "*"); } catch (_) {} };
const request = (method, params) => { const id = ++_id; _pending.set(id, method);
  post({ jsonrpc: "2.0", id, method, params }); return id; };
const INIT = { protocolVersion: "2026-01-26",
  appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
  appInfo: { name: "servirplatform-weights", version: "0.1.0" },
  capabilities: {}, clientInfo: { name: "servirplatform-weights", version: "0.1.0" } };

function callTool(name, args) {
  return new Promise((res, rej) => {
    if (!(_caps && _caps.serverTools)) { rej(new Error(
      "this host does not let an app call tools — paste the contribution instead")); return; }
    const id = ++_id; _calls.set(id, { res, rej });
    post({ jsonrpc: "2.0", id, method: "tools/call",
           params: { name, arguments: args } });
  });
}
const toolJSON = r => { for (const b of (r && r.content) || [])
  if (b.type === "text") { try { return JSON.parse(b.text); } catch (e) { return null; } }
  return null; };

let HAZ = null, W = {}, BASE = {}, LABELS = {};
const pretty = k => (LABELS[k] || k).replace(/^vulnerability_(reclass_)?/, "").replace(/_/g, " ");
const sum = () => Object.values(W).reduce((a, b) => a + b, 0);

function render() {
  if (!_data) return;
  const hz = document.getElementById("haz");
  const opts = (_data.hazards || []).map(h =>
    `<option value="${h}"${h === HAZ ? " selected" : ""}>${h}</option>`).join("");
  hz.innerHTML = `<label class="blk" for="hsel" style="margin-top:0">Hazard</label>
    <select id="hsel">${opts}</select>`;
  document.getElementById("hsel").onchange = e => { HAZ = e.target.value; load(HAZ); };

  const keys = Object.keys(W).sort();
  document.getElementById("rows").innerHTML = keys.map(k => {
    const v = W[k], b = BASE[k] || 0, d = v - b;
    const arrow = Math.abs(d) < 0.0005 ? "" :
      (d > 0 ? "\\u25b2 " : "\\u25bc ") + Math.abs(d).toFixed(2) + " from " + b.toFixed(2);
    return `<div class="row">
      <div class="lab"><span class="nm">${pretty(k)}</span>
        <input type="range" min="0" max="100" value="${Math.round(v * 100)}" data-k="${k}">
        <span class="hint">${k}</span></div>
      <div><div class="val">${v.toFixed(2)}</div><div class="delta">${arrow}</div></div>
    </div>`; }).join("");
  for (const el of document.querySelectorAll("input[type=range]"))
    el.oninput = e => { W[e.target.dataset.k] = (+e.target.value) / 100; normalise(e.target.dataset.k); render(); };

  const s = sum(), okSum = Math.abs(s - 1) < 0.001;
  const sEl = document.getElementById("sum");
  sEl.className = "sum" + (okSum ? "" : " bad");
  sEl.innerHTML = `<span>${okSum ? "Shares add up" : "Shares must add to 1.00"}</span>
                   <strong>${s.toFixed(2)}</strong>`;
  const changed = keys.some(k => Math.abs(W[k] - (BASE[k] || 0)) > 0.001);
  document.getElementById("go").disabled = !(okSum && changed);
  reportSize();
}
function normalise(held) {                 // the others absorb the change, so it always adds up
  const keys = Object.keys(W), others = keys.filter(k => k !== held);
  const rest = 1 - W[held], cur = others.reduce((a, k) => a + W[k], 0);
  if (!others.length) { W[held] = 1; return; }
  if (cur <= 0) { others.forEach(k => W[k] = rest / others.length); return; }
  others.forEach(k => W[k] = Math.max(0, +(W[k] * rest / cur).toFixed(4)));
  const drift = 1 - sum(); W[others[others.length - 1]] += +drift.toFixed(4);
}
function load(hazard) {
  const rec = (_data.recipes || {})[hazard] || {};
  BASE = {}; W = {};
  for (const k in rec) { BASE[k] = +rec[k]; W[k] = +rec[k]; }
  LABELS = _data.labels || {};
  const adj = (_data.adjusted || {})[hazard];
  document.getElementById("sub").textContent = adj
    ? `In force: adjusted by ${adj.by}. ${adj.rationale || ""}`
    : "In force: the platform's own starting values, uncalibrated.";
  msg("", null); render();
}
function msg(t, cls) { const m = document.getElementById("msg");
  m.hidden = !t; m.textContent = t; m.className = "msg" + (cls ? " " + cls : ""); reportSize(); }
function reportSize() { post({ jsonrpc: "2.0", method: "ui/notifications/size-changed",
  params: { height: Math.ceil(document.documentElement.scrollHeight) } }); }

document.getElementById("reset").onclick = () => load(HAZ);
document.getElementById("go").onclick = () => {
  const why = document.getElementById("why").value.trim();
  if (!why) { msg("Say why. A weight with no stated reason cannot be reviewed.", "err"); return; }
  const notes = document.getElementById("notes").value.trim();
  const manifest = { hazard: HAZ, rationale: why,
    weights: Object.fromEntries(Object.keys(W).map(k => [k, +W[k].toFixed(4)])) };
  if (notes) manifest.usage_notes = notes;
  document.getElementById("go").disabled = true;
  msg("Sending to the contribution gate\\u2026", null);
  callTool("contribute_submit", { kind: "weights", manifest }).then(r => {
    const d = toolJSON(r) || {};
    if (d.status === "staged") {
      msg("Staged as " + d.contribution_id + ". Your own risk answers use these weights now; "
        + "everyone else keeps the ones in force until a reviewer approves.", "ok");
    } else {
      msg((d.problems || [d.note || "declined"]).join("\\n"), "err");
      document.getElementById("go").disabled = false;
    }
  }).catch(e => { msg(String(e && e.message || e), "err");
                  document.getElementById("go").disabled = false; });
};

addEventListener("message", ev => {
  const m = ev.data; if (!m || m.jsonrpc !== "2.0") return;
  if (m.id && _pending.has(m.id)) {
    const method = _pending.get(m.id); _pending.delete(m.id);
    if (method === "ui/initialize") {
      _caps = (m.result && m.result.hostCapabilities) || {};
      const hc = (m.result && m.result.hostContext) || {};
      const vars = (hc.styles && hc.styles.variables) || {};
      for (const k in vars) if (vars[k]) document.documentElement.style.setProperty(k, vars[k]);
      if (hc.theme) document.documentElement.style.colorScheme = hc.theme;
      _ready = true;
      post({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    }
    return;
  }
  if (m.id && _calls.has(m.id)) {
    const h = _calls.get(m.id); _calls.delete(m.id);
    m.error ? h.rej(new Error(m.error.message || "tool call failed")) : h.res(m.result);
    return;
  }
  if (m.method === "ui/notifications/tool-result" || m.method === "ui/notifications/render-data") {
    const p = m.params || {};
    const raw = p.structuredContent || p.data || p.result || p;
    _data = (raw && raw.weights_picker) ? raw.weights_picker : raw;
    HAZ = _data.hazard || (_data.hazards || [])[0];
    load(HAZ);
  }
});
request("ui/initialize", INIT);
</script>
"""


def html() -> str:
    return template().replace("__TOKENS_CSS__", _tokens_css())


def payload(hazard: str | None = None) -> dict:
    """What the picker renders: every hazard's recipe, the layers a weight may name,
    and who last adjusted each. Read-only — the picker proposes, the gate decides."""
    from ..contrib import staging
    from ..graph.geo import combine, tiffs

    recipe = combine._recipe()
    recipes = {h: {k: float(v) for k, v in row.items()}
               for h, row in (recipe.get("weights") or {}).items()}
    catalog_hazards = {k.removeprefix("hazard_") for k in tiffs.catalog()
                       if k.startswith("hazard_")}
    hazards = sorted(h for h in recipes if h in catalog_hazards) or sorted(recipes)
    staged = {}
    for h in hazards:
        row = staging.visible_staged_weights(h)
        if row:
            staged[h] = {"by": row.get("contributor_label"),
                         "contribution_id": row.get("contribution_id"),
                         "weights": row.get("weights")}
            recipes[h] = {k: float(v) for k, v in (row.get("weights") or {}).items()}
    return {
        "hazard": hazard if hazard in hazards else (hazards[0] if hazards else None),
        "hazards": hazards,
        "recipes": recipes,
        "available_layers": staging.available_vulnerability_layers(),
        "adjusted": {h: v for h, v in (recipe.get("adjusted") or {}).items() if h in hazards},
        "staged_for_you": staged,
        "crossing_rule": recipe.get("crossing"),
        "note": ("Weights are shares of one vulnerability score and must sum to 1. "
                 "Changing them changes every risk level computed for that hazard, so a "
                 "proposal is staged for its author and live only once a reviewer approves."),
    }


def describe() -> str:
    return ("The vulnerability weights behind every risk level: what each hazard's recipe "
            "weighs today, who last changed it and why, and which vulnerability layers a "
            "weight may name.\n\n"
            "In a host that renders MCP Apps this opens a picker with a slider per layer "
            "that keeps the shares summing to 1, and a box for the reason. Proposing from "
            "it calls contribute_submit(kind='weights'), so the change is staged, visible "
            "in the author's own risk answers, and in force for everyone only after a "
            "reviewer approves. Elsewhere it is a plain reading of the recipe; contribute "
            "the same way by calling contribute_submit yourself.\n\n"
            "Pass a hazard name to open on it. Returns {hazard, hazards, recipes, "
            "available_layers, adjusted, staged_for_you, crossing_rule}.")
