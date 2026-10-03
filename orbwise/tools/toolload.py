"""Werkzeuge auf Abruf: Bei kleinem Kontextfenster sind nur Grundausstattung und passende Gruppen geladen. Fehlt dem
Modell ein Werkzeug (kein Stichwort hat gepasst), lädt es die Gruppe hier nach – im nächsten Schritt ist sie da."""

from __future__ import annotations

from typing import Annotated, Any

from .. import toolselect
from ..prompts import lang_of
from .registry import REGISTRY, ToolContext, tool


def loadable_groups(cfg: Any) -> list[str]:
    """Nachladbare Gruppen, die eingerichtet sind (mindestens ein Werkzeug aktiv)."""
    active = {s.group for s in REGISTRY.values() if cfg is None or s.is_enabled(cfg)}
    return [g for g in toolselect.KEYWORDS if g in active]


def _describe(cfg: Any) -> str:
    en = lang_of(cfg) == "en"
    labels = toolselect.GROUP_LABELS["en" if en else "de"]
    groups = "; ".join(f"{g} ({labels.get(g, g)})" for g in loadable_groups(cfg))
    if en:
        return f"Loads more tool groups when a tool for the request is missing – available from the next step. Groups: {groups}."
    return ("Lädt weitere Werkzeuggruppen nach, wenn dir für die Bitte ein Werkzeug fehlt – sie stehen ab dem nächsten "
            f"Schritt bereit. Gruppen: {groups}.")


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
    if conv is not None:
        ep = conv.epoch
        ep["groups"] = sorted(set(ep.get("groups") or []) | set(ok))
    names = [s.name for s in REGISTRY.values() if s.group in ok and s.is_enabled(ctx.cfg)]
    out = f"Geladen: {', '.join(names)} – ab dem nächsten Schritt verfügbar."
    if unknown:
        out += f" Unbekannt: {', '.join(unknown)}."
    return out
