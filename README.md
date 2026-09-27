# J.A.R.V.I.S. – lokaler KI-Sprachassistent für Linux

Ein Jarvis-artiger Assistent, der **komplett lokal** auf deinem Linux-PC läuft:
animierte HUD-Weboberfläche, Spracheingabe per Wake-Word („Hey Jarvis“) oder Push-to-talk,
Sprachausgabe, lokales LLM (Qwen über Ollama), **persistentes Gedächtnis in Tagesdateien** und
echte Systemsteuerung – Updates, Pakete, Dateien, NAS, Programme, Websuche, Shell-Befehle.

```
Browser (localhost:8765)                          Python-Backend (FastAPI, nur 127.0.0.1)
 ├─ Arc-Reactor-Orb (Canvas)            ◄──WS──►  Agent ── Tool-Schleife ──► 21 Tools
 ├─ Chat · Aktivität · Gedächtnis                  │
 ├─ Mikrofon → 16 kHz PCM               ──WS──►   Wake-Word (openWakeWord) → VAD → Whisper
 └─ Wiedergabe + Pegel → Orb            ◄──────   Piper-TTS (satzweise, deutsche Stimme)
                                                   │
                                     Ollama (qwen3 + bge-m3)   ~/.local/share/jarvis/memory/
```

## Funktionen

| Bereich | Was Jarvis kann |
|---|---|
| **Sprechen & Zuhören** | „Hey Jarvis“ (Wake-Word), Mikrofon-Taste bzw. **Leertaste halten** (Push-to-talk), kurz tippen = zuhören bis Stille. Antworten werden Satz für Satz vorgelesen, während das LLM noch schreibt. Unterbrechen jederzeit möglich. |
| **System** | Systemupdate (`pacman -Syu` + AUR via yay/paru), verfügbare Updates anzeigen, Pakete suchen/installieren/entfernen, Systeminfos (CPU, GPU, RAM, Speicher) |
| **Dateien** | Dateien nach Name finden (plocate/fd), Inhalte durchsuchen (ripgrep), Ordner auflisten, Textdateien lesen/schreiben, Dateien & URLs öffnen (`xdg-open`) |
| **NAS** | Durchsucht gemountete NAS-Pfade nach Namen oder Inhalt |
| **Programme** | Startet installierte Anwendungen über ihre `.desktop`-Einträge („öffne Firefox“, „starte den Dateimanager“) |
| **Web** | Websuche (DuckDuckGo oder eigene SearXNG-Instanz) und Abruf/Extraktion von Webseiten |
| **Shell** | Beliebige Bash-Befehle – lesende laufen sofort, verändernde nur nach Bestätigung, zerstörerische nie |
| **Trilium** | Notizen durchsuchen und vorlesen, neue Notizen in der Inbox anlegen, an Notizen anhängen (siehe unten) |
| **Gedächtnis** | Merkt sich alles dauerhaft (siehe unten), `remember` / `recall` / `forget` |

## Installation (Arch / Manjaro / EndeavourOS)

```bash
sudo pacman -S --needed git
git clone -b claude/epic-volta-vyvxlk https://github.com/ressjo/AIAgentLocal.git ~/jarvis
~/jarvis/scripts/install.sh     # Optionen: --model qwen3:8b · --vulkan · --cpu · --no-autostart
```

> Bitte per **Git** installieren, nicht als ZIP – dann gehen Updates mit einem Befehl (`jarvis update`).
> Startest du `install.sh` doch aus einem ZIP-Ordner, bietet es an, nach `~/jarvis` umzuziehen.

Das Skript
1. installiert `ollama-rocm`, `uv`, `fd`, `ripgrep`, `plocate`, `xdg-utils`, `polkit`, `pacman-contrib`,
2. richtet die Python-Umgebung ein (`uv sync --extra voice`, Python 3.12),
3. wählt das Modell passend zum Grafikspeicher (≥15 GB → `qwen3:14b`, ≥7 GB → `qwen3:8b`, sonst `qwen3:4b`) und lädt es plus `bge-m3` (Embeddings),
4. lädt die deutsche Piper-Stimme *Thorsten*, das Wake-Word-Modell und Whisper,
5. legt `~/.config/jarvis/config.yaml` an, einen Starter „JARVIS“ im Anwendungsmenü und einen Autostart.

Danach:

```bash
jarvis doctor        # prüft Ollama, Modelle, Stimme, Wake-Word, Werkzeuge, NAS
jarvis serve --open  # startet den Server und öffnet http://localhost:8765
```

Oder im Anwendungsmenü **JARVIS** anklicken – öffnet die Oberfläche als eigenes App-Fenster.

> **Wichtig:** Trage dein NAS in `~/.config/jarvis/config.yaml` ein:
> ```yaml
> tools:
>   nas_paths: [/mnt/nas]
> ```

## Aktualisieren

```bash
jarvis update
```

holt den neuesten Stand (`git pull`), aktualisiert die Python-Abhängigkeiten und startet einen laufenden
Jarvis-Server automatisch neu – danach nur die Browser-Seite neu laden. `jarvis version` zeigt den installierten Stand.

Deine **Konfiguration** (`~/.config/jarvis/`), **Stimmen** und das **Gedächtnis** (`~/.local/share/jarvis/`)
liegen außerhalb des Projektordners und bleiben bei Updates immer erhalten.

Der Befehl `jarvis` (`~/.local/bin/jarvis`) ist ein kleiner Starter, der auf den Projektordner zeigt. Fehlt
dort die Python-Umgebung (z. B. nach einem neuen Download), richtet er sie beim nächsten Aufruf selbst neu ein.

## Bedienung

- **Start:** Beim Öffnen „SYSTEM STARTEN“ klicken (Browser erlauben Audio/Mikrofon erst nach einem Klick).
- **WAKE** aktivieren → „Hey Jarvis, …“ sagen. Das Mikrofon wird dabei nur lokal an den eigenen Server gestreamt.
- **Mikrofon-Taste / Leertaste halten** → sprechen → loslassen.
- **TON** schaltet die Sprachausgabe, **STOP** (oder `Esc`) bricht die aktuelle Aufgabe ab.
- Reiter **GEDÄCHTNIS** zeigt gespeicherte Fakten und alle Tage (Zusammenfassung + Protokoll).

Beispiele:

- „Hey Jarvis, mach ein Systemupdate.“ → Bestätigungsdialog → Live-Ausgabe im Aktivitäts-Panel
- „Installier mir bitte htop und neofetch.“
- „Such die neueste PDF mit Rechnung im Namen auf dem NAS und öffne sie.“
- „Welche Dateien in Downloads sind größer als ein Gigabyte?“
- „Öffne Steam.“ · „Wie viel Speicher ist noch frei?“
- „Such im Internet nach den Neuerungen in Kernel 6.18.“
- „Merk dir, dass mein Server unter 192.168.1.10 erreichbar ist.“
- „Was haben wir letzten Dienstag gemacht?“

## Das Gedächtnis – ohne Context-Window-Probleme

Alles liegt als lesbare Dateien in `~/.local/share/jarvis/memory/`:

| Datei | Inhalt |
|---|---|
| `journal/2026-09-27.md` | Vollständiges Protokoll des Tages – jede Frage, jede Antwort, jeder Tool-Aufruf |
| `summaries/2026-09-27.md` | Tageszusammenfassung, automatisch erzeugt (Tageswechsel bzw. 15 min Leerlauf) |
| `facts.md` | Dauerhafte Fakten („Das NAS ist unter /mnt/nas gemountet“) – auch von Hand editierbar |
| `session.json` | Aktueller Gesprächsverlauf + laufende Zusammenfassung (überlebt Neustarts) |
| `index.sqlite` | Suchindex (Volltext + Embeddings) – nur Cache, mit `jarvis reindex` neu aufbaubar |

**So bleibt der Kontext klein:** Jeder Prompt hat ein festes Budget (Standard 11 000 Tokens) aus
System-Prompt, Fakten, den **relevantesten Erinnerungen** (hybride Suche: BM25-Volltext + bge-m3-Embeddings,
leichte Bevorzugung aktueller Einträge), der laufenden Zusammenfassung und den letzten Nachrichten.
Wird der Verlauf zu lang, faltet Jarvis die ältesten Nachrichten per LLM in die laufende Zusammenfassung –
im Journal und im Index bleiben sie vollständig erhalten und werden bei Bedarf wieder hervorgeholt.
So kann Jarvis sich über Monate „an alles erinnern“, ohne dass das Kontextfenster je überläuft.

## Alltag: Websites, Wetter, Erinnerungen, Briefing

**Websites** – „Öffne YouTube“, „Such auf Amazon nach 120-mm-Lüftern“, „Öffne heise.de“.
Bekannt sind u. a. YouTube, Google, Wikipedia, Amazon, eBay, Kleinanzeigen, idealo, GitHub, Reddit, Google Maps,
Netflix, Twitch, Spotify, Chefkoch, LEO, DeepL, Arch-Wiki und AUR. Eigene Seiten in der Config:

```yaml
websites:
  nas: http://192.168.1.10:5000          # „Öffne das NAS“
  geizhals: https://geizhals.de/?fs={q}  # {q} = Suchbegriff
```

**Wetter** (Open-Meteo, kostenlos, ohne Anmeldung) – „Wie wird das Wetter morgen?“, „Regnet es am Wochenende
in Berlin?“. Standardort in der Config: `weather: {location: "Freiburg im Breisgau"}`.

**Erinnerungen & Timer** – „Erinnere mich in 20 Minuten an die Pizza“, „Stell einen Timer auf 10 Minuten“,
„Erinnere mich morgen um 9 an den Zahnarzt“, „Welche Erinnerungen habe ich?“, „Lösch die Zahnarzt-Erinnerung“.
Zur fälligen Zeit gibt es eine Sprachansage, ein Banner mit Gong in der Oberfläche und eine Desktop-Benachrichtigung
(auch wenn die Oberfläche geschlossen ist). Verpasste Erinnerungen (PC war aus) werden beim nächsten Start gemeldet.
Anstehende Einträge stehen im Reiter **GEDÄCHTNIS** und lassen sich dort löschen.

**Morgen-Briefing** – „Guten Morgen, Jarvis“ oder „Was steht heute an?“: Datum, Wetter, heutige Erinnerungen,
verfügbare Systemupdates und Warnungen zu knappem Speicherplatz bzw. nicht gemountetem NAS.

*Geplant:* Kalender (iCloud per CalDAV – Termine direkt mit dem iPhone) und Spotify-Steuerung.

## Telemetrie

Oben über dem Orb zeigt eine HUD-Leiste live (alle 2 s, mit Verlaufskurve):

| Kachel | Quelle |
|---|---|
| **TOK/S** | Live geschätzt während Jarvis schreibt, danach der exakte Wert von Ollama (+ Prompt-Verarbeitung) |
| **GPU** | Auslastung in % und Temperatur |
| **VRAM** | Belegter / gesamter Grafikspeicher |
| **RAM** | Belegter / gesamter Arbeitsspeicher, dazu CPU-Last |
| **LEISTUNG** | Leistungsaufnahme der Grafikkarte in Watt |

NVIDIA wird über `nvidia-smi` gelesen, AMD direkt über den `amdgpu`-Treiber (sysfs) – es sind keine
Zusatzprogramme nötig. Ab 80 % färben sich die Balken orange, ab 95 % rot.

## Stimme & Jarvis-Effekt

Im Reiter **STIMME** der Oberfläche:

- **Deutsche Stimmen** per Klick installieren, **anhören** und **auswählen**: Thorsten (hoch/mittel/ruhig),
  Pavoque (tiefer, sehr butlerhaft), Karlsson sowie zwei Frauenstimmen. Sie landen in
  `~/.local/share/jarvis/voices/` – eigene Piper-Stimmen (`.onnx` + `.onnx.json`) dort ablegen, dann erscheinen sie ebenfalls.
- **Jarvis-Effekt** (an/aus + Stärke): etwas tiefere, sonore Stimme, leichter Raumhall, dezenter Chorus und
  „digitaler“ Schimmer. Das Sprechtempo bleibt gleich – Jarvis synthetisiert passend schneller.

Die Originalstimme aus den Filmen (bzw. der deutschen Synchronfassung) ist nicht enthalten: Stimme und Aufnahmen
gehören den Sprechern bzw. dem Studio.

## Trilium-Notizen

Jarvis kann auf deine [Trilium](https://github.com/TriliumNext/Trilium)-Notizen zugreifen (lokal oder auf dem NAS):

1. In Trilium: **Optionen → ETAPI → „Neuen ETAPI-Token erstellen“**, Token kopieren.
2. In `~/.config/jarvis/config.yaml` eintragen:
   ```yaml
   trilium:
     url: http://localhost:8080      # bzw. http://nas.local:8080
     token: "dein-etapi-token"       # oder Umgebungsvariable JARVIS_TRILIUM_TOKEN
   ```
3. `jarvis doctor` zeigt „✔ … (Version x.y)“, danach Jarvis neu starten.

| Tool | Was passiert | Bestätigung |
|---|---|---|
| `trilium_search` | Volltextsuche (neueste zuerst) mit Vorschau; Triliums Suchsyntax wie `#label` geht auch | – |
| `trilium_read` | Liest eine Notiz komplett (per Titel oder ID) | – |
| `trilium_create_note` | Neue Notiz, standardmäßig in der **Inbox**, auf Wunsch unter einer genannten Notiz. Markdown (`##`, Listen, `- [ ]`-Aufgaben, Codeblöcke) wird zu Trilium-Formatierung | – |
| `trilium_append` | Hängt Text an eine bestehende Notiz an | – |
| `trilium_update_note` | Ersetzt den Inhalt (und optional den Titel) | **ja** |

Beispiele: „Was steht in meiner Notiz über Docker?“ · „Notier dir: Router-Passwort liegt im Tresor.“ ·
„Häng an die Einkaufsliste Milch und Eier an.“ · „Leg eine Notiz ‚Server-Wartung‘ mit den Befehlen von eben an.“

Die Trilium-Tools werden dem Sprachmodell nur angeboten, wenn `url` und Token gesetzt sind.

## Sicherheit

Jarvis kann Befehle auf deinem System ausführen – deshalb:

- **Bestätigungspflicht:** Alles, was etwas verändert (Installationen, Updates, `rm`, Schreiben in Dateien,
  `sudo`, unbekannte Programme, Befehlsersetzung `$(…)`), erscheint als Dialog mit dem exakten Befehl.
  Bestätigen per Klick, `Enter`/`Esc` oder per Stimme („Ja“ / „Nein“).
- **Nur lesende Befehle** (`ls`, `df`, `pacman -Q…`, `systemctl status`, `grep`, …) laufen direkt.
- **Blockliste:** `rm -rf /` bzw. `~`/`*`, `mkfs`, `dd` auf Datenträger, Fork-Bomben, `chmod -R … /` usw.
  werden nie ausgeführt – auch nicht nach Bestätigung.
- **Nur lokal:** Der Server lauscht ausschließlich auf `127.0.0.1`, prüft den `Host`-Header (Schutz vor
  DNS-Rebinding) und die `Origin` jeder WebSocket-Verbindung – fremde Webseiten können Jarvis keine Befehle schicken.

### Root-Rechte

Standard ist `privilege_cmd: pkexec`: Bei Root-Aktionen erscheint der grafische Passwortdialog deines
Desktops (KDE/GNOME/…). Dafür muss Jarvis **innerhalb der Desktop-Sitzung** laufen – das erledigt der
Autostart des Installationsskripts.

Alternativ ohne Passwortdialog (z. B. als systemd-Dienst, `scripts/jarvis.service`):

```bash
# config.yaml → tools.privilege_cmd: sudo
sudo visudo -f /etc/sudoers.d/jarvis
#   DEINNAME ALL=(root) NOPASSWD: /usr/bin/pacman
```

> Hinweis: NOPASSWD für pacman bedeutet praktisch Root-Zugriff für deinen Benutzer. Die
> Bestätigungsdialoge von Jarvis bleiben trotzdem aktiv.

## AMD-GPU

- `ollama-rocm` nutzt ROCm. Prüfen mit `ollama ps` während einer Anfrage – dort sollte `100% GPU` stehen.
- Wird deine Karte nicht offiziell unterstützt (z. B. RX 6600/6700 oder RX 7600), hilft meist ein Override:
  ```bash
  sudo systemctl edit ollama
  # [Service]
  # Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"   # RDNA2 (RX 6000)
  # Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"   # RDNA3 (RX 7000)
  sudo systemctl restart ollama
  ```
- Klappt ROCm gar nicht: `./scripts/install.sh --vulkan` (Vulkan-Backend).
- Whisper (faster-whisper/CTranslate2) läuft auf AMD über die **CPU** – mit `small`/`int8` dauert ein
  Satz typischerweise 1–2 Sekunden. Zu langsam? `voice.stt_model: base`. Genauer? `medium`.

## Konfiguration

Alle Optionen mit Erklärung: [`jarvis/config.example.yaml`](jarvis/config.example.yaml)
(aktiv: `~/.config/jarvis/config.yaml`). Die wichtigsten:

| Option | Bedeutung |
|---|---|
| `llm.model` | Ollama-Modell, z. B. `qwen3:14b`, `qwen3:8b`, `qwen2.5:14b` |
| `llm.think` | Qwen3-Denkmodus (langsamer, bei kniffligen Aufgaben genauer) |
| `memory.context_budget_tokens` | Maximale Prompt-Größe – muss unter `llm.num_ctx` liegen |
| `voice.stt_model` | Whisper-Größe: `base`, `small`, `medium`, `large-v3` |
| `voice.wakeword_threshold` | Empfindlichkeit des Wake-Words (niedriger = empfindlicher) |
| `tools.nas_paths` | Liste der gemounteten NAS-Verzeichnisse |
| `tools.privilege_cmd` | `pkexec` oder `sudo` |
| `user_name`, `persona_extra` | Anrede und zusätzliche Persönlichkeit |

## Kommandos

```bash
jarvis serve [--open] [-v]   # Server starten
jarvis doctor                # Installation prüfen
jarvis reindex               # Suchindex aus den Markdown-Dateien neu aufbauen
jarvis summarize [YYYY-MM-DD]# Tageszusammenfassungen erzeugen
jarvis init-config           # Beispielkonfiguration anlegen
```

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| `jarvis: Kommando nicht gefunden` | Neues Terminal öffnen (PATH wurde ergänzt) oder `~/jarvis/scripts/install.sh` erneut ausführen |
| Orb bleibt „OFFLINE“ | Läuft `jarvis serve`? Log: `~/.local/state/jarvis.log` |
| „Ollama nicht erreichbar“ | `sudo systemctl enable --now ollama` |
| Keine Sprachausgabe (Piper) | `jarvis doctor` → Stimme fehlt? Dann spricht der Browser als Ersatz |
| Mikrofon geht nicht | Seite über `http://localhost:8765` öffnen (nicht über die IP), Mikrofonrechte im Browser prüfen |
| Wake-Word löst zu oft/selten aus | `voice.wakeword_threshold` anpassen (0.3–0.7) |
| `pkexec`: „Not authorized“ | Jarvis läuft außerhalb der Desktop-Sitzung → Autostart nutzen oder `privilege_cmd: sudo` |
| Datei/Programm öffnet sich nicht | `jarvis doctor` → Abschnitt „Desktop“: grafische Sitzung + Standardprogramme prüfen. Fehlt ein Standardprogramm: `xdg-mime default org.kde.kate.desktop text/plain`. Oder direkt sagen: „öffne X mit Kate“ |
| Dateisuche findet neue Dateien nicht | `sudo updatedb` (plocate-Index wird täglich aktualisiert) |

## Entwicklung

```bash
uv sync --extra voice --extra dev
uv run pytest                                   # Tests (Safety, Gedächtnis, Agent, Tools, LLM-Client)
JARVIS_FAKE_LLM=1 uv run jarvis serve --open    # UI-Demo ohne Ollama ("/tool <name> <json>" ruft Tools direkt)
```

Projektstruktur:

```
jarvis/
  agent.py            Kontextaufbau, Tool-Schleife, Bestätigungen
  llm.py              Ollama-Client (Streaming, Tool-Calls, Embeddings) + FakeLLM
  server.py           FastAPI, WebSocket, REST, Sprachausgabe-Verteilung
  memory/             Journal/Fakten/Zusammenfassungen, SQLite-Index, Kontextbudget
  tools/              registry, safety (Befehlsbewertung), shell, packages, files, apps, web, system
  voice/              listen (Wake-Word, VAD, Whisper), tts (Piper, Satz-Streaming)
  web/                index.html, style.css, orb.js (Animation), app.js, mic-worklet.js
scripts/              install.sh, jarvis-open, Desktop-Einträge, systemd-Unit
```

Neue Tools hinzufügen: Funktion in `jarvis/tools/` mit `@tool("Beschreibung", risk=...)` dekorieren –
das JSON-Schema für das LLM wird automatisch aus den `Annotated`-Typen erzeugt.
