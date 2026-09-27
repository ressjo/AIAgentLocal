"""Paketverwaltung für Arch/Manjaro: pacman + optional AUR-Helper (yay/paru)."""

from __future__ import annotations

import re
import shutil
from typing import Annotated

from . import proc
from .registry import CONFIRM, SAFE, ToolContext, tool

PKG_RE = re.compile(r"^[a-z0-9@._+-]+$")


def privileged(ctx: ToolContext, argv: list[str]) -> list[str]:
    mode = ctx.cfg.tools.privilege_cmd
    if mode == "sudo":
        return ["sudo", "-n", *argv]
    if mode == "pkexec":
        return ["pkexec", *argv]
    return ["sudo", "-A", *argv]  # "jarvis": Passwortdialog in der Oberfläche


def aur_helper(ctx: ToolContext) -> str | None:
    choice = ctx.cfg.tools.aur_helper
    if choice == "none":
        return None
    if choice in ("yay", "paru"):
        return choice if shutil.which(choice) else None
    return next((h for h in ("yay", "paru") if shutil.which(h)), None)


def helper_cmd(ctx: ToolContext, helper: str, *args: str) -> list[str]:
    # AUR-Helper laufen als normaler Nutzer und holen sich Root-Rechte selbst
    mode = ctx.cfg.tools.privilege_cmd
    if mode == "pkexec":
        priv = ["--sudo", "pkexec"]
    elif mode == "sudo":
        priv = ["--sudoflags", "-n"]
    else:
        priv = ["--sudoflags", "-A"]
    unattended = (["--answerclean", "None", "--answerdiff", "None", "--answeredit", "None"]
                  if helper == "yay" else ["--skipreview"])
    return [helper, *priv, *args, "--noconfirm", *unattended]


def split_names(names: str) -> list[str]:
    return [n for n in re.split(r"[\s,]+", names.strip().lower()) if n]


def _names_risk(verb: str):
    def risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
        pkgs = split_names(args.get("names", ""))
        bad = [p for p in pkgs if not PKG_RE.match(p)]
        if not pkgs or bad:
            return SAFE, "ungültige Paketnamen werden abgelehnt"
        return CONFIRM, f"{verb}: {', '.join(pkgs)}"
    return risk


@tool("Aktualisiert das komplette System (pacman -Syu, danach AUR-Pakete falls yay/paru vorhanden).",
      risk=lambda ctx, a: (CONFIRM, "Systemupdate mit Root-Rechten"))
async def system_update(ctx: ToolContext) -> str:
    limit = ctx.cfg.tools.max_output_chars
    timeout = ctx.cfg.tools.update_timeout
    rc, out = await proc.run(ctx, privileged(ctx, ["pacman", "-Syu", "--noconfirm"]), timeout=timeout)
    result = "pacman -Syu: " + proc.format_result(rc, out, limit // 2)
    helper = aur_helper(ctx)
    if rc == 0 and helper:
        rc2, out2 = await proc.run(ctx, helper_cmd(ctx, helper, "-Sua"), timeout=timeout)
        result += f"\n\n{helper} -Sua: " + proc.format_result(rc2, out2, limit // 2)
    if re.search(r"(upgrading|aktualisiere|Aktualisiere)\s+linux(-lts|-zen|-hardened)?[\s.]", out):
        result += "\n\nHinweis: Der Kernel wurde aktualisiert – ein Neustart wird empfohlen."
    return result


@tool("Listet verfügbare Updates auf, ohne etwas zu installieren.")
async def list_updates(ctx: ToolContext) -> str:
    if shutil.which("checkupdates"):
        rc, out = await proc.run(ctx, ["checkupdates"], timeout=120, stream=False)
        if rc == 2:
            out = "Keine Updates verfügbar."
    else:
        rc, out = await proc.run(ctx, ["pacman", "-Qu"], timeout=60, stream=False)
    helper = aur_helper(ctx)
    if helper:
        _, aur = await proc.run(ctx, [helper, "-Qua"], timeout=120, stream=False)
        if aur.strip():
            out += f"\nAUR:\n{aur}"
    return proc.clip(out.strip() or "Keine Updates verfügbar.", ctx.cfg.tools.max_output_chars)


@tool("Sucht Pakete in den offiziellen Repositories und (falls verfügbar) im AUR.")
async def search_package(ctx: ToolContext, query: Annotated[str, "Suchbegriff"]) -> str:
    rc, out = await proc.run(ctx, ["pacman", "-Ss", query], timeout=60, stream=False)
    result = "Repositories:\n" + ("\n".join(out.splitlines()[:30]) if out.strip() else "(nichts gefunden)")
    helper = aur_helper(ctx)
    if helper:
        _, aur = await proc.run(ctx, [helper, "-Ss", "--aur", query], timeout=60, stream=False)
        result += "\n\nAUR:\n" + ("\n".join(aur.splitlines()[:20]) if aur.strip() else "(nichts gefunden)")
    return result


@tool("Installiert ein oder mehrere Pakete (offizielle Repos, sonst AUR). Paketnamen exakt angeben – "
      "im Zweifel vorher search_package nutzen.", risk=_names_risk("Pakete installieren"))
async def install_package(ctx: ToolContext, names: Annotated[str, "Paketnamen, durch Leerzeichen getrennt"]) -> str:
    pkgs = split_names(names)
    invalid = [p for p in pkgs if not PKG_RE.match(p)]
    if not pkgs or invalid:
        return f"Ungültige Paketnamen: {', '.join(invalid) or '(leer)'}"
    repo, aur, missing = [], [], []
    helper = aur_helper(ctx)
    for p in pkgs:
        rc, _ = await proc.run(ctx, ["pacman", "-Si", p], timeout=30, stream=False)
        if rc == 0:
            repo.append(p)
        elif helper:
            rc, _ = await proc.run(ctx, [helper, "-Si", "--aur", p], timeout=60, stream=False)
            (aur if rc == 0 else missing).append(p)
        else:
            missing.append(p)
    parts = []
    limit = ctx.cfg.tools.max_output_chars
    if repo:
        rc, out = await proc.run(ctx, privileged(ctx, ["pacman", "-S", "--needed", "--noconfirm", *repo]),
                                 timeout=ctx.cfg.tools.update_timeout)
        parts.append(f"pacman -S {' '.join(repo)}: " + proc.format_result(rc, out, limit // 2))
    if aur and helper:
        rc, out = await proc.run(ctx, helper_cmd(ctx, helper, "-S", "--needed", *aur),
                                 timeout=ctx.cfg.tools.update_timeout)
        parts.append(f"{helper} -S {' '.join(aur)}: " + proc.format_result(rc, out, limit // 2))
    if missing:
        parts.append(f"Nicht gefunden: {', '.join(missing)}")
    return "\n\n".join(parts)


@tool("Deinstalliert Pakete inklusive nicht mehr benötigter Abhängigkeiten (pacman -Rns).",
      risk=_names_risk("Pakete entfernen"))
async def remove_package(ctx: ToolContext, names: Annotated[str, "Paketnamen, durch Leerzeichen getrennt"]) -> str:
    pkgs = split_names(names)
    if not pkgs or any(not PKG_RE.match(p) for p in pkgs):
        return "Ungültige Paketnamen."
    rc, out = await proc.run(ctx, privileged(ctx, ["pacman", "-Rns", "--noconfirm", *pkgs]),
                             timeout=ctx.cfg.tools.shell_timeout * 5)
    return proc.format_result(rc, out, ctx.cfg.tools.max_output_chars)
