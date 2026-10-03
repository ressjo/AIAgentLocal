"""Systeminformationen ohne externe Abhängigkeiten (liest /proc)."""

from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Annotated

from . import proc
from .registry import ToolContext, tool


def _meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            out[key] = int(val.split()[0]) * 1024
    except OSError:
        pass
    return out


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unbekannt"


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()


@tool("Zeigt Systeminformationen: Betriebssystem, Kernel, CPU, GPU, RAM, Auslastung, Speicherplatz, Laufzeit.")
async def system_info(ctx: ToolContext) -> str:
    lines = [f"System: {_os_name()} (Kernel {platform.release()})", f"Rechner: {platform.node()}",
             f"CPU: {_cpu_model()} ({os.cpu_count()} Threads)"]
    if shutil.which("lspci"):
        _, out = await proc.run(ctx, ["lspci"], timeout=10, stream=False)
        gpus = [ln.split(": ", 1)[-1] for ln in out.splitlines() if "VGA" in ln or "3D controller" in ln or "Display" in ln]
        if gpus:
            lines.append("GPU: " + "; ".join(gpus))
    try:
        load = os.getloadavg()
        lines.append(f"Last (1/5/15 min): {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}")
    except OSError:
        pass
    mem = _meminfo()
    if mem:
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        lines.append(f"RAM: {_gb(total - avail)} von {_gb(total)} belegt")
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        lines.append(f"Laufzeit: {int(secs // 86400)} Tage, {int(secs % 86400 // 3600)} Std., {int(secs % 3600 // 60)} Min.")
    except OSError:
        pass
    mounts = [Path("/"), Path.home(), *ctx.cfg.tools.nas_paths]
    seen = set()
    for m in mounts:
        try:
            du = shutil.disk_usage(m)
        except OSError:
            continue
        key = (du.total, du.used)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"Speicher {m}: {_gb(du.used)} von {_gb(du.total)} belegt, {_gb(du.free)} frei")
    return "\n".join(lines)


# ---------------------------------------------------------------- Datum, Wochentage, Feiertage
def _day_text(cfg, d) -> str:
    from datetime import datetime as _dt

    from ..prompts import format_date
    return format_date(cfg, _dt.combine(d, _dt.min.time()))[0]


def _relative(days: int) -> str:
    if days == 0:
        return "heute"
    if days == 1:
        return "morgen"
    if days == -1:
        return "gestern"
    return f"in {days} Tagen" if days > 0 else f"vor {-days} Tagen"


@tool("Rechnet Datumsangaben aus: Wochentag, „nächsten Dienstag“, „in 3 Wochen“, „KW 12“, Abstand zweier Daten, "
      "gesetzliche Feiertage („Feiertage im Mai“).")
async def date_info(
    ctx: ToolContext,
    question: Annotated[str, "Datumsangabe oder Frage, z. B. '14. März', 'vom 3.10. bis Weihnachten'"],
) -> str:
    from datetime import date

    from ..dates import MONTHS, holidays, resolve
    today = date.today()
    region = getattr(ctx.cfg, "holiday_region", "") if ctx.cfg else ""
    q = question.lower()
    if re.search(r"feiertag|holiday", q):
        year_m = re.search(r"\b(20\d\d)\b", q)
        month = next((n for name, n in MONTHS.items() if re.search(rf"\b{name}\b", q)), None)
        year = int(year_m.group(1)) if year_m else today.year
        days = holidays(year, region) | holidays(year + 1, region)
        if month:
            if not year_m and date(year, month, 28) < today:
                year += 1
            hits = [(d, n) for d, n in days.items() if d.year == year and d.month == month]
        elif year_m:
            hits = [(d, n) for d, n in days.items() if d.year == year]
        else:
            hits = [(d, n) for d, n in days.items() if d >= today][:5]
        where = f" ({region.upper()})" if region else " (bundesweit – Bundesland mit holiday_region einstellen)"
        if not hits:
            return f"Keine gesetzlichen Feiertage in diesem Zeitraum{where}."
        return f"Gesetzliche Feiertage{where}:\n" + "\n".join(
            f"- {_day_text(ctx.cfg, d)}: {n} ({_relative((d - today).days)})" for d, n in hits)
    question = re.sub(r"\b(heilig ?abend|weihnachten|christmas eve|christmas)\b", "24.12.", question, flags=re.I)
    question = re.sub(r"\b(silvester|new year'?s eve)\b", "31.12.", question, flags=re.I)
    parts = re.split(r"\s+(?:bis|und|to|and|until)\s+", question, maxsplit=1)
    if len(parts) == 2 and re.search(r"\b(zwischen|von|vom|from|between|bis|until)\b", q):
        a = resolve(re.sub(r"^.*?\b(zwischen|von|vom|from|between)\b", "", parts[0], flags=re.I), today) or \
            resolve(parts[0], today)
        b = resolve(parts[1], today)
        if a and b:
            n = (b.day - a.day).days
            return (f"{_day_text(ctx.cfg, a.day)} bis {_day_text(ctx.cfg, b.day)}: {abs(n)} Tage "
                    f"({abs(n) // 7} Wochen und {abs(n) % 7} Tage).")
    when = resolve(question, today)
    if when is None:
        return (f"Keine Datumsangabe erkannt. Heute ist {_day_text(ctx.cfg, today)} "
                f"(KW {today.isocalendar().week}).")
    d = when.day
    extra = holidays(d.year, region).get(d)
    out = (f"{_day_text(ctx.cfg, d)} – KW {d.isocalendar().week}, {_relative((d - today).days)}"
           + (f", Feiertag: {extra}" if extra else ""))
    if when.at:
        out += f", {when.at:%H:%M} Uhr"
    return out + "."
