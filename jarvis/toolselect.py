"""Werkzeug-Auswahl für kleine Kontextfenster.

Mit allen Integrationen belegen die Tool-Beschreibungen mehrere tausend Token. Ist das Kontextfenster klein
(z. B. 8k bei Bonsai), bekommt das Modell nur die Grundausstattung plus die Gruppen, die zur aktuellen Frage
passen (Stichwörter, Deutsch/Englisch) oder in dieser Runde schon benutzt wurden.
"""

from __future__ import annotations

import re

# Immer dabei (zusammen ca. 3k Token)
CORE_GROUPS = {"files", "shell", "web", "apps", "memory_tools", "reminder_tools", "power", "weather", "briefing",
               "system"}

KEYWORDS = {
    "packages": r"update|upgrade|paket|package|install|deinstall|uninstall|entfern|pacman|\bapt\b|\baur\b|yay|paru",
    "sysadmin": r"prozess|process|dienst|service|systemd|systemctl|\blogs?\b|journal|netzwerk|network|\bip\b|ping|"
                r"\bports?\b|wlan|wifi|lan\b|speicher|disk|festplatte|platz|space|aufräum|cleanup|clean up|\bcpu\b|"
                r"\bram\b|auslast|langsam|slow|kill|beend|hängt|hang|router|dns|erreichbar|reachable|docker|ssh",
    "homeassistant": r"licht|lampe|light|lamp|heizung|heating|thermostat|rollo|jalousie|blind|shutter|cover|"
                     r"steckdose|plug|schalte|switch|szene|scene|temperatur|temperature|sensor|smart ?home|"
                     r"home assistant|garage|\btür|door|schloss|lock|ventilator|fan|dimm|hell|bright|staubsauger|vacuum",
    "paperless": r"dokument|document|rechnung|invoice|vertrag|contract|\bbrief|letter|paperless|bescheid|"
                 r"versicherung|insurance|quittung|receipt|garantie|warranty|kündig|cancel|steuer|tax|\bpdf",
    "trilium": r"notiz|note|trilium|notier|aufschrieb|anleitung|how-?to|wiki|schreib (das |mir )?auf|write down",
    "calendar_tools": r"termin|kalender|calendar|meeting|appointment|\bevent|verabred|besprechung|"
                      r"frei(e zeit)?\b|free time|schedule|wann habe ich|when do i",
}
_COMPILED = {g: re.compile(p, re.I) for g, p in KEYWORDS.items()}


def relevant_groups(texts: list[str]) -> set[str]:
    joined = "\n".join(t for t in texts if t)
    return {g for g, rx in _COMPILED.items() if rx.search(joined)}


def select(schemas: list[dict], groups_of: dict[str, str], texts: list[str], used_groups: set[str]) -> list[dict]:
    """Grundausstattung + passende + bereits benutzte Gruppen; unbekannte Gruppen (z. B. Plugins) bleiben drin."""
    wanted = CORE_GROUPS | relevant_groups(texts) | used_groups
    out = []
    for s in schemas:
        group = groups_of.get(s["function"]["name"], "")
        if group in wanted or group not in KEYWORDS:
            out.append(s)
    return out
