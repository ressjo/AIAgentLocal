"""Werkzeuge auf Abruf: Passen nicht alle Werkzeuge ins Kontextfenster, sind nur Grundausstattung und passende Gruppen
geladen. Fehlt dem Modell ein Werkzeug (kein Stichwort hat gepasst), lädt es die Gruppe hier nach – im nächsten
Schritt ist sie da. Die Beschreibung nennt nur, was gerade fehlt (der Agent setzt sie je Auswahl, siehe
Agent.choose_tools)."""

from __future__ import annotations

from typing import Annotated, Any

from .. import toolselect
from ..prompts import lang_of
from .registry import REGISTRY, ToolContext, tool


def loadable_groups(cfg: Any) -> list[str]:
    """Nachladbare Gruppen, die eingerichtet sind (mindestens ein Werkzeug aktiv) – auch die, die nur im kleinen
    Kontextfenster fehlen können (ab 16k sind sie immer dabei)."""
    active = {s.group for s in REGISTRY.values() if cfg is None or s.is_enabled(cfg)}
    return [g for g in [*toolselect.KEYWORDS, *toolselect.SMALL_KEYWORDS] if g in active]


def describe_missing(cfg: Any, missing: dict[str, bool]) -> str:
    """Beschreibung von load_tools für die aktuelle Auswahl. missing: Gruppe → True, wenn nur ihr ändernder Teil
    fehlt (kleines Fenster)."""
    en = lang_of(cfg) == "en"
    labels = toolselect.GROUP_LABELS["en" if en else "de"]
    write = toolselect.WRITE_LABELS["en" if en else "de"]
    more = "more: " if en else "mehr: "
    groups = "; ".join(f"{g} ({more + write[g] if only_write and g in write else labels.get(g, g)})"
                       for g, only_write in missing.items())
    if en:
        return f"Loads tool groups when a tool for the request is missing (available from the next step): {groups}."
    return f"Lädt Werkzeuggruppen nach, wenn dir für die Bitte ein Werkzeug fehlt (ab dem nächsten Schritt): {groups}."


def _describe(cfg: Any) -> str:
    """Ohne Agent: alles, was ab 16k nachzuladen ist."""
    return describe_missing(cfg, {g: False for g in loadable_groups(cfg) if g in toolselect.KEYWORDS})


@tool("Lädt weitere Werkzeuggruppen nach, wenn dir für die Bitte ein Werkzeug fehlt.", describe=_describe)
async def load_tools(
    ctx: ToolContext,
    groups: Annotated[str, "Gruppennamen, mit Komma getrennt, z. B. 'paperless' oder 'mail, calendar_tools'"],
) -> str:
    wanted = [g.strip() for g in groups.replace(";", ",").split(",") if g.strip()]
    valid = loadable_groups(ctx.cfg)
    unknown = [g for g in wanted if g not in valid]
    ok = [g for g in wanted if g in valid]
    if not ok:
        return f"Unbekannte Gruppe {', '.join(unknown) or '(leer)'} – möglich: {', '.join(valid)}."
    conv = ctx.memory.conversation if ctx.memory else None
    if conv is not None:  # angeheftet: bleibt bis zur nächsten Komprimierung – samt ändernder Teile
        ep = conv.epoch
        ep["pinned"] = sorted(set(ep.get("pinned") or []) | set(ok) | toolselect.write_units(set(ok)))
    names = [s.name for s in REGISTRY.values() if s.group in ok and s.is_enabled(ctx.cfg)]
    out = f"Geladen: {', '.join(names)} – ab dem nächsten Schritt verfügbar."
    if unknown:
        out += f" Unbekannt: {', '.join(unknown)}."
    return out
