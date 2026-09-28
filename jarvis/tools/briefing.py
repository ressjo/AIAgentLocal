"""„Guten Morgen“-Briefing: Datum, Wetter, heutige Erinnerungen, Updates und Speicherplatz auf einen Blick."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path

from ..memory.files import german_date
from . import proc
from .calendar_tools import CalendarError, events_between
from .registry import ToolContext, tool
from .reminder_tools import store_for
from .weather import WeatherError, weather_report

LOW_SPACE_GB = 20


async def _weather(ctx: ToolContext) -> str | None:
    if not ctx.cfg.weather.location:
        return None
    try:
        return await weather_report(ctx.cfg.weather.location, 1)
    except WeatherError as e:
        return f"Wetter: {e}"


async def _calendar(ctx: ToolContext) -> str | None:
    if not ctx.cfg.calendar.enabled:
        return None
    try:
        events = await events_between(ctx.cfg, datetime.now().date(), 1)
    except CalendarError as e:
        return f"Kalender: {e}"
    if not events:
        return "Kalender: heute keine Termine."
    return "Termine heute:\n" + "\n".join(f"- {e.line()}" for e in events)


async def _updates(ctx: ToolContext) -> str | None:
    if not shutil.which("checkupdates"):
        return None
    rc, out = await proc.run(ctx, ["checkupdates"], timeout=45, stream=False)
    if rc == 2 or (rc == 0 and not out.strip()):
        return "Systemupdates: keine."
    if rc != 0:
        return None
    pkgs = [line.split()[0] for line in out.splitlines() if line.strip()]
    important = [p for p in pkgs if p.split("-")[0] in ("linux", "nvidia", "mesa", "ollama", "systemd", "firefox")]
    text = f"Systemupdates: {len(pkgs)} verfügbar"
    if important:
        text += f" (darunter {', '.join(important[:5])})"
    return text + "."


def _disks(ctx: ToolContext) -> str | None:
    notes = []
    for mount in [Path("/"), Path.home(), *ctx.cfg.tools.nas_paths]:
        try:
            free = shutil.disk_usage(mount).free / 1024 ** 3
        except OSError:
            if mount in ctx.cfg.tools.nas_paths:
                notes.append(f"NAS {mount} ist nicht erreichbar")
            continue
        if free < LOW_SPACE_GB:
            notes.append(f"wenig Platz auf {mount}: nur noch {free:.0f} GB frei")
    for nas in ctx.cfg.tools.nas_paths:
        if nas.exists() and not any(nas.iterdir()):
            notes.append(f"NAS {nas} scheint nicht gemountet")
    return ("Speicher: " + "; ".join(dict.fromkeys(notes)) + ".") if notes else None


@tool("Tagesüberblick für „Guten Morgen“ oder „Was steht heute an?“: Datum, Wetter am Standardort, heutige "
      "Kalendertermine, Erinnerungen, verfügbare Systemupdates und Speicher-/NAS-Warnungen.")
async def daily_briefing(ctx: ToolContext) -> str:
    now = datetime.now()
    parts = [f"Heute ist {german_date(now)}, {now.strftime('%H:%M')} Uhr."]
    weather, calendar, updates = await asyncio.gather(_weather(ctx), _calendar(ctx), _updates(ctx),
                                                       return_exceptions=True)
    if isinstance(weather, str):
        parts.append(weather)
    elif not ctx.cfg.weather.location:
        parts.append("Wetter: kein Standardort eingestellt (weather.location).")
    if isinstance(calendar, str):
        parts.append(calendar)
    reminders = store_for(ctx).today()
    if reminders:
        parts.append("Heutige Erinnerungen:\n" + "\n".join(
            f"- {r.due_dt.strftime('%H:%M')} {r.text}" for r in reminders))
    else:
        parts.append("Heute stehen keine Erinnerungen an.")
    if isinstance(updates, str):
        parts.append(updates)
    disks = _disks(ctx)
    if disks:
        parts.append(disks)
    return "\n".join(parts)
