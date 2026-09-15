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
    # Contributions are the one MUTABLE row type (pending -> approved/rejected/
    # withdrawn), so they get their own shape with the columns a review queue
    # filters on; the full record still rides `body` like everything else.
    con.execute("CREATE TABLE IF NOT EXISTS contributions "
                "(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "status TEXT NOT NULL, kind TEXT NOT NULL, contributor_id TEXT NOT NULL, "
                "body TEXT NOT NULL)")
    return con


def init() -> None:
    """Create the file + schema before serving, so a replicator has a WAL to
    follow from boot rather than from the first receipt."""
    _connect().close()


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
    if not row:
        return None
    body = json.loads(row[0])
    # A PREVIEW object (a pack citing a staged contribution, and the reports and
    # receipts minted from it) resolves only for its contributor and reviewers.
    # To anyone else it does not exist — the same answer as an unknown id, so a
    # guessed id reveals nothing (contrib/identity.py, the visibility rule).
    if body.get("staged_by"):
        from ..contrib import identity
        if not identity.current().may_see(body["staged_by"]):
            return None
    return body


def _inherit_preview(obj: dict) -> dict:
    """Reports and receipts carry their pack's preview tag, whoever minted them."""
    if obj.get("staged_by") or not obj.get("pack_id"):
        return obj
    pack = load_pack(obj["pack_id"])
    if pack and pack.get("staged_by"):
        return {**obj, "staged_by": pack["staged_by"], "staged_note": pack.get("staged_note")}
    return obj


def save_pack(pack: dict) -> str:
    # Safety net for every assembly path: a pack citing a staged contribution
    # is a preview owned by whoever assembled it (assemble.py sets this too).
    if not pack.get("staged_by") and any(
            isinstance(c, dict) and c.get("staged_by") for c in pack.get("citations") or []):
        from ..contrib import identity
        pack = {**pack, "staged_by": identity.current().id,
                "staged_note": ("PREVIEW — this pack cites a staged contribution awaiting "
                                "review; visible only to its contributor and to reviewers")}
    return _save("packs", pack, "pack_id")


def load_pack(pack_id: str) -> dict | None:
    return _load("packs", pack_id)


def save_report(report: dict) -> str:
    return _save("reports", _inherit_preview(report), "report_id")


def load_report(report_id: str) -> dict | None:
    return _load("reports", report_id)


def latest_receipt_id() -> str | None:
    """Most recent receipt — the pack manifest points at it as a REAL worked example
    (so a builder resolves a genuine receipt instead of faking a payload)."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, body FROM receipts ORDER BY created_at DESC LIMIT 50").fetchall()
    finally:
        con.close()
    for ident, body in rows:                    # never advertise someone's preview
        if not json.loads(body).get("staged_by"):
            return ident
    return None


def save_receipt(receipt: dict) -> str:
    return _save("receipts", _inherit_preview(receipt), "receipt_id")


def load_receipt(receipt_id: str) -> dict | None:
    return _load("receipts", receipt_id)


# --- contributions (contrib/staging.py) ---------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_contribution(obj: dict) -> str:
    """Insert a new contribution record; returns its id (also written into body)."""
    body = json.dumps(obj, sort_keys=True, default=str)
    ident = hashlib.sha256(body.encode() + os.urandom(8)).hexdigest()[:16]
    now = _now()
    stored = {**obj, "contribution_id": ident, "created_at": now, "updated_at": now}
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO contributions(id, created_at, updated_at, status, kind, "
                "contributor_id, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ident, now, now, obj["status"], obj["kind"], obj["contributor_id"],
                 json.dumps(stored, default=str)))
    finally:
        con.close()
    return ident


def update_contribution(ident: str, changes: dict) -> dict | None:
    """Merge `changes` into the record (status/kind/contributor_id columns follow)."""
    cur = load_contribution(ident)
    if cur is None:
        return None
    merged = {**cur, **changes, "updated_at": _now()}
    con = _connect()
    try:
        with con:
            con.execute(
                "UPDATE contributions SET updated_at = ?, status = ?, kind = ?, "
                "contributor_id = ?, body = ? WHERE id = ?",
                (merged["updated_at"], merged["status"], merged["kind"],
                 merged["contributor_id"], json.dumps(merged, default=str), ident))
    finally:
        con.close()
    return merged


def load_contribution(ident: str) -> dict | None:
    if not ident or not _ID.fullmatch(ident):
        return None
    con = _connect()
    try:
        row = con.execute("SELECT body FROM contributions WHERE id = ?", (ident,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def list_contributions(status: str | None = None, contributor_id: str | None = None,
                       kind: str | None = None, since: str | None = None) -> list[dict]:
    """Newest first. Filters are AND-ed; None means any."""
    where, args = [], []
    for col, val in (("status", status), ("contributor_id", contributor_id), ("kind", kind)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if since:
        where.append("created_at >= ?")
        args.append(since)
    sql = "SELECT body FROM contributions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    con = _connect()
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [json.loads(r[0]) for r in rows]
