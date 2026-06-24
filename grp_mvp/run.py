"""CLI.

  python -m grp_mvp.run "how many km of road are flooded in Battambang?"
  python -m grp_mvp.run --check "Battambang"     # validate the overlay against an independent method
"""
import argparse
import json
import sys

from . import agent, llm, narrate, store
from .ingest import ensure_aoi, hazard_clip


def _ask_user(question):
    print(f"\n{question}")
    try:
        return input("> ").strip() or "all"
    except EOFError:          # non-interactive stdin -> default to the full breakdown
        return "all"


def ask(question):
    if not llm.available():
        sys.exit("configure an LLM in config.py (GRP_MODEL / GRP_BASE_URL / key) — "
                 "or use --check, which needs no LLM")
    r = agent.ask(question, _ask_user)
    narrate.end()
    print(f"\nQ: {r['question']}\nA: {r['answer']}")
    if r["tool_result"]:
        tr = r["tool_result"]
        print(f"   {tr['method']}({r['tool_call']}) -> {tr.get('count', tr.get('length_km'))}")
    print(f"   ${r['cost_usd']:.4f}  grounded={r['grounded']}")


def check(place):
    """Cross-check roads_in_flood (raster sampling) against a vector clip of the flood."""
    import rasterio
    from rasterio import features
    from shapely.geometry import LineString, shape
    from shapely.ops import unary_union

    aoi = ensure_aoi(place)
    print(f"{aoi['name']}  ~{aoi['area_km2']} km²  {aoi['counts']}")
    tool = store.roads_in_hazard(place, "flood")

    src = rasterio.open(hazard_clip(place, "flood"))
    boundary = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
    mask = (src.read(1) >= 1).astype("uint8")
    flood = unary_union([shape(g) for g, _ in features.shapes(mask, mask=mask == 1, transform=src.transform)])
    flood = flood.intersection(boundary)

    def length_km(geom):
        return sum(store.haversine_km(c[i], c[i + 1])
                   for p in getattr(geom, "geoms", [geom]) if p.geom_type == "LineString"
                   for c in [list(p.coords)] for i in range(len(c) - 1))

    oracle = sum(length_km(LineString(f["geometry"]["coordinates"]).intersection(flood))
                 for f in json.load(open(aoi["roads"]))["features"]
                 if len(f["geometry"]["coordinates"]) >= 2)
    diff = abs(tool["length_km"] - oracle) / max(oracle, 1)
    print(f"  tool   {tool['length_km']} km flooded")
    print(f"  oracle {round(oracle, 1)} km flooded (independent vector clip)")
    print(f"  {'OK' if diff < 0.10 else 'MISMATCH'} — {diff * 100:.1f}% apart")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--check", metavar="PLACE")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="narrate the pipeline in plain text (for demos)")
    args = ap.parse_args()
    if args.verbose:
        narrate.enable()
    if args.check:
        check(args.check)
    elif args.question:
        ask(" ".join(args.question))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
