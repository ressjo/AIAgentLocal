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

from ..lang import T
from .registry import BLOCKED, CONFIRM, SAFE
from .secretpaths import contains_secrets, expand_arg, is_secret_path

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", ";;", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
ROOT_WRAPPERS = {"sudo", "doas", "pkexec", "su", "run0"}
TRANSPARENT_WRAPPERS = {"nice", "time", "command", "nohup", "ionice", "stdbuf"}

SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "fd", "locate", "plocate", "which",
    "whereis", "type", "file", "stat", "wc", "sort", "uniq", "cut", "tr", "echo", "printf", "pwd", "date",
    "cal", "uptime", "uname", "hostname", "whoami", "id", "groups", "df", "du", "free", "lsblk", "lscpu",
    "lsusb", "lspci", "lsmod", "findmnt", "ps", "pgrep", "sensors", "ss", "nslookup", "dig", "host",
    "journalctl", "checkupdates", "tree", "basename", "dirname", "realpath", "readlink",
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
    (re.compile(r"--no-preserve-root"), lambda: T("Löschen des Wurzelverzeichnisses", "deleting the root directory")),
    (re.compile(r"(^|[\s;&|(])mkfs(\.\w+)?\b"), lambda: T("Formatieren eines Dateisystems", "formatting a file system")),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(sd|nvme|hd|vd|mmcblk|disk)"),
     lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), lambda: T("Fork-Bombe", "fork bomb")),
    (re.compile(r"\b(wipefs|shred|blkdiscard)\b[^;&|]*/dev/"), lambda: T("Löschen eines Datenträgers", "wiping a disk")),
    (re.compile(r"\bch(mod|own|grp)\b[^;&|]*\s-\w*R\w*\b[^;&|]*\s/(\s|$|\*)"),
     lambda: T("Rechte des ganzen Systems ändern", "changing permissions of the whole system")),
]

# Befehle, die Dateiinhalte ausgeben – auf Schlüssel/Passwort-Dateien angewandt nur mit Rückfrage
READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "less", "more", "zcat", "xxd", "hexdump",
           "strings", "jq", "diff", "cmp", "sort", "uniq", "cut", "column", "tr", "sed", "awk", "base64", "od",
           "nl", "tac", "bat", "batcat", "view", "vim", "vi", "nano", "cp", "scp", "rsync", "tar", "zip", "curl"}
SECRET_VARS = re.compile(r"\$\{?\w*(TOKEN|PASSW|SECRET|API_?KEY|PRIVATE)\w*", re.I)
RECURSIVE_FLAGS = {"-r", "-R", "--recursive", "--dereference-recursive"}

WARN_PATTERNS = [
    (re.compile(r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da|fi)?sh\b"),
     lambda: T("führt ein Skript direkt aus dem Internet aus", "runs a script straight from the internet")),
    (re.compile(r"\b(fdisk|parted|sgdisk|gdisk|cfdisk)\b"), lambda: T("Partitionierungswerkzeug", "partitioning tool")),
    (re.compile(r"\bpacman\b[^;&|]*\s-R\w*d\w*d"),
     lambda: T("entfernt Pakete ohne Abhängigkeitsprüfung", "removes packages without dependency checks")),
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


def _reads_secret(targets: list[str], cwd: str, recursive: bool) -> bool:
    """Trifft eines der Ziele (nach cd, ~, $VAR, {a,b} und Globs wie die Shell) eine Geheimnis-Datei – oder
    durchsucht es rekursiv einen Ordner, in dem welche liegen? Unauflösbare Pfade gelten als verdächtig."""
    for arg in targets:
        paths = expand_arg(arg, cwd)
        if paths is None:
            return True
        for p in paths:
            if is_secret_path(p) or (recursive and contains_secrets(p)):
                return True
    return False


def _prints_secrets(name: str, args: list[str], cwd: str) -> bool:
    if name == "printenv":
        return True
    if name == "nmcli":  # -s / --show-secrets zeigt WLAN- und VPN-Passwörter
        return any(a == "--show-secrets" or (a.startswith("-") and not a.startswith("--") and "s" in a)
                   for a in args)
    if name == "ps":  # BSD-Modifikator „e“ (ps eww, ps auxe) gibt die Umgebung jedes Prozesses aus
        return any(a.isalpha() and "e" in a for a in args if not a.startswith("-"))
    if name == "systemctl":  # Units und Manager-Umgebung können Environment=…TOKEN enthalten
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("cat", "show-environment") or (sub == "show" and not any(
            a in ("-p", "--property") or a.startswith("--property=") or (a.startswith("-p") and len(a) > 2)
            for a in args))
    if name not in READERS:
        return False
    targets = [a for a in args if not a.startswith("-") and a not in REDIRECTS and a != "<"]
    recursive = name == "rg" or (name in ("grep", "egrep", "fgrep") and any(
        a in RECURSIVE_FLAGS or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"})
        for a in args))
    if recursive and len(targets) <= 1:
        targets = [*targets, "."]  # ohne Pfad durchsuchen grep -r und rg das aktuelle Verzeichnis
    return _reads_secret(targets, cwd, recursive)


def classify_command(command: str) -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return BLOCKED, T("leerer Befehl", "empty command")
    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(cmd):
            return BLOCKED, reason()
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return CONFIRM, T("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")

    segments = _segments(tokens)
    reasons: list[str] = []
    secrets = T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)")
    cwd = os.path.expanduser("~")  # run_shell startet im Home
    for seg in segments:
        core, root = _strip_wrappers(seg)
        for i, t in enumerate(seg[:-1]):  # Eingabeumleitung: cat < ~/.ssh/id_rsa
            if t == "<" and _reads_secret([seg[i + 1]], cwd, recursive=False):
                reasons.append(secrets)
        if not core:
            if any(os.path.basename(t) == "env" for t in seg):
                reasons.append(secrets)  # „env“ allein gibt alle Umgebungsvariablen samt Tokens aus
            continue
        name = os.path.basename(core[0])
        args = core[1:]
        if name in ("cd", "pushd"):  # spätere relative Pfade beziehen sich auf den neuen Ordner
            target = next((a for a in args if not a.startswith("-")), "~")
            cwd = os.path.join(cwd, os.path.expanduser(os.path.expandvars(target)))
        if _prints_secrets(name, args, cwd):
            reasons.append(secrets)
        if name == "rm" and _rm_is_catastrophic(args):
            return BLOCKED, T("rekursives Löschen eines Systemverzeichnisses", "recursive deletion of a system directory")
        if root:
            reasons.append(T("benötigt Root-Rechte", "needs root privileges"))
        elif name != "printenv" and not _segment_is_safe(name, [a for a in args if a not in REDIRECTS]):
            reasons.append(T(f"'{name}' kann das System verändern", f"'{name}' can change the system"))

    for i, t in enumerate(tokens):
        if t in REDIRECTS:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target not in ("/dev/null",) and not target.startswith("&"):
                reasons.append(T("schreibt in eine Datei", "writes to a file"))
                break
    if SECRET_VARS.search(cmd) or re.search(r"\benviron\b", cmd):
        reasons.append(secrets)
    if "$(" in cmd or "`" in cmd:
        reasons.append(T("enthält Befehlsersetzung", "contains command substitution"))
    for pattern, reason in WARN_PATTERNS:
        if pattern.search(cmd):
            reasons.append(T("ACHTUNG: ", "WARNING: ") + reason())

    if reasons:
        return CONFIRM, "; ".join(dict.fromkeys(reasons))
    return SAFE, T("nur lesender Befehl", "read-only command")


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
