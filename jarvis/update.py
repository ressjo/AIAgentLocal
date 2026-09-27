"""`jarvis update`: neuen Stand per Git holen, Abhängigkeiten aktualisieren, laufenden Server neu starten.

Konfiguration (~/.config/jarvis), Stimmen und Gedächtnis (~/.local/share/jarvis) liegen außerhalb des
Projektordners und werden dabei nicht angefasst.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

Runner = Callable[..., subprocess.CompletedProcess]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str, runner: Runner = subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(root), *args], capture_output=True, text=True)


def version(root: Path | None = None, runner: Runner = subprocess.run) -> str:
    root = root or project_root()
    if not (root / ".git").exists() or not shutil.which("git"):
        return "unbekannt (kein Git-Checkout)"
    r = _git(root, "log", "-1", "--format=%h %s (%cd)", "--date=format:%d.%m.%Y", runner=runner)
    return r.stdout.strip() or "unbekannt"


def server_running(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2,
                         headers={"Host": f"localhost:{port}"}).status_code == 200
    except httpx.HTTPError:
        return False


def restart_server(port: int, root: Path, out: Callable[[str], None] = print) -> None:
    """Beendet laufende `jarvis serve`-Prozesse und startet im Hintergrund neu."""
    pids = subprocess.run(["pgrep", "-f", "jarvis serve"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        if int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    for _ in range(40):
        if not server_running(port):
            break
        time.sleep(0.25)
    launcher = Path.home() / ".local/bin/jarvis"
    cmd = [str(launcher)] if launcher.exists() else [str(root / ".venv/bin/jarvis")]
    log_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "jarvis.log").open("ab") as log:
        subprocess.Popen([*cmd, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(Path.home()))
    for _ in range(60):
        if server_running(port):
            out("✔ Server neu gestartet – Browser-Seite neu laden.")
            return
        time.sleep(0.5)
    out(f"Server startet noch … (Log: {log_dir / 'jarvis.log'})")


def update(root: Path | None = None, port: int = 8765, runner: Runner = subprocess.run,
           restart: Callable[[int, Path], None] | None = None, out: Callable[[str], None] = print) -> int:
    root = root or project_root()
    if not (root / ".git").exists():
        out(f"{root} ist kein Git-Checkout (vermutlich ein ZIP-Download) – automatische Updates gehen so nicht.")
        out("Einmalig umziehen:  bash scripts/bootstrap.sh   (legt ~/jarvis per Git an und installiert)")
        return 1
    if not shutil.which("git"):
        out("git ist nicht installiert:  sudo pacman -S git")
        return 1

    dirty = _git(root, "status", "--porcelain", "--untracked-files=no", runner=runner).stdout.strip()
    if dirty:
        out("Im Projektordner gibt es lokale Änderungen – ich überschreibe nichts:")
        out(dirty)
        out(f"Sichern mit:  git -C {root} stash   (danach erneut jarvis update)")
        return 1

    before = _git(root, "rev-parse", "HEAD", runner=runner).stdout.strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", runner=runner).stdout.strip() or "HEAD"
    out(f"Aktueller Stand: {version(root, runner)}")
    out(f"Hole Updates ({branch}) …")
    pulled = _git(root, "pull", "--ff-only", "origin", branch, runner=runner)
    if pulled.returncode != 0:
        out("Update fehlgeschlagen:")
        out((pulled.stderr or pulled.stdout).strip())
        return 1
    after = _git(root, "rev-parse", "HEAD", runner=runner).stdout.strip()

    venv_ok = (root / ".venv/bin/jarvis").exists()
    if before == after:
        out("✔ Bereits auf dem neuesten Stand.")
        if venv_ok:
            return 0
    else:
        log = _git(root, "log", "--format=  • %s", f"{before}..{after}", runner=runner).stdout.rstrip()
        out("Neu:")
        out(log or "  (keine Beschreibung)")

    uv = shutil.which("uv")
    if not uv:
        out("uv fehlt – bitte scripts/install.sh ausführen.")
        return 1
    out("Aktualisiere Python-Abhängigkeiten …")
    synced = runner([uv, "sync", "--extra", "voice", "--quiet"], cwd=str(root))
    if synced.returncode != 0:
        out("uv sync ist fehlgeschlagen – Details siehe oben.")
        return 1
    out(f"✔ Aktualisiert auf: {version(root, runner)}")

    if server_running(port):
        out("Starte laufenden JARVIS-Server neu …")
        (restart or restart_server)(port, root)
    else:
        out("Starten mit:  jarvis serve --open")
    return 0


def main_update(port: int) -> None:
    sys.exit(update(port=port))
