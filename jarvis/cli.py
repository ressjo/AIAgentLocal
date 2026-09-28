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

EXAMPLE_CONFIG = Path(__file__).parent / "config.example.yaml"


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    cfg = load_config()
    app = create_app(cfg)
    url = f"http://localhost:{cfg.port}"
    print(f"JARVIS läuft auf {url}")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info" if args.verbose else "warning")


def cmd_init_config(args) -> None:
    path = config_path()
    if path.exists() and not args.force:
        print(f"{path} existiert bereits (--force zum Überschreiben).")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE_CONFIG, path)
    print(f"Konfiguration angelegt: {path}")


def cmd_doctor(args) -> None:
    cfg = load_config()

    def line(ok: bool, text: str, hint: str = "") -> None:
        print(f"  {'✔' if ok else '✘'} {text}" + (f"  → {hint}" if hint and not ok else ""))

    from .update import version
    print(f"Version: {version()}")
    print(f"Konfiguration: {config_path()} ({'vorhanden' if config_path().exists() else 'Standardwerte'})")
    print("LLM:")
    from .llm import OllamaLLM, OpenAICompatLLM
    from .llm_router import LLMRouter
    router = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
    ollama = OllamaLLM(cfg.llm)
    st = asyncio.run(ollama.status())
    line(st["online"], f"Ollama unter {cfg.llm.base_url}", "sudo systemctl enable --now ollama")
    if st["online"]:
        line(st["embed_available"], f"Embedding-Modell {cfg.llm.embed_model}", f"ollama pull {cfg.llm.embed_model}")
    for name, p in router.profiles.items():
        mark = " (aktiv)" if name == router.active else ""
        print(f"  Profil {name}{mark}: {p.backend} · {p.model} · {p.base_url}")
        if p.backend == "ollama":
            ps = asyncio.run(OllamaLLM(cfg.llm.model_copy(update={"base_url": p.base_url, "model": p.model})).status())
            if ps["online"]:
                line(ps["model_available"], f"    Modell {p.model}", f"ollama pull {p.model}")
        else:
            ps = asyncio.run(OpenAICompatLLM(p).status())
            if p.server:
                script = os.path.expanduser(p.server.command.split()[0])
                line(os.path.exists(script) or bool(shutil.which(script)), f"    Startbefehl {script}",
                     "Pfad in llm.profiles.<name>.server.command prüfen")
            line(ps["online"], "    Server erreichbar" if ps["online"] else "    Server läuft gerade nicht",
                 "startet automatisch beim Aktivieren" if p.server else "Server von Hand starten")
            if ps["online"]:
                n_ctx = asyncio.run(OpenAICompatLLM(p).server_context())
                if n_ctx:
                    print(f"    ℹ Kontextfenster laut Server: {n_ctx} Token")
    print("Sprache:")
    from .voice.listen import WakeWordFactory, WhisperSTT
    from .voice.tts import PiperTTS
    stt = WhisperSTT(cfg.voice)
    line(stt.available(), "faster-whisper (Spracherkennung)", "uv sync --extra voice")
    tts = PiperTTS(cfg.voice)
    line(tts.available(), f"Piper-Stimme {cfg.voice.tts_voice.name}", tts.error or "")
    wake = WakeWordFactory(cfg.voice)
    line(wake.available(), f"Wake-Word '{cfg.voice.wakeword_model}'", wake.error or "")
    print("Desktop (Dateien/Programme öffnen):")
    from .tools.proc import desktop_env
    env = desktop_env()
    session = env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    line(bool(session), f"Grafische Sitzung: {session or 'nicht gefunden'}",
         "Jarvis aus der Desktop-Sitzung starten (Autostart/Terminal)")
    line(bool(env.get("DBUS_SESSION_BUS_ADDRESS")), "D-Bus-Sitzung", "Jarvis aus der Desktop-Sitzung starten")
    if shutil.which("xdg-mime"):
        import subprocess
        for mime, label in (("text/plain", "Texteditor"), ("application/pdf", "PDF"), ("inode/directory", "Ordner")):
            app = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True,
                                 env=env).stdout.strip()
            line(bool(app), f"Standardprogramm {label}: {app or 'keins'}",
                 f"xdg-mime default <programm>.desktop {mime}")
    print("Werkzeuge:")
    for tool, pkg in [("fd", "fd"), ("rg", "ripgrep"), ("plocate", "plocate"), ("xdg-open", "xdg-utils"),
                      ("gtk-launch", "gtk3"), ("sudo", "sudo"), ("pkexec", "polkit"), ("checkupdates", "pacman-contrib"),
                      ("yay", "yay (AUR, optional)")]:
        line(bool(shutil.which(tool)), tool, f"sudo pacman -S {pkg}" if "optional" not in pkg else pkg)
    if cfg.tools.privilege_cmd == "jarvis":
        from .askpass import helper_path, write_helper
        try:
            write_helper()
            line(True, f"Root-Rechte: Passwortfeld in der Oberfläche (sudo -A, Helfer {helper_path()})")
        except OSError as e:
            line(False, "Askpass-Helfer anlegen", str(e))
    else:
        print(f"  ℹ Root-Rechte über {cfg.tools.privilege_cmd} (tools.privilege_cmd: jarvis = Passwortfeld im Dashboard)")
    print("Trilium:")
    from .tools.trilium import trilium_status
    tr = asyncio.run(trilium_status(cfg))
    if not tr["enabled"]:
        print("  – nicht konfiguriert (trilium.url und trilium.token in der Config)")
    else:
        from .tools.netutil import normalize_url
        line(tr["online"], f"{normalize_url(cfg.trilium.url, '/etapi')}" + (f" (Version {tr.get('version')})" if tr["online"] else ""),
             tr.get("error", ""))
    proxies = [k for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy") if os.environ.get(k)]
    if proxies and (cfg.trilium.enabled or cfg.paperless.enabled):
        print(f"  ℹ Proxy gesetzt ({', '.join(proxies)}) – Trilium/Paperless werden bewusst direkt angesprochen")
    print("Paperless:")
    from .tools.paperless import paperless_status
    ps_ = asyncio.run(paperless_status(cfg))
    if not ps_["enabled"]:
        print("  – nicht konfiguriert (paperless.url und paperless.token in der Config)")
    else:
        line(ps_["online"], f"{ps_.get('url') or cfg.paperless.url}" + (f" – {ps_['count']} Dokumente (Version {ps_['version']})"
                                                       if ps_["online"] else ""), ps_.get("error", ""))
    print("Home Assistant:")
    from .tools.homeassistant import ha_status
    hs = asyncio.run(ha_status(cfg))
    if not hs["enabled"]:
        print("  – nicht konfiguriert (homeassistant.url und homeassistant.token in der Config)")
    else:
        line(hs["online"], f"{hs.get('url') or cfg.homeassistant.url}" + (
            f" – {hs['entities']} Entitäten (Version {hs['version']})" if hs["online"] else ""), hs.get("error", ""))
    print("Kalender:")
    if not cfg.calendar.enabled:
        print("  – nicht konfiguriert (calendar.url, username, password)")
    else:
        from .tools.calendar_tools import calendar_status
        cs = asyncio.run(calendar_status(cfg))
        line(cs["online"], f"{cfg.calendar.url} – " + (", ".join(cs.get("calendars", [])) if cs["online"] else "Fehler"),
             cs.get("error", ""))
    print("Wetter & Erinnerungen:")
    if cfg.weather.location:
        from .tools.weather import WeatherError, geocode
        try:
            place = asyncio.run(geocode(cfg.weather.location))
            line(True, f"Wetter-Ort: {place.get('name')} ({place.get('admin1') or place.get('country', '')})")
        except WeatherError as e:
            line(False, f"Wetter-Ort '{cfg.weather.location}'", str(e))
    else:
        print("  – kein Standardort (weather.location) – Wetter fragt dann nach dem Ort")
    line(bool(shutil.which("notify-send")), "Desktop-Benachrichtigungen (notify-send)", "sudo pacman -S libnotify")
    print("NAS:")
    if not cfg.tools.nas_paths:
        print("  – kein NAS-Pfad konfiguriert (tools.nas_paths)")
    for p in cfg.tools.nas_paths:
        line(p.exists() and p.is_dir() and any(p.iterdir()), f"{p} gemountet", "Mount prüfen")


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

    print(f"Index neu aufgebaut: {asyncio.run(run())} Einträge")


def cmd_summarize(args) -> None:
    from .llm_router import LLMRouter
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
        mem = Memory(cfg.memory, llm)
        days = [args.day] if args.day else mem.days_needing_summary(include_today=True)
        for d in days:
            print(f"Fasse {d} zusammen …")
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
    router = LLMRouter(cfg.llm, state_path=state)
    base = f"http://127.0.0.1:{cfg.port}"
    headers = {"Host": f"localhost:{cfg.port}"}
    if not args.name:
        for info in router.describe():
            star = "▶" if info["active"] else " "
            extra = " · startet Server selbst" if info["managed"] else ""
            print(f"{star} {info['name']:<12} {info['backend']:<7} {info['model']}  ({info['base_url']}){extra}")
        print("\nUmschalten: jarvis model <name>   ·   Profile in ~/.config/jarvis/config.yaml unter llm.profiles")
        return
    if args.name not in router.profiles:
        print(f"Unbekanntes Profil '{args.name}'. Vorhanden: {', '.join(router.profiles)}")
        sys.exit(1)
    try:
        r = httpx.post(f"{base}/api/models/{args.name}/activate", headers=headers, timeout=600)
        if r.status_code == 200:
            print(f"✔ Aktiv: {args.name}")
            return
        print(f"✘ Umschalten fehlgeschlagen: {r.json().get('detail', r.text)}")
        sys.exit(1)
    except httpx.ConnectError:
        router.active = args.name
        router._write_state()
        print(f"Jarvis läuft gerade nicht – '{args.name}' wird beim nächsten Start verwendet.")


def cmd_update(args) -> None:
    from .update import main_update
    main_update(load_config().port)


def cmd_version(args) -> None:
    from .update import project_root, version
    print(f"JARVIS {version()}\n{project_root()}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS – lokaler KI-Assistent")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="Server starten (Standard)")
    p.add_argument("--open", action="store_true", help="Browser öffnen")
    p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("doctor", help="Installation prüfen")
    p = sub.add_parser("model", help="Modell-Profile anzeigen oder umschalten")
    p.add_argument("name", nargs="?", help="Profilname zum Umschalten")
    sub.add_parser("update", help="Auf den neuesten Stand bringen (git pull, Abhängigkeiten, Neustart)")
    sub.add_parser("version", help="Installierte Version anzeigen")
    sub.add_parser("reindex", help="Gedächtnis-Suchindex aus den Markdown-Dateien neu aufbauen")
    p = sub.add_parser("summarize", help="Tageszusammenfassungen erzeugen")
    p.add_argument("day", nargs="?", help="YYYY-MM-DD (Standard: alle fälligen Tage)")
    p = sub.add_parser("init-config", help="Beispielkonfiguration anlegen")
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
