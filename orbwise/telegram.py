"""Telegram-Bot: Orbwise vom Handy aus fragen, Erinnerungen aufs Handy, Rückfragen per Knopf.

Der Bot holt Nachrichten selbst ab (Long Polling) – kein offener Port, kein Webhook. Er reagiert nur auf die
eingetragene Chat-ID; alle anderen bekommen lediglich ihre Chat-ID genannt (für die Einrichtung).

Einrichtung:
    1. In Telegram @BotFather → /newbot → Token kopieren
    2. config.yaml:  telegram: { token: "123:ABC…" }   (oder $ORBWISE_TELEGRAM_TOKEN)
    3. Orbwise starten, dem Bot „/start“ schreiben – er antwortet mit deiner Chat-ID
    4. config.yaml:  telegram: { chat_id: 123456789 }  und Orbwise neu starten
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_TEXT = 4000          # Telegram erlaubt 4096 Zeichen je Nachricht
MAX_DOWNLOAD = 20_000_000  # Bots dürfen Dateien bis 20 MB abholen …
MAX_UPLOAD = 50_000_000    # … und bis 50 MB senden
FILE_CONTEXT_SECONDS = 900  # eine Datei ohne Text gilt 15 min als Bezug für die nächste Nachricht
CONFIRM_TIMEOUT = 300    # so lange wartet eine Rückfrage auf den Knopf
CHAT_TITLE = "📱 Telegram"

def explain(error: str) -> str:
    """Telegram-Fehler in einen konkreten Hinweis übersetzen."""
    low = error.lower()
    if "unauthorized" in low or "401" in low or "not found" in low or "404" in low:
        return "Token ungültig – bei @BotFather mit /token prüfen und telegram.token neu eintragen."
    if "conflict" in low or "409" in low:
        if "webhook" in low:
            return "Für den Bot ist ein Webhook gesetzt – Orbwise entfernt ihn beim Start automatisch."
        return ("Ein anderes Programm holt gerade Nachrichten für diesen Bot ab (z. B. ein zweites Orbwise, "
                "systemd-Dienst + Terminal) – nur eines laufen lassen.")
    if "connect" in low or "timeout" in low or "name resolution" in low or "network" in low:
        return "api.telegram.org nicht erreichbar – Internetverbindung/Proxy/Firewall prüfen."
    return error


Runner = Callable[[str, Callable[[dict], Awaitable[None]], Callable[..., Awaitable[Any]]], Awaitable[str]]


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Lange Antworten an Absätzen/Zeilen in Telegram-taugliche Stücke teilen."""
    text = text.strip() or "…"
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


class TelegramBot:
    TRANSPORT: httpx.AsyncBaseTransport | None = None  # für Tests: gefälschte Telegram-API

    def __init__(self, cfg: Any, run: Runner, describe: Callable[[str, dict], str],
                 editable: Callable[[str], bool], transcribe: Callable[[bytes], Awaitable[str]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, poll_timeout: int = 30):
        """run(text, emit, confirm) → Antwort: führt eine Anfrage im Telegram-Chat aus (Agent).
        describe(name, args) → lesbare Beschreibung einer Aktion für die Rückfrage.
        editable(name) → True für Aktionen mit Bearbeitungsfenster (z. B. mail_send) – nur im Dashboard."""
        self.cfg = cfg
        self.t = cfg.telegram
        self.run = run
        self.describe = describe
        self.editable = editable
        self.transcribe = transcribe
        self.poll_timeout = poll_timeout
        self.client = httpx.AsyncClient(base_url=f"{API}/bot{self.t.secret}/", transport=transport or self.TRANSPORT,
                                        timeout=httpx.Timeout(poll_timeout + 15))
        self.offset = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self.en = getattr(cfg, "language", "de") == "en"
        # für /api/status und die Oberfläche: läuft der Bot, wie heißt er, was ist das letzte Problem?
        self.status: dict[str, Any] = {"running": False, "bot": "", "error": "", "chat_id": self.t.chat_id}
        self.last_file: tuple[Path, float] | None = None  # zuletzt empfangene Datei (für „ab in Paperless“ danach)

    def L(self, de: str, en: str) -> str:
        return en if self.en else de

    # ---------- Telegram-API ----------
    async def call(self, method: str, **params) -> Any:
        r = await self.client.post(method, json=params)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {r.status_code}") from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data.get("result")

    async def send(self, text: str, chat_id: int | None = None, **extra) -> list[dict]:
        out = []
        for part in split_text(text):
            out.append(await self.call("sendMessage", chat_id=chat_id or self.t.chat_id, text=part, **extra))
        return out

    async def send_file(self, data: bytes, filename: str, caption: str = "") -> dict:
        """Datei an den eigenen Chat schicken (sendDocument, bis 50 MB)."""
        if len(data) > MAX_UPLOAD:
            raise RuntimeError(f"Datei zu groß für Telegram ({len(data) // 1_000_000} MB, höchstens 50 MB).")
        form = {"chat_id": str(self.t.chat_id)}
        if caption:
            form["caption"] = caption[:1000]
        r = await self.client.post("sendDocument", data=form, files={"document": (filename, data)},
                                   timeout=httpx.Timeout(120))
        try:
            result = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram sendDocument: HTTP {r.status_code}") from None
        if not result.get("ok"):
            raise RuntimeError(f"Telegram sendDocument: {result.get('description', r.status_code)}")
        return result["result"]

    async def notify(self, text: str) -> bool:
        """Nachricht an den Nutzer (z. B. Erinnerung). False, wenn der Bot nicht eingerichtet ist oder es scheitert."""
        if not self.t.enabled:
            return False
        try:
            await self.send(text)
            return True
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            log.warning("Telegram-Nachricht fehlgeschlagen: %s", e)
            return False

    # ---------- Abholen ----------
    async def poll_once(self) -> int:
        updates = await self.call("getUpdates", offset=self.offset, timeout=self.poll_timeout,
                                  allowed_updates=["message", "callback_query"])
        for u in updates or []:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            try:
                await self.handle(u)
            except Exception:  # noqa: BLE001 – ein kaputtes Update darf den Bot nicht stoppen
                log.exception("Telegram-Update fehlgeschlagen")
        return len(updates or [])

    async def check(self) -> None:
        """Beim Start: Token prüfen (getMe) und einen evtl. gesetzten Webhook entfernen – sonst liefert Telegram
        keine Nachrichten per getUpdates (Fehler „Conflict“)."""
        me = await self.call("getMe")
        self.status["bot"] = "@" + str(me.get("username", ""))
        await self.call("deleteWebhook", drop_pending_updates=False)
        if self.t.chat_id:
            log.warning("Telegram-Bot %s aktiv – bedient Chat %s", self.status["bot"], self.t.chat_id)
        else:
            log.warning("Telegram-Bot %s aktiv, aber noch ohne telegram.chat_id – schreib ihm „/start“, "
                        "er nennt dir deine Chat-ID", self.status["bot"])

    async def serve(self) -> None:
        """Hauptschleife: Nachrichten abholen, Anfragen der Reihe nach bearbeiten. Fehler werden gemeldet und
        wiederholt – die Schleife endet nie still."""
        worker = asyncio.create_task(self._worker())
        loop = asyncio.get_running_loop()
        backoff, checked = 2, False
        try:
            while True:
                try:
                    if not checked:
                        await self.check()
                        checked = True
                    started = loop.time()
                    got = await self.poll_once()
                    backoff = 2
                    self.status.update(running=True, error="")
                    # Antwortet der Server sofort ohne Neuigkeiten (kein echtes Long Polling), nicht im Kreis rasen
                    await asyncio.sleep(0.3 if not got and loop.time() - started < 1 else 0)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    hint = explain(str(e) or type(e).__name__)
                    if hint != self.status.get("error"):
                        log.warning("Telegram-Bot: %s (%s) – neuer Versuch in %s s", hint, e, backoff)
                    self.status.update(running=False, error=hint)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120)
        finally:
            worker.cancel()
            await self.client.aclose()

    # ---------- Eingang ----------
    async def handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat is None:
            return
        if chat != self.t.chat_id:
            # Fremde (oder noch nicht eingetragene) Chats: nur die Chat-ID nennen, sonst nichts
            await self.send(self.L(f"Dieser Chat ist nicht freigeschaltet. Deine Chat-ID: {chat}\n"
                                   f"Trage sie in Orbwise ein (config.yaml → telegram.chat_id) und starte neu.",
                                   f"This chat is not authorised. Your chat ID: {chat}\n"
                                   f"Add it to Orbwise (config.yaml → telegram.chat_id) and restart."), chat_id=chat)
            return
        text = (msg.get("text") or "").strip()
        if text in ("/start", "/help"):
            await self.send(self.L("Hallo! Schreib oder sprich mir einfach, was du brauchst – z. B. „Erinner mich "
                                   "morgen um 9 an den Zahnarzt“. Erinnerungen kommen hierher.",
                                   "Hi! Just write or speak what you need – e.g. “Remind me tomorrow at 9 about the "
                                   "dentist”. Reminders arrive here."))
            return
        if msg.get("document") or msg.get("photo"):
            path = await self._receive_file(msg)
            if not path:
                return
            caption = (msg.get("caption") or "").strip()
            if not caption:
                self.last_file = (path, time.monotonic())
                await self.send(self.L(f"📥 Gespeichert: {path}\nSchreib mir, was damit passieren soll – z. B. "
                                       "„ab in Paperless“ oder „fass zusammen“.",
                                       f"📥 Saved: {path}\nTell me what to do with it – e.g. “put it into "
                                       "Paperless” or “summarise it”."))
                return
            await self.send(f"📥 {path.name}")
            await self.queue.put((self._file_note(path) + caption, msg.get("message_id", 0)))
            return
        if not text and (msg.get("voice") or msg.get("audio")):
            text = await self._voice(msg.get("voice") or msg.get("audio"))
            if not text:
                return
            await self.send(f"🎙 „{text}“")
        if text:
            if self.last_file and time.monotonic() - self.last_file[1] < FILE_CONTEXT_SECONDS:
                text = self._file_note(self.last_file[0]) + text  # Bezug auf die eben geschickte Datei
            self.last_file = None
            await self.queue.put((text, msg.get("message_id", 0)))

    def _file_note(self, path: Path) -> str:
        size = path.stat().st_size if path.exists() else 0
        return self.L(f"[Datei vom Handy empfangen und gespeichert unter {path} ({size // 1024} KB)]\n",
                      f"[File received from the phone and saved as {path} ({size // 1024} KB)]\n")

    async def _receive_file(self, msg: dict) -> Path | None:
        """Dokument/Foto herunterladen und im Eingangsordner ablegen (Name bereinigt, nie überschreiben)."""
        if msg.get("document"):
            item = msg["document"]
            name = item.get("file_name") or "datei"
        else:
            item = max(msg["photo"], key=lambda p: p.get("file_size", 0) or p.get("width", 0))
            name = time.strftime("foto-%Y%m%d-%H%M%S.jpg")
        if (item.get("file_size") or 0) > MAX_DOWNLOAD:
            await self.send(self.L("Die Datei ist zu groß – Telegram-Bots können nur Dateien bis 20 MB abholen.",
                                   "The file is too big – Telegram bots can only fetch files up to 20 MB."))
            return None
        info = await self.call("getFile", file_id=item["file_id"])
        r = await self.client.get(f"{API}/file/bot{self.t.secret}/{info['file_path']}", timeout=httpx.Timeout(120))
        r.raise_for_status()
        inbox = self.t.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        stem, dot, ext = re.sub(r"[^\w\-. ]+", "_", Path(name).name).strip(" ._").rpartition(".")
        stem, ext = (stem, "." + ext) if dot and stem else (ext or "datei", "")
        target, n = inbox / f"{stem}{ext}", 1
        while target.exists():
            target, n = inbox / f"{stem}-{n}{ext}", n + 1
        target.write_bytes(r.content)
        return target

    async def _voice(self, voice: dict) -> str:
        if not self.transcribe:
            await self.send(self.L("Sprachnachrichten gehen nur mit Spracherkennung (voice.enabled).",
                                   "Voice messages need speech recognition (voice.enabled)."))
            return ""
        info = await self.call("getFile", file_id=voice["file_id"])
        r = await self.client.get(f"{API}/file/bot{self.t.secret}/{info['file_path']}")
        r.raise_for_status()
        try:
            return (await self.transcribe(r.content)).strip()
        except Exception as e:  # noqa: BLE001
            log.warning("Sprachnachricht nicht erkannt: %s", e)
            await self.send(self.L("Die Sprachnachricht konnte ich nicht verstehen.", "I couldn't understand that."))
            return ""

    async def _worker(self) -> None:
        while True:
            text, _ = await self.queue.get()
            typing = asyncio.create_task(self._typing())
            try:
                answer = await self.run(text, self._emit, self._confirm)
            except Exception as e:  # noqa: BLE001
                log.exception("Telegram-Anfrage fehlgeschlagen")
                answer = self.L(f"Da ist etwas schiefgegangen: {e}", f"Something went wrong: {e}")
            finally:
                typing.cancel()
            await self.notify(answer or self.L("(keine Antwort)", "(no answer)"))

    async def _typing(self) -> None:
        with contextlib.suppress(Exception):
            while True:
                await self.call("sendChatAction", chat_id=self.t.chat_id, action="typing")
                await asyncio.sleep(4.5)

    async def _emit(self, ev: dict) -> None:
        pass  # Zwischenstände (Tokens, Tools) nicht einzeln aufs Handy schicken

    # ---------- Rückfragen per Knopf ----------
    async def _confirm(self, call_id: str, name: str, args: dict, reason: str) -> bool:
        if self.editable(name):
            await self.notify(self.L("✉ Das geht nur am PC: Im Dashboard lässt sich die Mail vor dem Senden prüfen "
                                     "und bearbeiten.", "✉ This needs the PC: the dashboard lets you check and edit "
                                     "the e-mail before sending."))
            return False
        key = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[key] = fut
        detail = str(args.get("command")) if name == "run_shell" else json.dumps(args, ensure_ascii=False)
        text = self.L(f"❓ Soll ich {self.describe(name, args)} ausführen?", f"❓ Shall I run {self.describe(name, args)}?")
        text += f"\n\n{detail[:1500]}" + (f"\n\n{reason[:500]}" if reason else "")
        buttons = {"inline_keyboard": [[{"text": self.L("✅ Ausführen", "✅ Run"), "callback_data": f"ok:{key}"},
                                        {"text": self.L("❌ Ablehnen", "❌ Deny"), "callback_data": f"no:{key}"}]]}
        try:
            sent = await self.send(text, reply_markup=buttons)
        except (httpx.HTTPError, RuntimeError):
            self.pending.pop(key, None)
            return False
        try:
            approved = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self.pending.pop(key, None)
        with contextlib.suppress(Exception):  # Knöpfe entfernen, Entscheidung anzeigen
            status = self.L("✅ ausgeführt", "✅ approved") if approved else self.L("❌ abgelehnt", "❌ denied")
            await self.call("editMessageText", chat_id=self.t.chat_id, message_id=sent[-1]["message_id"],
                            text=f"{sent[-1].get('text', text)}\n\n→ {status}")
        return approved

    async def _callback(self, cq: dict) -> None:
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        with contextlib.suppress(Exception):
            await self.call("answerCallbackQuery", callback_query_id=cq.get("id"))
        if chat != self.t.chat_id:
            return
        action, _, key = str(cq.get("data", "")).partition(":")
        fut = self.pending.get(key)
        if fut and not fut.done():
            fut.set_result(action == "ok")


async def transcribe_voice(stt: Any, data: bytes) -> str:
    """Telegram-Sprachnachricht (OGG/Opus) → Text über Whisper (faster-whisper dekodiert selbst)."""
    def work() -> str:
        import numpy as np
        from faster_whisper import decode_audio
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            f.write(data)
            f.flush()
            audio = decode_audio(f.name, sampling_rate=16000)
        return stt.transcribe((np.clip(audio, -1, 1) * 32767).astype(np.int16))
    return await asyncio.to_thread(work)


def chat_state_file(cfg: Any) -> Path:
    return cfg.memory.dir.parent / "telegram.json"
