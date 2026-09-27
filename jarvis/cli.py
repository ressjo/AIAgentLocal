"""Kommandozeile: jarvis [serve|doctor|reindex|summarize|init-config]"""

from __future__ import annotations

import argparse
import asyncio
import logging
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
    from .llm import OllamaLLM
    llm = OllamaLLM(cfg.llm)
    st = asyncio.run(llm.status())
    line(st["online"], f"Ollama unter {cfg.llm.base_url}", "sudo systemctl enable --now ollama")
    if st["online"]:
        line(st["model_available"], f"Modell {cfg.llm.model}", f"ollama pull {cfg.llm.model}")
        line(st["embed_available"], f"Embedding-Modell {cfg.llm.embed_model}", f"ollama pull {cfg.llm.embed_model}")
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
                      ("gtk-launch", "gtk3"), ("pkexec", "polkit"), ("checkupdates", "pacman-contrib"),
                      ("yay", "yay (AUR, optional)")]:
        line(bool(shutil.which(tool)), tool, f"sudo pacman -S {pkg}" if "optional" not in pkg else pkg)
    print("Trilium:")
    from .tools.trilium import trilium_status
    tr = asyncio.run(trilium_status(cfg))
    if not tr["enabled"]:
        print("  – nicht konfiguriert (trilium.url und trilium.token in der Config)")
    else:
        line(tr["online"], f"{cfg.trilium.url}" + (f" (Version {tr.get('version')})" if tr["online"] else ""),
             tr.get("error", ""))
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
    from .llm import OllamaLLM
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = OllamaLLM(cfg.llm)
        mem = Memory(cfg.memory, llm)
        days = [args.day] if args.day else mem.days_needing_summary(include_today=True)
        for d in days:
            print(f"Fasse {d} zusammen …")
            print(await mem.summarize_day(d))
        await llm.close()
        mem.close()

    asyncio.run(run())


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
    sub.add_parser("update", help="Auf den neuesten Stand bringen (git pull, Abhängigkeiten, Neustart)")
    sub.add_parser("version", help="Installierte Version anzeigen")
    sub.add_parser("reindex", help="Gedächtnis-Suchindex aus den Markdown-Dateien neu aufbauen")
    p = sub.add_parser("summarize", help="Tageszusammenfassungen erzeugen")
    p.add_argument("day", nargs="?", help="YYYY-MM-DD (Standard: alle fälligen Tage)")
    p = sub.add_parser("init-config", help="Beispielkonfiguration anlegen")
    p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd is None:
        args = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    {"serve": cmd_serve, "doctor": cmd_doctor, "update": cmd_update, "version": cmd_version,
     "reindex": cmd_reindex, "summarize": cmd_summarize, "init-config": cmd_init_config}[args.cmd](args)


if __name__ == "__main__":
    main()
