"""Suchindex über das Gedächtnis: SQLite (FTS5 Volltext) + Embeddings, kombiniert per Reciprocal Rank Fusion.

Der Index ist nur ein Cache – die Wahrheit liegt in den Markdown-Dateien und kann mit
`jarvis reindex` jederzeit neu aufgebaut werden.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    embedding BLOB,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

MAX_CHUNK_CHARS = 1500
RRF_K = 60


@dataclass
class Hit:
    id: int
    kind: str
    day: str
    text: str
    created: float
    score: float


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(para[:max_chars])
            para = para[max_chars:]
        if len(buf) + len(para) + 2 > max_chars and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    return parts


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w{3,}", text.lower())][:24]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class MemoryIndex:
    def __init__(self, path: Path, embedder=None, min_similarity: float = 0.3):
        self.path = path
        self.embedder = embedder  # Objekt mit async embed(list[str]) -> list[list[float]]
        self.min_similarity = min_similarity
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self._matrix: np.ndarray | None = None
        self._matrix_ids: list[int] = []
        self._lock = asyncio.Lock()
        self._embed_warned = False

    def close(self) -> None:
        self.db.close()

    async def _embed(self, texts: list[str]) -> list[np.ndarray | None]:
        if not self.embedder or not texts:
            return [None] * len(texts)
        try:
            vecs = await self.embedder.embed(texts)
            return [np.asarray(v, dtype=np.float32) for v in vecs]
        except Exception as e:  # noqa: BLE001 – ohne Embeddings bleibt die Volltextsuche
            if not self._embed_warned:
                log.warning("Embeddings nicht verfügbar, nutze nur Volltextsuche: %s", e)
                self._embed_warned = True
            return [None] * len(texts)

    async def add(self, kind: str, day: str, source: str, text: str, created: float | None = None) -> int:
        chunks = split_text(text)
        if not chunks:
            return 0
        created = created or time.time()
        new = []
        for c in chunks:
            h = hashlib.sha1(f"{source}\n{c}".encode()).hexdigest()
            if not self.db.execute("SELECT 1 FROM chunks WHERE hash=?", (h,)).fetchone():
                new.append((c, h))
        if not new:
            return 0
        vecs = await self._embed([c for c, _ in new])
        async with self._lock:
            for (c, h), v in zip(new, vecs):
                blob = v.tobytes() if v is not None else None
                self.db.execute(
                    "INSERT OR IGNORE INTO chunks(kind, day, source, text, hash, embedding, created) VALUES (?,?,?,?,?,?,?)",
                    (kind, day, source, c, h, blob, created),
                )
            self.db.commit()
            self._matrix = None
        return len(new)

    def delete_source(self, source: str) -> None:
        self.db.execute("DELETE FROM chunks WHERE source=?", (source,))
        self.db.commit()
        self._matrix = None

    def clear(self) -> None:
        self.db.execute("DELETE FROM chunks")
        self.db.commit()
        self._matrix = None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def _load_matrix(self) -> None:
        rows = self.db.execute("SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        vecs, ids = [], []
        dims: dict[int, int] = {}
        for _, blob in rows:
            dims[len(blob)] = dims.get(len(blob), 0) + 1
        if not dims:
            self._matrix, self._matrix_ids = np.zeros((0, 1), dtype=np.float32), []
            return
        size = max(dims, key=dims.get)  # bei gewechseltem Embedding-Modell die häufigste Dimension
        for rid, blob in rows:
            if len(blob) == size:
                v = np.frombuffer(blob, dtype=np.float32)
                n = np.linalg.norm(v)
                vecs.append(v / n if n else v)
                ids.append(rid)
        self._matrix = np.vstack(vecs)
        self._matrix_ids = ids

    async def search(self, query: str, k: int = 6, exclude_after: float | None = None,
                     kinds: tuple[str, ...] | None = None, exclude_source: str | None = None) -> list[Hit]:
        """Hybride Suche. exclude_after: Journal-Einträge ab diesem Zeitpunkt ignorieren
        (die stehen ohnehin noch wörtlich im aktuellen Gesprächsverlauf). Mit exclude_source gilt das nur für
        Einträge dieser Quelle (des aktuellen Chats) – andere Chats bleiben auffindbar."""
        ranks: dict[int, float] = {}

        q = fts_query(query)
        if q:
            try:
                rows = self.db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT 30", (q,)
                ).fetchall()
                for rank, (rid,) in enumerate(rows):
                    ranks[rid] = ranks.get(rid, 0) + 1 / (RRF_K + rank)
            except sqlite3.OperationalError as e:
                log.debug("FTS-Fehler: %s", e)

        (qvec,) = await self._embed([query])
        if qvec is not None:
            if self._matrix is None:
                self._load_matrix()
            if self._matrix is not None and len(self._matrix_ids) and self._matrix.shape[1] == qvec.shape[0]:
                qn = qvec / (np.linalg.norm(qvec) or 1)
                sims = self._matrix @ qn
                order = np.argsort(-sims)[:30]
                for rank, idx in enumerate(order):
                    if sims[idx] < self.min_similarity:
                        break
                    rid = self._matrix_ids[idx]
                    ranks[rid] = ranks.get(rid, 0) + 1 / (RRF_K + rank)

        if not ranks:
            return []
        placeholders = ",".join("?" * len(ranks))
        rows = self.db.execute(
            f"SELECT id, kind, day, text, created, source FROM chunks WHERE id IN ({placeholders})", list(ranks)
        ).fetchall()
        now = time.time()
        hits = []
        for rid, kind, day, text, created, source in rows:
            if kinds and kind not in kinds:
                continue
            if exclude_after is not None and kind == "journal" and created >= exclude_after and \
                    (exclude_source is None or source == exclude_source):
                continue
            age_days = max(0.0, (now - created) / 86400)
            recency = 0.004 * math.exp(-age_days / 30)  # leichte Bevorzugung frischer Erinnerungen
            bonus = 0.003 if kind in ("fact", "summary") else 0.0
            hits.append(Hit(rid, kind, day, text, created, ranks[rid] + recency + bonus))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
