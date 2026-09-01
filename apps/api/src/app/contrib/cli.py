"""CLI for contributions.

  uv run python -m app.contrib.cli add <manifest.yml> [--dry-run]         # documents
  uv run python -m app.contrib.cli add-raster <manifest.yml> [--dry-run]  # raster layer
  uv run python -m app.contrib.cli add-table <manifest.yml> [--dry-run]   # CSV table
  uv run python -m app.contrib.cli new-pack <id> [key1,key2]               # scaffold a pack
  uv run python -m app.contrib.cli doctor <pack-id>                        # pack integrity

Prints results and exits non-zero if anything declined, so the command is
honest in scripts too."""

from __future__ import annotations

import json
import sys

from . import sources


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "new-pack":
        from . import packdev
        keys = tuple(argv[2].split(",")) if len(argv) > 2 else ("place",)
        out = packdev.new_pack(argv[1], target_keys=keys)
        print(json.dumps(out, indent=2))
        return 0 if out["status"] != "declined" else 1
    if len(argv) >= 2 and argv[0] == "doctor":
        from . import packdev
        out = packdev.doctor(argv[1])
        for c in out["checks"]:
            print(f"[{'ok' if c['ok'] else 'FAIL':>4}] {c['check']}"
                  + (f" — {c['detail']}" if c.get("detail") else ""))
        print(f"doctor: {out['status'].upper()} ({len(out['failures'])} failure(s))")
        return 0 if out["status"] == "passed" else 1
    if len(argv) >= 2 and argv[0] == "add-table":
        import yaml
        from . import tables
        m = yaml.safe_load(open(argv[1], encoding="utf-8"))
        out = tables.add(m, dry_run="--dry-run" in argv)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["status"] != "declined" else 1
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
