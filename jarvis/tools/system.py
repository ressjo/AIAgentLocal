"""Systeminformationen ohne externe Abhängigkeiten (liest /proc)."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

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
        gpus = [l.split(": ", 1)[-1] for l in out.splitlines() if "VGA" in l or "3D controller" in l or "Display" in l]
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
