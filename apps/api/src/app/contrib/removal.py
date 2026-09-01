"""Removal — the other half of every contribution path.

Removal stops FUTURE use; it never rewrites history. Packs and receipts minted
while a source was live are stored snapshots and stay replayable; document raw
archives are kept for exactly that reason. Every removal says what it did and
what it deliberately left."""

from __future__ import annotations

from pathlib import Path

import yaml

_HISTORY = ("existing packs and receipts are stored snapshots and remain "
            "replayable — removal stops future use, it does not rewrite history")


def remove_doc(pack_id: str, doc_id: str) -> dict:
    from ..mcp import packs
    from ..rag.store import Corpus
    if pack_id not in packs.PACKS:
        return {"status": "declined",
                "failures": [f"unknown pack {pack_id!r} — available: "
                             + ", ".join(packs.available())]}
    name = packs.PACKS[pack_id].get("corpus")
    if not name:
        return {"status": "declined",
                "failures": [f"pack {pack_id!r} has no corpus"]}
    out = Corpus(name).remove(doc_id)
    if not out["found"]:
        return {"status": "declined",
                "failures": [f"no document {doc_id!r} in the {name} corpus"]}
    return {"status": "removed", **out, "note": _HISTORY}


def remove_feed(dataset: str) -> dict:
    """Remove a DECLARATIVE feed row (and, for landed tables, the archived copy
    moves aside rather than vanishing). Code-row feeds are code changes."""
    from ..config import get_settings
    yml = Path(get_settings().feeds_conf_dir) / f"{dataset}.yml"
    if not yml.is_file():
        return {"status": "declined",
                "failures": [f"no declarative row {yml.name} — built-in feeds are "
                             "code and are removed by a code change"]}
    spec = yaml.safe_load(yml.read_text()) or {}
    removed = {"feed_row": str(yml)}
    path = (spec.get("fetch") or {}).get("path")
    if path and Path(path).is_file():                  # a landed table's copy
        retired = Path(path).with_suffix(".retired")
        Path(path).rename(retired)
        removed["archived_copy"] = f"moved aside to {retired.name} (kept for replay)"
    yml.unlink()
    return {"status": "removed", **removed,
            "note": _HISTORY + "; restart the server to stop serving it"}


def remove_raster(layer: str) -> dict:
    """Remove a CONTRIBUTED raster: catalog row + declared contract. The file
    moves aside, kept for replay of receipts minted against it. Built-in layers
    (not marked contributed) are refused."""
    from ..config import get_settings
    settings = get_settings()
    cat_path = Path(settings.tiffs_contrib_path)
    cat = (yaml.safe_load(cat_path.read_text()) or {}) if cat_path.exists() else {}
    if layer not in cat:
        from ..graph.geo import tiffs
        if layer in tiffs.catalog():
            return {"status": "declined",
                    "failures": [f"{layer!r} is a built-in layer, not a contribution "
                                 "— removing it is a code/catalog decision, not a "
                                 "CLI one"]}
        return {"status": "declined", "failures": [f"no catalog row {layer!r}"]}
    del cat[layer]
    header = ("# Contributed raster rows — machine-owned; written by the\n"
              "# contribution gate. Hand-edit conf/tiffs.yml, never this.\n")
    cat_path.write_text(header + yaml.safe_dump(cat, sort_keys=False, allow_unicode=True))
    sch_path = Path(settings.raster_schema_contrib_path)
    sch = (yaml.safe_load(sch_path.read_text()) or {}) if sch_path.exists() else {}
    (sch.get("layers") or {}).pop(layer, None)
    sch_path.write_text("# Contributed raster contracts — machine-owned.\n"
                        + yaml.safe_dump(sch, sort_keys=False, allow_unicode=True))
    out = {"catalog_row": "removed", "declared_contract": "removed"}
    tif = Path(settings.tiffs_dir) / f"{layer}.tif"
    if tif.is_file():
        retired = tif.with_suffix(".tif.retired")
        tif.rename(retired)
        out["file"] = f"moved aside to {retired.name} (kept for replay)"
    return {"status": "removed", "layer": layer, **out, "note": _HISTORY}


def remove_pack(pack_id: str) -> dict:
    """Point at the file — a contributed pack IS its module; built-ins refuse."""
    from ..mcp import packs
    ext = Path(__file__).resolve().parents[1] / "packs_ext"
    mod = ext / f"{pack_id.replace('-', '_')}.py"
    if mod.is_file():
        mod.unlink()
        return {"status": "removed", "module": str(mod),
                "note": _HISTORY + "; restart the server to deregister it"}
    if pack_id in packs.PACKS:
        return {"status": "declined",
                "failures": [f"{pack_id!r} is a built-in pack (no packs_ext module) "
                             "— removing it is a code change"]}
    return {"status": "declined", "failures": [f"no contributed pack {pack_id!r}"]}
