"""Englische Fassung der Oberfläche: index.html wird beim Ausliefern übersetzt (keine zweite HTML-Datei pflegen).
Die dynamischen Texte übersetzt app.js selbst (L("deutsch", "english"))."""

from __future__ import annotations

HTML_EN = [
    ('<html lang="de">', '<html lang="en">'),
    ('>PUNKTE &amp; REIHENFOLGE<', '>ITEMS &amp; ORDER<'),
    ('Vorausschau für Termine und Erinnerungen (Tage)', 'Look-ahead for events and reminders (days)'),
    ('Nachrichten-Themen (mit Komma getrennt)', 'News topics (comma-separated)'),
    ('placeholder="z. B. Linux, Freiburg, KI"', 'placeholder="e.g. Linux, Berlin, AI"'),
    ('Schlagzeilen je Thema', 'Headlines per topic'),
    ('Paperless-Posteingangs-Tag', 'Paperless inbox tag'),
    ('placeholder="leer = Posteingangs-Tags aus Paperless"', 'placeholder="empty = inbox tags from Paperless"'),
    ('Eigener Wunsch fürs Briefing', 'Your wish for the briefing'),
    ('placeholder="z. B. Halte dich kurz und fang mit den Terminen an."',
     'placeholder="e.g. Keep it short and start with my appointments."'),
    ('>VORSCHAU<', '>PREVIEW<'),
    ('title="Einstellungen aus der Config-Datei verwenden">ZURÜCKSETZEN<',
     'title="Use the settings from the config file">RESET<'),
    ('title="Sprachmodell wechseln"', 'title="Switch language model"'),
    ('title="Sprachein-/ausgabe"', 'title="Voice input/output"'),
    ('title="Gedächtnis"', 'title="Memory"'),
    ('KOMMUNIKATION <small', 'COMMUNICATION <small'),
    ('title="Neues Gespräch beginnen (Gedächtnis bleibt erhalten)">NEU<',
     'title="Start a new chat (memory is kept)">NEW<'),
    ('aria-label="Jarvis Statusanzeige"', 'aria-label="Jarvis status display"'),
    ('aria-label="Systemauslastung"', 'aria-label="System load"'),
    ('title="Kontext: wie voll der Prompt im Verhältnis zum Budget ist"',
     'title="Context: how full the prompt is relative to the budget"'),
    ('>KONTEXT<', '>CONTEXT<'),
    ('>LEISTUNG<', '>POWER<'),
    ('>GEDANKENGANG<', '>THOUGHTS<'),
    ('>AKTIVITÄT<', '>ACTIVITY<'),
    ('>VERLAUF<', '>HISTORY<'),
    ('>GEDÄCHTNIS<', '>MEMORY<'),
    ('>STIMME<', '>VOICE<'),
    ('>Noch keine Systemaktivität.<', '>No system activity yet.<'),
    ('placeholder="Chats durchsuchen …" aria-label="Chats durchsuchen"', 'placeholder="Search chats …" aria-label="Search chats"'),
    ('title="Neuen Chat beginnen">+ NEU<', 'title="Start a new chat">+ NEW<'),
    ("★ markiert wichtige Chats · Klick öffnet und setzt fort · Doppelklick auf den Titel benennt um ·\n"
     "          ✕ löscht den Chat auch aus Jarvis' Gedächtnis.",
     "★ marks important chats · click opens and continues · double-click the title to rename ·\n"
     "          ✕ deletes the chat from Jarvis' memory as well."),
    ('>JARVIS-EFFEKT<', '>JARVIS EFFECT<'),
    ('id="fx-toggle">AUS<', 'id="fx-toggle">OFF<'),
    ('aria-label="Effektstärke"', 'aria-label="Effect strength"'),
    ('>Tiefere, sonore Stimme mit leichtem Hall und digitalem Schimmer.<',
     '>Deeper, sonorous voice with a light reverb and a digital shimmer.<'),
    ('>DEUTSCHE STIMMEN<', '>VOICES<'),
    ('>ERINNERUNGEN<', '>REMINDERS<'),
    ('>FAKTEN<', '>FACTS<'),
    ('>TAGEBUCH<', '>JOURNAL<'),
    ('title="Halten zum Sprechen (Leertaste) · kurz tippen = zuhören bis Stille"',
     'title="Hold to talk (space bar) · tap = listen until silence"'),
    ('placeholder="Befehl eingeben oder „Hey Jarvis“ sagen …"', 'placeholder="Type a command or say “Hey Jarvis” …"'),
    ('title="Senden"', 'title="Send"'),
    ('title="Denkmodus: Jarvis denkt vor der Antwort nach – langsamer, dafür gründlicher. Der Gedankengang erscheint '
     'im Orb.">DENKEN<',
     'title="Thinking mode: Jarvis reasons before answering – slower but more thorough. The thoughts appear in the '
     'orb.">THINK<'),
    ('title="Wake-Word „Hey Jarvis“"', 'title="Wake word “Hey Jarvis”"'),
    ('title="Sprachausgabe">TON<', 'title="Speech output">SOUND<'),
    ('title="Aktuelle Aufgabe abbrechen (Esc)"', 'title="Cancel the current task (Esc)"'),
    ('>SYSTEM STARTEN<', '>START SYSTEM<'),
    ('>Aktiviert Audio &amp; Mikrofon im Browser<', '>Enables audio &amp; microphone in the browser<'),
    ('BESTÄTIGUNG ERFORDERLICH', 'CONFIRMATION REQUIRED'),
    ('ABBRECHEN <kbd>', 'CANCEL <kbd>'),
    ('AUSFÜHREN <kbd>', 'RUN <kbd>'),
    ('oder sag „Ja“ bzw. „Nein“', 'or say “yes” or “no”'),
    ('ROOT-RECHTE BENÖTIGT', 'ROOT PRIVILEGES REQUIRED'),
    ('>Root-Passwort (sudo)<', '>Root password (sudo)<'),
    ('placeholder="Passwort" aria-label="Passwort"', 'placeholder="Password" aria-label="Password"'),
    ('>Geht direkt an sudo – wird weder gespeichert noch an das Sprachmodell gegeben.<',
     '>Goes straight to sudo – never stored and never passed to the language model.<'),
    ('>SCHLIESSEN<', '>CLOSE<'),
    ('>ERINNERUNG<', '>REMINDER<'),
]


def translate_index(html: str, language: str) -> str:
    if language != "en":
        return html
    for de, en in HTML_EN:
        html = html.replace(de, en)
    return html
