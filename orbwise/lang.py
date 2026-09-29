"""Sprache der Kommandozeilen-Ausgaben (folgt der Config-Einstellung language)."""

_lang = "de"


def set_lang(language: str) -> None:
    global _lang
    _lang = "en" if language == "en" else "de"


def T(de: str, en: str) -> str:  # noqa: N802 – kurzer Name, wird überall in Ausgaben verwendet
    return en if _lang == "en" else de


# Begründungen der Rückfragen entstehen auf Deutsch (auch das Modell liest sie) – für die englische Oberfläche
# werden die häufigen übersetzt; Unbekanntes bleibt stehen.
_REASONS_EN = [
    (r"benötigt Root-Rechte", "needs root privileges"),
    (r"'([^']+)' kann das System verändern", r"'\1' can change the system"),
    (r"schreibt in eine Datei", "writes to a file"),
    (r"enthält Befehlsersetzung", "contains command substitution"),
    (r"liest Zugangsdaten \(Schlüssel/Passwörter\)", "reads credentials (keys/passwords)"),
    (r"Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely"),
    (r"ACHTUNG: führt ein Skript direkt aus dem Internet aus", "WARNING: runs a script straight from the internet"),
    (r"ACHTUNG: Partitionierungswerkzeug", "WARNING: partitioning tool"),
    (r"ACHTUNG: entfernt Pakete ohne Abhängigkeitsprüfung", "WARNING: removes packages without dependency checks"),
    (r"Systemupdate mit Root-Rechten", "system update with root privileges"),
    (r"Pakete installieren: ", "install packages: "),
    (r"Pakete entfernen: ", "remove packages: "),
    (r"Paket-Cache, verwaiste Pakete und alte Journal-Logs löschen",
     "delete package cache, orphaned packages and old journal logs"),
    (r"Prozess beenden: ", "end process: "),
    (r"Dienst ([^:]+): ", r"service \1: "),
    (r"Sicherheitsrelevantes Gerät: ", "security-relevant device: "),
    (r"Datei (.+) an Paperless übergeben", r"hand file \1 over to Paperless"),
    (r"Schlüssel, Passwörter und Zugangsdaten gibt Orbwise nicht heraus", "Orbwise never hands out keys, "
     "passwords or credentials"),
]


def reason_en(reason: str) -> str:
    import re
    for pattern, repl in _REASONS_EN:
        reason = re.sub(pattern, repl, reason)
    return reason
