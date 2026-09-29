"""„Denke nach“ darf nicht hängen bleiben: nach dem Laden des Modells und nach jeder Antwort kommt „idle“."""

import asyncio

from conftest import run
from fastapi.testclient import TestClient

from orbwise import llm_router, server
from orbwise.agent import Agent

WS = "ws://localhost:8765/ws"
ORIGIN = {"Origin": "http://localhost:8765"}


def test_agent_reports_idle_right_after_the_answer(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False

    compacting = []
    original = memory.conversation.compact

    async def slow_compact(*a, **kw):  # Verdichten läuft nach der Antwort – die Anzeige soll schon „bereit“ sein
        compacting.append([e.get("state") for e in events if e["type"] == "state"][-1])
        return await original(*a, **kw)

    memory.conversation.compact = slow_compact
    run(agent.run("Hallo", emit, confirm))
    types = [(e["type"], e.get("state")) for e in events]
    end = types.index(("assistant_end", None))
    assert ("state", "idle") in types[end:]
    assert compacting == ["idle"]


def test_ui_leaves_thinking_after_model_start(cfg, monkeypatch):
    """Wer sich verbindet, während das Modell beim Start lädt, sieht „denke nach“ – danach muss „idle“ kommen."""
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")

    async def slow_start(self, progress=None):
        await asyncio.sleep(0.6)

    monkeypatch.setattr(llm_router.LLMRouter, "start", slow_start)
    cfg.llm.base_url = "http://127.0.0.1:9"
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["busy"] is True
            seen = []
            while ("state", "idle") not in seen:
                ev = ws.receive_json()
                seen.append((ev["type"], ev.get("state")))
            assert ("model_active", None) in seen


def test_model_start_failure_also_ends_thinking(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")

    async def broken_start(self, progress=None):
        await asyncio.sleep(0.4)
        raise llm_router.LLMError("llama-server ist beim Start beendet worden")

    monkeypatch.setattr(llm_router.LLMRouter, "start", broken_start)
    cfg.llm.base_url = "http://127.0.0.1:9"
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            assert ws.receive_json()["busy"] is True
            seen = []
            while ("state", "idle") not in seen:
                ev = ws.receive_json()
                seen.append((ev["type"], ev.get("state")))
            assert ("model_error", None) in seen


def test_cancelled_transcription_does_not_block_the_microphone():
    """Wird die Transkription abgebrochen, darf „Transkribiere …“ nicht stehen bleiben."""
    import threading

    import numpy as np

    from orbwise.config import VoiceConfig
    from orbwise.voice.listen import AudioSession

    release = threading.Event()

    class SlowSTT:
        def transcribe(self, audio):
            release.wait(5)
            return "zu spät"

    sent, heard = [], []

    async def send(ev):
        sent.append(ev)

    async def on_text(text):
        heard.append(text)

    async def scenario():
        s = AudioSession(VoiceConfig(), SlowSTT(), None, send, on_text)
        s.recorded = [np.ones(16000, dtype=np.int16)]
        await s._finish()
        assert s.mode == "transcribing"
        await asyncio.sleep(0.05)
        s.task.cancel()
        release.set()
        await asyncio.gather(s.task, return_exceptions=True)
        return s

    s = run(scenario())
    assert s.mode == "idle" and sent[-1] == {"type": "voice", "state": "idle"} and not heard


def test_ui_files_are_revalidated_after_updates(cfg):
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        resp = client.get("/static/app.js")
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache"
        again = client.get("/static/app.js", headers={"If-None-Match": resp.headers["etag"]})
        assert again.status_code == 304
