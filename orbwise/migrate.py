"""Einmaliger Umzug von Installationen aus der Zeit, als das Projekt „Jarvis“ hieß.

Läuft bei jedem Programmstart, tut aber nur etwas, solange alte Ordner/Starter existieren:
- ~/.config/jarvis und ~/.local/share/jarvis → …/orbwise (alter Pfad bleibt als Symlink erhalten)
- Pfade in config.yaml auf die neuen Ordner umschreiben
- Starter (~/.local/bin/jarvis, jarvis-open), Anwendungsmenü- und Autostart-Eintrag auf Orbwise umstellen;
  die alten Befehle bleiben als kleine Weiterleitungen bestehen.
Die Persona „Jarvis“ (assistant_name, Wake-Word) ist davon nicht betroffen.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

OLD, NEW = "jarvis", "orbwise"
REPO = "https://github.com/ressjo/orbwise-linux-agent.git"
SHIM = """#!/usr/bin/env bash
# Früherer Befehlsname – das Projekt heißt jetzt Orbwise. Leitet weiter an {target}.
ORBWISE_VIA_ALIAS=1 exec "$HOME/.local/bin/{target}" "$@"
"""


def _dirs(home: Path) -> list[tuple[Path, Path]]:
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [(config / OLD, config / NEW), (data / OLD, data / NEW)]


def _ours(path: Path, *markers: str) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return any(m in text for m in markers)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def migrate(home: Path | None = None, root: Path | None = None) -> list[str]:
    """Führt den Umzug aus (idempotent) und liefert kurze Meldungen über das, was geändert wurde."""
    home = home or Path.home()
    root = root or Path(__file__).resolve().parents[1]
    done: list[str] = []

    # 1) Konfiguration und Daten
    for old, new in _dirs(home):
        if old.is_dir() and not old.is_symlink():
            if new.exists():
                done.append(f"{old} ✘ {new} existiert schon – alter Ordner bleibt unverändert")
                continue
            shutil.move(str(old), str(new))
            old.symlink_to(new)
            done.append(f"{old} → {new}")
    config = _dirs(home)[0][1] / "config.yaml"
    if config.is_file():
        text = config.read_text(encoding="utf-8")
        fixed = text.replace(f"/.local/share/{OLD}", f"/.local/share/{NEW}").replace(f"/.config/{OLD}", f"/.config/{NEW}")
        if fixed != text:
            config.write_text(fixed, encoding="utf-8")
            done.append(f"{config}: Pfade auf {NEW} umgestellt")

    # 2) Starter und Menüeinträge – nur die, die der Installer angelegt hat
    scripts = root / "scripts"
    local_bin = home / ".local" / "bin"
    old_launcher = local_bin / OLD
    if _ours(old_launcher, "JARVIS_HOME", "JARVIS launcher") and (scripts / f"{NEW}-launcher").is_file():
        launcher = (scripts / f"{NEW}-launcher").read_text()
        launcher = (launcher.replace("@ORBWISE_HOME@", str(root)).replace("@ORBWISE_BRANCH@", "main")
                    .replace("@ORBWISE_REPO@", REPO))
        _write(local_bin / NEW, launcher, 0o755)
        _write(old_launcher, SHIM.format(target=NEW), 0o755)
        done.append(f"{old_launcher} → {local_bin / NEW}")
    old_open = local_bin / f"{OLD}-open"
    if _ours(old_open, "JARVIS", "/.local/bin/jarvis") and (scripts / f"{NEW}-open").is_file():
        _write(local_bin / f"{NEW}-open", (scripts / f"{NEW}-open").read_text(), 0o755)
        _write(old_open, SHIM.format(target=f"{NEW}-open"), 0o755)
    entries = [
        (Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "applications", f"{NEW}.desktop"),
        (Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "autostart", f"{NEW}-autostart.desktop"),
    ]
    for folder, template in entries:
        old_entry = folder / f"{OLD}.desktop"
        if _ours(old_entry, "/.local/bin/jarvis") and (scripts / template).is_file():
            _write(folder / f"{NEW}.desktop", (scripts / template).read_text().replace("@HOME@", str(home)))
            old_entry.unlink()
            done.append(f"{old_entry} → {folder / f'{NEW}.desktop'}")
    return done
