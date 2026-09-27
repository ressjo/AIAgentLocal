import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from conftest import run

from jarvis.agent import Agent
from jarvis.config import Config, LLMConfig, ProfileConfig, ServerConfig
from jarvis.llm import LLMError, OpenAICompatLLM, to_openai_messages
from jarvis.llm_router import LLMRouter, ManagedServer

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- Config

def test_legacy_config_becomes_standard_profile():
    cfg = Config.model_validate({"llm": {"model": "qwen3:8b", "base_url": "http://nas:11434"}})
    profiles = cfg.llm.resolved_profiles()
    assert list(profiles) == ["standard"]
    p = profiles["standard"]
    assert p.backend == "ollama" and p.model == "qwen3:8b" and p.base_url == "http://nas:11434"
    assert p.temperature == 0.6 and p.think is False


def test_profiles_config():
    cfg = Config.model_validate({"llm": {"active": "bonsai", "profiles": {
        "qwen": {"model": "qwen3:8b"},
        "bonsai": {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1/", "model": "bonsai",
                   "num_ctx": 8192, "embed_on_cpu": True,
                   "server": {"command": "~/bonsai/scripts/start_llama_server.sh -np 1",
                              "env": {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}}}}}})
    ps = cfg.llm.resolved_profiles()
    assert ps["qwen"].base_url == "http://localhost:11434"
    assert ps["bonsai"].base_url == "http://127.0.0.1:8080/v1" and ps["bonsai"].num_ctx == 8192
    assert ps["bonsai"].server.env["HSA_OVERRIDE_GFX_VERSION"] == "10.3.0"
    with pytest.raises(ValueError):
        Config.model_validate({"llm": {"profiles": {"x": {"backend": "magic"}}}})


def test_example_config_profiles_parse():
    import yaml
    data = yaml.safe_load((HERE.parent / "jarvis" / "config.example.yaml").read_text())
    Config.model_validate(data).llm.resolved_profiles()


# ---------------------------------------------------------------- Nachrichtenformat

def test_to_openai_messages():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {"x": 1}}},
            {"function": {"name": "b", "arguments": "{\"y\": 2}"}}]},
        {"role": "tool", "content": "ra", "tool_name": "a"},
        {"role": "tool", "content": "rb", "tool_name": "b"},
        {"role": "assistant", "content": "fertig"},
    ]
    out = to_openai_messages(msgs)
    calls = out[2]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1} and calls[1]["function"]["arguments"] == '{"y": 2}'
    assert out[3] == {"role": "tool", "tool_call_id": calls[0]["id"], "content": "ra"}
    assert out[4]["tool_call_id"] == calls[1]["id"]
    assert out[5] == {"role": "assistant", "content": "fertig"}


# ---------------------------------------------------------------- OpenAI-Client

def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def client_with(handler, **profile) -> OpenAICompatLLM:
    p = ProfileConfig(backend="openai", base_url="http://llm/v1", model="bonsai", temperature=0.6, think=False,
                      **profile)
    return OpenAICompatLLM(p, transport=httpx.MockTransport(handler))


def collect(llm, messages, tools=None):
    async def go():
        return [e async for e in llm.chat_stream(messages, tools)]
    return run(go())


def test_stream_text_and_timings():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, text=sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Guten "}}]},
            {"choices": [{"delta": {"content": "Abend."}}]},
            {"choices": [], "timings": {"predicted_n": 5, "predicted_per_second": 19.244,
                                        "prompt_n": 800, "prompt_per_second": 300.0}}))

    events = collect(client_with(handler, api_key="geheim"), [{"role": "user", "content": "Hi"}], [{"type": "function"}])
    assert [e["text"] for e in events if e["type"] == "token"] == ["Guten ", "Abend."]
    done = events[-1]
    assert done["message"] == {"role": "assistant", "content": "Guten Abend."}
    assert done["stats"] == {"tokens": 5, "tps": 19.2, "prompt_tokens": 800, "prompt_tps": 300.0}
    assert seen["auth"] == "Bearer geheim"
    body = seen["body"]
    assert body["stream"] and body["tools"] and body["chat_template_kwargs"] == {"enable_thinking": False}


def test_stream_fragmented_tool_calls():
    handler = lambda req: httpx.Response(200, text=sse(  # noqa: E731
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "find_", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "files", "arguments": "{\"que"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ry\": \"pdf\"}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "system_info", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 20, "prompt_tokens": 10}}))
    done = collect(client_with(handler), [{"role": "user", "content": "x"}])[-1]
    assert done["message"]["tool_calls"] == [
        {"function": {"name": "find_files", "arguments": {"query": "pdf"}}},
        {"function": {"name": "system_info", "arguments": {}}}]
    assert done["stats"]["tokens"] == 20


def test_errors():
    llm = client_with(lambda req: httpx.Response(500, text="kaputt"))
    with pytest.raises(LLMError, match="500"):
        collect(llm, [{"role": "user", "content": "x"}])

    def refuse(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="nicht erreichbar"):
        collect(client_with(refuse), [{"role": "user", "content": "x"}])


# ---------------------------------------------------------------- Router

def make_llm_cfg(port: int, server: dict | None = None) -> LLMConfig:
    bonsai = {"backend": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "model": "bonsai", "embed_on_cpu": True}
    if server:
        bonsai["server"] = server
    return LLMConfig.model_validate({"active": "qwen", "profiles": {"qwen": {"model": "qwen3:8b"}, "bonsai": bonsai}})


def ollama_mock(calls):
    def handler(req):
        calls.append((req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}, {"name": "qwen3:8b"}]})
        return httpx.Response(200, json={})
    return handler


def test_router_embed_on_cpu_and_state(tmp_path):
    calls = []
    cfg = make_llm_cfg(9)
    router = LLMRouter(cfg, state_path=tmp_path / "state.json", transport=httpx.MockTransport(ollama_mock(calls)))
    assert router.active == "qwen"
    run(router.embed(["a"]))
    assert "options" not in calls[-1][1]
    # Umschalten ohne verwalteten Server (Server „läuft extern“ – hier nicht nötig)
    router.active = "bonsai"
    router._build_client()
    run(router.embed(["a"]))
    assert calls[-1][1]["options"] == {"num_gpu": 0}
    router._write_state()
    assert LLMRouter(cfg, state_path=tmp_path / "state.json").active == "bonsai"


def test_unknown_profile(tmp_path):
    router = LLMRouter(make_llm_cfg(9), state_path=tmp_path / "s.json")
    with pytest.raises(LLMError, match="Unbekanntes Profil"):
        run(router.activate("gibtsnicht"))


def server_cfg(port: int, delay: float = 0.0, timeout: float = 20) -> dict:
    return {"command": f"{sys.executable} {HERE / 'fake_llama_server.py'} {port} {delay}", "startup_timeout": timeout}


def test_managed_server_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    server = ManagedServer(ServerConfig(**server_cfg(port, delay=1.5)), f"http://127.0.0.1:{port}/v1")
    progress = []

    async def prog(t):
        progress.append(t)

    async def scenario():
        started = await server.ensure_running(prog)
        assert started and await server.healthy() and server.running()
        again = await server.ensure_running(prog)  # läuft schon → kein zweiter Start
        assert again is False
        pid = server.proc.pid
        await server.stop()
        assert not server.running()
        return pid

    pid = run(scenario())
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(pid, 0)
    assert progress and "gestartet" in progress[0]


def test_managed_server_start_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = ManagedServer(ServerConfig(command="echo 'GPU nicht gefunden' >&2; exit 3", startup_timeout=10),
                           f"http://127.0.0.1:{free_port()}/v1")
    with pytest.raises(LLMError, match="Exit-Code 3") as err:
        run(server.ensure_running())
    assert "GPU nicht gefunden" in str(err.value)


def test_switch_starts_server_unloads_ollama_and_agent_works(cfg, llm, memory, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    llm_cfg = make_llm_cfg(port, server_cfg(port))
    llm_cfg.profiles["bonsai"].unload_ollama = True
    calls = []
    router = LLMRouter(llm_cfg, state_path=tmp_path / "state.json")
    # Ollama-Aufrufe abfangen, echte HTTP-Aufrufe zum Fake-llama-server zulassen
    router.ollama = type(router.ollama)(llm_cfg, transport=httpx.MockTransport(ollama_mock(calls)))
    cfg.llm = llm_cfg

    async def scenario():
        await router.activate("bonsai")
        assert router.active == "bonsai" and router.servers["bonsai"].running()
        assert ("/api/generate", {"model": "qwen3:8b", "keep_alive": 0}) in calls
        events = []

        async def emit(e):
            events.append(e)

        async def confirm(*a):
            return True

        agent = Agent(cfg, router, memory)
        answer = await agent.run("Hallo Bonsai", emit, confirm)
        stats = [e for e in events if e["type"] == "llm_stats"]
        # Tool-Aufruf über den Fake-Server: list_updates → Ergebnis → Antwort
        answer2 = await agent.run("Mach ein Systemupdate-Check", emit, confirm)
        tool_calls = [e["name"] for e in events if e["type"] == "tool_call"]
        await router.activate("qwen")
        stopped = not router.servers["bonsai"].running()
        await router.close()
        return answer, stats, answer2, tool_calls, stopped

    answer, stats, answer2, tool_calls, stopped = run(scenario())
    assert answer.startswith("Bonsai meldet: Hallo Bonsai")
    assert stats and stats[0]["tps"] == 19.2
    assert tool_calls == ["list_updates"] and answer2.startswith("Bonsai meldet")
    assert stopped
    assert json.loads((tmp_path / "state.json").read_text())["active_profile"] == "qwen"


def test_commented_example_profile_is_valid():
    import re

    import yaml
    text = (HERE.parent / "jarvis" / "config.example.yaml").read_text()
    block = text[text.index("  # active: bonsai"):text.index("memory:")]
    enabled = text.replace(block, re.sub(r"^  # ?", "  ", block, flags=re.M))
    cfg = Config.model_validate(yaml.safe_load(enabled))
    ps = cfg.llm.resolved_profiles()
    assert cfg.llm.active == "bonsai" and ps["bonsai"].backend == "openai" and ps["bonsai"].server
    assert "--api-key" in ps["bonsai"].server.command and ps["bonsai"].api_key
