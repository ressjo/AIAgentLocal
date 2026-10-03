from __future__ import annotations

from typing import Annotated

from . import proc
from .registry import ToolContext, tool
from .safety import apply_privilege, classify_command


def _risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return classify_command(args.get("command", ""), ctx.cwd)


@tool(
    "Führt einen Bash-Befehl auf dem Linux-System des Nutzers aus und liefert die Ausgabe. "
    "Für Root-Rechte 'sudo' voranstellen. Keine interaktiven Programme (vim, htop, less) verwenden.",
    risk=_risk,
)
async def run_shell(
    ctx: ToolContext,
    command: Annotated[str, "Der auszuführende Bash-Befehl"],
    timeout_seconds: Annotated[int, "Zeitlimit in Sekunden (Standard 120)"] = 0,
) -> str:
    timeout = timeout_seconds if timeout_seconds > 0 else ctx.cfg.tools.shell_timeout
    command = apply_privilege(command, ctx.cfg.tools.privilege_cmd)
    rc, out = await proc.run(ctx, command, timeout=min(timeout, ctx.cfg.tools.update_timeout), cwd=ctx.cwd)
    return proc.format_result(rc, out, ctx.limit())
