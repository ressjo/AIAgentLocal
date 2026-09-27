"""LLM-Anbindung: Ollama (Chat mit Tool-Calls, Streaming, Embeddings) und ein FakeLLM für Tests/Demo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class OllamaLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.request_timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[dict], tools: list[dict] | None, stream: bool) -> dict:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self.cfg.keep_alive,
            "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx},
            "think": self.cfg.think,
        }
        if tools:
            payload["tools"] = tools
        return payload

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        """Liefert {"type": "token", "text": ...} und abschließend {"type": "done", "message": {...}}."""
        content: list[str] = []
        tool_calls: list[dict] = []
        try:
            async with self._client.stream("POST", "/api/chat", json=self._payload(messages, tools, True)) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise LLMError(f"Ollama antwortet mit {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise LLMError(chunk["error"])
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        content.append(msg["content"])
                        yield {"type": "token", "text": msg["content"]}
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        break
        except httpx.ConnectError as e:
            raise LLMError(
                f"Ollama unter {self.cfg.base_url} nicht erreichbar – läuft 'ollama serve'?"
            ) from e
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        yield {"type": "done", "message": message}

    async def chat(self, messages: list[dict]) -> str:
        try:
            resp = await self._client.post("/api/chat", json=self._payload(messages, None, False))
        except httpx.ConnectError as e:
            raise LLMError(f"Ollama unter {self.cfg.base_url} nicht erreichbar") from e
        if resp.status_code != 200:
            raise LLMError(f"Ollama antwortet mit {resp.status_code}: {resp.text[:300]}")
        return strip_think(resp.json().get("message", {}).get("content", ""))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(
            "/api/embed", json={"model": self.cfg.embed_model, "input": texts, "keep_alive": self.cfg.keep_alive}
        )
        if resp.status_code != 200:
            raise LLMError(f"Embedding fehlgeschlagen ({resp.status_code}): {resp.text[:200]}")
        return resp.json()["embeddings"]

    async def status(self) -> dict:
        try:
            resp = await self._client.get("/api/tags", timeout=3)
            names = [m["name"] for m in resp.json().get("models", [])]
        except Exception as e:  # noqa: BLE001
            return {"online": False, "error": str(e), "model": self.cfg.model}
        def has(model: str) -> bool:
            return any(n == model or n == f"{model}:latest" or n.split(":")[0] == model for n in names)
        return {
            "online": True,
            "model": self.cfg.model,
            "model_available": has(self.cfg.model),
            "embed_model": self.cfg.embed_model,
            "embed_available": has(self.cfg.embed_model),
        }


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


class FakeLLM:
    """Deterministisches Ersatz-LLM (JARVIS_FAKE_LLM=1) für Tests und UI-Demo ohne Ollama.

    - "/tool <name> <json-args>" erzeugt einen Tool-Call
    - "systemupdate" löst system_update aus, "welche dateien ..." find_files
    - nach einem Tool-Ergebnis wird dieses kurz zusammengefasst
    """

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def close(self) -> None:
        pass

    def _decide(self, messages: list[dict]) -> dict:
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": f"Erledigt. Ergebnis: {last['content'][:200]}"}
        text = last.get("content", "")
        if text.startswith("/tool "):
            parts = text.split(" ", 2)
            args = json.loads(parts[2]) if len(parts) > 2 else {}
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": parts[1], "arguments": args}}]}
        low = text.lower()
        if "systemupdate" in low:
            return {"role": "assistant", "content": "Ich starte das Systemupdate.",
                    "tool_calls": [{"function": {"name": "system_update", "arguments": {}}}]}
        if low.startswith("welche dateien"):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "find_files", "arguments": {"query": text.split()[-1]}}}]}
        return {"role": "assistant",
                "content": f"Sehr wohl. Sie sagten: {text}. Wie kann ich sonst behilflich sein?"}

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
        self.calls.append(messages)
        msg = self._decide(messages)
        for word in re.findall(r"\S+\s*", msg["content"]):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {"type": "token", "text": word}
        yield {"type": "done", "message": msg}

    async def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        body = messages[-1]["content"]
        return "Zusammenfassung: " + " ".join(body.split()[:60])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [fake_embedding(t) for t in texts]

    async def status(self) -> dict:
        return {"online": True, "model": "fake", "model_available": True,
                "embed_model": "fake", "embed_available": True}


def fake_embedding(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
