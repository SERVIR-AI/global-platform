"""Durable id->object persistence — one SQLite file shipped with the server
(cache/mcp/grp.db). Holds runtime RECORD-state only: packs, groundedness reports,
receipts. Human-facing config/registries stay diff-able files, not in here.

This is the state that makes rule 3 (replayable) real. It is NOT the rule-7
retention institution: immutability (append-only) and an open self-hostable
resolver are separate disciplines layered on top later.

Ids are 16 hex, validated on load. A fresh connection per call (WAL mode) keeps
concurrent HTTP requests safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings

_ID = re.compile(r"[0-9a-f]{16}")
_TABLES = ("packs", "reports", "receipts")


def _db_path() -> Path:
    d = Path(get_settings().cache_dir) / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d / "grp.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    for t in _TABLES:
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} "
                    "(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL)")
    return con


def _save(table: str, obj: dict, id_field: str) -> str:
    assert table in _TABLES
    body = json.dumps(obj, sort_keys=True, default=str)
    ident = hashlib.sha256(body.encode() + os.urandom(8)).hexdigest()[:16]
    stored = json.dumps({**obj, id_field: ident}, default=str)
    con = _connect()
    try:
        with con:
            con.execute(f"INSERT INTO {table}(id, created_at, body) VALUES (?, ?, ?)",
                        (ident, datetime.now(timezone.utc).isoformat(), stored))
    finally:
        con.close()
    return ident


def _load(table: str, ident: str) -> dict | None:
    assert table in _TABLES
    if not ident or not _ID.fullmatch(ident):
        return None
    con = _connect()
    try:
        row = con.execute(f"SELECT body FROM {table} WHERE id = ?", (ident,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def save_pack(pack: dict) -> str:
    return _save("packs", pack, "pack_id")


def load_pack(pack_id: str) -> dict | None:
    return _load("packs", pack_id)


def save_report(report: dict) -> str:
    return _save("reports", report, "report_id")


def load_report(report_id: str) -> dict | None:
    return _load("reports", report_id)


def latest_receipt_id() -> str | None:
    """Most recent receipt — the pack manifest points at it as a REAL worked example
    (so a builder resolves a genuine receipt instead of faking a payload)."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT id FROM receipts ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def save_receipt(receipt: dict) -> str:
    return _save("receipts", receipt, "receipt_id")


def load_receipt(receipt_id: str) -> dict | None:
    return _load("receipts", receipt_id)
