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
%s
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;
  background:var(--grp-base-100,#fbfcfd);color:var(--grp-base-content,#1c212a);padding:14px}
h2{margin:.2rem 0 .1rem;font-size:1.05rem}
.sub{color:var(--grp-neutral,#5a6472);font-size:.8rem;margin-bottom:.9rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.6rem}
.card{border:1px solid var(--grp-base-300,#e0e6ec);border-radius:8px;padding:.6rem .7rem;
  background:var(--grp-base-200,#f1f4f7)}
.card b{display:block;font-size:.82rem}
.meta{color:var(--grp-neutral,#5a6472);font-size:.74rem;margin-top:.15rem}
.pill{display:inline-block;font-size:.68rem;padding:.05rem .4rem;border-radius:4px;
  border:1px solid currentColor;margin-right:.25rem}
.live{color:var(--grp-info,#2380b0)} .arch{color:var(--grp-neutral,#5a6472)}
.gap{border-left:3px solid var(--grp-warning,#a06a08);padding:.5rem .7rem;margin:.3rem 0;
  background:color-mix(in srgb,var(--grp-warning,#a06a08) 8%%,transparent);font-size:.82rem}
.foot{margin-top:1rem;font-size:.76rem;color:var(--grp-neutral,#5a6472);
  border-top:1px solid var(--grp-base-300,#e0e6ec);padding-top:.6rem}
a{color:var(--grp-primary,#2380b0)}
</style>
<div id="root">loading…</div>
<script>
// GENERATED from _VERDICT_FIELDS — mirrored, never re-typed, so the two cannot drift.
const VERDICT_FIELDS = %s;
// Rule 5 enforced at the RENDERER, which is the only place that matters: the tool
// result legitimately carries the gate outcome (the MODEL must see it), and
// the host hands that whole result to this iframe. Stripping server-side would
// blind the model; stripping here keeps the verdict out of the frozen surface
// without taking it away from the caller.
const strip = o => { const c = {...o}; for (const k of VERDICT_FIELDS) delete c[k]; return c; };
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
function render(d){
  const srcs = d.sources || [], gaps = d.gaps || [];
  const pulled = new Set((d.evidence_freshness||{}).pulled_sources || []);
  document.getElementById("root").innerHTML = `
    <h2>${esc(d.question || "Evidence")}</h2>
    <div class="sub">${srcs.length} sources · ${pulled.size} pulled live · ${gaps.length} declared gap(s)</div>
    <div class="grid">${srcs.map(s => `
      <div class="card">
        <b>[${esc(s.n)}] ${esc(s.source)}</b>
        <div class="meta">${esc(s.title||"")}</div>
        <div class="meta">
          <span class="pill ${pulled.has(s.n)?"live":"arch"}">${pulled.has(s.n)?"pulled live":"archived"}</span>
          ${esc(s.pub_date||"undated")} · ${esc(s.validation||"unvalidated")}
        </div>
        ${s.caveat?`<div class="meta">⚠ ${esc(s.caveat)}</div>`:""}
      </div>`).join("")}</div>
    ${gaps.length?`<h2 style="margin-top:1rem">What is missing</h2>
      ${gaps.map(g=>`<div class="gap">${esc(g)}</div>`).join("")}`:""}
    <div class="foot">
      This shows EVIDENCE only. The verdict is deliberately not drawn here: a rendered
      surface freezes what is in it, and a frozen verdict attests nothing.
      ${d.public_resolver?`Resolve it live: <a href="${esc(d.public_resolver)}" target="_blank">${esc(d.receipt_id||"receipt")}</a>`:""}
    </div>`;
}
// MCP Apps delivers the tool result by postMessage as `ui/notifications/tool-result`.
// Accept a couple of shapes: the spec is young and hosts differ, and a template that
// only understands one of them silently renders nothing.
window.addEventListener("message", e => {
  const m = e.data || {};
  const d = (m.method === "ui/notifications/tool-result" && (m.params?.structuredContent || m.params))
         || m.structuredContent || m.evidence;
  if (d && (d.sources || d.question)) render(strip(d));
});
// Tell the host we are ready, in case it waits before sending.
try { parent.postMessage({ method: "ui/notifications/initialized" }, "*"); } catch (_) {}
const inline = document.getElementById("payload");
if (inline) render(strip(JSON.parse(inline.textContent)));
</script>
""" % (_tokens_css(), json.dumps(list(_VERDICT_FIELDS)))


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
    return template().replace('<div id="root">loading…</div>',
                              '<div id="root">loading…</div>\n'
                              '<script type="application/json" id="payload">'
                              + json.dumps(evidence_payload(data)) + "</script>")
