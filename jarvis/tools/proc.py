"""Hilfsfunktionen zum Ausführen von Prozessen mit Live-Ausgabe, Timeout und Abbruch."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess

from .registry import ToolContext

QUIET_ENV = {"TERM": "dumb", "PAGER": "cat", "SYSTEMD_PAGER": "", "GIT_PAGER": "cat", "NO_COLOR": "1",
             "SYSTEMD_COLORS": "0"}


def clip(text: str, limit: int) -> str:
    """Behält Anfang und Ende, falls die Ausgabe zu lang ist."""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} Zeichen ausgelassen] …\n{text[-tail:]}"


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


async def run(ctx: ToolContext, cmd: str | list[str], timeout: float, stream: bool = True,
              cwd: str | None = None) -> tuple[int | None, str]:
    env = {**os.environ, **QUIET_ENV}
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  cwd=cwd or os.path.expanduser("~"), env=env, start_new_session=True)
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, executable="/bin/bash", **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

    chunks: list[str] = []

    async def pump() -> None:
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            chunks.append(text)
            if stream:
                await ctx.output(text)

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        _kill(proc)
        chunks.append(f"\n[Abgebrochen: Zeitlimit von {int(timeout)} s überschritten]\n")
        rc = None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return rc, "".join(chunks)


def spawn_detached(argv: list[str]) -> None:
    """Startet ein Programm unabhängig von Jarvis (überlebt dessen Neustart)."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=os.path.expanduser("~"))


def format_result(rc: int | None, output: str, limit: int) -> str:
    status = "Zeitüberschreitung" if rc is None else f"Exit-Code {rc}"
    body = clip(output.strip(), limit) or "(keine Ausgabe)"
    return f"{status}\n{body}"
