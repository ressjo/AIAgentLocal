"""Risikobewertung von Shell-Befehlen.

safe     – nur lesend, wird direkt ausgeführt
confirm  – verändert etwas / unbekannt / Root → Nutzer muss bestätigen
blocked  – zerstörerisch, wird nie ausgeführt
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .registry import BLOCKED, CONFIRM, SAFE

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", ";;", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
ROOT_WRAPPERS = {"sudo", "doas", "pkexec", "su", "run0"}
TRANSPARENT_WRAPPERS = {"nice", "time", "command", "nohup", "ionice", "stdbuf"}

SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "fd", "locate", "plocate", "which",
    "whereis", "type", "file", "stat", "wc", "sort", "uniq", "cut", "tr", "echo", "printf", "pwd", "date",
    "cal", "uptime", "uname", "hostname", "whoami", "id", "groups", "df", "du", "free", "lsblk", "lscpu",
    "lsusb", "lspci", "lsmod", "findmnt", "ps", "pgrep", "sensors", "ss", "nslookup", "dig", "host",
    "journalctl", "checkupdates", "tree", "basename", "dirname", "realpath", "readlink", "printenv",
    "locale", "inxi", "fastfetch", "neofetch", "nvidia-smi", "rocm-smi", "rocminfo", "glxinfo",
    "vulkaninfo", "nproc", "getconf", "md5sum", "sha1sum", "sha256sum", "column", "jq", "diff", "cmp",
    "zcat", "xxd", "hexdump", "strings", "cd", "true", "test", "[", "lsof", "vmstat", "iostat", "w",
    "last", "timedatectl", "hostnamectl", "loginctl", "nmcli", "iwconfig", "xdg-mime", "tldr", "less",
    "more", "wmctrl", "xrandr", "fc-list", "flatpak", "snap", "pactl", "wpctl", "amixer", "lsb_release",
}

CRITICAL_PATHS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "/home", "/etc", "/usr", "/boot", "/var",
                  "/bin", "/sbin", "/lib", "/lib64", "/opt", "/root", "/srv", "/dev", "/proc", "/sys", "/mnt",
                  "*", ".", "./", "./*", "..", "../", str(Path.home())}

BLOCK_PATTERNS = [
    (re.compile(r"--no-preserve-root"), "Löschen des Wurzelverzeichnisses"),
    (re.compile(r"(^|[\s;&|(])mkfs(\.\w+)?\b"), "Formatieren eines Dateisystems"),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(sd|nvme|hd|vd|mmcblk|disk)"), "Überschreiben eines Datenträgers"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), "Überschreiben eines Datenträgers"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "Fork-Bombe"),
    (re.compile(r"\b(wipefs|shred|blkdiscard)\b[^;&|]*/dev/"), "Löschen eines Datenträgers"),
    (re.compile(r"\bch(mod|own|grp)\b[^;&|]*\s-\w*R\w*\b[^;&|]*\s/(\s|$|\*)"), "Rechte des ganzen Systems ändern"),
]

WARN_PATTERNS = [
    (re.compile(r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da|fi)?sh\b"), "führt ein Skript direkt aus dem Internet aus"),
    (re.compile(r"\b(fdisk|parted|sgdisk|gdisk|cfdisk)\b"), "Partitionierungswerkzeug"),
    (re.compile(r"\bpacman\b[^;&|]*\s-R\w*d\w*d"), "entfernt Pakete ohne Abhängigkeitsprüfung"),
]


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _segments(tokens: list[str]) -> list[list[str]]:
    segs, cur = [], []
    for t in tokens:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _strip_wrappers(seg: list[str]) -> tuple[list[str], bool]:
    """Entfernt VAR=wert, env, nice, timeout … und erkennt sudo/pkexec."""
    root = False
    i = 0
    while i < len(seg):
        t = seg[i]
        base = os.path.basename(t)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif base in ROOT_WRAPPERS:
            root = True
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in ("-u", "-g", "--user") else 1
        elif base == "env" or base in TRANSPARENT_WRAPPERS:
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        elif base == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
            i += 1  # Dauer
        else:
            break
    return seg[i:], root


def _rm_is_catastrophic(args: list[str]) -> bool:
    recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "r" in a.lower())
                    for a in args)
    targets = {a.rstrip("/") or "/" for a in args if not a.startswith("-")}
    targets |= {a for a in args if not a.startswith("-")}
    return recursive and bool(targets & CRITICAL_PATHS)


def _segment_is_safe(cmd: str, args: list[str]) -> bool:
    if cmd == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
                       for a in args)
    if cmd == "sed":
        return not any(a.startswith("-i") or a.startswith("--in-place") for a in args)
    if cmd == "top":
        return "-b" in args or any(a.startswith("-b") for a in args)
    if cmd == "ping":
        return any(a.startswith("-c") for a in args)
    if cmd in ("pacman", "yay", "paru"):
        op = next((a for a in args if a.startswith("-")), "")
        if op.startswith("-Q") or op.startswith("--query"):
            return True
        if op.startswith("-S") and len(op) > 2 and set(op[2:]) <= set("silg"):
            return True
        if op.startswith("-F") and "y" not in op:
            return True
        return op in ("--version", "-V")
    if cmd == "systemctl":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("status", "list-units", "list-unit-files", "list-timers", "is-active", "is-enabled",
                       "is-failed", "show", "cat", "list-dependencies") or (not sub and "--failed" in args)
    if cmd == "ip":
        words = [a for a in args if not a.startswith("-")]
        return bool(words) and words[0] in ("a", "addr", "address", "l", "link", "r", "route", "n", "neigh") \
            and not any(w in ("add", "del", "delete", "set", "flush", "change", "replace") for w in words[1:])
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("status", "log", "diff", "show", "ls-files", "blame", "rev-parse")
    if cmd in ("flatpak", "snap"):
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("list", "info", "search", "find")
    if cmd in ("journalctl",):
        return not any(a.startswith("--vacuum") or a in ("--rotate", "--flush") for a in args)
    if cmd in ("pactl", "wpctl", "amixer", "nmcli", "xrandr", "timedatectl", "hostnamectl", "loginctl"):
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("", "status", "list", "info", "show", "get", "inspect", "get-volume", "scontents")
    return cmd in SAFE_COMMANDS


def classify_command(command: str) -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return BLOCKED, "leerer Befehl"
    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(cmd):
            return BLOCKED, reason
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return CONFIRM, "Befehl konnte nicht sicher analysiert werden"

    segments = _segments(tokens)
    reasons: list[str] = []
    for seg in segments:
        core, root = _strip_wrappers(seg)
        if not core:
            continue
        name = os.path.basename(core[0])
        args = core[1:]
        if name == "rm" and _rm_is_catastrophic(args):
            return BLOCKED, "rekursives Löschen eines Systemverzeichnisses"
        if root:
            reasons.append("benötigt Root-Rechte")
        elif not _segment_is_safe(name, [a for a in args if a not in REDIRECTS]):
            reasons.append(f"'{name}' kann das System verändern")

    for i, t in enumerate(tokens):
        if t in REDIRECTS or (t == ">" or t == ">>"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target not in ("/dev/null",) and not target.startswith("&"):
                reasons.append("schreibt in eine Datei")
                break
    if "$(" in cmd or "`" in cmd:
        reasons.append("enthält Befehlsersetzung")
    for pattern, reason in WARN_PATTERNS:
        if pattern.search(cmd):
            reasons.append(f"ACHTUNG: {reason}")

    if reasons:
        return CONFIRM, "; ".join(dict.fromkeys(reasons))
    return SAFE, "nur lesender Befehl"


_SUDO_RE = re.compile(r"(^|[;&|(]\s*|\s)sudo((?:\s+(?:-[ugpCrtUDRTh]\s+[^\s-]\S*|-[A-Za-z]+))*)\s+")
# Optionen mit Wert (z. B. -u root) bleiben erhalten; -n/-S/-A werden durch den gewählten Modus ersetzt
_SUDO_OWN_FLAGS = re.compile(r"\s+-[nSA]+\b")


_PKEXEC_RE = re.compile(r"(^|[;&|(]\s*|\s)pkexec\s+")


def apply_privilege(command: str, privilege_cmd: str) -> str:
    """Ersetzt 'sudo' durch das konfigurierte Werkzeug: 'orbwise' → sudo -A (Passwortdialog in der Oberfläche),
    'pkexec' → grafischer Polkit-Dialog, 'sudo' → sudo -n (nur mit NOPASSWD-Regel)."""
    if privilege_cmd in ("dashboard", "orbwise", "jarvis"):
        command = _PKEXEC_RE.sub(lambda m: f"{m.group(1)}sudo ", command)
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -A{_SUDO_OWN_FLAGS.sub('', m.group(2))} ", command)
    if privilege_cmd == "pkexec":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}pkexec ", command)
    if privilege_cmd == "sudo":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -n ", command)
    return command
