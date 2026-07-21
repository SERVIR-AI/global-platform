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


def design(fmt: str = "all") -> dict:
    """The design language for a consumer: tokens to reason over, CSS custom
    properties that work in any stack, a daisyUI theme for the common one — and the
    honesty conventions (trust_rules + voice) that ride WITH the paint."""
    t = tokens()
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
    if want in ("all", "tokens", "json"):
        out["tokens"] = {k: t[k] for k in
                         ("palette", "typography", "radii", "provenance")}
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


def catalog() -> dict:
    """The component inventory — what exists, its trust class, and how it is
    delivered. Derived from the theme config, so it cannot drift."""
    t = tokens()
    comps = dict(t.get("components") or {})
    note = comps.pop("note", None)
    return {"status": "ok", "note": note,
            "components": [{"name": k, **v} for k, v in sorted(comps.items())],
            "delivery_modes": {
                "embed": "runs as OUR component bound to a server id — trust state "
                         "resolves from the platform, not from a prop",
                "recipe": "self-contained markup you copy into your app and own "
                          "(versioned; no updates after copying)"}}


def component(name: str) -> dict:
    """A ready-built component: real markup you drop in, not a description to
    reimplement. Recipes live in conf/ui_recipes/<name>.html — adding one is a file
    plus a catalog entry, never a code change."""
    t = tokens()
    comps = {k: v for k, v in (t.get("components") or {}).items() if k != "note"}
    spec = comps.get(name)
    if spec is None:
        return {"status": "declined", "note": f"unknown component {name!r}",
                "available": sorted(comps)}
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
                      ("{{platform_name}}", t["product"]["platform_name"])):
        markup = markup.replace(slot, val)
    out = {"status": "ok", "name": name, "version": f"{t['id']}-{t['version']}",
           "trust_class": spec.get("trust_class"), "markup": markup,
           "styling": ("uses --grp-* custom properties — paste ui_design's `css` once "
                       "and this renders in your palette, in any framework"),
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
