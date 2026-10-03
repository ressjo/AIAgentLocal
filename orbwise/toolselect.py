"""Werkzeug-Auswahl für kleine Kontextfenster.

Mit allen Integrationen belegen die Tool-Beschreibungen mehrere tausend Token. Ist das Kontextfenster klein
(z. B. 8k bei Bonsai), bekommt das Modell nur die Grundausstattung plus die Gruppen, die zur aktuellen Frage
passen (Stichwörter, Deutsch/Englisch) oder in dieser Runde schon benutzt wurden.
"""

from __future__ import annotations

import re

# Immer dabei (zusammen ca. 3k Token)
CORE_GROUPS = {"files", "shell", "web", "apps", "memory_tools", "reminder_tools", "power", "weather", "briefing",
               "system", "todo_tools", "tool_loader"}

# Kurzbeschreibung je nachladbarer Gruppe (für load_tools – das Modell weiß so, was es gibt)
GROUP_LABELS = {
    "de": {"packages": "Pakete installieren/entfernen, Systemupdates",
           "sysadmin": "Prozesse, Dienste, Logs, Netzwerk, Ports, Speicherplatz",
           "homeassistant": "Smart Home: Licht, Heizung, Rollos, Geräte",
           "mail": "E-Mails lesen, suchen, sortieren, senden",
           "routine_tools": "wiederkehrende, zeitgesteuerte Aufgaben",
           "paperless": "Dokumente: Rechnungen, Verträge, Briefe",
           "trilium": "Notizen in Trilium", "obsidian": "Notizen in Obsidian",
           "vision": "Bildschirm und Bilder ansehen",
           "telegram_tools": "Dateien aufs Handy schicken",
           "calendar_tools": "Kalender: Termine, freie Zeit"},
    "en": {"packages": "install/remove packages, system updates",
           "sysadmin": "processes, services, logs, network, ports, disk space",
           "homeassistant": "smart home: lights, heating, covers, devices",
           "mail": "read, search, sort, send e-mail",
           "routine_tools": "recurring, scheduled tasks",
           "paperless": "documents: invoices, contracts, letters",
           "trilium": "notes in Trilium", "obsidian": "notes in Obsidian",
           "vision": "look at the screen and images",
           "telegram_tools": "send files to the phone",
           "calendar_tools": "calendar: events, free time"},
}

KEYWORDS = {
    "packages": r"update|upgrade|paket|package|install|deinstall|uninstall|entfern|pacman|\bapt\b|\baur\b|yay|paru",
    "sysadmin": r"prozess|process|dienst|service|systemd|systemctl|\blogs?\b|journal|netzwerk|network|\bip\b|ping|"
                r"\bports?\b|wlan|wifi|lan\b|speicher|disk|festplatte|platz|space|aufräum|cleanup|clean up|\bcpu\b|"
                r"\bram\b|auslast|langsam|slow|kill|beend|hängt|hang|router|dns|erreichbar|reachable|docker|ssh",
    "homeassistant": r"licht|lampe|light|lamp|heizung|heating|thermostat|rollo|jalousie|blind|shutter|cover|"
                     r"steckdose|plug|schalte|switch|szene|scene|temperatur|temperature|sensor|smart ?home|"
                     r"home assistant|garage|\btür|door|schloss|lock|ventilator|fan|dimm|hell|bright|staubsauger|vacuum",
    "mail": r"\bmail|e-?mail|postfach|mailbox|\binbox|posteingang|nachricht(en)? von|absender|sender|newsletter|"
            r"anhang|anhänge|attachment|ungelesen|unread|spam|archivier|archive",
    "routine_tools": r"routine|jeden (morgen|abend|tag|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
                     r"werktag)|werktags|täglich|wöchentlich|regelmäßig|automatisch|zeitplan|um \d{1,2}([:.]\d\d)? ?uhr|"
                     r"every (day|morning|evening|week|monday|tuesday|wednesday|thursday|friday)|daily|weekly|"
                     r"schedul|at \d{1,2}(:\d\d)? ?(am|pm)",
    "paperless": r"dokument|document|rechnung|invoice|vertrag|contract|\bbrief|letter|paperless|bescheid|"
                 r"versicherung|insurance|quittung|receipt|garantie|warranty|kündig|cancel|steuer|tax|\bpdf|"
                 r"\btag|korrespondent|correspondent|dokumenttyp|document type|posteingang|inbox|einordn|sortier|classif",
    "trilium": r"notiz|note|trilium|notier|aufschrieb|anleitung|how-?to|wiki|schreib (das |mir )?auf|write down",
    "obsidian": r"notiz|note|obsidian|notier|aufschrieb|anleitung|how-?to|wiki|protokoll|minutes|vault|"
                r"schreib (das |mir )?auf|write down",
    "vision": r"bildschirm|screen|monitor|fenster|window|siehst du|sieh dir|schau (dir |mal )?|guck|"
              r"fehlermeldung|error message|meldung|dialog|popup|pop-up|foto|photo|bild\b|bilder|image|picture|"
              r"screenshot|was steht da|what does it say|look at|anschau",
    "telegram_tools": r"telegram|handy|smartphone|\bphone|aufs? (telefon|mobil)|schick (mir|sie|es|das|die|den)|"
                      r"send (me|it|this|that)",
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
