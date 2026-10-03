"""Tools für das Langzeitgedächtnis."""

from __future__ import annotations

from typing import Annotated

from ..memory.files import valid_day
from . import proc
from .registry import ToolContext, tool


@tool("Merkt sich dauerhaft einen wichtigen Fakt über den Nutzer, seine Vorlieben, sein System oder seine "
      "Projekte (z. B. 'Das NAS ist unter /mnt/nas gemountet'). Einen vollständigen Satz formulieren.")
async def remember(ctx: ToolContext, fact: Annotated[str, "Der Fakt als vollständiger Satz"]) -> str:
    added, replaced = await ctx.memory.remember_fact(fact)
    if replaced:
        return f"Aktualisiert (vorher: {replaced})."
    return "Gespeichert." if added else "Das war bereits bekannt."


@tool("Löscht gespeicherte Fakten, die den Suchtext enthalten.")
async def forget(ctx: ToolContext, query: Annotated[str, "Suchtext des zu löschenden Fakts"]) -> str:
    removed = await ctx.memory.forget(query)
    return ("Gelöscht: " + "; ".join(removed)) if removed else "Kein passender Fakt gefunden."


@tool("Durchsucht das Langzeitgedächtnis (frühere Gespräche, Tageszusammenfassungen, Fakten). Mit 'date' "
      "(YYYY-MM-DD) wird das Protokoll bzw. die Zusammenfassung dieses Tages geladen – relative Angaben wie "
      "'letzten Dienstag' vorher anhand des heutigen Datums umrechnen.")
async def recall(
    ctx: ToolContext,
    query: Annotated[str, "Wonach gesucht wird"] = "",
    date: Annotated[str, "Optional: Tag im Format YYYY-MM-DD"] = "",
) -> str:
    mem = ctx.memory
    parts = []
    if date:
        if not valid_day(date):
            return "Ungültiges Datum, bitte YYYY-MM-DD verwenden."
        summary = mem.summaries.read(date)
        journal = mem.journal.read(date)
        if summary:
            parts.append(summary)
        if journal and (not summary or query):
            parts.append("Protokoll:\n" + proc.clip(journal, 5000))
        if not parts:
            parts.append(f"Für {date} gibt es keine Einträge. Vorhandene Tage: {', '.join(mem.journal.days()[:15])}")
    if query:
        hits = await mem.index.search(query, k=8)
        parts.append(mem.format_hits(hits) if hits else "Keine passenden Erinnerungen gefunden.")
    if not parts:
        days = mem.journal.days()
        parts.append("Tage mit Einträgen: " + (", ".join(days[:30]) or "keine"))
    return "\n\n".join(parts)
