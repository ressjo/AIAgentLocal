"""Kommandozeile: jarvis [serve|doctor|model|update|version|reindex|summarize|init-config]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from pathlib import Path

from .config import config_path, load_config
from .lang import T, set_lang

EXAMPLE_CONFIG = Path(__file__).parent / "config.example.yaml"


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    cfg = load_config()
    app = create_app(cfg)
    url = f"http://localhost:{cfg.port}"
    print(T("JARVIS läuft auf ", "JARVIS is running at ") + url)
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info" if args.verbose else "warning")


def cmd_init_config(args) -> None:
    path = config_path()
    if path.exists() and not args.force:
        print(f"{path} " + T("existiert bereits (--force zum Überschreiben).", "already exists (--force to overwrite)."))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE_CONFIG, path)
    print(T("Konfiguration angelegt: ", "Configuration created: ") + str(path))


def cmd_doctor(args) -> None:
    cfg = load_config()

    def line(ok: bool, text: str, hint: str = "") -> None:
        print(f"  {'✔' if ok else '✘'} {text}" + (f"  → {hint}" if hint and not ok else ""))

    from .update import version
    print(f"Version: {version()}")
    print(T("Konfiguration: ", "Configuration: ") + f"{config_path()} ("
          + (T("vorhanden", "found") if config_path().exists() else T("Standardwerte", "defaults")) + ")")
    print(T("Sprache: ", "Language: ") + cfg.language)
    print("LLM:")
    from .llm import OllamaLLM, OpenAICompatLLM
    from .llm_router import LLMRouter
    router = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
    ollama = OllamaLLM(cfg.llm)
    st = asyncio.run(ollama.status())
    line(st["online"], f"Ollama {T('unter', 'at')} {cfg.llm.base_url}", "sudo systemctl enable --now ollama")
    if st["online"]:
        line(st["embed_available"], f"{T('Embedding-Modell', 'Embedding model')} {cfg.llm.embed_model}", f"ollama pull {cfg.llm.embed_model}")
    for name, p in router.profiles.items():
        mark = T(" (aktiv)", " (active)") if name == router.active else ""
        print(f"  {T('Profil', 'Profile')} {name}{mark}: {p.backend} · {p.model} · {p.base_url}")
        if p.backend == "ollama":
            ps = asyncio.run(OllamaLLM(cfg.llm.model_copy(update={"base_url": p.base_url, "model": p.model})).status())
            if ps["online"]:
                line(ps["model_available"], f"    {T('Modell', 'Model')} {p.model}", f"ollama pull {p.model}")
        else:
            ps = asyncio.run(OpenAICompatLLM(p).status())
            if p.server:
                script = os.path.expanduser(p.server.command.split()[0])
                line(os.path.exists(script) or bool(shutil.which(script)), f"    {T('Startbefehl', 'Start command')} {script}",
                     T("Pfad in llm.profiles.<name>.server.command prüfen", "check the path in llm.profiles.<name>.server.command"))
            line(ps["online"], T("    Server erreichbar", "    Server reachable") if ps["online"]
                 else T("    Server läuft gerade nicht", "    Server is not running"),
                 T("startet automatisch beim Aktivieren", "starts automatically when activated") if p.server
                 else T("Server von Hand starten", "start the server manually"))
            if ps["online"]:
                n_ctx = asyncio.run(OpenAICompatLLM(p).server_context())
                if n_ctx:
                    print(f"    ℹ {T('Kontextfenster laut Server', 'Context window reported by server')}: {n_ctx} Token")
    print(T("Sprache:", "Voice:"))
    from .voice.listen import WakeWordFactory, WhisperSTT
    from .voice.tts import PiperTTS
    stt = WhisperSTT(cfg.voice)
    line(stt.available(), T("faster-whisper (Spracherkennung)", "faster-whisper (speech recognition)"), "uv sync --extra voice")
    tts = PiperTTS(cfg.voice)
    line(tts.available(), f"{T('Piper-Stimme', 'Piper voice')} {cfg.voice.tts_voice.name}", tts.error or "")
    wake = WakeWordFactory(cfg.voice)
    line(wake.available(), f"Wake-Word '{cfg.voice.wakeword_model}'", wake.error or "")
    print(T("Desktop (Dateien/Programme öffnen):", "Desktop (opening files/apps):"))
    from .tools.proc import desktop_env
    env = desktop_env()
    session = env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    line(bool(session), f"{T('Grafische Sitzung', 'Graphical session')}: {session or T('nicht gefunden', 'not found')}",
         T("Jarvis aus der Desktop-Sitzung starten (Autostart/Terminal)", "start Jarvis from the desktop session (autostart/terminal)"))
    line(bool(env.get("DBUS_SESSION_BUS_ADDRESS")), T("D-Bus-Sitzung", "D-Bus session"),
         T("Jarvis aus der Desktop-Sitzung starten", "start Jarvis from the desktop session"))
    if shutil.which("xdg-mime"):
        import subprocess
        for mime, label in (("text/plain", T("Texteditor", "Text editor")), ("application/pdf", "PDF"),
                            ("inode/directory", T("Ordner", "Folder"))):
            app = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True,
                                 env=env).stdout.strip()
            line(bool(app), f"{T('Standardprogramm', 'Default app')} {label}: {app or T('keins', 'none')}",
                 f"xdg-mime default <app>.desktop {mime}")
    print(T("Werkzeuge:", "Tools:"))
    arch = bool(shutil.which("pacman"))
    tools = [("rg", "ripgrep", "ripgrep"), ("plocate", "plocate", "plocate"), ("xdg-open", "xdg-utils", "xdg-utils"),
             ("gtk-launch", "gtk3", "libgtk-3-bin"), ("sudo", "sudo", "sudo"), ("ip", "iproute2", "iproute2"),
             ("ping", "iputils", "iputils-ping")]
    tools += [("fd", "fd", "")] if arch else [("fdfind", "", "fd-find")]
    tools += [("checkupdates", "pacman-contrib", "")] if arch else []
    for tool, arch_pkg, deb_pkg in tools:
        line(bool(shutil.which(tool)), tool, f"sudo pacman -S {arch_pkg}" if arch else f"sudo apt install {deb_pkg}")
    if arch:
        print(f"  {'✔' if shutil.which('yay') or shutil.which('paru') else '–'} AUR helper (yay/paru, "
              + T("optional", "optional") + ")")
    if cfg.tools.privilege_cmd == "jarvis":
        from .askpass import helper_path, write_helper
        try:
            write_helper()
            line(True, T("Root-Rechte: Passwortfeld in der Oberfläche", "Root privileges: password field in the web UI")
                 + f" (sudo -A, {helper_path()})")
        except OSError as e:
            line(False, T("Askpass-Helfer anlegen", "Create askpass helper"), str(e))
    else:
        print(f"  ℹ {T('Root-Rechte über', 'Root privileges via')} {cfg.tools.privilege_cmd} "
              + T("(tools.privilege_cmd: jarvis = Passwortfeld im Dashboard)", "(tools.privilege_cmd: jarvis = password field in the dashboard)"))
    print("Trilium:")
    from .tools.trilium import trilium_status
    tr = asyncio.run(trilium_status(cfg))
    if not tr["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (trilium.url, trilium.token)")
    else:
        from .tools.netutil import normalize_url
        line(tr["online"], f"{normalize_url(cfg.trilium.url, '/etapi')}" + (f" (Version {tr.get('version')})" if tr["online"] else ""),
             tr.get("error", ""))
    print("Obsidian:")
    from .tools.obsidian import obsidian_status
    obs = asyncio.run(obsidian_status(cfg))
    if not obs["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (obsidian.vault)")
    else:
        line(obs["online"], f"Vault {obs['vault']}" + (f" – {obs['count']} {T('Notizen', 'notes')}" if obs["online"] else ""),
             obs.get("error", ""))
    proxies = [k for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy") if os.environ.get(k)]
    if proxies and (cfg.trilium.enabled or cfg.paperless.enabled):
        print(f"  ℹ Proxy ({', '.join(proxies)}) – " + T("Heimnetz-Dienste werden bewusst direkt angesprochen", "home network services are contacted directly on purpose"))
    print("Paperless:")
    from .tools.paperless import paperless_status
    ps_ = asyncio.run(paperless_status(cfg))
    if not ps_["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (paperless.url, paperless.token)")
    else:
        line(ps_["online"], f"{ps_.get('url') or cfg.paperless.url}" + (f" – {ps_['count']} {T('Dokumente', 'documents')} (Version {ps_['version']})"
                                                       if ps_["online"] else ""), ps_.get("error", ""))
    print("Home Assistant:")
    from .tools.homeassistant import ha_status
    hs = asyncio.run(ha_status(cfg))
    if not hs["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (homeassistant.url, homeassistant.token)")
    else:
        line(hs["online"], f"{hs.get('url') or cfg.homeassistant.url}" + (
            f" – {hs['entities']} {T('Entitäten', 'entities')} (Version {hs['version']})" if hs["online"] else ""), hs.get("error", ""))
    print(T("Kalender:", "Calendar:"))
    if not cfg.calendar.enabled:
        print(T("  – nicht konfiguriert", "  – not configured") + " (calendar.url, username, password)")
    else:
        from .tools.calendar_tools import calendar_status
        cs = asyncio.run(calendar_status(cfg))
        line(cs["online"], f"{cfg.calendar.url} – " + (", ".join(cs.get("calendars", [])) if cs["online"] else T("Fehler", "error")),
             cs.get("error", ""))
    print(T("Wetter & Erinnerungen:", "Weather & reminders:"))
    if cfg.weather.location:
        from .tools.weather import WeatherError, geocode
        try:
            place = asyncio.run(geocode(cfg.weather.location))
            line(True, f"{T('Wetter-Ort', 'Weather location')}: {place.get('name')} ({place.get('admin1') or place.get('country', '')})")
        except WeatherError as e:
            line(False, f"{T('Wetter-Ort', 'Weather location')} '{cfg.weather.location}'", str(e))
    else:
        print(T("  – kein Standardort (weather.location) – Wetter fragt dann nach dem Ort",
                "  – no default location (weather.location) – weather will ask for a place"))
    line(bool(shutil.which("notify-send")), T("Desktop-Benachrichtigungen (notify-send)", "Desktop notifications (notify-send)"),
         "sudo pacman -S libnotify" if shutil.which("pacman") else "sudo apt install libnotify-bin")
    print("NAS:")
    if not cfg.tools.nas_paths:
        print(T("  – kein NAS-Pfad konfiguriert", "  – no NAS path configured") + " (tools.nas_paths)")
    for p in cfg.tools.nas_paths:
        line(p.exists() and p.is_dir() and any(p.iterdir()), f"{p} " + T("gemountet", "mounted"), T("Mount prüfen", "check the mount"))


def cmd_reindex(args) -> None:
    from .llm import OllamaLLM
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = OllamaLLM(cfg.llm)
        mem = Memory(cfg.memory, llm)
        n = await mem.rebuild_index()
        await llm.close()
        mem.close()
        return n

    print(T("Index neu aufgebaut: ", "Index rebuilt: ") + f"{asyncio.run(run())} " + T("Einträge", "entries"))


def cmd_summarize(args) -> None:
    from .llm_router import LLMRouter
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
        mem = Memory(cfg.memory, llm)
        days = [args.day] if args.day else mem.days_needing_summary(include_today=True)
        for d in days:
            print(T("Fasse zusammen: ", "Summarising: ") + f"{d} …")
            print(await mem.summarize_day(d))
        await llm.close()
        mem.close()

    asyncio.run(run())


def cmd_model(args) -> None:
    """Profile anzeigen bzw. umschalten (bei laufendem Server live, sonst für den nächsten Start)."""
    import httpx

    from .llm_router import LLMRouter

    cfg = load_config()
    state = cfg.memory.dir.parent / "state.json"
    if args.name in ("add", "remove", "choose"):
        return cmd_model_manage(args, cfg, state)
    router = LLMRouter(cfg.llm, state_path=state)
    base = f"http://127.0.0.1:{cfg.port}"
    headers = {"Host": f"localhost:{cfg.port}"}
    if not args.name:
        for info in router.describe():
            star = "▶" if info["active"] else " "
            extra = T(" · startet Server selbst", " · starts its own server") if info["managed"] else ""
            print(f"{star} {info['name']:<12} {info['backend']:<7} {info['model']}  ({info['base_url']}){extra}")
        print(T("\nUmschalten: jarvis model <name>   ·   neues Modell laden: jarvis model add   ·   entfernen: "
                "jarvis model remove <name>\nEigene Profile (z. B. llama-server): ~/.config/jarvis/config.yaml → llm.profiles",
                "\nSwitch: jarvis model <name>   ·   download a new model: jarvis model add   ·   remove: "
                "jarvis model remove <name>\nCustom profiles (e.g. llama-server): ~/.config/jarvis/config.yaml → llm.profiles"))
        return
    if args.name not in router.profiles:
        print(T("Unbekanntes Profil", "Unknown profile") + f" '{args.name}'. " + T("Vorhanden: ", "Available: ")
              + ", ".join(router.profiles))
        sys.exit(1)
    try:
        r = httpx.post(f"{base}/api/models/{args.name}/activate", headers=headers, timeout=600)
        if r.status_code == 200:
            print(f"✔ {T('Aktiv', 'Active')}: {args.name}")
            return
        print(f"✘ {T('Umschalten fehlgeschlagen', 'Switching failed')}: {r.json().get('detail', r.text)}")
        sys.exit(1)
    except httpx.ConnectError:
        router.active = args.name
        router._write_state()
        print(T(f"Jarvis läuft gerade nicht – '{args.name}' wird beim nächsten Start verwendet.",
                f"Jarvis is not running – '{args.name}' will be used on the next start."))


def cmd_model_manage(args, cfg, state: Path) -> None:
    """jarvis model add [ollama-name] · jarvis model remove <name> · jarvis model choose (für den Installer)."""
    import subprocess

    import httpx

    from . import models as mdl

    base = f"http://127.0.0.1:{cfg.port}"
    headers = {"Host": f"localhost:{cfg.port}"}
    if args.name == "choose":
        vram = args.vram if args.vram is not None else mdl.detect_gpu()["vram_gb"]
        if not sys.stdin.isatty():
            tag = mdl.recommend(vram)
        else:
            tag = mdl.choose_interactive(vram, mdl.installed_models(cfg.llm.base_url))
        if args.out:
            Path(args.out).write_text(tag or "", encoding="utf-8")
        else:
            print(tag)
        return
    if args.name == "remove":
        if not args.tag:
            print(T("Welches Modell? jarvis model remove <name>", "Which model? jarvis model remove <name>"))
            sys.exit(1)
        tag = mdl.unregister_model(state, args.tag)
        if not tag:
            print(T(f"'{args.tag}' ist kein per 'jarvis model add' geladenes Modell.",
                    f"'{args.tag}' is not a model added with 'jarvis model add'."))
            sys.exit(1)
        print(T(f"✔ '{tag}' aus der Modellliste entfernt.", f"✔ Removed '{tag}' from the model list."))
        if shutil.which("ollama") and sys.stdin.isatty() and \
                input(T("Auch die Modelldatei löschen (ollama rm)? [j/N] ", "Also delete the model files (ollama rm)? [y/N] ")
                      ).strip().lower() in ("j", "ja", "y", "yes"):
            subprocess.run(["ollama", "rm", tag])
        return
    # add
    gpu = mdl.detect_gpu()
    tag = (args.tag or "").strip().lower() or mdl.choose_interactive(gpu["vram_gb"], mdl.installed_models(cfg.llm.base_url))
    if not tag or not mdl.TAG_RE.match(tag):
        print(T("Ungültiger Modellname.", "Invalid model name."))
        sys.exit(1)
    if not shutil.which("ollama"):
        print(T("ollama ist nicht installiert – erst scripts/install.sh ausführen.",
                "ollama is not installed – run scripts/install.sh first."))
        sys.exit(1)
    print(T(f"Lade {tag} … (das kann dauern)", f"Downloading {tag} … (this can take a while)"))
    if subprocess.run(["ollama", "pull", tag]).returncode != 0:
        print(T(f"✘ '{tag}' konnte nicht geladen werden – Name prüfen (ollama.com/library) oder anderes Modell wählen.",
                f"✘ Could not download '{tag}' – check the name (ollama.com/library) or pick another model."))
        sys.exit(1)
    name = mdl.register_model(state, tag)
    activate = not sys.stdin.isatty() or input(T(f"'{tag}' jetzt aktivieren? [J/n] ", f"Activate '{tag}' now? [Y/n] ")
                                                ).strip().lower() not in ("n", "nein", "no")
    try:
        httpx.post(f"{base}/api/models/reload", headers=headers, timeout=10)
        if activate:
            r = httpx.post(f"{base}/api/models/{name}/activate", headers=headers, timeout=600)
            ok = r.status_code == 200
            print(f"✔ {T('Aktiv', 'Active')}: {name}" if ok else f"✘ {r.json().get('detail', r.text)}")
        else:
            print(T(f"✔ '{name}' steht jetzt im Modell-Menü.", f"✔ '{name}' is now in the model menu."))
    except httpx.ConnectError:
        if activate:
            mdl.register_model(state, tag, activate=True)
        print(T(f"✔ '{name}' gespeichert" + (" und wird beim nächsten Start verwendet." if activate else "."),
                f"✔ '{name}' saved" + (" and will be used on the next start." if activate else ".")))


def cmd_update(args) -> None:
    from .update import main_update
    main_update(load_config().port)


def cmd_version(args) -> None:
    from .update import project_root, version
    print(f"JARVIS {version()}\n{project_root()}")


def main(argv: list[str] | None = None) -> None:
    try:
        set_lang(load_config().language)
    except Exception:  # noqa: BLE001 – kaputte Config: doctor/serve melden das selbst
        pass
    if os.environ.get("JARVIS_LANG"):  # z. B. vom Installer, bevor es eine Config gibt
        set_lang(os.environ["JARVIS_LANG"])
    parser = argparse.ArgumentParser(prog="jarvis", description=T("JARVIS – lokaler KI-Assistent",
                                                                  "JARVIS – local AI assistant"))
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help=T("Server starten (Standard)", "start the server (default)"))
    p.add_argument("--open", action="store_true", help=T("Browser öffnen", "open the browser"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("doctor", help=T("Installation prüfen", "check the installation"))
    p = sub.add_parser("model", help=T("Modelle anzeigen, umschalten, laden (add) oder entfernen (remove)",
                                       "list, switch, download (add) or remove (remove) models"))
    p.add_argument("name", nargs="?", help=T("Profilname zum Umschalten – oder add / remove",
                                             "profile to switch to – or add / remove"))
    p.add_argument("tag", nargs="?", help=T("bei add/remove: Ollama-Modellname", "with add/remove: Ollama model name"))
    p.add_argument("--vram", type=float, help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)
    sub.add_parser("update", help=T("Auf den neuesten Stand bringen (git pull, Abhängigkeiten, Neustart)",
                                    "update (git pull, dependencies, restart)"))
    sub.add_parser("version", help=T("Installierte Version anzeigen", "show the installed version"))
    sub.add_parser("reindex", help=T("Gedächtnis-Suchindex aus den Markdown-Dateien neu aufbauen",
                                     "rebuild the memory search index from the Markdown files"))
    p = sub.add_parser("summarize", help=T("Tageszusammenfassungen erzeugen", "create daily summaries"))
    p.add_argument("day", nargs="?", help=T("YYYY-MM-DD (Standard: alle fälligen Tage)", "YYYY-MM-DD (default: all due days)"))
    p = sub.add_parser("init-config", help=T("Beispielkonfiguration anlegen", "create the example configuration"))
    p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "quic", "niquests", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.cmd is None:
        args = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    {"serve": cmd_serve, "doctor": cmd_doctor, "update": cmd_update, "model": cmd_model, "version": cmd_version,
     "reindex": cmd_reindex, "summarize": cmd_summarize, "init-config": cmd_init_config}[args.cmd](args)


if __name__ == "__main__":
    main()
