"""The design language, generated from ONE source of truth (conf/ui_theme.json).

The web app imports generated artifacts (theme.css, theme.ts) and `ui.design`
serves the same tokens — so "looks like the platform" can't drift. Nothing here
hardcodes a colour; edit the JSON and regenerate:

    uv run python -m app.mcp.ui --write-app

The honesty conventions ride WITH the paint: `trust_rules` and `voice` travel in
the token payload, so a vibe-coded UI inherits unverified-by-default even on
screens we never shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import get_settings

_APP_WEB = Path(__file__).resolve().parents[4] / "web" / "src"


def theme_path() -> Path:
    return Path(get_settings().crop_calendar_path).parent / "ui_theme.json"


def tokens() -> dict:
    """The canonical token JSON — what an LLM reasons over."""
    return json.loads(theme_path().read_text())


def css_vars(t: dict | None = None) -> str:
    """`--grp-*` custom properties: the substrate that works in any stack."""
    t = t or tokens()
    lines = [f"  --grp-{k}: {v};" for k, v in t["palette"].items()]
    lines += [f"  --grp-section-{i + 1}: {c};"
              for i, c in enumerate(t["provenance"]["section_edges"])]
    lines += [f"  --grp-parse-edge: {t['provenance']['parse_edge']};",
              f"  --grp-cite-edge: {t['provenance']['cite_edge']};",
              f"  --grp-font-sans: {t['typography']['font_sans']};",
              f"  --grp-font-mono: {t['typography']['font_mono']};"]
    return ":root {\n" + "\n".join(lines) + "\n}"


def daisy_theme(t: dict | None = None) -> str:
    """A daisyUI 5 theme block — the common stack for hub builders."""
    t = t or tokens()
    lines = [f'  --color-{k}: {v};' for k, v in t["palette"].items()]
    lines += [f'  --radius-{k}: {v};' for k, v in t["radii"].items()]
    return ('@plugin "daisyui/theme" {\n'
            f'  name: "{t["id"]}";\n  default: true;\n  color-scheme: light;\n'
            + "\n".join(lines) + "\n}")


def app_css(t: dict | None = None) -> str:
    t = t or tokens()
    return (f"/* GENERATED from conf/ui_theme.json (theme {t['id']} {t['version']}).\n"
            "   Do not hand-edit — run: uv run python -m app.mcp.ui --write-app */\n\n"
            + daisy_theme(t) + "\n\n" + css_vars(t) + "\n")


def app_ts(t: dict | None = None) -> str:
    """Tokens for TS/JS consumers (the provenance graph draws SVG strokes in JS)."""
    t = t or tokens()
    body = {"id": t["id"], "version": t["version"], "palette": t["palette"],
            "provenance": t["provenance"], "validationLevels": t["validation_levels"],
            "trustRules": t["trust_rules"], "voice": t["voice"]}
    return ("// GENERATED from conf/ui_theme.json — do not hand-edit.\n"
            "// Regenerate: uv run python -m app.mcp.ui --write-app\n"
            "export const theme = " + json.dumps(body, indent=2) + " as const;\n")


def _live_design() -> dict | None:
    """Colour tokens as the Figma file defines them RIGHT NOW, or None.

    Config is the safety net by design: `ui_design` must never fail, and a consumer
    mid-build must never get an empty palette because a third party was down. So a
    Figma outage degrades to the committed theme silently in behaviour but LOUDLY in
    the payload — `design_source` always says which one you got.
    """
    try:
        from . import figma
        return figma.read_tokens()
    except Exception:                      # any upstream trouble: fall back, never fail
        return None


def design(fmt: str = "all") -> dict:
    """The design language for a consumer: tokens to reason over, CSS custom
    properties that work in any stack, a daisyUI theme for the common one — and the
    honesty conventions (trust_rules + voice) that ride WITH the paint."""
    t = tokens()
    live = _live_design()
    want = (fmt or "all").lower()
    if want not in ("all", "tokens", "json", "css", "daisyui", "tailwind"):
        return {"status": "declined",
                "note": f"unknown format {fmt!r}",
                "available": ["all", "tokens", "css", "daisyui"]}
    out = {"status": "ok", "theme": {"id": t["id"], "version": t["version"]},
           "brand": t.get("brand"), "product": t.get("product"),
           # the conventions are NOT optional decoration — a UI built on these
           # tokens inherits unverified-by-default even on screens we never shipped
           "trust_rules": t["trust_rules"], "voice": t["voice"],
           "semantic": t["semantic"], "validation_levels": t["validation_levels"]}
    # Which source you are actually looking at. Never inferred, never absent.
    out["design_source"] = ({"source": "figma-live", **live["provenance"],
                             "conflicts": live["conflicts"]} if live else
                            {"source": "config", "file": str(theme_path().name),
                             "note": ("Figma was unreachable or unconfigured, so this is the "
                                      "committed theme. It is a maintained copy, not the "
                                      "live design file.")})
    if want in ("all", "tokens", "json"):
        out["tokens"] = {k: t[k] for k in
                         ("palette", "typography", "radii", "provenance")}
        if live:
            # The named brand colours as the designers currently have them. Kept
            # SEPARATE from `palette`: palette is the semantic role map the UI is
            # built on, and silently overwriting roles from a colour page would
            # change every component the moment a designer renames a swatch.
            out["tokens"]["brand_colors"] = live["colors"]
            out["tokens"]["declared_styles"] = live["declared_styles"]
            # Same separation as colour: `typography` is the config's semantic scale
            # (body/heading roles). This is what the design file actually SETS on
            # text, observed not declared, so it informs rather than replaces.
            out["tokens"]["design_file_type_scale"] = live.get("typography", [])
    if want in ("all", "css"):
        out["css"] = css_vars(t)
    if want in ("all", "daisyui", "tailwind"):
        out["daisyui_theme"] = daisy_theme(t)
    if want == "all":
        out["how_to_use"] = [
            "paste `css` once at the top of your stylesheet; every value is a --grp-* var",
            "on Tailwind/daisyUI, paste `daisyui_theme` and set data-theme on <html>",
            "map a source's validation level through `validation_levels` -> `semantic`",
            "use `voice` verbatim for declines, ADJUSTED labels and claim scope",
            "success/verified styling is RESERVED for server-verified state (trust_rules)",
            "REQUIRED: render ui_component('platform_header') on every page — the SERVIR "
            "logo plus the domain name (display_name from the pack manifest)",
        ]
    return out


def _live_components() -> dict:
    """Component sets from the design file, or an honest note. Never raises: the
    catalogue must still list our own recipes when Figma is unreachable."""
    try:
        from . import figma
        c = figma.read_components()
        return {"source": "figma-live", "file": c["provenance"]["file"],
                "last_modified": c["provenance"]["last_modified"],
                "cache": c.get("cache"),
                "sets": [{"name": s["name"], "node_id": s["node_id"],
                          "variants": [{k: v for k, v in var.items() if k != "label"}
                                       for var in s["variants"]]}
                         for s in c["sets"]],
                "how": ("Ask ui_component(name=<set name>, treatment=..., layout=...) "
                        "for a rendered asset.")}
    except Exception as exc:
        return {"source": "unavailable", "sets": [],
                "note": f"design file not reachable ({exc}); our own recipes are unaffected"}


def _design_file_component(name: str, **want) -> dict | None:
    """One variant of a design-file component set, rendered. None if the name is not
    a set there, so the caller can fall through to its own decline."""
    try:
        from . import figma
        sets = {s["name"]: s for s in figma.read_components()["sets"]}
    except Exception:
        return None
    s = sets.get(name)
    if s is None:
        return None
    wanted = {k: str(v).lower() for k, v in want.items() if v is not None}
    matches = [v for v in s["variants"]
               if all(str(v.get(k, "")).lower() == val for k, val in wanted.items())]
    if not matches:
        return {"status": "declined", "name": name,
                "note": (f"no variant of {name!r} matches {wanted or '{}'}"
                         if wanted else f"{name!r} has no variants"),
                "available_variants": [{k: x for k, x in v.items() if k != "node_id"}
                                       for v in s["variants"]]}
    if len(matches) > 1 and wanted:
        return {"status": "declined", "name": name,
                "note": f"{wanted} matches {len(matches)} variants; narrow it",
                "available_variants": [{k: x for k, x in v.items() if k != "node_id"}
                                       for v in matches]}
    chosen = matches[0]
    img = figma.render([chosen["node_id"]], "svg")
    return {"status": "ok", "name": name, "source": "figma-live",
            "variant": {k: v for k, v in chosen.items() if k != "node_id"},
            "svg_url": img["images"].get(chosen["node_id"]),
            "caveat": img["caveat"],
            "trust_class": "presentational",
            "note": ("Brand asset from the design file. Presentational only: it carries "
                     "no verdict and needs no receipt.")}


def catalog() -> dict:
    """The component inventory — what exists, its trust class, and how it is
    delivered. Derived from the theme config, so it cannot drift."""
    t = tokens()
    comps = dict(t.get("components") or {})
    note = comps.pop("note", None)
    # The design file's own published UI, alongside our recipes. Kept in a SEPARATE
    # key: these are brand assets rendered by Figma, not trust-bound components with
    # a delivery mode, and conflating them would imply a guarantee they do not carry.
    live = _live_components()
    return {"status": "ok", "note": note,
            "components": [{"name": k, **v} for k, v in sorted(comps.items())],
            "design_file_components": live,
            "delivery_modes": {
                "embed": "runs as OUR component bound to a server id — trust state "
                         "resolves from the platform, not from a prop",
                "recipe": "self-contained markup you copy into your app and own "
                          "(versioned; no updates after copying)"}}


def component(name: str, **variants) -> dict:
    """A ready-built component: real markup you drop in, not a description to
    reimplement. Recipes live in conf/ui_recipes/<name>.html — adding one is a file
    plus a catalog entry, never a code change."""
    t = tokens()
    comps = {k: v for k, v in (t.get("components") or {}).items() if k != "note"}
    spec = comps.get(name)
    if spec is None:
        # Not one of ours — it may be a set in the design file. The platform REQUIRES
        # every UI to carry the SERVIR identity, so a builder asking for the logo must
        # get the real asset with the right treatment, not a config URL.
        live = _design_file_component(name, **variants)
        if live is not None:
            return live
        return {"status": "declined", "note": f"unknown component {name!r}",
                "available": sorted(comps),
                "design_file": [s["name"] for s in
                                _live_components().get("sets", [])]}
    path = theme_path().parent / "ui_recipes" / f"{name}.html"
    if not path.exists():
        return {"status": "declined", "name": name,
                "note": (f"`{name}` is catalogued (trust_class {spec.get('trust_class')}, "
                         f"delivery {spec.get('delivery')}) but no recipe has been "
                         "authored yet — not implemented"),
                "available": sorted(p.stem for p in path.parent.glob("*.html"))}
    markup = path.read_text()
    # brand values are substituted here so the markup is paste-ready and the theme
    # stays the single source of truth (no logo copy living in a recipe file)
    for slot, val in (("{{logo_data_uri}}", t["brand"]["logo"]["data_uri"]),
                      ("{{logo_alt}}", t["brand"]["logo"]["alt"]),
                      ("{{platform_name}}", t["product"]["platform_name"]),
                      # absolute: a relative path resolves against the CONSUMER's
                      # origin, 404s there, and fails closed to "unverified" silently
                      # ...and its {receipt_id} is normalised to the {{slot}} style the
                      # rest of the recipe uses, so one fill pass leaves nothing behind
                      ("{{resolver_url}}",
                       (t["product"]["resolver"]["base"]
                        + t["product"]["resolver"]["receipt"]).replace("{receipt_id}",
                                                                       "{{receipt_id}}"))):
        markup = markup.replace(slot, val)
    out = {"status": "ok", "name": name, "version": f"{t['id']}-{t['version']}",
           "trust_class": spec.get("trust_class"), "markup": markup,
           "styling": ("uses --grp-* custom properties — paste ui_design's `css` once "
                       "and this renders in your palette, in any framework"),
           "resolver": t["product"]["resolver"],
           "notes": spec}
    if spec.get("trust_class") == "receipt_bound":
        out["guardrail"] = (
            "RECEIPT-BOUND: render the unverified state unless you have a payload "
            "resolved FROM the platform (verify_groundedness / record_receipt). Never "
            "hardcode a passed verdict, and never suppress a decline or declared gap. "
            "A verdict with no resolvable receipt id is not ours. NOTE (honest limit): "
            "a copied component cannot cryptographically prove this — the authoritative "
            "check is resolving the receipt against the server; signed receipts are a "
            "later step.")
    return out


def describe_component() -> str:
    t = tokens()
    have = sorted(p.stem for p in (theme_path().parent / "ui_recipes").glob("*.html"))
    return ("Get the READY-BUILT markup for a component instead of writing UI from "
            "scratch. Returns self-contained HTML+CSS styled with the platform's "
            "--grp-* custom properties, plus its data contract and guardrail.\n\n"
            "Authored now: " + (", ".join(f"`{h}`" for h in have) or "(none)") +
            ".\nSee ui_catalog for every catalogued component and its trust class; one "
            "not yet authored declines as not-implemented rather than pretending.\n\n"
            "receipt_bound components must render UNVERIFIED unless fed a payload "
            "resolved from the platform — there is no 'passed' input.")


def embed(component: str, receipt_id: str | None = None) -> dict:
    """A LIVE embed: a reference to our component, not a copy of it. It resolves its
    own state from the platform at view time, so a verdict can never be frozen into
    the host page — and there is no prop that could assert one."""
    t = tokens()
    comps = {k: v for k, v in (t.get("components") or {}).items() if k != "note"}
    spec = comps.get(component)
    if spec is None:
        return {"status": "declined", "note": f"unknown component {component!r}",
                "available": sorted(k for k, v in comps.items() if v.get("embeddable"))}
    if not spec.get("embeddable"):
        return {"status": "declined", "component": component,
                "note": (f"`{component}` is not embeddable yet"
                         + (f" — {spec['status']}" if spec.get("status") else "")),
                "available": sorted(k for k, v in comps.items() if v.get("embeddable"))}
    base = t["product"]["embed_base"]
    src = base["url"] + base["path"].format(component=component,
                                            receipt_id=receipt_id or "{receipt_id}")
    return {"status": "ok", "component": component, "binds": spec.get("binds"),
            "renders": spec.get("renders"), "src": src,
            "html": (f'<iframe src="{src}" title="{component}" '
                     'style="width:100%;height:520px;border:1px solid var(--grp-base-300);'
                     'border-radius:var(--grp-radius-box,.5rem)" loading="lazy"></iframe>'),
            "guarantee": ("the embed resolves from /api/resolve/* at VIEW time — no verdict "
                          "prop exists, and with no reachable platform it renders unverified "
                          "rather than anything else"),
            "note": base["note"]}


def describe_embed() -> str:
    t = tokens()
    comps = {k: v for k, v in (t.get("components") or {}).items() if k != "note"}
    live = [f"  - `{k}` (binds {v.get('binds')})" for k, v in sorted(comps.items())
            if v.get("embeddable")]
    pending = [f"  - `{k}` — {v.get('status', 'not embeddable yet')}"
               for k, v in sorted(comps.items())
               if v.get("delivery") == "embed" and not v.get("embeddable")]
    return ("Embed a LIVE platform component in your page — our component, running our "
            "code, resolving its own state. Use this instead of a copied recipe whenever "
            "a verdict is shown: an embed cannot be handed a 'passed' flag and cannot "
            "freeze one into your markup.\n\nEmbeddable now:\n" + ("\n".join(live) or "  (none)")
            + ("\n\nPlanned:\n" + "\n".join(pending) if pending else "")
            + "\n\nReturns an iframe snippet bound to your id. Copy the recipe instead "
              "(ui_component) only for inert chrome like source cards.")


def describe_design() -> str:
    t = tokens()
    return (f"Get the platform design language (theme `{t['id']}` {t['version']}, built on "
            f"the {t['brand']['name']} identity) so you do NOT invent a colour scheme.\n\n"
            "Returns tokens (palette/typography/radii/provenance colours), `css` "
            "(--grp-* custom properties for any stack), `daisyui_theme` (Tailwind/daisyUI), "
            "plus `trust_rules`, `semantic`, `validation_levels` and `voice` — the honesty "
            "conventions that must travel with the styling.\n\n"
            "`format`: all (default) | tokens | css | daisyui.\n"
            f"RULE: {t['trust_rules']['success_reserved_for']} — success/verified styling is "
            "reserved for server-verified state; default is "
            f"'{t['trust_rules']['default_state']}'.\n"
            "REQUIRED: every page shows the platform header (logo + domain name) — get it "
            "from ui_component('platform_header'); the logo ships embedded in the markup.")


def describe_catalog() -> str:
    t = tokens()
    comps = {k: v for k, v in (t.get("components") or {}).items() if k != "note"}
    lines = [f"  - `{k}` ({v.get('trust_class')}, delivery: {v.get('delivery')})"
             for k, v in sorted(comps.items())]
    return ("List the ready-built UI components you can use instead of writing them "
            "from scratch. Each entry states its TRUST CLASS and how it is delivered.\n\n"
            + "\n".join(lines) +
            "\n\nreceipt_bound = the component shows a verdict, so it binds to a server "
            "id and cannot be handed a 'passed' flag. input = it emits a human judgement "
            "but never renders its own verdict. presentational = style freely.")


def write_app_artifacts() -> list[str]:
    t = tokens()
    written = []
    for name, text in (("theme.css", app_css(t)), ("theme.ts", app_ts(t))):
        p = _APP_WEB / name
        p.write_text(text)
        written.append(str(p))
    return written


if __name__ == "__main__":
    import sys
    if "--write-app" in sys.argv:
        for p in write_app_artifacts():
            print("wrote", p)
    else:
        print(json.dumps(tokens(), indent=2))
