"""FastAPI-Server: Weboberfläche, WebSocket für Chat/Audio, REST-Endpunkte für Status und Gedächtnis."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import askpass, metrics, prompts
from .agent import Agent
from .config import Config
from .llm import FakeLLM, LLMError
from .llm_router import LLMRouter
from .memory import Memory
from .memory.files import valid_day
from .reminders import ReminderStore
from .tools import proc
from .tools.calendar_tools import calendar_status
from .tools.trilium import trilium_status
from .voice import catalog
from .voice.listen import AudioSession, WakeWordFactory, WhisperSTT
from .voice.tts import PiperTTS, Speaker
from .web_i18n import translate_index

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

YES = re.compile(r"\b(ja|jawohl|jep|jo|okay|ok|klar|mach(\s+es|'s)?|los|bestätig\w*|ausführen|genehmigt|positiv|yes|yeah|yep|sure|go\s+ahead|do\s+it|confirm\w*|proceed)\b", re.I)
NO = re.compile(r"\b(nein|nee|nö|stopp?|abbrechen|abbruch|nicht|lass\s+es|negativ|no|nope|cancel|don'?t|abort)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    no, yes = bool(NO.search(text)), bool(YES.search(text))
    if no:
        return False
    if yes:
        return True
    return None


CALL_TEXTS = {"run_shell": ("call_shell", "command"), "install_package": ("call_install", "names"),
              "remove_package": ("call_remove", "names"), "system_update": ("call_update", ""),
              "calendar_update": ("call_cal_update", "query"), "calendar_delete": ("call_cal_delete", "query"),
              "trilium_update_note": ("call_trilium", "note"), "write_file": ("call_write", "path")}


def describe_call(name: str, args: dict, cfg=None) -> str:
    key, field = CALL_TEXTS.get(name, ("call_other", ""))
    value = str(args.get(field, "")) if field else name
    return prompts.spoken(cfg, key, v=value)


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
        self.askpass = None  # AskpassBroker (Passwortfeld für sudo -A)
        self.think: bool | None = None  # Denkmodus-Knopf der Oberfläche (None = Profil-Einstellung)
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
                              "reason": reason, "summary": describe_call(name, args, self.cfg)})
        self.speaker.say(prompts.spoken(self.cfg, "confirm", what=describe_call(name, args, self.cfg)))
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
                self.speaker.say(prompts.spoken(self.cfg, "yes_no"))
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
            await self.agent.run(text, self.emit, self.confirm, think=self.think)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("Agent-Fehler")
            await self.emit({"type": "error", "text": f"Interner Fehler: {e}"})
        finally:
            if not self.agent.lock.locked():
                await self.broadcast({"type": "state", "state": "idle"})

    async def stop(self) -> None:
        if self.askpass:
            self.askpass.cancel_all()
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
    llm = FakeLLM(language=cfg.language) if fake else LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
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
    # Root-Rechte per Passwortfeld in der Oberfläche (sudo -A)
    helper = None
    if cfg.tools.privilege_cmd == "jarvis":
        try:
            helper = askpass.write_helper()
        except OSError as e:
            log.warning("Askpass-Helfer konnte nicht angelegt werden: %s", e)
    broker = askpass.AskpassBroker(cfg.port, notify=hub.broadcast, helper=helper,
                                   has_ui=lambda: bool(hub.clients), say=hub.speaker.say,
                                   say_text=prompts.spoken(cfg, "password"))
    askpass.BROKER = broker
    hub.askpass = broker
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
        kind = prompts.spoken(cfg, "kind_timer" if r.kind == "timer" else "kind_reminder")
        spoken = (prompts.spoken(cfg, "missed", kind=kind, time=r.due_dt.strftime("%H:%M"), text=r.text) if late
                  else prompts.spoken(cfg, "timer" if r.kind == "timer" else "reminder", text=r.text))
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
        html = translate_index((WEB_DIR / "index.html").read_text(encoding="utf-8"), cfg.language)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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

    @app.post("/api/askpass")
    async def askpass_request(request: Request):
        """Vom Askpass-Helfer (sudo -A) aufgerufen – nur mit gültigem Einmal-Token."""
        token = request.headers.get("x-jarvis-askpass", "")
        try:
            prompt = str((await request.json()).get("prompt", ""))[:200]
        except ValueError:
            prompt = ""
        password = await broker.request(token, prompt)
        if password is None:
            return JSONResponse({"error": "abgelehnt"}, status_code=403)
        return JSONResponse({"password": password}, headers={"Cache-Control": "no-store"})

    # ---------- Modelle hinzufügen (Vorauswahl + ollama pull mit Fortschritt) ----------
    state_file = cfg.memory.dir.parent / "state.json"
    pulls: dict[str, asyncio.Task] = {}

    @app.get("/api/models/presets")
    async def model_presets():
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        installed = await asyncio.to_thread(mdl.installed_models, cfg.llm.base_url)
        return {"gpu": gpu, "presets": mdl.preset_list(gpu["vram_gb"], installed),
                "pulling": sorted(pulls)}

    async def pull_model(tag: str) -> None:
        from . import models as mdl
        last = 0.0
        try:
            async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=None) as client:
                async with client.stream("POST", "/api/pull", json={"model": tag, "stream": True}) as resp:
                    if resp.status_code != 200:
                        raise LLMError(f"Ollama {resp.status_code}: {(await resp.aread()).decode(errors='replace')[:200]}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        if ev.get("error"):
                            raise LLMError(ev["error"])
                        now = time.monotonic()
                        if now - last > 0.5 or ev.get("status") == "success":
                            last = now
                            await hub.broadcast({"type": "model_pull", "tag": tag, "status": ev.get("status", ""),
                                                 "completed": ev.get("completed"), "total": ev.get("total")})
            name = mdl.register_model(state_file, tag)
            if isinstance(llm, LLMRouter):
                llm.add_downloaded_models()
            await hub.broadcast({"type": "model_pull", "tag": tag, "done": True, "profile": name})
        except (httpx.HTTPError, LLMError, ValueError) as e:
            text = str(e) if not isinstance(e, httpx.ConnectError) else "Ollama ist nicht erreichbar"
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": text})
        finally:
            pulls.pop(tag, None)

    @app.post("/api/models/pull")
    async def model_pull(request: Request):
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        tag = str((await request.json()).get("tag", "")).strip().lower()
        if not mdl.TAG_RE.match(tag):
            raise HTTPException(400, "Ungültiger Modellname")
        if tag not in pulls:
            pulls[tag] = asyncio.create_task(pull_model(tag))
        return {"ok": True, "tag": tag}

    @app.post("/api/models/reload")
    async def models_reload():
        added = llm.add_downloaded_models() if isinstance(llm, LLMRouter) else []
        return {"added": added}

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
        conv = memory.conversation
        msgs = [m for m in conv.history if m["role"] in ("user", "assistant") and m.get("content")]
        return {"summary": conv.running_summary, "chat": {"id": conv.chat_id, "title": conv.meta.get("title", "")},
                "messages": [{"role": m["role"], "content": m["content"]} for m in msgs[-40:]]}

    # ---------- Chat-Historie ----------
    def chat_or_404(chat_id: str) -> None:
        if not memory.chats.exists(chat_id):
            raise HTTPException(404, "Chat nicht gefunden")

    def not_busy() -> None:
        if agent.lock.locked():
            raise HTTPException(409, "Jarvis arbeitet gerade – bitte kurz warten oder STOP drücken")

    async def chat_switched() -> None:
        conv = memory.conversation
        await hub.broadcast({"type": "chat_switched", "id": conv.chat_id, "title": conv.meta.get("title", "")})

    @app.get("/api/chats")
    async def chats(q: str = ""):
        memory.conversation.save()
        return memory.chats.list(q)

    @app.post("/api/chats")
    async def chat_new():
        not_busy()
        memory.new_chat()
        await chat_switched()
        return {"id": memory.conversation.chat_id}

    @app.post("/api/chats/{chat_id}/activate")
    async def chat_activate(chat_id: str):
        chat_or_404(chat_id)
        not_busy()
        memory.switch_chat(chat_id)
        await chat_switched()
        return {"id": chat_id}

    @app.post("/api/chats/{chat_id}/star")
    async def chat_star(chat_id: str, request: Request):
        chat_or_404(chat_id)
        body = await request.json()
        memory.star_chat(chat_id, bool(body.get("starred")))
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True}

    @app.patch("/api/chats/{chat_id}")
    async def chat_rename(chat_id: str, request: Request):
        chat_or_404(chat_id)
        title = str((await request.json()).get("title", "")).strip()
        if not title:
            raise HTTPException(400, "Titel fehlt")
        memory.rename_chat(chat_id, title)
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def chat_delete(chat_id: str):
        chat_or_404(chat_id)
        was_active = chat_id == memory.conversation.chat_id
        if was_active:
            not_busy()
        days = memory.forget_chat(chat_id)
        if was_active:
            await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True, "days": days}

    @app.get("/api/voices")
    async def voices():
        if not tts:
            return {"available": False, "voices": []}
        tts.available()
        return {"available": True, "current": tts.current, "rate": tts.rate,
                "voices": catalog.voice_list(tts.voices_dir, tts.current, cfg.language)}

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
                           "pending": [cid for cid in hub.pending], "context": agent.last_context})
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
                elif t == "password":
                    # Passwort nur an den wartenden sudo weiterreichen – nie loggen oder speichern
                    broker.answer(str(data.get("id", "")), data.get("password") or None)
                elif t == "password_cancel":
                    broker.answer(str(data.get("id", "")), None)
                elif t == "think":
                    hub.think = bool(data.get("enabled"))
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
                    # NEU-Knopf: neuer Chat (der bisherige bleibt in der Historie)
                    if not agent.lock.locked():
                        memory.new_chat()
                        await hub.broadcast({"type": "conversation_reset"})
                        await chat_switched()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(client)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
