"""Durable id->object persistence for evidence packs (and, later, receipts).
Atomic writes under cache/mcp; ids are 16 hex, validated on load (no traversal).
This is the state that makes rule 3 (replayable) real.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..config import get_settings

_ID = re.compile(r"[0-9a-f]{16}")


def _dir(kind: str) -> Path:
    d = Path(get_settings().cache_dir) / "mcp" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(kind: str, obj: dict, id_field: str) -> str:
    body = json.dumps(obj, sort_keys=True, default=str).encode()
    ident = hashlib.sha256(body + os.urandom(8)).hexdigest()[:16]
    path = _dir(kind) / f"{ident}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**obj, id_field: ident}, default=str))
    os.replace(tmp, path)
    return ident


def _load(kind: str, ident: str) -> dict | None:
    if not ident or not _ID.fullmatch(ident):
        return None
    path = _dir(kind) / f"{ident}.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_pack(pack: dict) -> str:
    return _save("packs", pack, "pack_id")


def load_pack(pack_id: str) -> dict | None:
    return _load("packs", pack_id)
