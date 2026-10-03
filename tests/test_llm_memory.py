"""Kontext im Speicher (VRAM/RAM) anzeigen und das Kontextfenster in der Oberfläche ändern."""

import json

import httpx
from conftest import run

from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import OllamaLLM
from orbwise.llm_memory import kv_per_token_from_info, parse_llama_log, recommend, summary
from orbwise.llm_router import LLMRouter

GB = 1024 ** 3
LOG = """altes Zeug
llama_model_loader: loaded meta data with 30 key-value pairs
print_info: n_ctx_train      = 40960
load_tensors: offloaded 49/49 layers to GPU
load_tensors:        ROCm0 model buffer size =  6825.32 MiB
load_tensors:   CPU_Mapped model buffer size =   417.66 MiB
llama_context: n_ctx         = 16384
llama_kv_cache_unified:      ROCm0 KV buffer size =   512.00 MiB
llama_context:      ROCm0 compute buffer size =   300.00 MiB
"""


def test_llama_log_shows_where_the_context_lives():
    info = parse_llama_log("früherer Start\nllama_kv_cache_unified: ROCm0 KV buffer size = 9999.00 MiB\n" + LOG)
    s = summary(info)
    assert info["ctx"] == 16384 and info["kv_vram"] == 512 * 1024 ** 2 and info["kv_ram"] == 0  # nur der letzte Start
    assert s["kv_vram_share"] == 1.0 and info["kv_per_token"] == 32768 and info["model_ram_offload"] == 0
    assert parse_llama_log("nichts dergleichen") is None


def test_recommendation_grows_with_free_vram_and_shrinks_on_spill():
    info = parse_llama_log(LOG)
    roomy = recommend(info, {"vram_total": 12 * GB, "vram_used": 8 * GB})
    assert roomy["verdict"] == "increase" and roomy["max_ctx"] == 32768  # 40k trainiert → höchste Stufe darunter
    assert all(o["ctx"] <= 40960 for o in roomy["options"])
    tight = recommend(info, {"vram_total": 8 * GB, "vram_used": 7.9 * GB})
    assert tight["verdict"] == "ok" and tight["max_ctx"] == 16384
    spill = parse_llama_log(LOG.replace("ROCm0 KV buffer size =   512.00", "CPU KV buffer size =   256.00")
                            + "llama_kv_cache_unified: ROCm0 KV buffer size = 256.00 MiB\n")
    r = recommend(spill, {"vram_total": 8 * GB, "vram_used": 7.9 * GB})
    assert r["verdict"] == "reduce" and r["max_ctx"] == 8192
    assert summary(spill)["kv_vram_share"] == 0.5


def test_ollama_memory_estimate():
    info = {"qwen3.block_count": 36, "qwen3.attention.head_count": 32, "qwen3.attention.head_count_kv": 8,
            "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128, "qwen3.context_length": 40960}
    per_token = kv_per_token_from_info(info)
    assert per_token == 36 * 8 * 256 * 2  # 144 KiB pro Token (f16)

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b", "size": 8 * GB,
                                                         "size_vram": 6 * GB, "context_length": 16384}]})
        return httpx.Response(200, json={"model_info": info})

    llm = OllamaLLM(LLMConfig(model="qwen3:8b"), transport=httpx.MockTransport(handler))
    m = run(llm.memory_info())
    assert m["ctx"] == 16384 and m["ctx_train"] == 40960 and m["kv_ram"] > 0 and m["model_ram_offload"] > 0
    assert recommend(m, {"vram_total": 8 * GB, "vram_used": 7.5 * GB})["verdict"] == "reduce"


def test_context_setting_is_applied_and_remembered(tmp_path):
    cfg = LLMConfig(profiles={
        "qwen": ProfileConfig(model="qwen3:8b"),
        "bonsai": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8080/v1", model="bonsai",
                                server=ServerConfig(command="~/bonsai/start.sh -np 1", env={"BONSAI_CTX": "8192"})),
        "other": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8081/v1", model="x",
                               server=ServerConfig(command="llama-server -m x.gguf -c 4096 -np 1")),
    }, active="qwen")
    state = tmp_path / "state.json"
    router = LLMRouter(cfg, state_path=state)
    run(router.set_context("bonsai", 16384))  # nicht aktiv: nur merken und Startbefehl anpassen
    run(router.set_context("other", 12288))
    assert router.profiles["bonsai"].server.env["BONSAI_CTX"] == "16384"
    assert router.profiles["other"].server.command == "llama-server -m x.gguf -c 12288 -np 1"
    run(router.set_context("qwen", 24576))  # aktiv (Ollama): neuer Client mit num_ctx
    assert router.client.cfg.num_ctx == 24576 and router.context_size == 24576
    assert json.loads(state.read_text())["context"] == {"bonsai": 16384, "other": 12288, "qwen": 24576}
    again = LLMRouter(cfg.model_copy(deep=True), state_path=state)  # nach Neustart wieder angewendet
    assert again.profiles["bonsai"].server.env["BONSAI_CTX"] == "16384" and again.context_size == 24576
    assert {p["name"]: p["num_ctx"] for p in again.describe()}["other"] == 12288


def test_memory_api_in_demo_mode(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/llm/memory").json() == {"available": False}
        assert client.post("/api/models/demo/context", json={"ctx": 8192}).status_code == 400


def write_gguf(path, meta: dict, vocab: int = 50):
    """Kleine GGUF-Datei: Zahlen-Metadaten plus ein Tokenizer-Array (wird beim Lesen übersprungen)."""
    import struct

    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = [s("general.architecture") + struct.pack("<I", 8) + s("qwen3")]
    kvs.append(s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, vocab) + b"".join(s(f"t{i}") for i in range(vocab)))
    kvs += [s(k) + struct.pack("<II", 4, v) for k, v in meta.items()]
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs) + b"\0" * 1000)


def test_estimate_from_gguf_with_compressed_cache(tmp_path):
    from orbwise.llm_memory import cache_bytes, estimate_from_gguf, gguf_metadata
    model = tmp_path / "bonsai.gguf"
    write_gguf(model, {"qwen3.block_count": 48, "qwen3.attention.head_count": 40, "qwen3.attention.head_count_kv": 8,
                       "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128,
                       "qwen3.context_length": 32768})
    assert gguf_metadata(str(model))["qwen3.block_count"] == 48
    assert cache_bytes("x -ctk q8_0 -ctv q8_0", {}) == 34 / 32 and cache_bytes("x", {"BONSAI_KV4": "1"}) == 18 / 32
    est = estimate_from_gguf(str(model), 16384, "start.sh -np 1", {"BONSAI_KV4": "1"})
    assert est["kv_per_token"] == int(48 * 8 * 256 * 18 / 32) and est["kv_ram"] == 0 and est["ctx_train"] == 32768
    assert estimate_from_gguf(str(model), 16384, "llama-server -nkvo")["kv_vram"] == 0  # Cache bewusst im RAM
    assert estimate_from_gguf(str(tmp_path / "fehlt.gguf"), 16384) is None


def test_llama_server_memory_falls_back_and_explains(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    model = tmp_path / "m.gguf"
    write_gguf(model, {"llama.block_count": 32, "llama.attention.head_count": 32,
                       "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096})
    cfg = LLMConfig(profiles={"bonsai": ProfileConfig(
        backend="openai", base_url="http://127.0.0.1:8080/v1", model="bonsai", num_ctx=8192,
        server=ServerConfig(command="start.sh", env={"BONSAI_CTX": "8192"}))}, active="bonsai")
    router = LLMRouter(cfg)
    # älterer Start mit Angaben, letzter Start ohne: nicht die alten Zahlen nehmen
    (tmp_path / "orbwise-llm.log").write_text("===== Starte: alt\n" + LOG + "\n===== Starte: neu\nserver listening\n")
    props = {}

    async def server_props():
        return props
    monkeypatch.setattr(router.client, "server_props", server_props)
    info, reason = run(router.memory_info())
    assert info is None and "Modelldatei" in reason
    props["model_path"] = str(model)
    info, reason = run(router.memory_info())
    assert info["source"] == "estimate" and info["ctx"] == 8192 and info["kv_per_token"] == 32 * 8 * 256 * 2
    (tmp_path / "orbwise-llm.log").write_text("===== Starte: neu\n" + LOG)  # Log mit Angaben: genau
    assert run(router.memory_info())[0]["source"] == "log"
