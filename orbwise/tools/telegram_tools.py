"""Dateien aufs Handy schicken (Telegram) – nur an den eigenen, eingetragenen Chat."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from .registry import BLOCKED, SAFE, ToolContext, tool
from .secretpaths import is_secret_path, secret_reason

MAX_FILE = 50_000_000  # Grenze der Telegram-Bot-API
CONFIG_NAMES = {"config.yaml", "config.yml", "config.json", "config.toml", "config.ini", "settings.json"}


def _enabled(cfg: Any) -> bool:
    tg = getattr(cfg, "telegram", None)
    return bool(tg and tg.enabled)


def _risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    # Geheimnisse verlassen den PC nie – auch nicht aufs eigene Handy
    path = str(args.get("path") or "")
    # beim Verschicken strenger als beim Lesen: auch Konfigurationsdateien anderer Programme bleiben daheim
    if is_secret_path(path) or Path(path).name.lower() in CONFIG_NAMES or path.lower().endswith((".conf", ".env")):
        return BLOCKED, secret_reason()
    return SAFE, ""


@tool("Schickt dem Nutzer eine Datei aufs Handy (Telegram, eigener Chat): eine lokale Datei (path) oder ein "
      "Paperless-Dokument (paperless_id, als PDF). Z. B. „schick mir die Rechnung aufs Handy“.",
      risk=_risk, enabled=_enabled)
async def telegram_send_file(
    ctx: ToolContext,
    path: Annotated[str, "Pfad einer lokalen Datei (oder leer, wenn paperless_id gesetzt ist)"] = "",
    paperless_id: Annotated[int, "ID eines Paperless-Dokuments (aus paperless_search)"] = 0,
    caption: Annotated[str, "Kurzer Text zur Datei (optional)"] = "",
) -> str:
    bot = ctx.services.get("telegram")
    if bot is None or not bot.t.enabled:
        return "Telegram ist nicht eingerichtet (telegram.token und telegram.chat_id)."
    if paperless_id:
        if not ctx.cfg.paperless.enabled:
            return "Paperless ist nicht eingerichtet."
        from .paperless import PaperlessError, download_document
        try:
            name, data = await download_document(ctx.cfg, int(paperless_id))
        except PaperlessError as e:
            return str(e)
    else:
        p = Path(path).expanduser()
        if not path or not p.is_file():
            return f"Datei nicht gefunden: {p}" if path else "Bitte path oder paperless_id angeben."
        if p.stat().st_size > MAX_FILE:
            return f"{p.name} ist zu groß für Telegram ({p.stat().st_size // 1_000_000} MB, höchstens 50 MB)."
        name, data = p.name, p.read_bytes()
    try:
        await bot.send_file(data, name, caption)
    except Exception as e:  # noqa: BLE001
        return f"Senden fehlgeschlagen: {bot.redact(e)}"
    return f"Aufs Handy geschickt: {name} ({max(1, len(data) // 1024)} KB)."
