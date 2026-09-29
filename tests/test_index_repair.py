"""Beschädigter Gedächtnis-Suchindex („database disk image is malformed“) repariert sich selbst."""

import os
import sqlite3
import time

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise.memory import Memory
from orbwise.memory.index import MemoryIndex, check_file


async def noop(ev):
    pass


async def deny(*a):
    return False


def fill(index: MemoryIndex, n: int = 120) -> None:
    for i in range(n):
        run(index.add("journal", "2026-09-29", f"chat:{i % 3}", f"Nutzer fragt nach dem Wetter in Freiburg {i} " * 12))


def smash(path) -> None:
    """Seiten der Datei mit Zufallsdaten überschreiben – wie nach einem Absturz mitten im Schreiben."""
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        for page in range(2, size // 4096):
            f.seek(page * 4096)
            f.write(b"\xde\xad\xbe\xef" * 16)  # Seitenkopf zerstören – fest statt zufällig, damit reproduzierbar


def test_fts_out_of_sync_is_rebuilt_on_open(tmp_path):
    path = tmp_path / "index.sqlite"
    idx = MemoryIndex(path)
    fill(idx, 20)
    # Tabelle ändern, ohne dass der Volltextindex es mitbekommt → „malformed“ bei der FTS-Prüfung
    idx.db.execute("UPDATE chunks SET text='ganz anderer inhalt' WHERE source='chat:1'")
    idx.db.commit()
    idx.close()
    assert not check_file(path)

    idx = MemoryIndex(path)  # Stufe 1: Volltextindex neu aufgebaut, Einträge bleiben
    assert idx.healthy() and not idx.needs_rebuild and idx.count() == 20
    idx.delete_source("chat:1")
    assert run(idx.search("Wetter Freiburg")) and idx.healthy()
    idx.close()


def test_broken_file_is_replaced_and_delete_does_not_fail(tmp_path):
    path = tmp_path / "index.sqlite"
    idx = MemoryIndex(path)
    fill(idx)
    idx.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    idx.close()
    smash(path)
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="malformed|vtable constructor failed"):
        raw.execute("DELETE FROM chunks WHERE source='chat:1'")  # genau der Fehler aus dem VERLAUF
    raw.close()

    idx = MemoryIndex.__new__(MemoryIndex)  # wie ein laufender Server, dessen Datei erst später kaputtgeht
    idx.path, idx.embedder, idx.min_similarity = path, None, 0.3
    idx._matrix, idx._matrix_ids, idx.needs_rebuild = None, [], False
    idx.db = sqlite3.connect(path, check_same_thread=False)
    idx.delete_source("chat:1")  # kein Fehler: Datei beiseitegelegt, leer neu angelegt
    assert idx.needs_rebuild and idx.healthy() and idx.count() == 0
    assert list(tmp_path.glob("index.sqlite.corrupt-*"))
    idx.close()


def test_broken_file_on_start_is_rebuilt_from_markdown(cfg, llm):
    mem = Memory(cfg.memory, llm)
    run(mem.log_exchange("Wie heißt mein NAS?", "Dein NAS heißt Tresor.", []))
    run(mem.remember("Das NAS heißt Tresor"))
    mem.index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    fill(mem.index)
    mem.index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    mem.close()
    smash(cfg.memory.dir / "index.sqlite")

    mem = Memory(cfg.memory, llm)
    assert mem.index.needs_rebuild
    assert run(mem.heal_index_if_needed())
    assert not mem.index.needs_rebuild and mem.index.healthy()
    assert any("Tresor" in h.text for h in run(mem.index.search("NAS Tresor")))
    mem.close()


def test_reindex_works_on_broken_file(cfg, llm):
    mem = Memory(cfg.memory, llm)
    run(mem.log_exchange("Hallo", "Guten Tag.", []))
    fill(mem.index)
    mem.index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    smash(cfg.memory.dir / "index.sqlite")
    mem.index.db.close()
    mem.index.db = sqlite3.connect(cfg.memory.dir / "index.sqlite", check_same_thread=False)
    assert run(mem.rebuild_index()) >= 1  # `orbwise reindex`: legt die Datei neu an statt DELETE
    assert mem.index.healthy()
    mem.close()


def test_chat_delete_with_broken_index_returns_ok(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app

    client = TestClient(create_app(cfg), base_url="http://localhost:8765")
    with client:
        memory = client.app.state.memory
        hub = client.app.state.hub
        client.portal.call(hub.agent.run, "Erster Chat über das Wetter", noop, deny)
        first = memory.conversation.chat_id
        client.post("/api/chats")
        client.portal.call(hub.agent.run, "Zweiter Chat", noop, deny)
        fill(memory.index)
        memory.index.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        memory.index.db.close()
        path = cfg.memory.dir / "index.sqlite"
        smash(path)
        memory.index.db = sqlite3.connect(path, check_same_thread=False)

        r = client.delete(f"/api/chats/{first}")
        assert r.status_code == 200
        assert not memory.chats.exists(first)
        assert all(e.chat != first for d in memory.journal.days() for e in memory.journal.entries(d))
        for _ in range(50):  # Neuaufbau läuft im Hintergrund
            if not memory.index.needs_rebuild:
                break
            time.sleep(0.05)
        assert not memory.index.needs_rebuild and memory.index.healthy()
        assert memory.index.count() >= 1  # der zweite Chat ist wieder im Index
