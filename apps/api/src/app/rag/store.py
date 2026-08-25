"""E2/E3 — corpus store: chunk -> embed -> persist -> retrieve, with declines.

One corpus = cache_dir/rag/<name>: chunks.jsonl + embeddings.npy (row-aligned,
unit-normalized) + meta.json (which embedding model built the vectors — a
mismatch refuses to serve rather than score across spaces). Below the relevance
floor, search returns [] and the caller declines. Locks are in-process only
(single-uvicorn deployment).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import numpy as np

from ..config import get_settings
from .docloader import EmptyDocument


class CorpusError(Exception):
    """The corpus on disk is unusable — the message names the fix."""


_LOCKS: dict[str, threading.RLock] = {}


def _corpus_lock(name: str) -> threading.RLock:
    # dict.setdefault is atomic under the GIL; a losing thread's fresh RLock is
    # discarded harmlessly. (Revisit only under free-threaded Python.)
    return _LOCKS.setdefault(name, threading.RLock())


def _chunk(text: str, target: int = 1800) -> list[str]:
    """Paragraph-packing (~450 tokens at ~4 chars each); oversized paragraphs
    split at sentence boundaries."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        while len(p) > target:
            cut = p.rfind(". ", 0, target)
            cut = cut + 1 if cut > target // 2 else target
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > target:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def _matches(meta: dict, filters: dict) -> bool:
    """Case-insensitive: scalars by equality, list fields by intersection;
    empty filter values are ignored."""
    for key, want in filters.items():
        if want in (None, "", []):
            continue
        have = meta.get(key)
        wanted = [str(w).lower() for w in (want if isinstance(want, (list, tuple)) else [want])]
        if isinstance(have, (list, tuple)):
            if not set(wanted) & {str(h).lower() for h in have}:
                return False
        elif str(have).lower() not in wanted:
            return False
    return True


class Corpus:
    """A named, bounded document library."""

    _FILES = ("chunks.jsonl", "embeddings.npy", "meta.json")

    def __init__(self, name: str, embedder=None):
        from .embed import ProviderEmbedder  # keeps store importable without llm deps
        self.name = name
        self.embedder = embedder or ProviderEmbedder()
        self.dir = Path(get_settings().cache_dir) / "rag" / name
        self._lock = _corpus_lock(name)
        self._chunks: list[dict] = []
        self._matrix: np.ndarray | None = None
        self._meta: dict = {}
        with self._lock:
            self._load()

    def _fix(self) -> str:
        return f"delete cache_dir/rag/{self.name} and re-ingest"

    def _embedder_id(self) -> dict:
        return {"provider": getattr(self.embedder, "provider", "custom"),
                "model": getattr(self.embedder, "model", "custom")}

    def _load(self) -> None:
        self._chunks, self._matrix, self._meta = [], None, {}
        present = [f for f in self._FILES if (self.dir / f).exists()]
        if not present:
            return
        if len(present) < len(self._FILES):  # half-present is corruption, not emptiness
            missing = sorted(set(self._FILES) - set(present))
            raise CorpusError(f"corpus {self.name!r} is torn ({', '.join(missing)} "
                              f"missing but {', '.join(present)} present) — {self._fix()}")
        try:
            self._chunks = [json.loads(l) for l in
                            (self.dir / "chunks.jsonl").read_text().splitlines() if l.strip()]
            self._matrix = np.load(self.dir / "embeddings.npy")
            self._meta = json.loads((self.dir / "meta.json").read_text())
        except (ValueError, OSError, EOFError) as exc:
            raise CorpusError(
                f"corpus {self.name!r} is unreadable ({type(exc).__name__}: {exc}) "
                f"— {self._fix()}") from exc
        if len(self._chunks) != len(self._matrix):
            raise CorpusError(
                f"corpus {self.name!r} files disagree ({len(self._chunks)} chunks vs "
                f"{len(self._matrix)} vectors) — {self._fix()}")
        active = self._embedder_id()
        if self._chunks and self._meta.get("model") != active["model"]:
            raise CorpusError(
                f"corpus {self.name!r} was embedded with "
                f"{self._meta.get('provider')}:{self._meta.get('model')} but the active "
                f"embedder is {active['provider']}:{active['model']} — scores would be "
                f"nonsense; switch the embedding settings back, or {self._fix()}")

    def _save(self) -> None:
        """Atomic per file (tmp + os.replace), under the corpus lock. np.save gets
        a file object — given a path it appends '.npy' to our '.tmp' name."""
        self.dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
        writers = {
            "chunks.jsonl": lambda f: f.write(
                "".join(json.dumps(c) + "\n" for c in self._chunks).encode()),
            "embeddings.npy": lambda f: np.save(f, self._matrix),
            "meta.json": lambda f: f.write(json.dumps(
                {**self._embedder_id(), "dim": int(self._matrix.shape[1])}).encode()),
        }
        for fname, write in writers.items():
            tmp = self.dir / (fname + suffix)
            with open(tmp, "wb") as f:
                write(f)
            os.replace(tmp, self.dir / fname)

    def raw_path(self, doc_id: str) -> Path | None:
        """The archived original document, if one was stored at ingest."""
        hits = list((self.dir / "raw").glob(f"{doc_id}.*")) if (self.dir / "raw").exists() else []
        return hits[0] if hits else None

    def _archive_raw(self, doc_id: str, raw: bytes, filename: str | None) -> None:
        ext = Path(filename or "").suffix or ".txt"
        target = self.dir / "raw" / f"{doc_id}{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, target)

    def ingest(self, text: str, metadata: dict, raw: bytes | None = None,
               filename: str | None = None) -> dict:
        """Idempotent per document text; same text with different metadata updates
        provenance in place and reports metadata_updated. When `raw` is given, the
        exact original bytes are archived so every citation stays auditable even
        after the source URL rots or is overwritten upstream."""
        doc_id = hashlib.sha1(text.encode()).hexdigest()[:16]
        parts = _chunk(text)
        if not parts:
            raise EmptyDocument("the document contains no usable text")
        with self._lock:
            self._load()  # another thread may have written since construction
            if raw is not None and self.raw_path(doc_id) is None:
                self._archive_raw(doc_id, raw, filename)  # backfills on re-ingest too
            existing = [c for c in self._chunks if c["doc_id"] == doc_id]
            if existing:
                out = {"doc_id": doc_id, "chunks": len(existing), "already_ingested": True}
                if existing[0]["metadata"] != metadata:
                    for c in existing:
                        c["metadata"] = metadata
                    self._save()
                    out["metadata_updated"] = True
                return out
            vectors = self.embedder.embed(parts)
            if self._matrix is not None and vectors.shape[1] != self._matrix.shape[1]:
                raise CorpusError(
                    f"embedding dimension changed ({self._matrix.shape[1]} -> "
                    f"{vectors.shape[1]}) — the model moved under the corpus; {self._fix()}")
            self._chunks.extend(
                {"id": f"{doc_id}:{i}", "doc_id": doc_id, "text": t, "metadata": metadata}
                for i, t in enumerate(parts))
            self._matrix = vectors if self._matrix is None else np.vstack(
                [self._matrix, vectors])
            self._save()
        return {"doc_id": doc_id, "chunks": len(parts), "already_ingested": False}

    def search(self, query: str, k: int = 5, min_relevance: float | None = None,
               **filters) -> list[dict]:
        """Top-k above the relevance floor; metadata filters narrow BEFORE ranking;
        below the floor returns []."""
        if min_relevance is None:
            min_relevance = get_settings().rag_min_relevance
        k = max(int(k), 0)
        idx = [i for i, c in enumerate(self._chunks) if _matches(c["metadata"], filters)]
        if not idx or k == 0:
            return []
        q = self.embedder.embed([query])[0]
        sims = self._matrix[idx] @ q
        order = np.argsort(sims)[::-1][:k]
        return [{"score": round(float(sims[j]), 4),
                 **{key: self._chunks[idx[j]][key] for key in ("id", "doc_id", "text", "metadata")}}
                for j in order if sims[j] >= min_relevance]

    def count(self, **filters) -> int:
        """Chunks surviving the filters (pre-similarity) — distinguishes
        'nothing matches your filters' from 'below threshold'."""
        return sum(1 for c in self._chunks if _matches(c["metadata"], filters))

    def documents(self) -> list[dict]:
        docs: dict[str, dict] = {}
        for c in self._chunks:
            entry = docs.setdefault(
                c["doc_id"], {"doc_id": c["doc_id"], "chunks": 0, "metadata": c["metadata"]})
            entry["chunks"] += 1
        return list(docs.values())


def source_block(hits: list[dict]) -> str:
    """E3: hits -> the numbered citation block a synthesis prompt injects.
    Formats evidence only; the caller's prompt must still enforce grounding."""
    lines = []
    for n, h in enumerate(hits, 1):
        m = h["metadata"]
        bits = [str(m.get("source") or "unknown source")]
        bits += [str(m[k]) for k in ("title", "pub_date") if m.get(k)]
        if m.get("validation"):
            bits.append(f"validation: {m['validation']}")
        if m.get("url"):
            bits.append(str(m["url"]))
        lines.append(f"[{n}] {' — '.join(bits)}\n{h['text']}")
    return "\n\n".join(lines)
