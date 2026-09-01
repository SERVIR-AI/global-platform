"""CLI for contributions.

  uv run python -m app.contrib.cli add <manifest.yml> [--dry-run]         # documents
  uv run python -m app.contrib.cli add-raster <manifest.yml> [--dry-run]  # raster layer

Prints results and exits non-zero if anything declined, so the command is
honest in scripts too."""

from __future__ import annotations

import json
import sys

from . import sources


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "add-raster":
        import yaml
        from . import rasters
        m = yaml.safe_load(open(argv[1], encoding="utf-8"))
        out = rasters.add(m, dry_run="--dry-run" in argv)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["status"] != "declined" else 1
    if not argv or argv[0] != "add" or len(argv) < 2:
        print(__doc__)
        return 2
    entries = sources.load_manifest(argv[1])
    out = sources.contribute(entries, dry_run="--dry-run" in argv)
    for r in out["results"]:
        line = f"[{r['status']:>9}] {r['entry']}"
        if r.get("doc_id"):
            line += f"  doc_id={r['doc_id']} chunks={r['chunks']}" + (
                "  (already in corpus; provenance refreshed)" if r["already_ingested"] else "")
        print(line)
        for f in r.get("failures", []):
            print(f"            - {f}")
    print(json.dumps({k: out[k] for k in ("ingested", "declined", "dry_run")}))
    return 0 if out["declined"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
