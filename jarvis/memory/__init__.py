"""Persistentes Gedächtnis von Jarvis.

~/.local/share/jarvis/memory/
    journal/YYYY-MM-DD.md     Rohprotokoll jedes Tages
    summaries/YYYY-MM-DD.md   Tageszusammenfassungen
    facts.md                  dauerhafte Fakten
    session.json              aktueller Gesprächsverlauf + laufende Zusammenfassung
    index.sqlite              Suchindex (aus den .md-Dateien rekonstruierbar)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from ..config import MemoryConfig
from .context import Conversation, est_tokens
from .files import Facts, Journal, Summaries, day_str
from .index import Hit, MemoryIndex, split_text

log = logging.getLogger(__name__)

__all__ = ["Memory", "Conversation", "Hit", "est_tokens"]

DAY_CHUNK_CHARS = 12000


class Memory:
    def __init__(self, cfg: MemoryConfig, llm):
        self.cfg = cfg
        self.llm = llm
        cfg.dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(cfg.dir / "journal")
        self.summaries = Summaries(cfg.dir / "summaries")
        self.facts = Facts(cfg.dir / "facts.md")
        self.index = MemoryIndex(cfg.dir / "index.sqlite", embedder=llm)
        self.conversation = Conversation(cfg.dir / "session.json")
        self.last_activity = time.time()

    def close(self) -> None:
        self.index.close()

    # ---------- Schreiben ----------
    async def log_exchange(self, user_text: str, assistant_text: str, tool_notes: list[str]) -> None:
        now = datetime.now()
        self.journal.append("Du", user_text, now)
        for note in tool_notes:
            self.journal.append("Tool", note, now)
        self.journal.append("Jarvis", assistant_text or "(keine Antwort)", now)
        self.last_activity = time.time()
        text = f"Nutzer: {user_text}\n" + "".join(f"Tool: {n}\n" for n in tool_notes) + f"Jarvis: {assistant_text}"
        await self.index.add("journal", day_str(now), f"journal:{day_str(now)}", text)

    async def remember(self, fact: str) -> bool:
        added = self.facts.add(fact)
        if added:
            await self.index.add("fact", day_str(), "facts", fact)
        return added

    async def forget(self, query: str) -> list[str]:
        removed = self.facts.remove(query)
        if removed:
            # Fakten-Chunks neu aufbauen (klein, daher billig)
            self.index.delete_source("facts")
            await self.reindex_facts()
        return removed

    async def reindex_facts(self) -> None:
        for fact, day in self.facts.list():
            await self.index.add("fact", day or day_str(), "facts", fact)

    # ---------- Lesen ----------
    async def retrieve(self, query: str, exclude_after: float | None = None) -> list[Hit]:
        return await self.index.search(query, k=self.cfg.retrieval_top_k, exclude_after=exclude_after)

    def format_hits(self, hits: list[Hit]) -> str:
        out, used = [], 0
        budget = self.cfg.retrieval_max_tokens
        labels = {"journal": "Gespräch", "summary": "Tageszusammenfassung", "fact": "Fakt"}
        for h in hits:
            entry = f"[{h.day} · {labels.get(h.kind, h.kind)}]\n{h.text.strip()}"
            t = est_tokens(entry)
            if used + t > budget:
                continue
            out.append(entry)
            used += t
        return "\n\n".join(out)

    def facts_text(self) -> str:
        return self.facts.as_text(self.cfg.facts_max_tokens * 3)

    # ---------- Tageszusammenfassungen ----------
    async def summarize_day(self, day: str) -> str | None:
        text = self.journal.read(day)
        if not text:
            return None
        system = {"role": "system", "content": (
            "Du fasst das Tagesprotokoll zwischen einem Nutzer und seinem KI-Assistenten Jarvis zusammen. "
            "Schreibe auf Deutsch prägnante Stichpunkte: was wurde gemacht, welche Befehle/Pakete/Dateien "
            "waren beteiligt, Ergebnisse, Vorlieben des Nutzers, offene Punkte. Keine Einleitung.")}
        parts = split_text(text, DAY_CHUNK_CHARS)
        partials = []
        for part in parts:
            partials.append(await self.llm.chat([system, {"role": "user", "content": part}]))
        summary = partials[0] if len(partials) == 1 else await self.llm.chat([
            system, {"role": "user", "content": "Fasse diese Teilzusammenfassungen zu einer zusammen:\n\n"
                     + "\n\n".join(partials)}])
        self.summaries.write(day, summary)
        self.index.delete_source(f"summary:{day}")
        created = datetime.strptime(day, "%Y-%m-%d").timestamp() + 86399
        await self.index.add("summary", day, f"summary:{day}", summary, created=min(created, time.time()))
        return summary

    def days_needing_summary(self, include_today: bool) -> list[str]:
        today = day_str()
        out = []
        for day in self.journal.days():
            if day == today and not include_today:
                continue
            if self.journal.mtime(day) > self.summaries.mtime(day):
                out.append(day)
        return out

    async def summarize_pending(self) -> list[str]:
        idle = time.time() - self.last_activity > self.cfg.summarize_idle_minutes * 60
        done = []
        for day in self.days_needing_summary(include_today=idle):
            try:
                await self.summarize_day(day)
                done.append(day)
            except Exception as e:  # noqa: BLE001
                log.warning("Zusammenfassung für %s fehlgeschlagen: %s", day, e)
                break
        return done

    # ---------- Wartung ----------
    async def rebuild_index(self) -> int:
        self.index.clear()
        for day in sorted(self.journal.days()):
            entries = self.journal.entries(day)
            block: list[str] = []
            for e in entries:
                if e.speaker == "Du" and block:
                    await self.index.add("journal", day, f"journal:{day}", "\n".join(block),
                                         created=datetime.strptime(f"{day} {e.time}", "%Y-%m-%d %H:%M:%S").timestamp())
                    block = []
                label = {"Du": "Nutzer"}.get(e.speaker, e.speaker)
                block.append(f"{label}: {e.text}")
            if block:
                await self.index.add("journal", day, f"journal:{day}", "\n".join(block),
                                     created=datetime.strptime(day, "%Y-%m-%d").timestamp())
        for day in self.summaries.days():
            body = self.summaries.read(day) or ""
            body = body.split("\n", 2)[-1]
            await self.index.add("summary", day, f"summary:{day}", body,
                                 created=datetime.strptime(day, "%Y-%m-%d").timestamp() + 86399)
        await self.reindex_facts()
        return self.index.count()
