"""Tools für das Langzeitgedächtnis."""

from __future__ import annotations

from typing import Annotated

from ..memory.files import valid_day
from . import proc
from .registry import ToolContext, tool


@tool("Merkt sich dauerhaft einen wichtigen Fakt über den Nutzer, seine Vorlieben, sein System oder seine "
      "Projekte (z. B. 'Das NAS ist unter /mnt/nas gemountet'). Einen vollständigen Satz formulieren.")
async def remember(ctx: ToolContext, fact: Annotated[str, "Der Fakt als vollständiger Satz"]) -> str:
    added = await ctx.memory.remember(fact)
    return "Gespeichert." if added else "Das war bereits bekannt."


@tool("Holt die vollständige Ausgabe eines früheren Werkzeug-Ergebnisses, das bei einer langen Aufgabe auf einen "
      "Auszug gefaltet wurde (Nummer aus dem Hinweis 'earlier_output(step=N)'). Nur nutzen, wenn die Details "
      "wirklich gebraucht werden.")
async def earlier_output(ctx: ToolContext, step: Annotated[int, "Nummer aus dem Hinweis"]) -> str:
    entry = ctx.memory.conversation.stash.get(str(step))
    if not entry:
        return "Keine gefaltete Ausgabe mit dieser Nummer (ältere Einträge werden irgendwann verworfen)."
    return f"[Schritt {step} · {entry.get('tool', '')}] {entry.get('call', '')}\n{entry.get('text', '')}"


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
