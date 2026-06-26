"""Verification pass: open a raster and confirm it matches its declared schema
(schema.py) before any op uses it — the gate that catches the units/scale traps the
source sheet hides. Stats are windowed (rasterstats) so a continent-scale file is
never fully loaded.

CLI:  python -m app.graph.geo.verify [name ...]
      (no args -> verify every layer declared in conf/raster_schema.yml)
Exits non-zero if any file fails.
"""
import sys
from dataclasses import dataclass, field

from . import ingest
from . import schema as schema_mod
from .rasterstats import windowed_stats


@dataclass
class VerifyReport:
    name: str
    ok: bool
    declared: dict
    observed: dict
    mismatches: list = field(default_factory=list)
    warnings: list = field(default_factory=list)   # soft signals that don't fail the gate

    def __str__(self):
        o = self.observed
        head = f"[{'PASS' if self.ok else 'FAIL'}] {self.name}"
        line = (f"      dtype={o.get('dtype')} sampled=[{o.get('sampled_min')},{o.get('sampled_max')}] "
                f"nodata={o.get('nodata')} crs=EPSG:{o.get('crs_epsg')} "
                f"px={o.get('pixel_size_deg')} size={o.get('width')}x{o.get('height')}")
        body = "".join("\n      x " + m for m in self.mismatches)
        body += "".join("\n      ! " + w for w in self.warnings)
        return head + "\n" + line + body


def _declared_epsg(decl):
    crs = decl.get("crs")
    if not crs:
        return None
    try:
        return int(str(crs).split(":")[1])
    except (IndexError, ValueError):
        return None


def _check(decl, obs):
    """List of human-readable mismatches between a declared schema and observed stats."""
    tol = float(decl.get("float_tol", 0.0001))
    out = []

    if "dtype" in decl and obs["dtype"] != decl["dtype"]:
        out.append(f"dtype {obs['dtype']} != declared {decl['dtype']}")

    vmax, smax = decl.get("valid_max"), obs["sampled_max"]
    if vmax is not None and smax is not None and smax > vmax + tol:
        out.append(f"max {smax} > declared valid_max {vmax} (declared units={decl.get('units')})")
    vmin, smin = decl.get("valid_min"), obs["sampled_min"]
    if vmin is not None and smin is not None and smin < vmin - tol:
        out.append(f"min {smin} < declared valid_min {vmin}")

    if "nodata" in decl:
        dn, on = decl["nodata"], obs["nodata"]
        if (dn is None) != (on is None) or (dn is not None and on is not None and abs(dn - on) > tol):
            out.append(f"nodata {on} != declared {dn}")

    depsg, oepsg = _declared_epsg(decl), obs["crs_epsg"]
    if depsg is not None and oepsg is not None and depsg != oepsg:
        out.append(f"crs EPSG:{oepsg} != declared EPSG:{depsg}")

    if decl.get("pixel_size_deg") is not None and obs["pixel_size_deg"] is not None:
        if abs(obs["pixel_size_deg"] - float(decl["pixel_size_deg"])) > 1e-6:
            out.append(f"pixel_size {obs['pixel_size_deg']} != declared {decl['pixel_size_deg']}")

    return out


def verify_raster(name, schema=None):
    """Download (if needed) and verify `name` against its declared schema (or an
    explicit `schema` override). Returns a VerifyReport; never raises on a mismatch."""
    decl = schema if schema is not None else schema_mod.schema_for(name)
    path = ingest.source_raster(name)
    obs = windowed_stats(path)
    if decl is None:
        return VerifyReport(name, False, {}, obs, ["no declared schema in raster_schema.yml"])
    mismatches = _check(decl, obs)
    return VerifyReport(name, not mismatches, decl, obs, mismatches)


def _main(argv):
    names = argv or schema_mod.names()
    all_ok = True
    for name in names:
        report = verify_raster(name)
        print(report)
        all_ok = all_ok and report.ok
    print(f"\n{'ALL PASS' if all_ok else 'FAILURES PRESENT'} ({len(names)} layer(s))")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
