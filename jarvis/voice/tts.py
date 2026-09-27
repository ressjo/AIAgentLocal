"""Sprachausgabe mit Piper (lokal) + satzweises Streaming."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import wave
from collections.abc import Awaitable, Callable

from ..config import VoiceConfig

log = logging.getLogger(__name__)


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "Link", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class SentenceSplitter:
    """Sammelt Tokens und gibt vollständige Sätze zurück, sobald sie fertig sind. Codeblöcke werden übersprungen."""

    MIN_CHARS = 30
    BOUNDARY = re.compile(r"(?<=[.!?:;])\s+|\n+")

    def __init__(self):
        self.buf = ""
        self.in_code = False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    return out
                self.buf = self.buf[end + 3:]
                self.in_code = False
                continue
            start = self.buf.find("```")
            scan = self.buf if start < 0 else self.buf[:start]
            cut = None
            for m in self.BOUNDARY.finditer(scan):
                if m.start() >= self.MIN_CHARS or "\n" in m.group(0):
                    cut = m
                    break
            if cut:
                sentence, self.buf = self.buf[:cut.start()], self.buf[cut.end():]
                if sentence.strip():
                    out.append(sentence.strip())
                continue
            if start >= 0:
                if scan.strip():
                    out.append(scan.strip())
                self.buf = self.buf[start + 3:]
                self.in_code = True
                continue
            return out

    def flush(self) -> list[str]:
        rest, self.buf = ("" if self.in_code else self.buf), ""
        self.in_code = False
        return [rest.strip()] if rest.strip() else []


class PiperTTS:
    def __init__(self, cfg: VoiceConfig):
        self.cfg = cfg
        self._voice = None
        self._error: str | None = None

    def available(self) -> bool:
        if not self.cfg.tts_voice.exists():
            self._error = f"Stimme fehlt: {self.cfg.tts_voice}"
            return False
        try:
            import piper  # noqa: F401
        except ImportError:
            self._error = "piper-tts ist nicht installiert"
            return False
        return True

    @property
    def error(self) -> str | None:
        return self._error

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice
            self._voice = PiperVoice.load(str(self.cfg.tts_voice))
        return self._voice

    def synth(self, text: str) -> bytes:
        voice = self._load()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            try:
                from piper import SynthesisConfig
                voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(length_scale=self.cfg.tts_length_scale))
            except ImportError:  # ältere piper-tts-Versionen
                voice.synthesize(text, wav, length_scale=self.cfg.tts_length_scale)
        return buf.getvalue()


class Speaker:
    """Wandelt gestreamte Antworten satzweise in Audio um und verteilt es an die Clients.
    Ohne Piper werden nur die Sätze geschickt – der Browser spricht sie dann selbst (Web Speech API)."""

    def __init__(self, tts: PiperTTS | None, send: Callable[[dict], Awaitable[None]],
                 wanted: Callable[[], bool]):
        self.tts = tts if tts and tts.available() else None
        self.send = send
        self.wanted = wanted
        self.splitters: dict[str, SentenceSplitter] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.generation = 0
        self.seq = 0
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        self.worker = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()

    def feed(self, msg_id: str, text: str) -> None:
        if not self.wanted():
            return
        sp = self.splitters.setdefault(msg_id, SentenceSplitter())
        for s in sp.feed(text):
            self._enqueue(msg_id, s)

    def end(self, msg_id: str) -> None:
        sp = self.splitters.pop(msg_id, None)
        if sp and self.wanted():
            for s in sp.flush():
                self._enqueue(msg_id, s)

    def say(self, text: str, msg_id: str = "system") -> None:
        if self.wanted():
            self._enqueue(msg_id, text)

    def stop(self) -> None:
        self.generation += 1
        self.splitters.clear()
        while not self.queue.empty():
            self.queue.get_nowait()

    def _enqueue(self, msg_id: str, sentence: str) -> None:
        spoken = clean_for_speech(sentence)
        if re.search(r"\w", spoken):
            self.seq += 1
            self.queue.put_nowait((self.generation, msg_id, self.seq, spoken))

    async def _run(self) -> None:
        while True:
            gen, msg_id, seq, text = await self.queue.get()
            if gen != self.generation:
                continue
            event = {"type": "speak", "id": msg_id, "seq": seq, "text": text}
            if self.tts:
                try:
                    wav = await asyncio.to_thread(self.tts.synth, text)
                    event["audio"] = base64.b64encode(wav).decode()
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS fehlgeschlagen: %s", e)
            if gen == self.generation:
                await self.send(event)
