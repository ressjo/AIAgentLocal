"""Erkennt Dateien mit Schlüsseln, Passwörtern und Zugangsdaten.

Solche Dateien liest bzw. verschickt Orbwise nie ohne Weiteres – eine präparierte Mail oder Webseite könnte das
Modell sonst dazu bringen, sie auszulesen und (z. B. per Telegram) nach draußen zu geben. Geprüft wird der
aufgelöste Pfad, ein Symlink wie ~/notiz.txt → ~/.ssh/id_ed25519 hilft also nicht.
"""

from __future__ import annotations

import os
from pathlib import Path

# Verzeichnisse, deren gesamter Inhalt geheim ist – im eigenen Home und überall sonst (Backups, andere Nutzer)
SECRET_DIRS = (
    ".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".aws", ".kube", ".azure", ".config/gcloud",
    ".mozilla", ".thunderbird", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
    ".config/vivaldi", ".config/microsoft-edge", ".config/opera", ".config/protonmail", ".config/Proton Mail",
    ".config/Signal", ".config/orbwise", ".docker", ".config/gh", ".config/rclone",
)
# Einzelne Dateinamen (egal wo)
SECRET_NAMES = {
    ".netrc", ".pgpass", ".git-credentials", ".env", ".my.cnf", ".pypirc", ".npmrc", "credentials",
    "credentials.json", "secrets.yaml", "secrets.yml", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", "shadow", "gshadow", "shadow-", "gshadow-", "sudoers",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".ovpn")
SYSTEM_SECRETS = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh", "/etc/NetworkManager/system-connections",
                  "/etc/wpa_supplicant", "/proc/self/environ")


def _extra_dirs() -> list[Path]:
    """Orbwise-Konfiguration (Tokens, Passwörter) – auch wenn sie per XDG/ORBWISE_CONFIG woanders liegt."""
    from ..config import CONFIG_DIR, config_path
    out = [CONFIG_DIR]
    try:
        out.append(config_path())
    except Exception:  # noqa: BLE001
        pass
    return out


def _within(path: Path, base: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except ValueError:
        return False


def is_secret_path(path: str | os.PathLike) -> bool:
    raw = str(path).strip()
    if not raw:
        return False
    p = Path(os.path.expandvars(raw)).expanduser()  # auch $HOME/.ssh/…
    candidates = {p.absolute()}
    try:
        candidates.add(p.resolve())  # Symlinks auflösen
    except (OSError, RuntimeError):
        pass
    for c in candidates:
        name = c.name.lower()
        if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.startswith((".env.", "id_rsa", "id_ed25519")):
            return True
        text = c.as_posix().lower() + "/"
        if any(f"/{d.lower()}/" in text for d in SECRET_DIRS):
            return True
        if any(_within(c, Path(s)) for s in SYSTEM_SECRETS):
            return True
        if c.parts[:2] == ("/", "proc") and name == "environ":
            return True
        for extra in _extra_dirs():
            try:
                if _within(c, extra.expanduser().resolve()):
                    return True
            except (OSError, RuntimeError):
                continue
    return False


SECRET_REASON = "Schlüssel, Passwörter und Zugangsdaten gibt Orbwise nicht heraus"
