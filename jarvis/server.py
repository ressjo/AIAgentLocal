"""FastAPI-Server: Weboberfläche, WebSocket für Chat/Audio, REST-Endpunkte für Status und Gedächtnis."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import Agent
from .config import Config
from .llm import FakeLLM, LLMError
from .llm_router import LLMRouter
from .memory import Memory
from . import metrics
from .memory.files import valid_day
from .reminders import ReminderStore
from .tools import proc
from .tools.calendar_tools import calendar_status
from .tools.trilium import trilium_status
from .voice.listen import AudioSession, WakeWordFactory, WhisperSTT
from .voice import catalog
from .voice.tts import PiperTTS, Speaker

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

YES = re.compile(r"\b(ja|jawohl|jep|jo|okay|ok|klar|mach(\s+es|'s)?|los|bestätig\w*|ausführen|genehmigt|positiv|yes)\b", re.I)
NO = re.compile(r"\b(nein|nee|nö|stopp?|abbrechen|abbruch|nicht|lass\s+es|negativ|no)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    no, yes = bool(NO.search(text)), bool(YES.search(text))
    if no:
        return False
    if yes:
        return True
    return None


def describe_call(name: str, args: dict) -> str:
    if name == "run_shell":
        return f"den Befehl {args.get('command', '')}"
    if name == "install_package":
        return f"die Installation von {args.get('names', '')}"
    if name == "remove_package":
        return f"das Entfernen von {args.get('names', '')}"
    if name == "system_update":
        return "ein vollständiges Systemupdate"
    if name == "calendar_update":
        return f"das Ändern des Termins {args.get('query', '')}"
    if name == "calendar_delete":
        return f"das Löschen des Termins {args.get('query', '')}"
    if name == "trilium_update_note":
        return f"das Überschreiben der Trilium-Notiz {args.get('note', '')}"
    if name == "write_file":
        return f"das Schreiben der Datei {args.get('path', '')}"
    return f"die Aktion {name}"


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.tts = True
        self.audio: AudioSession | None = None
        self.lock = asyncio.Lock()

    async def send(self, event: dict) -> None:
        async with self.lock:
            try:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:  # noqa: BLE001 – Verbindung weg
                pass


class Hub:
    def __init__(self, cfg: Config, agent: Agent, speaker_tts: PiperTTS | None,
                 stt: WhisperSTT | None, wake: WakeWordFactory | None):
        self.cfg = cfg
        self.agent = agent
        self.clients: set[Client] = set()
        # Erinnerungen, die fällig wurden, als keine Oberfläche offen war – werden beim Verbinden zugestellt
        self.undelivered: list[dict] = []
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: set[asyncio.Task] = set()
        self.stt = stt
        self.wake = wake
        self.speaker = Speaker(speaker_tts, self.broadcast_tts, lambda: any(c.tts for c in self.clients))

    async def broadcast(self, event: dict) -> None:
        await asyncio.gather(*(c.send(event) for c in list(self.clients)))

    async def broadcast_tts(self, event: dict) -> None:
        await asyncio.gather(*(c.send(event) for c in list(self.clients) if c.tts))

    async def emit(self, event: dict) -> None:
        t = event.get("type")
        if t == "token":
            self.speaker.feed(event["id"], event["text"])
        elif t in ("segment_end", "assistant_end"):
            self.speaker.end(event["id"])
        elif t == "error":
            self.speaker.say(event["text"])
        await self.broadcast(event)

    async def confirm(self, call_id: str, name: str, args: dict, reason: str) -> bool:
        fut = asyncio.get_running_loop().create_future()
        self.pending[call_id] = fut
        await self.broadcast({"type": "confirm_request", "id": call_id, "name": name, "args": args,
                              "reason": reason, "summary": describe_call(name, args)})
        self.speaker.say(f"Soll ich {describe_call(name, args)} ausführen?")
        try:
            return await asyncio.wait_for(fut, timeout=180)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(call_id, None)
            await self.broadcast({"type": "confirm_done", "id": call_id})

    def resolve(self, call_id: str, approved: bool) -> None:
        fut = self.pending.get(call_id)
        if fut and not fut.done():
            fut.set_result(approved)

    async def submit(self, text: str, source: str = "text") -> None:
        text = text.strip()
        if not text:
            return
        # Wartet eine Bestätigung, wird gesprochener Text als Ja/Nein interpretiert
        if self.pending:
            decision = parse_yes_no(text)
            if decision is not None:
                for cid in list(self.pending):
                    self.resolve(cid, decision)
                return
            if source == "voice":
                self.speaker.say("Bitte mit Ja oder Nein antworten.")
                return
            # Neue getippte Anfrage statt Antwort → offene Aktion ablehnen
            for cid in list(self.pending):
                self.resolve(cid, False)
        self.speaker.stop()
        await self.broadcast({"type": "audio_stop"})
        await self.broadcast({"type": "user", "text": text, "source": source})
        task = asyncio.create_task(self._run(text))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, text: str) -> None:
        try:
            await self.agent.run(text, self.emit, self.confirm)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("Agent-Fehler")
            await self.emit({"type": "error", "text": f"Interner Fehler: {e}"})
        finally:
            if not self.agent.lock.locked():
                await self.broadcast({"type": "state", "state": "idle"})

    async def stop(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
        for task in list(self.tasks):
            task.cancel()
        self.speaker.stop()
        await self.broadcast({"type": "audio_stop"})


def check_host(host: str | None, port: int) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")


def create_app(cfg: Config) -> FastAPI:
    fake = os.environ.get("JARVIS_FAKE_LLM") == "1"
    llm = FakeLLM() if fake else LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
    memory = Memory(cfg.memory, llm)
    agent = Agent(cfg, llm, memory)
    reminders = ReminderStore(cfg.memory.dir.parent / "reminders.json")
    agent.services["reminders"] = reminders

    stt = wake = tts = None
    if cfg.voice.enabled:
        stt = WhisperSTT(cfg.voice)
        stt = stt if stt.available() else None
        wake = WakeWordFactory(cfg.voice)
        tts = PiperTTS(cfg.voice)
    hub = Hub(cfg, agent, tts, stt, wake)
    background: list[asyncio.Task] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.speaker.start()
        if isinstance(llm, LLMRouter):
            background.append(asyncio.create_task(start_model()))
        background.append(asyncio.create_task(summary_loop()))
        background.append(asyncio.create_task(metrics_loop()))
        background.append(asyncio.create_task(reminder_loop()))
        if stt and os.environ.get("JARVIS_SKIP_WARMUP") != "1":
            background.append(asyncio.create_task(asyncio.to_thread(stt.warmup)))
        yield
        for t in background:
            t.cancel()
        await hub.speaker.close()
        await llm.close()
        memory.close()

    async def model_progress(text: str) -> None:
        await hub.broadcast({"type": "model_progress", "text": text})

    async def start_model() -> None:
        # Beim Start das aktive Profil vorbereiten (z. B. llama-server starten); Chats warten so lange
        name = llm.active
        llm.switching = name if llm.server_for(name) else None
        try:
            async with agent.lock:
                await llm.start(model_progress)
        finally:
            llm.switching = None
        await hub.broadcast({"type": "model_active", "name": name})

    async def fire_reminder(r, now) -> None:
        late = (now - r.due_dt).total_seconds() > 120
        kind = "Timer" if r.kind == "timer" else "Erinnerung"
        spoken = (f"Verpasste {kind} von {r.due_dt.strftime('%H:%M')} Uhr: {r.text}" if late
                  else ("Der Timer ist abgelaufen: " if r.kind == "timer" else "Erinnerung: ") + r.text)
        reminders.mark_done(r.id)
        memory.journal.append("Erinnerung", spoken)
        event = {"type": "reminder", "id": r.id, "text": r.text, "kind": r.kind,
                 "due": r.due, "late": late, "spoken": spoken}
        if hub.clients:
            await hub.broadcast(event)
            hub.speaker.say(spoken)
        else:
            hub.undelivered.append(event)
        if shutil.which("notify-send"):
            await proc.launch(["notify-send", "--app-name=JARVIS", "--urgency=critical",
                               f"JARVIS – {kind}", r.text], wait=1)

    async def reminder_loop() -> None:
        while True:
            try:
                now = datetime.now()
                for r in reminders.due(now):
                    await fire_reminder(r, now)
            except Exception as e:  # noqa: BLE001
                log.warning("Erinnerung fehlgeschlagen: %s", e)
            await asyncio.sleep(1)

    async def metrics_loop() -> None:
        while True:
            if hub.clients:
                try:
                    data = await asyncio.to_thread(metrics.collect)
                    await hub.broadcast({"type": "metrics", **data})
                except Exception as e:  # noqa: BLE001
                    log.debug("Telemetrie fehlgeschlagen: %s", e)
            await asyncio.sleep(2)

    async def summary_loop() -> None:
        await asyncio.sleep(30)
        while True:
            if not agent.lock.locked():
                try:
                    done = await memory.summarize_pending()
                    if done:
                        log.info("Tageszusammenfassungen erstellt: %s", ", ".join(done))
                except Exception as e:  # noqa: BLE001
                    log.warning("Zusammenfassungen fehlgeschlagen: %s", e)
            await asyncio.sleep(300)

    app = FastAPI(title="JARVIS", lifespan=lifespan)
    app.state.hub = hub
    app.state.memory = memory

    @app.middleware("http")
    async def only_local(request: Request, call_next):
        # Schutz vor DNS-Rebinding: nur Anfragen an localhost zulassen
        if not check_host(request.headers.get("host"), cfg.port):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        # Schreibende Anfragen nur von der eigenen Oberfläche (Schutz vor CSRF)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and \
                not check_host(re.sub(r"^https?://", "", origin), cfg.port):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/status")
    async def status():
        voice = {
            "stt": stt is not None,
            "tts": bool(tts and tts.available()),
            "tts_error": tts.error if tts else "Sprache deaktiviert",
            "wake": bool(wake and wake.available()),
            "wake_error": wake.error if wake else None,
        }
        return {
            "name": cfg.assistant_name,
            "llm": await llm.status(),
            "voice": voice,
            "memory": {
                "days": len(memory.journal.days()),
                "facts": len(memory.facts.list()),
                "chunks": memory.index.count(),
                "dir": str(cfg.memory.dir),
            },
            "trilium": await trilium_status(cfg),
            "calendar": await calendar_status(cfg),
            "busy": agent.lock.locked(),
        }

    @app.get("/api/models")
    async def get_models():
        if not isinstance(llm, LLMRouter):
            return {"active": "demo", "switching": None,
                    "profiles": [{"name": "demo", "label": "Demo (Fake-LLM)", "backend": "fake", "model": "fake",
                                  "base_url": "", "managed": False, "active": True}]}
        return {"active": llm.active, "switching": llm.switching, "profiles": llm.describe()}

    @app.post("/api/models/{name}/activate")
    async def activate_model(name: str):
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
        llm.switching = name
        try:
            async with agent.lock:  # wartet, bis eine laufende Antwort fertig ist
                await llm.activate(name, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(502, str(e)) from e
        finally:
            llm.switching = None
        await hub.broadcast({"type": "model_active", "name": name})
        return {"ok": True, "active": llm.active}

    @app.get("/api/reminders")
    async def get_reminders():
        return [{"id": r.id, "text": r.text, "due": r.due, "kind": r.kind} for r in reminders.upcoming()]

    @app.delete("/api/reminders/{rid}")
    async def delete_reminder(rid: str):
        if not reminders.cancel(rid):
            raise HTTPException(404, "Erinnerung nicht gefunden")
        return {"ok": True}

    @app.get("/api/metrics")
    async def get_metrics():
        return await asyncio.to_thread(metrics.collect)

    @app.get("/api/memory/days")
    async def memory_days():
        summaries = set(memory.summaries.days())
        return [{"day": d, "summary": d in summaries} for d in memory.journal.days()]

    @app.get("/api/memory/day/{day}")
    async def memory_day(day: str):
        if not valid_day(day):
            raise HTTPException(400, "ungültiges Datum")
        return {"day": day, "journal": memory.journal.read(day), "summary": memory.summaries.read(day)}

    @app.get("/api/memory/facts")
    async def memory_facts():
        return [{"fact": f, "day": d} for f, d in memory.facts.list()]

    @app.get("/api/history")
    async def history():
        msgs = [m for m in memory.conversation.history if m["role"] in ("user", "assistant") and m.get("content")]
        return {"summary": memory.conversation.running_summary,
                "messages": [{"role": m["role"], "content": m["content"]} for m in msgs[-40:]]}

    @app.get("/api/voices")
    async def voices():
        if not tts:
            return {"available": False, "voices": []}
        tts.available()
        return {"available": True, "current": tts.current, "rate": tts.rate,
                "voices": catalog.voice_list(tts.voices_dir, tts.current)}

    @app.post("/api/voices/{name}/install")
    async def install_voice(name: str):
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        if name not in catalog.BY_NAME:
            raise HTTPException(404, "Unbekannte Stimme")
        try:
            await catalog.install_voice(name, tts.voices_dir)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Download fehlgeschlagen: {e}") from e
        return {"ok": True}

    @app.get("/api/voices/{name}/preview")
    async def preview_voice(name: str, text: str = ""):
        if not tts or name not in tts.installed() or not tts.available():
            raise HTTPException(404, "Stimme nicht installiert")
        sample = text.strip()[:200] or "Guten Abend. Alle Systeme sind einsatzbereit. Womit kann ich dienen?"
        wav = await asyncio.to_thread(tts.synth, sample, name)
        return Response(wav, media_type="audio/wav")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        host = re.sub(r"^https?://", "", origin)
        # Schutz vor Cross-Site-WebSocket-Hijacking: fremde Webseiten dürfen keine Befehle senden
        if not check_host(host, cfg.port) or not check_host(ws.headers.get("host"), cfg.port):
            await ws.close(code=4403)
            return
        await ws.accept()
        client = Client(ws)
        client.audio = AudioSession(cfg.voice, stt, wake, client.send, lambda t: hub.submit(t, "voice"))
        hub.clients.add(client)
        await client.send({"type": "hello", "busy": agent.lock.locked(),
                           "pending": [cid for cid in hub.pending]})
        if hub.undelivered:
            events, hub.undelivered = hub.undelivered, []
            for event in events:
                await client.send(event)
                hub.speaker.say(event["spoken"])
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await client.audio.feed(msg["bytes"])
                    continue
                if not msg.get("text"):
                    continue
                data = json.loads(msg["text"])
                t = data.get("type")
                if t == "user_message":
                    await hub.submit(str(data.get("text", "")))
                elif t == "confirm":
                    hub.resolve(str(data.get("id")), bool(data.get("approved")))
                elif t == "stop":
                    await hub.stop()
                elif t == "tts":
                    client.tts = bool(data.get("enabled"))
                elif t == "voice_settings" and tts:
                    if data.get("voice"):
                        tts.select(str(data["voice"]))
                    if data.get("rate"):
                        tts.set_rate(float(data["rate"]))
                elif t == "wake":
                    await client.audio.set_wake(bool(data.get("enabled")))
                elif t == "ptt_start":
                    hub.speaker.stop()
                    await client.send({"type": "audio_stop"})
                    await client.audio.start_recording(ptt=True)
                elif t == "ptt_stop":
                    await client.audio.stop_recording()
                elif t == "listen":
                    await client.audio.start_recording(ptt=False)
                elif t == "cancel_listen":
                    await client.audio.cancel()
                elif t == "speech_interrupt":
                    hub.speaker.stop()
                elif t == "reset_conversation":
                    memory.conversation.reset()
                    await hub.broadcast({"type": "conversation_reset"})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(client)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
