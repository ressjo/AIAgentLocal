"""Modell-Profile: aktives Chat-Modell (Ollama oder OpenAI-kompatibler Server) umschalten, optional den
Modell-Server (z. B. llama-server für Bonsai) selbst starten/stoppen. Embeddings laufen immer über Ollama."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx

from .config import LLMConfig, ProfileConfig, ServerConfig
from .llm import LLMError, OllamaLLM, OpenAICompatLLM

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


def _log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    state.mkdir(parents=True, exist_ok=True)
    return state / "jarvis-llm.log"


class ManagedServer:
    """Startet einen Modell-Server als eigene Prozessgruppe und wartet, bis er bereit ist."""

    def __init__(self, server: ServerConfig, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = server
        self.base_url = base_url.rstrip("/")
        self.proc: subprocess.Popen | None = None
        self.transport = transport

    @property
    def health_url(self) -> str:
        if self.cfg.health_url:
            return self.cfg.health_url
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        return root + "/health"

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport) as c:
                r = await c.get(self.health_url)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def port_in_use(self) -> bool:
        """Antwortet überhaupt etwas (auch 503 = lädt noch)?"""
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport) as c:
                await c.get(self.health_url)
            return True
        except httpx.HTTPError:
            return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def ensure_running(self, progress: Progress | None = None) -> bool:
        """True, wenn Jarvis den Server gestartet hat; False, wenn er bereits lief."""
        if await self.healthy():
            return False
        if not self.running() and not await self.port_in_use():
            command = os.path.expanduser(self.cfg.command)
            env = {**os.environ, **{k: str(v) for k, v in self.cfg.env.items()}}
            cwd = os.path.expanduser(self.cfg.cwd) if self.cfg.cwd else str(Path.home())
            log_file = _log_path().open("ab")
            log_file.write(f"\n===== Starte: {command}\n".encode())
            log_file.flush()
            self.proc = subprocess.Popen(["bash", "-c", command], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                         stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
            log_file.close()
            if progress:
                await progress("Modell-Server wird gestartet …")
        waited = 0.0
        while waited < self.cfg.startup_timeout:
            if await self.healthy():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                raise LLMError(f"Modell-Server ist beim Start beendet worden (Exit-Code {self.proc.returncode}).\n"
                               + tail_log())
            await asyncio.sleep(1)
            waited += 1
            if progress and int(waited) % 10 == 0:
                await progress(f"Modell wird geladen … {int(waited)} s")
        await self.stop()
        raise LLMError(f"Modell-Server nicht rechtzeitig bereit ({int(self.cfg.startup_timeout)} s).\n" + tail_log())

    async def stop(self) -> None:
        if not self.running():
            self.proc = None
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None


def tail_log(lines: int = 15) -> str:
    try:
        text = _log_path().read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return "Letzte Zeilen aus ~/.local/state/jarvis-llm.log:\n" + "\n".join(text)


class LLMRouter:
    """Bietet dieselbe Schnittstelle wie OllamaLLM (chat_stream, chat, embed, status, close)."""

    def __init__(self, cfg: LLMConfig, state_path: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.profiles = cfg.resolved_profiles()
        self.state_path = state_path
        self.transport = transport
        self.ollama = OllamaLLM(cfg, transport=transport)  # Embeddings + Entladen
        self.active = self._initial_profile()
        self.client = None
        self.servers: dict[str, ManagedServer] = {}
        self.switching: str | None = None
        self._lock = asyncio.Lock()
        self._build_client()

    # ---------- Auswahl ----------
    def _initial_profile(self) -> str:
        remembered = self._read_state().get("active_profile")
        for name in (remembered, self.cfg.active):
            if name and name in self.profiles:
                return name
        if self.cfg.active and self.cfg.active not in self.profiles:
            log.warning("Profil '%s' aus llm.active existiert nicht", self.cfg.active)
        return next(iter(self.profiles))

    def _read_state(self) -> dict:
        if self.state_path and self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _write_state(self) -> None:
        if not self.state_path:
            return
        data = self._read_state()
        data["active_profile"] = self.active
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    @property
    def profile(self) -> ProfileConfig:
        return self.profiles[self.active]

    def _client_for(self, p: ProfileConfig):
        if p.backend == "openai":
            return OpenAICompatLLM(p, timeout=self.cfg.request_timeout, transport=self.transport)
        return OllamaLLM(self.cfg.model_copy(update={
            "base_url": p.base_url, "model": p.model, "temperature": p.temperature,
            "num_ctx": p.num_ctx, "think": p.think}), transport=self.transport)

    def _build_client(self) -> None:
        self.client = self._client_for(self.profile)
        self.ollama.embed_on_cpu = self.profile.embed_on_cpu

    def server_for(self, name: str) -> ManagedServer | None:
        p = self.profiles[name]
        if not p.server:
            return None
        if name not in self.servers:
            self.servers[name] = ManagedServer(p.server, p.base_url, transport=self.transport)
        return self.servers[name]

    async def _prepare(self, name: str, progress: Progress | None) -> None:
        p = self.profiles[name]
        if p.unload_ollama:
            unloaded = await self.ollama.unload_all()
            if unloaded and progress:
                await progress("Grafikspeicher freigegeben (" + ", ".join(unloaded) + ")")
        server = self.server_for(name)
        if server:
            await server.ensure_running(progress)

    async def start(self, progress: Progress | None = None) -> None:
        """Beim Start von Jarvis: aktives Profil vorbereiten (Server starten usw.)."""
        try:
            await self._prepare(self.active, progress)
        except LLMError as e:
            log.error("Profil '%s' konnte nicht gestartet werden: %s", self.active, e)

    async def activate(self, name: str, progress: Progress | None = None) -> None:
        if name not in self.profiles:
            raise LLMError(f"Unbekanntes Profil '{name}'. Vorhanden: {', '.join(self.profiles)}")
        async with self._lock:
            if name == self.active and (not self.server_for(name) or await self.server_for(name).healthy()):
                return
            previous = self.active
            self.switching = name
            try:
                # Zuerst den bisherigen, von Jarvis gestarteten Server beenden (gibt VRAM frei)
                old = self.servers.get(previous)
                if old and previous != name:
                    await old.stop()
                await self._prepare(name, progress)
            except LLMError:
                # Zurück zum vorherigen Profil
                self.switching = None
                try:
                    await self._prepare(previous, None)
                except LLMError:
                    pass
                raise
            finally:
                self.switching = None
            old_client = self.client
            self.active = name
            self._build_client()
            self._write_state()
            if old_client is not None and old_client is not self.ollama:
                await old_client.close()

    # ---------- LLM-Schnittstelle ----------
    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        async for ev in self.client.chat_stream(messages, tools):
            yield ev

    async def chat(self, messages: list[dict]) -> str:
        return await self.client.chat(messages)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.ollama.embed(texts)

    async def status(self) -> dict:
        p = self.profile
        st = await self.client.status()
        if p.backend == "openai":
            emb = await self.ollama.status()
            st.update({"embed_model": self.cfg.embed_model, "embed_available": emb.get("embed_available", False)})
        st.update({"profile": self.active, "label": p.label, "backend": p.backend, "switching": self.switching,
                   "model": p.model})
        return st

    def describe(self) -> list[dict]:
        return [{"name": n, "label": p.label, "backend": p.backend, "model": p.model, "base_url": p.base_url,
                 "managed": p.server is not None, "active": n == self.active} for n, p in self.profiles.items()]

    async def close(self) -> None:
        for server in self.servers.values():
            await server.stop()
        if self.client is not None:
            await self.client.close()
        await self.ollama.close()
