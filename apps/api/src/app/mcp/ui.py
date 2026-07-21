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
